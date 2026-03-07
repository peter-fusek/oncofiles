"""Google OAuth 2.0 per-user authorization flow."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from oncofiles.config import (
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    GOOGLE_OAUTH_REDIRECT_URI,
)

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def get_auth_url(state: str = "") -> str:
    """Generate the Google OAuth 2.0 authorization URL for the user to visit."""
    from urllib.parse import urlencode

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
    }
    if state:
        params["state"] = state
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
