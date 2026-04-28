"""Lock the #502 invariant: dashboard session and OAuth state HMAC keys
are domain-separated from MCP_BEARER_TOKEN and from each other.

Pre-#502 a single MCP_BEARER_TOKEN leak would forge all three token types
(HTTP bearer, dashboard session, OAuth state). These tests prove that
post-#502 the session and state signing keys are different bytes than the
raw bearer, and tokens minted with the raw bearer no longer verify.
"""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest

from oncofiles import oauth, secrets_keys
from oncofiles.server import _make_session_token, _verify_session_token


@pytest.fixture(autouse=True)
def _restore_env(monkeypatch):
    """Each test runs with a known bearer; overrides cleared by default."""
    monkeypatch.setattr(secrets_keys.config, "MCP_BEARER_TOKEN", "test-bearer-secret")
    monkeypatch.delenv("DASHBOARD_SESSION_SECRET", raising=False)
    monkeypatch.delenv("OAUTH_STATE_SECRET", raising=False)
    # Patch the bearer reference inside server / oauth modules too — they
    # capture MCP_BEARER_TOKEN at import time.
    import oncofiles.server as srv

    monkeypatch.setattr(srv, "MCP_BEARER_TOKEN", "test-bearer-secret")
    monkeypatch.setattr(oauth, "MCP_BEARER_TOKEN", "test-bearer-secret")
    yield


def test_session_key_differs_from_raw_bearer():
    assert secrets_keys.dashboard_session_key() != b"test-bearer-secret"


def test_state_key_differs_from_raw_bearer():
    assert secrets_keys.oauth_state_key() != b"test-bearer-secret"


def test_session_and_state_keys_are_different():
    """Domain separation: a leak of one signing key does not enable the other."""
    assert secrets_keys.dashboard_session_key() != secrets_keys.oauth_state_key()


def test_session_key_env_override(monkeypatch):
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "explicit-session-key")
    assert secrets_keys.dashboard_session_key() == b"explicit-session-key"


def test_state_key_env_override(monkeypatch):
    monkeypatch.setenv("OAUTH_STATE_SECRET", "explicit-state-key")
    assert secrets_keys.oauth_state_key() == b"explicit-state-key"


def test_derive_requires_bearer(monkeypatch):
    monkeypatch.setattr(secrets_keys.config, "MCP_BEARER_TOKEN", "")
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN must be set"):
        secrets_keys.dashboard_session_key()
    with pytest.raises(RuntimeError, match="MCP_BEARER_TOKEN must be set"):
        secrets_keys.oauth_state_key()


def test_session_token_roundtrips_with_derived_key():
    token = _make_session_token("user@example.com")
    assert _verify_session_token(token) == "user@example.com"


def test_session_token_signed_with_raw_bearer_no_longer_verifies():
    """A token forged with a raw-bearer leak must NOT pass verification."""
    expiry = str(int(time.time()) + 3600)
    payload = f"attacker@example.com|{expiry}"
    sig = hmac.new(b"test-bearer-secret", payload.encode(), hashlib.sha256).hexdigest()[:32]
    forged = f"{payload}|{sig}"
    assert _verify_session_token(forged) is None


def test_state_token_roundtrips_with_derived_key():
    token = oauth._make_state_token(patient_id="abc-123")
    valid, pid = oauth.verify_state_token(token)
    assert valid is True
    assert pid == "abc-123"


def test_state_token_signed_with_raw_bearer_no_longer_verifies():
    ts = str(int(time.time()))
    prefix = f"abc-123:{ts}"
    sig = hmac.new(b"test-bearer-secret", prefix.encode(), hashlib.sha256).hexdigest()[:32]
    forged = f"{prefix}.{sig}"
    valid, _pid = oauth.verify_state_token(forged)
    assert valid is False


def test_session_key_override_does_not_affect_state_key(monkeypatch):
    """Setting one override must not change the other key."""
    state_before = secrets_keys.oauth_state_key()
    monkeypatch.setenv("DASHBOARD_SESSION_SECRET", "only-session-override")
    state_after = secrets_keys.oauth_state_key()
    assert state_before == state_after
