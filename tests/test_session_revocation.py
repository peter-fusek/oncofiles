"""Server-side session revocation invariants (#510).

Pre-#510 dashboard logout was client-only — the HMAC-signed session token
remained valid for its full 24h lifetime. Anyone holding a captured token
(proxy log, browser history dump, malicious extension) could keep
authenticating until natural expiry.

These tests lock in:
- Each issued session token carries a unique random tid (16 hex chars)
- _verify_session_token rejects tokens whose tid has been revoked
- Legacy 3-part tokens (pre-#510) still verify but cannot be revoked
- The persistence layer (Turso table) survives restarts via load_from_db
- Stale rows past expires_at are purged
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from oncofiles import session_revocation
from oncofiles.server import (
    _extract_session_tid_and_expiry,
    _make_session_token,
    _verify_session_token,
)


@pytest.fixture(autouse=True)
def _reset_in_memory_set():
    session_revocation._reset_for_tests()
    yield
    session_revocation._reset_for_tests()


@pytest.fixture(autouse=True)
def _bearer_set():
    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        yield


def test_make_tid_is_unique_and_fixed_length():
    seen = {session_revocation.make_tid() for _ in range(100)}
    assert len(seen) == 100
    for tid in seen:
        assert len(tid) == 16
        int(tid, 16)  # must parse as hex


def test_session_token_has_4_parts():
    token = _make_session_token("user@example.com")
    assert token.count("|") == 3, "token must be email|expiry|tid|sig"


def test_session_token_tids_differ_per_issue():
    """Each call to _make_session_token produces a fresh tid."""
    t1 = _make_session_token("user@example.com")
    t2 = _make_session_token("user@example.com")
    assert t1 != t2  # different tid + sig even with same email/sec


def test_extract_tid_returns_tid_and_expiry():
    token = _make_session_token("user@example.com")
    parsed = _extract_session_tid_and_expiry(token)
    assert parsed is not None
    tid, expiry = parsed
    assert len(tid) == 16
    assert expiry > int(time.time())


def test_extract_tid_returns_none_for_legacy_3_part_token():
    """Legacy tokens without tid cannot be revoked individually."""
    legacy = "user@example.com|9999999999|" + "a" * 32
    assert _extract_session_tid_and_expiry(legacy) is None


def test_revoked_token_no_longer_verifies():
    """The core #510 invariant — once revoked, the token is dead."""
    token = _make_session_token("user@example.com")
    parsed = _extract_session_tid_and_expiry(token)
    assert parsed is not None
    tid, expiry = parsed

    # Sanity: token works before revocation
    assert _verify_session_token(token) == "user@example.com"

    # Revoke (in-memory only — DB layer tested separately below)
    session_revocation._add_to_memory(tid, expiry)

    assert _verify_session_token(token) is None


def test_revoking_one_tid_does_not_affect_other_sessions():
    """Per-token granularity: revoking session A leaves session B alive."""
    a = _make_session_token("alice@example.com")
    b = _make_session_token("bob@example.com")
    parsed = _extract_session_tid_and_expiry(a)
    assert parsed is not None
    tid_a, expiry_a = parsed
    session_revocation._add_to_memory(tid_a, expiry_a)

    assert _verify_session_token(a) is None
    assert _verify_session_token(b) == "bob@example.com"


def test_legacy_3_part_token_still_verifies():
    """Backward-compat — pre-#510 tokens issued before deploy still work
    until natural expiry, but cannot be individually revoked. The #502 key
    rotation already invalidated all in-circulation tokens at deploy time;
    this test exercises the parser path, not a real-world scenario.
    """
    import hashlib
    import hmac as _hmac

    from oncofiles.secrets_keys import dashboard_session_key

    email = "legacy@example.com"
    expiry = str(int(time.time()) + 3600)
    payload = f"{email}|{expiry}"
    key = dashboard_session_key("test-secret")
    sig = _hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    legacy_token = f"{payload}|{sig}"

    assert _verify_session_token(legacy_token) == email


def test_is_revoked_returns_false_for_unknown_tid():
    assert session_revocation.is_revoked("deadbeefdeadbeef") is False


def test_purge_expired_removes_stale_in_memory_rows():
    past = int(time.time()) - 100
    future = int(time.time()) + 3600
    session_revocation._add_to_memory("expired1", past)
    session_revocation._add_to_memory("expired2", past)
    session_revocation._add_to_memory("active1", future)

    removed = session_revocation._purge_expired_inplace()
    assert removed == 2
    assert session_revocation.is_revoked("active1") is True
    assert session_revocation.is_revoked("expired1") is False


# ── DB-backed persistence ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revoke_persists_to_db_and_loads_back(db):
    """Round-trip: revoke → reset memory → load_from_db → still revoked."""
    expiry = int(time.time()) + 3600
    await session_revocation.revoke(db.db, "abc123def456abcd", expiry)
    assert session_revocation.is_revoked("abc123def456abcd") is True

    # Simulate Railway restart: in-memory set wiped, but DB row survives
    session_revocation._reset_for_tests()
    assert session_revocation.is_revoked("abc123def456abcd") is False

    loaded = await session_revocation.load_from_db(db.db)
    assert loaded == 1
    assert session_revocation.is_revoked("abc123def456abcd") is True


@pytest.mark.asyncio
async def test_load_from_db_skips_stale_rows(db):
    """Rows past expires_at are not re-loaded into memory."""
    past = int(time.time()) - 100
    future = int(time.time()) + 3600
    await session_revocation.revoke(db.db, "stalestalestale1", past)
    await session_revocation.revoke(db.db, "freshfreshfresh1", future)

    session_revocation._reset_for_tests()
    loaded = await session_revocation.load_from_db(db.db)
    assert loaded == 1  # only the fresh one
    assert session_revocation.is_revoked("freshfreshfresh1") is True
    assert session_revocation.is_revoked("stalestalestale1") is False


@pytest.mark.asyncio
async def test_purge_expired_deletes_db_rows(db):
    past = int(time.time()) - 100
    future = int(time.time()) + 3600
    await session_revocation.revoke(db.db, "stale-row-tid-aa", past)
    await session_revocation.revoke(db.db, "fresh-row-tid-aa", future)

    await session_revocation.purge_expired(db.db)

    # Reload — only the fresh row should remain
    session_revocation._reset_for_tests()
    loaded = await session_revocation.load_from_db(db.db)
    assert loaded == 1
    assert session_revocation.is_revoked("fresh-row-tid-aa") is True


@pytest.mark.asyncio
async def test_revoke_no_op_for_empty_tid(db):
    """Defensive — never insert empty-string tid rows."""
    await session_revocation.revoke(db.db, "", int(time.time()) + 3600)
    loaded = await session_revocation.load_from_db(db.db)
    assert loaded == 0
