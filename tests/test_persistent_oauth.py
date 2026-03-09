"""Tests for PersistentOAuthProvider — MCP OAuth session persistence."""

import time

import pytest

from oncofiles.database import Database
from oncofiles.persistent_oauth import PersistentOAuthProvider


@pytest.fixture
async def db():
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
def provider(db):
    p = PersistentOAuthProvider(db=db)
    return p


def _make_client_info(client_id="test-client"):
    from mcp.shared.auth import OAuthClientInformationFull

    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="secret",
        redirect_uris=["http://localhost:3000/callback"],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="client_secret_post",
    )


# ── Client registration persistence ─────────────────────────────────


async def test_register_client_persisted(db, provider):
    """Registered clients survive provider restart."""
    client = _make_client_info()
    await provider.register_client(client)

    # Verify in memory
    assert await provider.get_client("test-client") is not None

    # Create new provider and restore from same DB
    provider2 = PersistentOAuthProvider(db=db)
    assert await provider2.get_client("test-client") is None  # not yet restored
    stats = await provider2.restore_from_db()
    assert stats["clients"] == 1
    restored = await provider2.get_client("test-client")
    assert restored is not None
    assert restored.client_id == "test-client"


async def test_register_client_update(db, provider):
    """Re-registering a client updates the persisted data."""
    client1 = _make_client_info()
    await provider.register_client(client1)

    client2 = _make_client_info()
    client2.client_secret = "new-secret"
    await provider.register_client(client2)

    provider2 = PersistentOAuthProvider(db=db)
    await provider2.restore_from_db()
    restored = await provider2.get_client("test-client")
    assert restored.client_secret == "new-secret"


# ── Token exchange and persistence ───────────────────────────────────


async def test_token_exchange_persisted(db, provider):
    """Access and refresh tokens from code exchange survive restart."""
    from mcp.server.auth.provider import AuthorizationParams

    client = _make_client_info()
    await provider.register_client(client)

    # Simulate authorization
    params = AuthorizationParams(
        state="test-state",
        scopes=[],
        code_challenge="challenge123",
        code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback",
        redirect_uri_provided_explicitly=True,
    )
    redirect_uri = await provider.authorize(client, params)
    # Extract code from redirect URI
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(redirect_uri)
    code = parse_qs(parsed.query)["code"][0]

    # Load and exchange
    auth_code = await provider.load_authorization_code(client, code)
    assert auth_code is not None
    oauth_token = await provider.exchange_authorization_code(client, auth_code)

    assert oauth_token.access_token
    assert oauth_token.refresh_token

    # Restore in new provider
    provider2 = PersistentOAuthProvider(db=db)
    stats = await provider2.restore_from_db()
    assert stats["access_tokens"] == 1
    assert stats["refresh_tokens"] == 1

    # Verify access token works
    verified = await provider2.verify_token(oauth_token.access_token)
    assert verified is not None
    assert verified.client_id == "test-client"


async def test_refresh_token_exchange_persisted(db, provider):
    """Token refresh persists new tokens and removes old ones."""
    from mcp.server.auth.provider import AuthorizationParams

    client = _make_client_info()
    await provider.register_client(client)

    params = AuthorizationParams(
        state="s", scopes=[], code_challenge="c", code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback", redirect_uri_provided_explicitly=True,
    )
    redirect_uri = await provider.authorize(client, params)
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(redirect_uri).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    token1 = await provider.exchange_authorization_code(client, auth_code)

    # Refresh
    refresh_obj = await provider.load_refresh_token(client, token1.refresh_token)
    token2 = await provider.exchange_refresh_token(client, refresh_obj, [])

    assert token2.access_token != token1.access_token
    assert token2.refresh_token != token1.refresh_token

    # Restore in new provider — should have only the new tokens
    provider2 = PersistentOAuthProvider(db=db)
    await provider2.restore_from_db()

    # Old tokens should be gone
    assert await provider2.verify_token(token1.access_token) is None
    # New tokens should work
    assert await provider2.verify_token(token2.access_token) is not None


# ── Token revocation ─────────────────────────────────────────────────


async def test_revoke_removes_from_db(db, provider):
    """Revoking a token removes it and its pair from the DB."""
    from mcp.server.auth.provider import AuthorizationParams

    client = _make_client_info()
    await provider.register_client(client)

    params = AuthorizationParams(
        state="s", scopes=[], code_challenge="c", code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback", redirect_uri_provided_explicitly=True,
    )
    redirect_uri = await provider.authorize(client, params)
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(redirect_uri).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    token = await provider.exchange_authorization_code(client, auth_code)

    # Revoke the access token (should also revoke refresh)
    access_obj = provider.access_tokens[token.access_token]
    await provider.revoke_token(access_obj)

    # DB should be empty
    async with db.db.execute("SELECT COUNT(*) as cnt FROM mcp_oauth_tokens") as cursor:
        row = await cursor.fetchone()
        assert row["cnt"] == 0


# ── Bearer token support ────────────────────────────────────────────


async def test_bearer_token_verification(db):
    """Static bearer token works alongside OAuth."""
    provider = PersistentOAuthProvider(db=db, bearer_token="my-secret-token")

    result = await provider.verify_token("my-secret-token")
    assert result is not None
    assert result.client_id == "oncoteam"

    # Invalid token returns None
    assert await provider.verify_token("wrong-token") is None


# ── No DB graceful degradation ──────────────────────────────────────


async def test_no_db_still_works():
    """Provider works without DB (falls back to pure in-memory)."""
    provider = PersistentOAuthProvider(db=None)
    client = _make_client_info()
    await provider.register_client(client)
    assert await provider.get_client("test-client") is not None

    stats = await provider.restore_from_db()
    assert stats["clients"] == 0  # no DB to restore from


# ── Expired tokens cleaned on restore ────────────────────────────────


async def test_expired_tokens_cleaned_on_verify(db, provider):
    """Expired access tokens are not returned by verify_token."""
    from mcp.server.auth.provider import AuthorizationParams

    client = _make_client_info()
    await provider.register_client(client)

    params = AuthorizationParams(
        state="s", scopes=[], code_challenge="c", code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback", redirect_uri_provided_explicitly=True,
    )
    redirect_uri = await provider.authorize(client, params)
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(redirect_uri).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    token = await provider.exchange_authorization_code(client, auth_code)

    # Manually expire the access token
    provider.access_tokens[token.access_token].expires_at = int(time.time()) - 10

    # Verify should return None for expired token
    assert await provider.verify_token(token.access_token) is None
