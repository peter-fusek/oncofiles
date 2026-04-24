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
        state="s",
        scopes=[],
        code_challenge="c",
        code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback",
        redirect_uri_provided_explicitly=True,
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
        state="s",
        scopes=[],
        code_challenge="c",
        code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback",
        redirect_uri_provided_explicitly=True,
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
        state="s",
        scopes=[],
        code_challenge="c",
        code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback",
        redirect_uri_provided_explicitly=True,
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


# ── Cross-patient isolation (Michal Gašparík report, 2026-04-24) ──────


async def _make_oauth_token(provider, client_id="test-client"):
    """Helper: mint an MCP OAuth access token end-to-end."""
    from mcp.server.auth.provider import AuthorizationParams

    client = _make_client_info(client_id=client_id)
    await provider.register_client(client)
    params = AuthorizationParams(
        state="s",
        scopes=[],
        code_challenge="c",
        code_challenge_method="S256",
        redirect_uri="http://localhost:3000/callback",
        redirect_uri_provided_explicitly=True,
    )
    redirect_uri = await provider.authorize(client, params)
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(redirect_uri).query)["code"][0]
    auth_code = await provider.load_authorization_code(client, code)
    return await provider.exchange_authorization_code(client, auth_code)


async def _insert_patient(db, *, patient_id, slug, display_name, caregiver_email=None):
    from oncofiles.models import Patient

    p = Patient(
        patient_id=patient_id,
        slug=slug,
        display_name=display_name,
        caregiver_email=caregiver_email,
    )
    return await db.insert_patient(p)


# Test DB is seeded with 1 patient (q1b / 0000-0000-0000-0000-000000000001)
# by migration 028. Tests that need "multi-patient" or "zero-patient" states
# adjust from this baseline explicitly.
SEED_PID = "00000000-0000-4000-8000-000000000001"


async def _delete_all_patients(db):
    """Wipe the seed patient for zero-patient-deployment tests."""
    await db.db.execute("DELETE FROM patients")
    await db.db.commit()


async def test_oauth_multi_patient_returns_sentinel(db, provider):
    """MULTI-patient deployment + OAuth token → sentinel, not cross-patient leak.

    This locks down the bug Michal Gašparík reported: he OAuth'd via claude.ai,
    had no stored selection, and the old _resolve_oauth_patient fell back to
    resolve_default_patient() → returned the first active patient (Peter's
    test patient e5g) instead of nothing. New contract: multi-patient =
    sentinel, and the caller MUST pass patient_slug per call.
    """
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.persistent_oauth import _verified_patient_id

    # Add a second patient (seed SEED_PID is already there) — matches the shape
    # of Michal's report: seed = Peter's test patient, insert = Michal.
    await _insert_patient(
        db,
        patient_id="22222222-2222-4222-8222-222222222222",
        slug="michal",
        display_name="Michal G.",
        caregiver_email="michal@example.com",
    )

    token = await _make_oauth_token(provider)
    verified = await provider.verify_token(token.access_token)
    assert verified is not None
    # The ContextVar must hold the sentinel — NOT the seed pid, NOT Michal's pid.
    assert _verified_patient_id.get() == NO_PATIENT_ACCESS_SENTINEL


async def test_oauth_ignores_stranger_selection(db, provider):
    """Pre-existing `patient_selection` rows for OTHER emails must NOT leak.

    Before the fix, _resolve_oauth_patient iterated all patients, found any
    patient whose owner had a stored selection, and returned it — regardless
    of who was calling. This test proves that an OAuth token issued to a
    fresh MCP client no longer inherits some other user's selection.
    """
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.persistent_oauth import _verified_patient_id

    michal_pid = "22222222-2222-4222-8222-222222222222"
    await _insert_patient(
        db,
        patient_id=michal_pid,
        slug="michal",
        display_name="Michal G.",
        caregiver_email="michal@example.com",
    )
    # Peter previously called select_patient(seed) as admin
    await db.set_patient_selection("peter@example.com", SEED_PID)

    # Michal (different caller) OAuths freshly
    token = await _make_oauth_token(provider, client_id="claude-ai-michal")
    verified = await provider.verify_token(token.access_token)
    assert verified is not None
    # Must NOT inherit Peter's selection — the MCP token has no owner identity,
    # so the only safe answer is sentinel.
    assert _verified_patient_id.get() == NO_PATIENT_ACCESS_SENTINEL
    assert _verified_patient_id.get() != SEED_PID


