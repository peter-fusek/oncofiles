"""MCP OAuth token storage: only ``token_digest`` values are kept.

Covers the provider's in-memory maps and the ``mcp_oauth_tokens`` rows, the
restart path, refresh rotation, access-token expiry, conversion of rows in
the earlier format, revocation and log output. Synthetic values only.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from oncofiles.database import Database
from oncofiles.persistent_oauth import (
    PersistentOAuthProvider,
    _verified_caller_email,
    stash_email_for_challenge,
    token_digest,
)

CLIENT_ID = "storage-test-client"
EMAIL = "caregiver-storage@example.com"


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def provider(db):
    p = PersistentOAuthProvider(db=db)
    await p.register_client(_client())
    return p


def _client() -> OAuthClientInformationFull:
    return OAuthClientInformationFull(
        client_id=CLIENT_ID,
        client_secret="synthetic-secret",
        redirect_uris=["http://localhost:3000/callback"],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="client_secret_post",
    )


async def _mint(provider: PersistentOAuthProvider, *, challenge="chal-storage", email=None):
    if email:
        stash_email_for_challenge(challenge, email)
    client = await provider.get_client(CLIENT_ID)
    params = AuthorizationParams(
        state="s",
        scopes=[],
        code_challenge=challenge,
        code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback",
        redirect_uri_provided_explicitly=True,
    )
    redirect = await provider.authorize(client, params)
    code = parse_qs(urlparse(redirect).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    return await provider.exchange_authorization_code(client, auth_code)


async def _refresh(provider: PersistentOAuthProvider, refresh_token: str):
    client = await provider.get_client(CLIENT_ID)
    obj = await provider.load_refresh_token(client, refresh_token)
    assert obj is not None
    return await provider.exchange_refresh_token(client, obj, [])


async def _all_text_values(db: Database) -> list[str]:
    async with db.db.execute("SELECT * FROM mcp_oauth_tokens") as cursor:
        rows = await cursor.fetchall()
    return [str(v) for row in rows for v in tuple(row) if v is not None]


async def _rows(db: Database) -> dict[str, dict]:
    async with db.db.execute("SELECT * FROM mcp_oauth_tokens") as cursor:
        return {r["token"]: dict(r) for r in await cursor.fetchall()}


def _assert_absent(values, *tokens):
    for tok in tokens:
        assert not any(tok in v for v in values)


def _assert_memory_keys_are_digests(provider: PersistentOAuthProvider, *tokens):
    keys = (
        list(provider.access_tokens)
        + list(provider.refresh_tokens)
        + list(provider._access_to_refresh_map)
        + list(provider._access_to_refresh_map.values())
        + list(provider._refresh_to_access_map)
        + list(provider._refresh_to_access_map.values())
        + [o.token for o in provider.access_tokens.values()]
        + [o.token for o in provider.refresh_tokens.values()]
    )
    assert keys
    assert all(k.startswith("sha256:") for k in keys)
    _assert_absent(keys, *tokens)


async def test_code_exchange_stores_digests_only(db, provider):
    tok = await _mint(provider)
    assert tok.access_token != tok.refresh_token
    assert not tok.access_token.startswith("test_")

    values = await _all_text_values(db)
    _assert_absent(values, tok.access_token, tok.refresh_token)
    rows = await _rows(db)
    a, r = token_digest(tok.access_token), token_digest(tok.refresh_token)
    assert rows[a]["token_type"] == "access" and rows[a]["linked_token"] == r
    assert rows[r]["token_type"] == "refresh" and rows[r]["linked_token"] == a
    _assert_memory_keys_are_digests(provider, tok.access_token, tok.refresh_token)


async def test_verify_accepts_token_not_its_digest(provider):
    tok = await _mint(provider)
    verified = await provider.verify_token(tok.access_token)
    assert verified is not None and verified.client_id == CLIENT_ID
    assert await provider.verify_token(token_digest(tok.access_token)) is None
    client = await provider.get_client(CLIENT_ID)
    assert await provider.load_refresh_token(client, token_digest(tok.refresh_token)) is None


async def test_restart_verifies_and_refreshes(db, provider):
    tok = await _mint(provider, challenge="chal-restart", email=EMAIL)

    restarted = PersistentOAuthProvider(db=db)
    stats = await restarted.restore_from_db()
    assert stats["access_tokens"] == 1 and stats["refresh_tokens"] == 1
    assert stats["converted_rows"] == 0
    _assert_memory_keys_are_digests(restarted, tok.access_token, tok.refresh_token)

    assert await restarted.verify_token(tok.access_token) is not None
    assert _verified_caller_email.get() == EMAIL
    new = await _refresh(restarted, tok.refresh_token)
    assert await restarted.verify_token(new.access_token) is not None


async def test_refresh_rotation_replaces_pair_and_keeps_email(db, provider):
    tok = await _mint(provider, challenge="chal-rotate", email=EMAIL)
    new = await _refresh(provider, tok.refresh_token)

    old_keys = {token_digest(tok.access_token), token_digest(tok.refresh_token)}
    new_keys = {token_digest(new.access_token), token_digest(new.refresh_token)}
    for key in old_keys:
        assert key not in provider.access_tokens and key not in provider.refresh_tokens
        assert key not in provider._access_to_refresh_map
        assert key not in provider._refresh_to_access_map
    rows = await _rows(db)
    assert set(rows) == new_keys
    assert all(r["user_email"] == EMAIL for r in rows.values())
    _assert_absent(
        await _all_text_values(db),
        tok.access_token,
        tok.refresh_token,
        new.access_token,
        new.refresh_token,
    )

    assert await provider.verify_token(tok.access_token) is None
    client = await provider.get_client(CLIENT_ID)
    assert await provider.load_refresh_token(client, tok.refresh_token) is None
    assert await provider.verify_token(new.access_token) is not None
    assert _verified_caller_email.get() == EMAIL


async def test_expired_access_keeps_refresh_usable(db, provider):
    tok = await _mint(provider, challenge="chal-expire", email=EMAIL)
    provider.access_tokens[token_digest(tok.access_token)].expires_at = int(time.time()) - 5

    assert await provider.verify_token(tok.access_token) is None
    assert token_digest(tok.access_token) not in provider.access_tokens
    assert token_digest(tok.refresh_token) in provider.refresh_tokens

    new = await _refresh(provider, tok.refresh_token)
    assert await provider.verify_token(new.access_token) is not None
    assert _verified_caller_email.get() == EMAIL
    # The expired access row went with the rotated pair.
    rows = await _rows(db)
    assert set(rows) == {token_digest(new.access_token), token_digest(new.refresh_token)}


async def test_expired_access_after_restart_keeps_refresh_usable(db, provider):
    tok = await _mint(provider, challenge="chal-expire-restart")
    await db.db.execute(
        "UPDATE mcp_oauth_tokens SET expires_at = ? WHERE token_type = 'access'",
        (int(time.time()) - 5,),
    )
    await db.db.commit()

    restarted = PersistentOAuthProvider(db=db)
    await restarted.restore_from_db()
    assert await restarted.verify_token(tok.access_token) is None
    new = await _refresh(restarted, tok.refresh_token)
    assert await restarted.verify_token(new.access_token) is not None


async def _insert_earlier_format_pair(db: Database, access: str, refresh: str) -> None:
    for token, kind, linked, expires in (
        (access, "access", refresh, int(time.time()) + 3600),
        (refresh, "refresh", access, None),
    ):
        await db.db.execute(
            "INSERT INTO mcp_oauth_tokens "
            "(token, token_type, client_id, scopes_json, expires_at, linked_token, user_email) "
            "VALUES (?, ?, ?, '[]', ?, ?, ?)",
            (token, kind, CLIENT_ID, expires, linked, EMAIL),
        )
    await db.db.commit()


async def test_earlier_format_rows_are_converted_on_restore(db, provider):
    access = "synthetic-earlier-access-0001"
    refresh = "synthetic-earlier-refresh-0001"
    await _insert_earlier_format_pair(db, access, refresh)

    restarted = PersistentOAuthProvider(db=db)
    stats = await restarted.restore_from_db()
    assert stats["converted_rows"] == 2
    _assert_absent(await _all_text_values(db), access, refresh)
    rows = await _rows(db)
    assert rows[token_digest(access)]["linked_token"] == token_digest(refresh)
    assert rows[token_digest(refresh)]["linked_token"] == token_digest(access)
    _assert_memory_keys_are_digests(restarted, access, refresh)

    assert await restarted.verify_token(access) is not None
    assert _verified_caller_email.get() == EMAIL

    # Idempotent: a second restart converts nothing and still verifies.
    again = PersistentOAuthProvider(db=db)
    stats2 = await again.restore_from_db()
    assert stats2["converted_rows"] == 0
    assert set(await _rows(db)) == set(rows)
    assert await again.verify_token(access) is not None
    new = await _refresh(again, refresh)
    assert await again.verify_token(new.access_token) is not None


async def test_revoke_removes_both_rows(db, provider):
    tok = await _mint(provider)
    loaded = await provider.load_access_token(tok.access_token)
    await provider.revoke_token(loaded)
    assert await _rows(db) == {}
    assert not provider.access_tokens and not provider.refresh_tokens

    tok2 = await _mint(provider, challenge="chal-revoke-refresh")
    client = await provider.get_client(CLIENT_ID)
    loaded_refresh = await provider.load_refresh_token(client, tok2.refresh_token)
    await provider.revoke_token(loaded_refresh)
    assert await _rows(db) == {}
    assert await provider.verify_token(tok2.access_token) is None


async def test_logs_never_contain_tokens(db, provider, caplog):
    # Application loggers at DEBUG, everything else at the production default.
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="oncofiles")
    await _insert_earlier_format_pair(db, "synthetic-earlier-access-0002", "synthetic-refresh-0002")
    tok = await _mint(provider, challenge="chal-logs", email=EMAIL)
    await provider.verify_token(tok.access_token)
    new = await _refresh(provider, tok.refresh_token)
    restarted = PersistentOAuthProvider(db=db)
    await restarted.restore_from_db()
    await restarted.verify_token(new.access_token)
    await restarted.verify_token("synthetic-earlier-access-0002")
    loaded = await restarted.load_access_token(new.access_token)
    await restarted.revoke_token(loaded)

    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (
        tok.access_token,
        tok.refresh_token,
        new.access_token,
        new.refresh_token,
        "synthetic-earlier-access-0002",
        "synthetic-refresh-0002",
    ):
        assert secret not in text
        assert secret[:12] not in text
