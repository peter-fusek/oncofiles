"""Persistent OAuth provider — write-through cache over InMemoryOAuthProvider.

Stores MCP OAuth clients and tokens in the database so they survive deploys.
Auth codes are ephemeral (5 min) and not persisted.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import TYPE_CHECKING

from fastmcp.server.auth.auth import ClientRegistrationOptions, RevocationOptions
from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider
from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

if TYPE_CHECKING:
    from oncofiles.database import Database

logger = logging.getLogger(__name__)


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
        # 1. Static bearer token (Oncoteam, dev)
        if self._bearer_token and hmac.compare_digest(token.encode(), self._bearer_token.encode()):
            return AccessToken(token=token, client_id="oncoteam", scopes=[])
        # 2. MCP OAuth tokens (in-memory, restored from DB on startup)
        result = await super().verify_token(token)
        if result:
            return result
        # 3. Patient bearer tokens (DB lookup — survives restarts without restore)
        if self._db:
            try:
                patient_id = await self._db.resolve_patient_from_token(token)
                if patient_id:
                    return AccessToken(token=token, client_id=f"patient:{patient_id}", scopes=[])
            except Exception:
                logger.debug("Patient token lookup failed", exc_info=True)
        return None

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
