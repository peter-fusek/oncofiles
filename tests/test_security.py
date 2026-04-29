"""Tests for security hardening (V1-V4)."""

from __future__ import annotations

import time
from unittest.mock import patch

from oncofiles.oauth import _make_state_token, verify_state_token
from tests.helpers import ERIKA_UUID

# ── OAuth state token (V3) ───────────────────────────────────────────

_TEST_TOKEN = "test-bearer-token-for-hmac"


def test_state_token_roundtrip():
    """Valid state token passes verification."""
    with patch("oncofiles.oauth.MCP_BEARER_TOKEN", _TEST_TOKEN):
        token = _make_state_token(patient_id=ERIKA_UUID)
        valid, patient_id = verify_state_token(token)
        assert valid is True
        assert patient_id == ERIKA_UUID


def test_state_token_roundtrip_with_patient_id():
    """State token embeds and recovers patient_id."""
    with patch("oncofiles.oauth.MCP_BEARER_TOKEN", _TEST_TOKEN):
        token = _make_state_token(patient_id="jan-novak")
        valid, patient_id = verify_state_token(token)
        assert valid is True
        assert patient_id == "jan-novak"


def test_state_token_requires_bearer_token():
    """State token creation fails without MCP_BEARER_TOKEN."""
    import pytest

    with (
        patch("oncofiles.oauth.MCP_BEARER_TOKEN", ""),
        pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN must be set"),
    ):
        _make_state_token(patient_id=ERIKA_UUID)


def test_state_token_rejects_empty():
    valid, _ = verify_state_token("")
    assert valid is False
    valid, _ = verify_state_token(None)
    assert valid is False


def test_state_token_rejects_garbage():
    valid, _ = verify_state_token("not-a-valid-token")
    assert valid is False
    valid, _ = verify_state_token("abc.def")
    assert valid is False


def test_state_token_rejects_tampered():
    with patch("oncofiles.oauth.MCP_BEARER_TOKEN", _TEST_TOKEN):
        token = _make_state_token(patient_id=ERIKA_UUID)
        # Replace signature with garbage
        dot_idx = token.rfind(".")
        tampered = f"{token[:dot_idx]}.{'a' * 32}"
        valid, _ = verify_state_token(tampered)
        assert valid is False


def test_state_token_rejects_expired(monkeypatch):
    """Tokens older than 30 minutes are rejected."""
    with patch("oncofiles.oauth.MCP_BEARER_TOKEN", _TEST_TOKEN):
        token = _make_state_token(patient_id=ERIKA_UUID)
        # Fast-forward time by 31 minutes
        real_time = time.time()
        import oncofiles.oauth as oauth_mod

        monkeypatch.setattr(oauth_mod.time, "time", lambda: real_time + 1900)
        valid, _ = verify_state_token(token)
        assert valid is False


def test_state_token_rejects_no_dot():
    valid, _ = verify_state_token("1234567890")
    assert valid is False


# ── Constant-time comparison (V1 + V4) ──────────────────────────────


async def test_bearer_token_constant_time():
    """PersistentOAuthProvider uses constant-time comparison for bearer tokens."""
    from oncofiles.persistent_oauth import PersistentOAuthProvider

    provider = PersistentOAuthProvider(db=None, bearer_token="secret-token-123")

    # Correct token works
    result = await provider.verify_token("secret-token-123")
    assert result is not None
    assert result.client_id == "oncoteam"

    # Wrong token rejected
    assert await provider.verify_token("wrong-token") is None
    assert await provider.verify_token("secret-token-12") is None
    assert await provider.verify_token("") is None


# ── Error sanitization (V2) ──────────────────────────────────────────


def test_metrics_error_no_leak():
    """Verify the metrics endpoint returns generic error, not exception details.

    This is a code-level check — the actual endpoint test requires a running server.
    """
    # Verify the source code uses generic error message
    import inspect

    from oncofiles.server import metrics

    source = inspect.getsource(metrics)
    assert '"internal error"' in source
    assert "str(e)" not in source


def test_upload_error_no_leak():
    """Verify upload_document returns generic error, not raw exception."""
    import inspect

    from oncofiles.tools.documents import upload_document

    source = inspect.getsource(upload_document)
    assert "Check server logs" in source
    assert 'f"Files API upload failed: {e}"' not in source


def test_upload_document_sanitizes_filename_at_entry():
    """#508: upload_document MUST call sanitize_filename before any
    downstream use (Files API, parser, DB insert, GDrive upload).

    Pre-#508 the raw caller-supplied filename flowed unchanged into
    documents.filename / documents.original_filename / GDrive upload —
    only the FilesClient layer scrubbed forbidden chars, and even there
    `..` segments survived. A path-traversal-shaped name like
    `../../foo.pdf` could be persisted in the DB and rendered in the UI
    as a path with traversal segments.
    """
    import inspect

    from oncofiles.tools.documents import upload_document

    source = inspect.getsource(upload_document)
    # The sanitizer must be called on the caller-supplied `filename`.
    assert "sanitize_filename(filename)" in source, (
        "upload_document no longer sanitizes filename at entry — #508 regression"
    )
    # The reassignment must happen BEFORE the value is used downstream
    # (parse_filename, get_active_document_by_filename, Document(...)).
    sanitize_idx = source.index("sanitize_filename(filename)")
    parse_idx = source.index("parse_filename(filename)")
    db_lookup_idx = source.index("get_active_document_by_filename(filename")
    assert sanitize_idx < parse_idx, "sanitize must precede parse_filename"
    assert sanitize_idx < db_lookup_idx, "sanitize must precede DB lookup"


