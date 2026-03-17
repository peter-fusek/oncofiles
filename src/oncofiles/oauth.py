"""Google OAuth 2.0 per-user authorization flow."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime

from oncofiles.config import (
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    GOOGLE_OAUTH_REDIRECT_URI,
    MCP_BEARER_TOKEN,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# State token validity window (seconds)
_STATE_MAX_AGE = 600  # 10 minutes


def _make_state_token() -> str:
    """Generate an HMAC-signed state token with embedded timestamp."""
    ts = str(int(time.time()))
    if not MCP_BEARER_TOKEN:
        raise RuntimeError(
            "MCP_BEARER_TOKEN must be set when OAuth is configured — "
            "it is used as the HMAC signing key for state tokens."
        )
    key = MCP_BEARER_TOKEN.encode()
    sig = hmac.new(key, ts.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{sig}"


def verify_state_token(state: str) -> bool:
    """Verify an HMAC-signed state token. Returns True if valid and not expired."""
    if not state or "." not in state:
        return False
    parts = state.split(".", 1)
    if len(parts) != 2:
        return False
    ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    # Check expiry
    if time.time() - ts > _STATE_MAX_AGE:
        return False
    # Verify signature
    if not MCP_BEARER_TOKEN:
        return False
    key = MCP_BEARER_TOKEN.encode()
    expected = hmac.new(key, ts_str.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected)


def get_auth_url(state: str = "") -> str:
    """Generate the Google OAuth 2.0 authorization URL for the user to visit.

    If no state is provided, an HMAC-signed state token is generated automatically
    for CSRF protection.
    """
    from urllib.parse import urlencode

    if not state:
        state = _make_state_token()

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict:
    """Exchange an authorization code for access + refresh tokens.

    Returns dict with: access_token, refresh_token, expires_in, token_type.
    """
    import httpx

    response = httpx.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )
    response.raise_for_status()
    return response.json()


def refresh_access_token(refresh_token: str) -> dict:
    """Refresh an expired access token.

    Returns dict with: access_token, expires_in, token_type.
    """
    import httpx

    response = httpx.post(
        TOKEN_URL,
        data={
            "refresh_token": refresh_token,
            "client_id": GOOGLE_OAUTH_CLIENT_ID,
            "client_secret": GOOGLE_OAUTH_CLIENT_SECRET,
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    return response.json()


def is_token_expired(expiry_str: str | None) -> bool:
    """Check if a token expiry timestamp is in the past."""
    if not expiry_str:
        return True
    try:
        expiry = datetime.fromisoformat(expiry_str)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        return datetime.now(UTC) >= expiry
    except ValueError:
        return True
