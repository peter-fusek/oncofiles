"""Persistent OAuth provider — write-through cache over InMemoryOAuthProvider.

Stores MCP OAuth clients and tokens in the database so they survive deploys.
Auth codes are ephemeral (5 min) and not persisted.
"""

from __future__ import annotations

import contextvars
import hmac
import json
import logging
from typing import TYPE_CHECKING

from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL

if TYPE_CHECKING:
    from oncofiles.database import Database

logger = logging.getLogger(__name__)

# Set during verify_token so middleware can read the resolved patient_id
# without re-resolving from session attributes that may not exist (#290).
_verified_patient_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "verified_patient_id", default=""
)


class PersistentOAuthProvider(InMemoryOAuthProvider):
    """InMemoryOAuthProvider with write-through DB persistence.

    - On startup: ``restore_from_db()`` loads saved state into memory.
    - On mutation: super() runs first, then state is persisted to DB.
    - Auth codes are NOT persisted (short-lived, single-use).
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
        # Reset per-request ContextVar (#290)
        _verified_patient_id.set("")

        # 1. Static bearer token (Oncoteam, dev)
        if self._bearer_token and hmac.compare_digest(token.encode(), self._bearer_token.encode()):
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
            return AccessToken(token=token, client_id="oncoteam", scopes=[])
        # 2. MCP OAuth tokens (in-memory, restored from DB on startup — Claude.ai, ChatGPT)
        result = await super().verify_token(token)
        if result:
            # OAuth user → resolve scoped patient. In multi-patient deployments
            # this is the sentinel until we plumb Google owner_email through
            # the MCP token issuance flow — see #478 follow-up.
            if self._db:
                pid = await self._resolve_oauth_patient()
                if pid:
                    _verified_patient_id.set(pid)
            return result
        # 3. Patient bearer tokens (DB lookup — survives restarts without restore)
        if self._db:
            try:
                patient_id = await self._db.resolve_patient_from_token(token)
                if patient_id:
                    _verified_patient_id.set(patient_id)
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

    async def _resolve_oauth_patient(self) -> str:
        """Resolve patient for an OAuth user.

        Current limitation: the MCP OAuth flow (InMemoryOAuthProvider) does
        NOT plumb the caller's Google email through to the issued access
        token. Without the email we cannot correlate the caller to a
        patient by caregiver_email, so any heuristic based on "pick a
        patient with any stored selection" is caller-agnostic and leaks
        across users. That exact bug routed Michal Gašparík to Peter's
        test-patient (e5g) when he connected claude.ai 2026-04-24.

        Until the follow-up issue ships proper email plumbing (see #478),
        return the sole active patient's UUID ONLY in single-patient
        deployments; otherwise return the no-access sentinel so
        downstream patient-scoped DB queries match zero rows and the
        caller is forced to pass ``patient_slug`` explicitly per call.

        Bearer-token (onco_*) flows are unaffected and remain per-patient
        scoped via the token itself.
        """
        return await self._safe_single_patient_pid()

    # ── Restore from DB on startup ──────────────────────────────────────

    async def restore_from_db(self) -> dict:
        """Load persisted clients and tokens into memory. Returns stats."""
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

        # Restore tokens + build association maps
        access_links: dict[str, str] = {}  # access_token -> linked_token (refresh)
        refresh_links: dict[str, str] = {}  # refresh_token -> linked_token (access)

        async with self._db.db.execute(
            "SELECT token, token_type, client_id, scopes_json, expires_at, linked_token "
            "FROM mcp_oauth_tokens"
        ) as cursor:
            for row in await cursor.fetchall():
                try:
                    token_str = row["token"]
                    token_type = row["token_type"]
                    client_id = row["client_id"]
                    scopes = json.loads(row["scopes_json"])
                    expires_at = row["expires_at"]
                    linked = row["linked_token"]

                    if token_type == "access":
                        self.access_tokens[token_str] = AccessToken(
                            token=token_str,
                            client_id=client_id,
                            scopes=scopes,
                            expires_at=expires_at,
                        )
                        if linked:
                            access_links[token_str] = linked
                        access_loaded += 1
                    elif token_type == "refresh":
                        self.refresh_tokens[token_str] = RefreshToken(
                            token=token_str,
                            client_id=client_id,
                            scopes=scopes,
                            expires_at=expires_at,
                        )
                        if linked:
                            refresh_links[token_str] = linked
                        refresh_loaded += 1
                except Exception:
                    logger.warning(
                        "Failed to restore MCP token %s", row["token"][:20], exc_info=True
                    )

        # Rebuild association maps
        for access_tok, refresh_tok in access_links.items():
            self._access_to_refresh_map[access_tok] = refresh_tok
        for refresh_tok, access_tok in refresh_links.items():
            self._refresh_to_access_map[refresh_tok] = access_tok

        stats = {
            "clients": clients_loaded,
            "access_tokens": access_loaded,
            "refresh_tokens": refresh_loaded,
        }
        self._last_restore_stats = stats
        if clients_loaded or access_loaded or refresh_loaded:
            logger.info("Restored MCP OAuth state: %s", stats)
        return stats

    # ── Write-through: persist after mutations ──────────────────────────

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await super().register_client(client_info)
        await self._persist_client(client_info)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        result = await super().exchange_authorization_code(client, authorization_code)
        # Persist the newly issued access + refresh tokens
        await self._persist_token_pair(result.access_token, result.refresh_token)
        return result

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # The old tokens are revoked inside super() via _revoke_internal
        old_access = self._refresh_to_access_map.get(refresh_token.token)
        old_refresh = refresh_token.token

        result = await super().exchange_refresh_token(client, refresh_token, scopes)

        # Delete old tokens from DB
        await self._delete_tokens(old_access, old_refresh)
        # Persist new tokens
        await self._persist_token_pair(result.access_token, result.refresh_token)
        return result

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Capture linked tokens before super() removes them
        if isinstance(token, AccessToken):
            linked_refresh = self._access_to_refresh_map.get(token.token)
            await super().revoke_token(token)
            await self._delete_tokens(token.token, linked_refresh)
        elif isinstance(token, RefreshToken):
            linked_access = self._refresh_to_access_map.get(token.token)
            await super().revoke_token(token)
            await self._delete_tokens(linked_access, token.token)

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
        self, access_token_str: str, refresh_token_str: str | None
    ) -> None:
        if not self._db:
            return
        try:
            access_obj = self.access_tokens.get(access_token_str)
            if access_obj:
                await self._db.db.execute(
                    """
                    INSERT OR REPLACE INTO mcp_oauth_tokens
                        (token, token_type, client_id, scopes_json, expires_at, linked_token)
                    VALUES (?, 'access', ?, ?, ?, ?)
                    """,
                    (
                        access_obj.token,
                        access_obj.client_id,
                        json.dumps(access_obj.scopes),
                        access_obj.expires_at,
                        refresh_token_str,
                    ),
                )

            if refresh_token_str:
                refresh_obj = self.refresh_tokens.get(refresh_token_str)
                if refresh_obj:
                    await self._db.db.execute(
                        """
                        INSERT OR REPLACE INTO mcp_oauth_tokens
                            (token, token_type, client_id, scopes_json, expires_at, linked_token)
                        VALUES (?, 'refresh', ?, ?, ?, ?)
                        """,
                        (
                            refresh_obj.token,
                            refresh_obj.client_id,
                            json.dumps(refresh_obj.scopes),
                            refresh_obj.expires_at,
                            access_token_str,
                        ),
                    )

            await self._db.db.commit()
        except Exception:
            logger.warning("Failed to persist MCP tokens", exc_info=True)

    async def _delete_tokens(
        self, access_token_str: str | None, refresh_token_str: str | None
    ) -> None:
        if not self._db:
            return
        try:
            for tok in [access_token_str, refresh_token_str]:
                if tok:
                    await self._db.db.execute(
                        "DELETE FROM mcp_oauth_tokens WHERE token = ?", (tok,)
                    )
            await self._db.db.commit()
        except Exception:
            logger.warning("Failed to delete MCP tokens from DB", exc_info=True)
