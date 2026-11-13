"""Persistent OAuth provider — write-through cache over InMemoryOAuthProvider.

Stores MCP OAuth clients and tokens in the database so they survive deploys.
Auth codes are ephemeral (5 min) and not persisted.

Token storage: only a SHA-256 digest of each issued access / refresh token is
kept (``token_digest``), in memory and in ``mcp_oauth_tokens``, the same way
patient ``onco_*`` tokens are stored as ``token_hash``. The token itself is
returned to the client once and afterwards only arrives with requests, where
it is digested before any lookup.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import logging
import secrets
import time
from typing import TYPE_CHECKING

from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.auth.providers.in_memory import (
    DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS,
    InMemoryOAuthProvider,
)
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken, TokenError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL

if TYPE_CHECKING:
    from oncofiles.database import Database

logger = logging.getLogger(__name__)

_DIGEST_PREFIX = "sha256:"
_DIGEST_LEN = len(_DIGEST_PREFIX) + 64


def token_digest(token: str) -> str:
    """Storage key for an MCP OAuth token: ``"sha256:" + hex digest``.

    Plain SHA-256 is enough because issued tokens are 256-bit random values.
    It is deliberately not keyed by ``MCP_BEARER_TOKEN``: rotating that secret
    must not invalidate every connected client.
    """
    return _DIGEST_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_digest(value: str | None) -> bool:
    """True when ``value`` already has the ``token_digest`` shape."""
    if not value or len(value) != _DIGEST_LEN or not value.startswith(_DIGEST_PREFIX):
        return False
    try:
        int(value[len(_DIGEST_PREFIX) :], 16)
    except ValueError:
        return False
    return True


def _new_token() -> str:
    return secrets.token_urlsafe(32)


# Rows converted from the earlier format get at most this much remaining
# refresh lifetime; active clients rotate well within it.
CONVERTED_REFRESH_MAX_TTL_S = 14 * 86400

# Expired MCP OAuth rows are kept this long before the cleanup job removes them.
EXPIRED_TOKEN_GRACE_S = 30 * 86400


def refresh_token_ttl_seconds() -> int:
    """Refresh-token lifetime (``MCP_REFRESH_TOKEN_TTL_DAYS``, default 60 days)."""
    from oncofiles.config import MCP_REFRESH_TOKEN_TTL_DAYS

    return MCP_REFRESH_TOKEN_TTL_DAYS * 86400


async def delete_expired_oauth_tokens(db: Database) -> int:
    """Delete MCP OAuth token rows that expired more than 30 days ago.

    ``expires_at`` is INTEGER epoch seconds, so it is compared with an
    integer cutoff. Rows without an expiry are kept. Used by the
    ``oauth_token_cleanup`` job; returns the number of rows removed.
    """
    async with db.db.execute(
        "DELETE FROM mcp_oauth_tokens WHERE expires_at IS NOT NULL "
        "AND expires_at < CAST(strftime('%s', 'now') AS INTEGER) - ?",
        (EXPIRED_TOKEN_GRACE_S,),
    ) as cursor:
        deleted = cursor.rowcount
    await db.db.commit()
    return deleted


# Set during verify_token so middleware can read the resolved patient_id
# without re-resolving from session attributes that may not exist (#290).
_verified_patient_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "verified_patient_id", default=""
)

# Set during verify_token to flag admin callers (static MCP_BEARER_TOKEN OR
# OAuth with an email in DASHBOARD_ADMIN_EMAILS). Used by Phase 3 of the
# v5.15 security sweep (#487) to gate admin-only MCP tools and to permit
# ownership-check bypass on read-by-id tools. Never set by regular patient
# OAuth or per-patient `onco_*` bearer tokens.
_verified_caller_is_admin: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "verified_caller_is_admin", default=False
)

# Set during verify_token to surface the OAuth-bound Google email to tools
# that need to check caregiver_email scoping or admin-list membership.
# Empty string when absent (static bearer, patient bearer, legacy OAuth).
_verified_caller_email: contextvars.ContextVar[str] = contextvars.ContextVar(
    "verified_caller_email", default=""
)

# Set during verify_token to record HOW the caller authenticated (#604
# whoami): "static_bearer" (operator MCP_BEARER_TOKEN), "oauth" (MCP OAuth),
# "patient_bearer" (onco_* token). Empty when no token was verified (stdio,
# direct calls from tests).
_verified_auth_method: contextvars.ContextVar[str] = contextvars.ContextVar(
    "verified_auth_method", default=""
)


def caller_auth_method(transport: str = "") -> str:
    """The current caller's auth method, with the no-token cases filled in.

    ``verify_token`` sets ``oauth`` / ``patient_bearer`` / ``static_bearer``;
    without a verified token it is ``stdio`` on the stdio transport, else
    ``none``. Shared by ``whoami`` and the audit middleware (#604).
    """
    method = _verified_auth_method.get()
    if method:
        return method
    return "stdio" if transport == "stdio" else "none"


# #478 email-capture bridge: populated by MCPAuthorizeEmailCaptureMiddleware
# when a signed-in user's browser hits GET /authorize. Keyed by code_challenge
# (stable across the OAuth handshake — AuthorizationCode carries it unchanged).
# TTL-cleaned on every read; auth codes are short-lived (5 min) so worst-case
# stash size is tiny.
_email_for_challenge: dict[str, tuple[str, float]] = {}
_EMAIL_STASH_TTL_S = 600.0  # 10 min — longer than code TTL to avoid races


def stash_email_for_challenge(code_challenge: str, email: str) -> None:
    """Bind a Google account email to an in-flight OAuth code_challenge."""
    _prune_email_stash()
    _email_for_challenge[code_challenge] = (email, time.monotonic())


def pop_email_for_challenge(code_challenge: str) -> str | None:
    """Consume the stashed email for a code_challenge, returning None if expired/absent."""
    _prune_email_stash()
    entry = _email_for_challenge.pop(code_challenge, None)
    if entry is None:
        return None
    email, created_at = entry
    if time.monotonic() - created_at > _EMAIL_STASH_TTL_S:
        return None
    return email


def _prune_email_stash() -> None:
    now = time.monotonic()
    stale = [k for k, (_, t) in _email_for_challenge.items() if now - t > _EMAIL_STASH_TTL_S]
    for k in stale:
        _email_for_challenge.pop(k, None)


def _email_matches_caregiver(email: str, caregiver_email: str | None) -> bool:
    """Case-insensitive check supporting comma-separated caregiver_email lists.

    The single implementation (#651): OAuth resolution, the MCP resolver ACL,
    ``list_patients`` and the dashboard all use it. Lives here, not in
    server.py, to avoid the circular import we broke with constants.py.
    """
    if not email or not email.strip() or not caregiver_email:
        return False
    email_lower = email.strip().lower()
    return any(e.strip().lower() == email_lower for e in caregiver_email.split(","))


class PersistentOAuthProvider(InMemoryOAuthProvider):
    """InMemoryOAuthProvider with write-through DB persistence.

    - On startup: ``restore_from_db()`` loads saved state into memory.
    - On mutation: super() runs first, then state is persisted to DB.
    - Auth codes are NOT persisted (short-lived, single-use).
    - Access / refresh tokens are keyed by ``token_digest`` everywhere the
      provider keeps them: ``access_tokens``, ``refresh_tokens``, both link
      maps and the ``token`` / ``linked_token`` columns. Methods that take or
      return a token translate at the boundary; the ``AccessToken`` /
      ``RefreshToken`` objects handed back to FastMCP carry the caller's own
      token for the duration of the request only.
    """

    def __init__(
        self,
        db: Database | None = None,
        bearer_token: str | None = None,
        base_url: AnyHttpUrl | str | None = None,
        client_registration_options: ClientRegistrationOptions | None = None,
        revocation_options: RevocationOptions | None = None,
        **kwargs,
    ):
        super().__init__(
            base_url=base_url,
            client_registration_options=client_registration_options,
            revocation_options=revocation_options,
            **kwargs,
        )
        self._db = db
        self._bearer_token = bearer_token

    def set_db(self, db: Database) -> None:
        """Set the database reference (called during lifespan after DB init)."""
        self._db = db

    # ── Bearer token support (server-to-server) ─────────────────────────

    async def verify_token(self, token: str) -> AccessToken | None:
        # Reset per-request ContextVars (#290, #487)
        _verified_patient_id.set("")
        _verified_caller_is_admin.set(False)
        _verified_caller_email.set("")
        _verified_auth_method.set("")

        # 1. Static bearer token (Claude Desktop, dev) — implicit admin scope
        if self._bearer_token and hmac.compare_digest(token.encode(), self._bearer_token.encode()):
            _verified_caller_is_admin.set(True)
            _verified_auth_method.set("static_bearer")
            if self._db:
                # Try patient_tokens first (if static token is also a patient token)
                pid = await self._db.resolve_patient_from_token(token)
                if not pid:
                    # Static bearer without patient mapping → only safe to
                    # default in a single-patient deployment (caller is
                    # necessarily the one patient's owner). In multi-patient
                    # deployments the static token is operator-level and
                    # MUST pass patient_slug per call, so we return the
                    # sentinel to force explicit scoping and close the
                    # cross-patient leak class reported by Michal Gašparík
                    # 2026-04-24 (same class as #476).
                    pid = await self._safe_single_patient_pid()
                if pid:
                    _verified_patient_id.set(pid)
            return AccessToken(token=token, client_id="static-bearer", scopes=[])
        # 2. MCP OAuth tokens (in-memory, restored from DB on startup — Claude.ai, ChatGPT)
        result = await self.load_access_token(token)
        if result:
            _verified_auth_method.set("oauth")
            # #478: resolve patient via the Google email bound to this token
            # at /authorize time. If no email was captured (unsigned-in user
            # or a legacy token from before migration 064), fall back to the
            # safe single-patient / sentinel path.
            if self._db:
                bound_email = await self._read_user_email(token)
                if bound_email:
                    _verified_caller_email.set(bound_email)
                    # #487: admin scope for OAuth callers whose Google email
                    # is listed in DASHBOARD_ADMIN_EMAILS. Matches the
                    # dashboard /api/* scoping for consistency.
                    from oncofiles.config import DASHBOARD_ADMIN_EMAILS

                    email_lower = bound_email.lower()
                    admin_emails_lower = {e.lower() for e in DASHBOARD_ADMIN_EMAILS}
                    if email_lower in admin_emails_lower:
                        _verified_caller_is_admin.set(True)
                pid = await self._resolve_oauth_patient(user_email=bound_email)
                if pid:
                    _verified_patient_id.set(pid)
            return result
        # 3. Patient bearer tokens (DB lookup — survives restarts without restore)
        if self._db:
            try:
                patient_id = await self._db.resolve_patient_from_token(token)
                if patient_id:
                    _verified_patient_id.set(patient_id)
                    _verified_auth_method.set("patient_bearer")
                    return AccessToken(token=token, client_id=f"patient:{patient_id}", scopes=[])
            except Exception:
                logger.warning("Patient token lookup failed", exc_info=True)
        return None

    async def _safe_single_patient_pid(self) -> str:
        """Return the sole active patient's UUID, OR the no-access sentinel.

        Single-patient deployments (dev, small self-hosted) are unambiguous:
        whoever is calling is necessarily the one patient's caregiver, so the
        lone patient is a safe default. Multi-patient deployments require an
        explicit patient identity — anything else leaks across patients.
        """
        try:
            patients = await self._db.list_patients(active_only=True)
        except Exception:
            logger.warning("list_patients failed during auth fallback", exc_info=True)
            return NO_PATIENT_ACCESS_SENTINEL
        if len(patients) == 1:
            return patients[0].patient_id
        return NO_PATIENT_ACCESS_SENTINEL

    async def _resolve_oauth_patient(self, *, user_email: str | None = None) -> str:
        """Resolve patient for an MCP OAuth caller.

        Resolution order (#478 proper fix):
          1. If a Google account email was bound to the token at /authorize
             time AND exactly one active patient has that email in its
             `caregiver_email` list → return that pid. Frictionless UX.
          2. If the bound email matches MULTIPLE active patients → consult
             `patient_selection[email]`. If set and still valid → return it.
             Otherwise → sentinel (caller must call `select_patient` OR
             pass `patient_slug` per call; ambiguous defaults leak).
          3. If the bound email matches ZERO active patients → sentinel.
             (New user who hasn't created a patient yet, or an unauthorized
             Google account.)
          4. If NO email was bound (unsigned-in caller or legacy token)
             → fall back to single-patient safe default, else sentinel.

        Bearer-token (`onco_*`) flows are unaffected and remain per-patient
        scoped via the token itself.
        """
        if not user_email:
            return await self._safe_single_patient_pid()

        try:
            patients = await self._db.list_patients(active_only=True)
        except Exception:
            logger.warning("list_patients failed during OAuth resolve", exc_info=True)
            return NO_PATIENT_ACCESS_SENTINEL

        matches = [p for p in patients if _email_matches_caregiver(user_email, p.caregiver_email)]
        if len(matches) == 1:
            return matches[0].patient_id
        if len(matches) > 1:
            try:
                sel = await self._db.get_patient_selection(user_email)
            except Exception:
                logger.debug("get_patient_selection failed", exc_info=True)
                sel = None
            if sel and any(p.patient_id == sel for p in matches):
                return sel
            logger.info(
                "OAuth caller %s matches %d patients but no stored selection — "
                "returning sentinel so caller must pass patient_slug",
                user_email,
                len(matches),
            )
            return NO_PATIENT_ACCESS_SENTINEL
        # No caregiver match — email has no patient access
        logger.info(
            "OAuth caller %s has no caregiver match in active patients — sentinel",
            user_email,
        )
        return NO_PATIENT_ACCESS_SENTINEL

    # ── Restore from DB on startup ──────────────────────────────────────

    async def restore_from_db(self) -> dict:
        """Load persisted clients and tokens into memory. Returns stats.

        Rows written before digest storage are rewritten to ``token_digest``
        form first (``_convert_legacy_token_rows``), so already connected
        clients keep working.
        """
        if not self._db:
            return {"clients": 0, "access_tokens": 0, "refresh_tokens": 0}

        clients_loaded = 0
        access_loaded = 0
        refresh_loaded = 0

        # Restore clients
        async with self._db.db.execute(
            "SELECT client_id, client_info_json FROM mcp_oauth_clients"
        ) as cursor:
            for row in await cursor.fetchall():
                try:
                    client_id = row["client_id"]
                    info = OAuthClientInformationFull.model_validate_json(row["client_info_json"])
                    self.clients[client_id] = info
                    clients_loaded += 1
                except Exception:
                    logger.warning(
                        "Failed to restore MCP client %s", row["client_id"], exc_info=True
                    )

        converted = await self._convert_legacy_token_rows()

        # Restore tokens + build association maps (all keyed by digest)
        access_links: dict[str, str] = {}  # access digest -> refresh digest
        refresh_links: dict[str, str] = {}  # refresh digest -> access digest

        async with self._db.db.execute(
            "SELECT token, token_type, client_id, scopes_json, expires_at, linked_token "
            "FROM mcp_oauth_tokens"
        ) as cursor:
            for row in await cursor.fetchall():
                token_type = row["token_type"]
                try:
                    # Normally already digests; a row the conversion could
                    # not rewrite is still keyed by digest in memory.
                    key = _as_digest(row["token"])
                    linked = _as_digest(row["linked_token"]) if row["linked_token"] else None
                    client_id = row["client_id"]
                    scopes = json.loads(row["scopes_json"])
                    expires_at = row["expires_at"]

                    if token_type == "access":
                        self.access_tokens[key] = AccessToken(
                            token=key,
                            client_id=client_id,
                            scopes=scopes,
                            expires_at=expires_at,
                        )
                        if linked:
                            access_links[key] = linked
                        access_loaded += 1
                    elif token_type == "refresh":
                        self.refresh_tokens[key] = RefreshToken(
                            token=key,
                            client_id=client_id,
                            scopes=scopes,
                            expires_at=expires_at,
                        )
                        if linked:
                            refresh_links[key] = linked
                        refresh_loaded += 1
                except Exception as exc:
                    logger.warning(
                        "Failed to restore an MCP OAuth %s token row (%s)",
                        token_type,
                        type(exc).__name__,
                    )

        # Rebuild association maps
        for access_key, refresh_key in access_links.items():
            self._access_to_refresh_map[access_key] = refresh_key
        for refresh_key, access_key in refresh_links.items():
            self._refresh_to_access_map[refresh_key] = access_key

        stats = {
            "clients": clients_loaded,
            "access_tokens": access_loaded,
            "refresh_tokens": refresh_loaded,
            "converted_rows": converted,
        }
        self._last_restore_stats = stats
        if clients_loaded or access_loaded or refresh_loaded:
            logger.info("Restored MCP OAuth state: %s", stats)
        return stats

    async def _convert_legacy_token_rows(self) -> int:
        """Rewrite ``token`` / ``linked_token`` values not yet in digest form.

        One transaction; every UPDATE must touch exactly one row, otherwise
        the whole conversion is rolled back (the rows are still loaded under
        their digest in memory and the conversion is retried next start).
        Idempotent: rows already in digest form are left alone. A converted
        refresh row gets ``expires_at = min(existing, now + 14 days)``, so a
        client that is still active rotates into a new pair with the regular
        lifetime. ``secure_delete`` is on for the UPDATEs and the WAL is
        checkpointed afterwards. Logs counts only.
        """
        async with self._db.db.execute(
            "SELECT token, token_type, expires_at, linked_token FROM mcp_oauth_tokens"
        ) as cursor:
            rows = await cursor.fetchall()

        refresh_cap = int(time.time()) + CONVERTED_REFRESH_MAX_TTL_S
        updates: list[tuple[str, str | None, int | None, str]] = []
        for row in rows:
            stored, linked = row["token"], row["linked_token"]
            expires_at = row["expires_at"]
            new_key = _as_digest(stored)
            new_linked = _as_digest(linked) if linked else linked
            if new_key == stored and new_linked == linked:
                continue
            if row["token_type"] == "refresh":
                expires_at = refresh_cap if expires_at is None else min(expires_at, refresh_cap)
            updates.append((new_key, new_linked, expires_at, stored))
        if not updates:
            return 0

        db = self._db.db
        try:
            # Overwrite freed page content on this connection (stays on for
            # later token row deletes too).
            async with db.execute("PRAGMA secure_delete = ON"):
                pass
            for new_key, new_linked, expires_at, stored in updates:
                async with db.execute(
                    "UPDATE mcp_oauth_tokens SET token = ?, linked_token = ?, expires_at = ? "
                    "WHERE token = ?",
                    (new_key, new_linked, expires_at, stored),
                ) as cursor:
                    if cursor.rowcount != 1:
                        raise RuntimeError(f"expected 1 row, updated {cursor.rowcount}")
            await db.commit()
        except Exception as exc:
            await db.rollback()
            logger.warning(
                "MCP OAuth token row conversion rolled back (%d rows, %s)",
                len(updates),
                type(exc).__name__,
            )
            return 0
        try:
            # Fold the WAL into the main file so earlier page versions are
            # not left behind (a no-op for databases without a WAL).
            async with db.execute("PRAGMA wal_checkpoint(TRUNCATE)"):
                pass
        except Exception as exc:
            logger.warning(
                "WAL checkpoint after token row conversion failed (%s)", type(exc).__name__
            )
        logger.info("MCP OAuth token rows converted to digest form: %d", len(updates))
        return len(updates)

    # ── Token lookup (raw token in, digest-keyed state) ─────────────────

    @staticmethod
    def _key_for(obj: AccessToken | RefreshToken, store: dict) -> str:
        """Storage key for a token object.

        Objects handed out by ``load_*`` carry the caller's token, which is
        digested. The provider's own stored objects (``token`` already the
        digest, same instance as in ``store``) map to themselves. A
        digest-shaped string supplied by a caller is digested again and
        therefore never matches: a digest is not a credential.
        """
        if store.get(obj.token) is obj:
            return obj.token
        return token_digest(obj.token)

    def _drop_access_only(self, access_key: str) -> None:
        """Forget one access token and its map links; its refresh token stays."""
        self.access_tokens.pop(access_key, None)
        refresh_key = self._access_to_refresh_map.pop(access_key, None)
        if refresh_key and self._refresh_to_access_map.get(refresh_key) == access_key:
            self._refresh_to_access_map.pop(refresh_key, None)

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        key = token_digest(token)
        token_obj = self.access_tokens.get(key)
        if token_obj is None:
            return None
        if token_obj.expires_at is not None and token_obj.expires_at < time.time():
            # An expired access token is simply not valid. Its refresh token
            # stays usable until its own expiry (the client refreshes next).
            self._drop_access_only(key)
            return None
        return token_obj.model_copy(update={"token": token})

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        key = token_digest(refresh_token)
        token_obj = self.refresh_tokens.get(key)
        if token_obj is None or token_obj.client_id != client.client_id:
            return None
        if token_obj.expires_at is not None and token_obj.expires_at < time.time():
            access_key = self._refresh_to_access_map.get(key)
            self._revoke_internal(refresh_token_str=key)
            await self._delete_tokens(access_key, key)
            return None
        return token_obj.model_copy(update={"token": refresh_token})

    def _issue_token_pair(self, client_id: str, scopes: list[str]) -> OAuthToken:
        """Mint a new access/refresh pair, keep only digests, return the tokens."""
        access_value = _new_token()
        refresh_value = _new_token()
        access_key = token_digest(access_value)
        refresh_key = token_digest(refresh_value)

        refresh_expires_at = int(time.time()) + refresh_token_ttl_seconds()

        self.access_tokens[access_key] = AccessToken(
            token=access_key,
            client_id=client_id,
            scopes=scopes,
            expires_at=int(time.time() + DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS),
        )
        self.refresh_tokens[refresh_key] = RefreshToken(
            token=refresh_key,
            client_id=client_id,
            scopes=scopes,
            expires_at=refresh_expires_at,
        )
        self._access_to_refresh_map[access_key] = refresh_key
        self._refresh_to_access_map[refresh_key] = access_key

        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=DEFAULT_ACCESS_TOKEN_EXPIRY_SECONDS,
            refresh_token=refresh_value,
            scope=" ".join(scopes),
        )

    # ── Write-through: persist after mutations ──────────────────────────

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await super().register_client(client_info)
        await self._persist_client(client_info)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Same contract as InMemoryOAuthProvider, re-implemented so the
        # issued tokens are only ever kept as digests.
        if authorization_code.code not in self.auth_codes:
            raise TokenError("invalid_grant", "Authorization code not found or already used.")
        del self.auth_codes[authorization_code.code]
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        result = self._issue_token_pair(client.client_id, authorization_code.scopes)
        # #478: pop the email bound at /authorize time (if any) and persist
        # it alongside the new token rows. None → unsigned-in OAuth caller,
        # sentinel behavior preserved.
        user_email = pop_email_for_challenge(authorization_code.code_challenge)
        await self._persist_token_pair(
            token_digest(result.access_token),
            token_digest(result.refresh_token) if result.refresh_token else None,
            user_email=user_email,
        )
        return result

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        if not set(scopes).issubset(set(refresh_token.scopes)):
            raise TokenError(
                "invalid_scope",
                "Requested scopes exceed those authorized by the refresh token.",
            )
        if client.client_id is None:
            raise TokenError("invalid_client", "Client ID is required")

        old_refresh = self._key_for(refresh_token, self.refresh_tokens)
        old_access = self._refresh_to_access_map.get(old_refresh)

        # #478: preserve the user_email across refresh — read it off the old
        # refresh token row BEFORE we delete it.
        old_email = await self._read_user_email_by_digest(old_refresh)

        # Rotation: the old pair is revoked when the new pair is issued.
        self._revoke_internal(refresh_token_str=old_refresh)
        result = self._issue_token_pair(client.client_id, scopes)

        await self._delete_tokens(old_access, old_refresh)
        await self._persist_token_pair(
            token_digest(result.access_token),
            token_digest(result.refresh_token) if result.refresh_token else None,
            user_email=old_email,
        )
        return result

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        """Revoke a token and its counterpart, in memory and in the DB."""
        if isinstance(token, AccessToken):
            access_key = self._key_for(token, self.access_tokens)
            linked_refresh = self._access_to_refresh_map.get(access_key)
            self._revoke_internal(access_token_str=access_key)
            await self._delete_tokens(access_key, linked_refresh)
        elif isinstance(token, RefreshToken):
            refresh_key = self._key_for(token, self.refresh_tokens)
            linked_access = self._refresh_to_access_map.get(refresh_key)
            self._revoke_internal(refresh_token_str=refresh_key)
            await self._delete_tokens(linked_access, refresh_key)

    # ── DB persistence helpers ──────────────────────────────────────────

    async def _persist_client(self, client_info: OAuthClientInformationFull) -> None:
        if not self._db:
            return
        try:
            await self._db.db.execute(
                """
                INSERT INTO mcp_oauth_clients (client_id, client_info_json)
                VALUES (?, ?)
                ON CONFLICT(client_id) DO UPDATE SET client_info_json = excluded.client_info_json
                """,
                (client_info.client_id, client_info.model_dump_json()),
            )
            await self._db.db.commit()
        except Exception:
            logger.warning("Failed to persist MCP client", exc_info=True)

    async def _persist_token_pair(
        self,
        access_key: str,
        refresh_key: str | None,
        *,
        user_email: str | None = None,
    ) -> None:
        """Write the pair's rows; both keys are ``token_digest`` values."""
        if not self._db:
            return
        try:
            access_obj = self.access_tokens.get(access_key)
            if access_obj:
                await self._db.db.execute(
                    """
                    INSERT OR REPLACE INTO mcp_oauth_tokens
                        (token, token_type, client_id, scopes_json, expires_at,
                         linked_token, user_email)
                    VALUES (?, 'access', ?, ?, ?, ?, ?)
                    """,
                    (
                        access_key,
                        access_obj.client_id,
                        json.dumps(access_obj.scopes),
                        access_obj.expires_at,
                        refresh_key,
                        user_email,
                    ),
                )

            if refresh_key:
                refresh_obj = self.refresh_tokens.get(refresh_key)
                if refresh_obj:
                    await self._db.db.execute(
                        """
                        INSERT OR REPLACE INTO mcp_oauth_tokens
                            (token, token_type, client_id, scopes_json, expires_at,
                             linked_token, user_email)
                        VALUES (?, 'refresh', ?, ?, ?, ?, ?)
                        """,
                        (
                            refresh_key,
                            refresh_obj.client_id,
                            json.dumps(refresh_obj.scopes),
                            refresh_obj.expires_at,
                            access_key,
                            user_email,
                        ),
                    )

            await self._db.db.commit()
        except Exception:
            logger.warning("Failed to persist MCP tokens", exc_info=True)

    async def _read_user_email(self, token_str: str | None) -> str | None:
        """Bound Google account email for a token presented by a caller."""
        if not token_str:
            return None
        return await self._read_user_email_by_digest(token_digest(token_str))

    async def _read_user_email_by_digest(self, key: str | None) -> str | None:
        """Look up the bound Google account email on a persisted MCP token row."""
        if not self._db or not key:
            return None
        try:
            async with self._db.db.execute(
                "SELECT user_email FROM mcp_oauth_tokens WHERE token = ?",
                (key,),
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return row["user_email"] if isinstance(row, dict) else row[0]
        except Exception:
            logger.debug("user_email lookup failed for token", exc_info=True)
            return None

    async def _delete_tokens(self, access_key: str | None, refresh_key: str | None) -> None:
        """Delete the pair's rows (digest keys).

        Also removes an access row still linked to ``refresh_key`` whose
        in-memory entry was already dropped on expiry.
        """
        if not self._db:
            return
        try:
            for key in [access_key, refresh_key]:
                if key:
                    await self._db.db.execute(
                        "DELETE FROM mcp_oauth_tokens WHERE token = ?", (key,)
                    )
            if refresh_key:
                await self._db.db.execute(
                    "DELETE FROM mcp_oauth_tokens WHERE token_type = 'access' AND linked_token = ?",
                    (refresh_key,),
                )
            await self._db.db.commit()
        except Exception:
            logger.warning("Failed to delete MCP tokens from DB", exc_info=True)


def _as_digest(value: str) -> str:
    """``value`` if already a digest, else its ``token_digest``."""
    return value if _is_digest(value) else token_digest(value)
