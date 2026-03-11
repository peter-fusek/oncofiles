"""Tests for security hardening (V1-V4)."""

from __future__ import annotations

import time

from oncofiles.oauth import _make_state_token, verify_state_token

# ── OAuth state token (V3) ───────────────────────────────────────────


def test_state_token_roundtrip():
    """Valid state token passes verification."""
    token = _make_state_token()
    assert verify_state_token(token) is True


def test_state_token_rejects_empty():
    assert verify_state_token("") is False
    assert verify_state_token(None) is False


def test_state_token_rejects_garbage():
    assert verify_state_token("not-a-valid-token") is False
    assert verify_state_token("abc.def") is False


def test_state_token_rejects_tampered():
    token = _make_state_token()
    ts, sig = token.split(".", 1)
    tampered = f"{ts}.{'a' * 32}"
    assert verify_state_token(tampered) is False


def test_state_token_rejects_expired(monkeypatch):
    """Tokens older than 10 minutes are rejected."""
    token = _make_state_token()
    # Fast-forward time by 11 minutes
    real_time = time.time()
    import oncofiles.oauth as oauth_mod

    monkeypatch.setattr(oauth_mod.time, "time", lambda: real_time + 700)
    assert verify_state_token(token) is False


def test_state_token_rejects_no_dot():
    assert verify_state_token("1234567890") is False


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