async def test_oauth_single_patient_deployment_resolves(db, provider):
    """Single-patient deployment is unambiguous — the lone patient IS the caller.

    Preserves frictionless UX for dev / self-hosted single-user setups where
    there's only one patient in the DB. The test fixture seeds exactly one
    patient (SEED_PID), so we just assert the fix returns it.
    """
    from oncofiles.persistent_oauth import _verified_patient_id

    token = await _make_oauth_token(provider)
    verified = await provider.verify_token(token.access_token)
    assert verified is not None
    assert _verified_patient_id.get() == SEED_PID


async def test_oauth_zero_patients_returns_sentinel(db, provider):
    """Empty deployment → sentinel. Caller literally has nothing to see."""
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.persistent_oauth import _verified_patient_id

    await _delete_all_patients(db)

    token = await _make_oauth_token(provider)
    verified = await provider.verify_token(token.access_token)
    assert verified is not None
    assert _verified_patient_id.get() == NO_PATIENT_ACCESS_SENTINEL


async def test_static_bearer_multi_patient_returns_sentinel(db):
    """Same cross-patient leak existed on the static-bearer path (line 75-76).

    Static MCP_BEARER_TOKEN is operator-level and should NOT auto-default to
    patient 0 in multi-patient deployments — operators must pass patient_slug
    per call (same contract as OAuth post-fix).
    """
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.persistent_oauth import _verified_patient_id

    # Seed is already in DB — add one more to hit multi-patient path
    await _insert_patient(
        db,
        patient_id="22222222-2222-4222-8222-222222222222",
        slug="michal",
        display_name="Michal G.",
    )

    provider = PersistentOAuthProvider(db=db, bearer_token="op-static-token")
    verified = await provider.verify_token("op-static-token")
    assert verified is not None
    assert verified.client_id == "oncoteam"
    assert _verified_patient_id.get() == NO_PATIENT_ACCESS_SENTINEL


async def test_static_bearer_single_patient_still_defaults(db):
    """Static bearer in single-patient deployment preserves the default-pid UX.

    Fixture seeds exactly one patient (SEED_PID), matching the single-patient
    contract.
    """
    from oncofiles.persistent_oauth import _verified_patient_id

    provider = PersistentOAuthProvider(db=db, bearer_token="op-static-token")
    verified = await provider.verify_token("op-static-token")
    assert verified is not None
    assert _verified_patient_id.get() == SEED_PID


async def test_patient_bearer_token_unaffected_by_fix(db):
    """`onco_*` patient bearer tokens identify the patient via the token
    itself — must still resolve to that specific patient regardless of
    deployment size. The leak fix must NOT regress this path."""
    from oncofiles.persistent_oauth import _verified_patient_id

    # Multi-patient deployment (seed + new)
    michal_pid = "22222222-2222-4222-8222-222222222222"
    await _insert_patient(
        db,
        patient_id=michal_pid,
        slug="michal",
        display_name="Michal G.",
    )
    # Mint a patient token specifically for Michal
    michal_token = await db.create_patient_token(michal_pid, label="claude-desktop")

    provider = PersistentOAuthProvider(db=db)
    verified = await provider.verify_token(michal_token)
    assert verified is not None
    assert verified.client_id == f"patient:{michal_pid}"
    assert _verified_patient_id.get() == michal_pid