# ── Rate limiting (#346) ────────────────────────────────────────────


def test_rate_limits_configured():
    """All auth endpoints have rate limits configured."""
    from oncofiles.server import _RATE_LIMITS

    assert "dashboard-verify" in _RATE_LIMITS
    assert "oauth-authorize" in _RATE_LIMITS
    assert "oauth-callback" in _RATE_LIMITS
    # Existing limits still present
    assert "share-link" in _RATE_LIMITS
    assert "patients" in _RATE_LIMITS


def test_rate_limit_in_dashboard_verify():
    """dashboard_verify calls _check_rate_limit."""
    import inspect

    from oncofiles.server import dashboard_verify

    source = inspect.getsource(dashboard_verify)
    assert '_check_rate_limit("dashboard-verify")' in source


def test_rate_limit_in_oauth_endpoints():
    """OAuth authorize and callback call _check_rate_limit."""
    import inspect

    from oncofiles.server import oauth_authorize, oauth_callback

    auth_src = inspect.getsource(oauth_authorize)
    assert '_check_rate_limit("oauth-authorize")' in auth_src

    cb_src = inspect.getsource(oauth_callback)
    assert '_check_rate_limit("oauth-callback")' in cb_src


# ── #519: rate limiter actually engages ────────────────────────────


def test_rate_limit_blocks_after_limit_reached():
    """Pre-#519 the limiter dropped the bucket on an empty window and returned
    BEFORE recording the current call, so the count never grew. Lock the new
    contract: the first request lands in the bucket and the (limit+1)-th call
    in the window gets the 429."""
    from oncofiles.server import _RATE_LIMITS, _check_rate_limit, _rate_limits

    # Pick a limiter with a small cap to keep the test fast and deterministic.
    key = "share-link"  # cap = 5
    limit = _RATE_LIMITS[key]

    _rate_limits.pop(key, None)
    try:
        # Allowed for the first ``limit`` calls.
        for i in range(limit):
            assert _check_rate_limit(key) is None, f"call #{i + 1} should be allowed"
        # Bucket must have grown with each allowed call — pre-#519 it stayed empty.
        assert len(_rate_limits[key]) == limit
        # Next call hits the cap.
        blocked = _check_rate_limit(key)
        assert blocked is not None
        assert blocked.status_code == 429
    finally:
        _rate_limits.pop(key, None)


def test_rate_limit_per_ip_for_share_redeem():
    """share-redeem buckets are scoped per client IP (brute-force protection).
    Two distinct IPs can each consume their own quota; the same IP twice gets
    counted twice."""
    from unittest.mock import MagicMock

    from oncofiles.server import _RATE_LIMITS, _check_rate_limit, _rate_limits

    key = "share-redeem"
    limit = _RATE_LIMITS[key]

    def _req(ip: str):
        r = MagicMock()
        r.client.host = ip
        return r

    for ip in ("1.1.1.1", "2.2.2.2"):
        _rate_limits.pop(f"{key}:{ip}", None)
    try:
        # Burn IP 1's quota.
        for _ in range(limit):
            assert _check_rate_limit(key, request=_req("1.1.1.1")) is None
        # IP 1 is throttled.
        blocked = _check_rate_limit(key, request=_req("1.1.1.1"))
        assert blocked is not None and blocked.status_code == 429
        # IP 2 is unaffected.
        assert _check_rate_limit(key, request=_req("2.2.2.2")) is None
    finally:
        for ip in ("1.1.1.1", "2.2.2.2"):
            _rate_limits.pop(f"{key}:{ip}", None)


def test_rate_limit_window_expiry_releases_bucket():
    """Stale timestamps outside the window are filtered out so a returning
    caller starts with a fresh quota — the rolling-window contract."""
    import time

    from oncofiles.server import _RATE_LIMITS, _RATE_WINDOW, _check_rate_limit, _rate_limits

    key = "share-link"
    limit = _RATE_LIMITS[key]

    # Pre-seed a full bucket ENTIRELY outside the window.
    stale = time.time() - _RATE_WINDOW - 5
    _rate_limits[key] = [stale] * limit
    try:
        # First call should filter the stale entries and land successfully.
        assert _check_rate_limit(key) is None
        # Bucket holds exactly one fresh timestamp now.
        assert len(_rate_limits[key]) == 1
        assert _rate_limits[key][0] > stale
    finally:
        _rate_limits.pop(key, None)
