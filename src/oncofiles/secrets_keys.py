"""Domain-separated key derivation for HMAC-signed tokens (#502).

Historically, ``MCP_BEARER_TOKEN`` was used directly as the HMAC key for both
dashboard session tokens and OAuth state tokens. That meant a single leak of
the bearer secret would forge tokens across three independent auth paths.

This module produces a separate purpose-bound key per token type. Each key is
either:

1. Read from a dedicated env var (``DASHBOARD_SESSION_SECRET`` /
   ``OAUTH_STATE_SECRET``) when set — preferred for explicit rotation.
2. Otherwise derived from ``MCP_BEARER_TOKEN`` via
   ``HMAC-SHA256(MCP_BEARER_TOKEN, purpose)`` with a versioned purpose string
   for domain separation.

Both paths produce raw bytes (not hex). Callers feed the bytes straight into
``hmac.new(key, ...)``.

A single root-secret leak no longer compromises session/state signing.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from oncofiles import config

_DASHBOARD_SESSION_PURPOSE = b"oncofiles.dashboard.session.v1"
_OAUTH_STATE_PURPOSE = b"oncofiles.oauth.state.v1"


def _resolve_bearer(bearer: str | None) -> str:
    if bearer is None:
        # Read fresh from the config module so test monkeypatches of
        # `oncofiles.config.MCP_BEARER_TOKEN` are honored. `from x import y`
        # would have captured the value at import time and defeated patching.
        bearer = config.MCP_BEARER_TOKEN
    if not bearer:
        raise RuntimeError("MCP_BEARER_TOKEN must be set to derive token-signing keys.")
    return bearer


def _derive(purpose: bytes, bearer: str | None) -> bytes:
    resolved = _resolve_bearer(bearer)
    return hmac.new(resolved.encode(), purpose, hashlib.sha256).digest()


def dashboard_session_key(bearer: str | None = None) -> bytes:
    """Key for dashboard session-cookie HMACs.

    Override with ``DASHBOARD_SESSION_SECRET`` (raw string, any length).
    Pass *bearer* explicitly when the caller has its own (patchable) reference
    to ``MCP_BEARER_TOKEN``; otherwise the value is read from
    ``oncofiles.config`` at call time.
    """
    override = os.environ.get("DASHBOARD_SESSION_SECRET", "")
    if override:
        return override.encode()
    return _derive(_DASHBOARD_SESSION_PURPOSE, bearer)


def oauth_state_key(bearer: str | None = None) -> bytes:
    """Key for OAuth ``state`` parameter HMACs.

    Override with ``OAUTH_STATE_SECRET`` (raw string, any length).
    Pass *bearer* explicitly when the caller has its own (patchable) reference
    to ``MCP_BEARER_TOKEN``; otherwise the value is read from
    ``oncofiles.config`` at call time.
    """
    override = os.environ.get("OAUTH_STATE_SECRET", "")
    if override:
        return override.encode()
    return _derive(_OAUTH_STATE_PURPOSE, bearer)
