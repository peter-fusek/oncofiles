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
from oncofiles.secrets_keys import oauth_state_key

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]

# Per-service scope constants
SCOPE_DRIVE = "https://www.googleapis.com/auth/drive"
SCOPE_GMAIL = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_CALENDAR = "https://www.googleapis.com/auth/calendar.readonly"

# Cumulative scope sets for incremental authorization
GMAIL_SCOPES = [SCOPE_DRIVE, SCOPE_GMAIL]
CALENDAR_SCOPES = [SCOPE_DRIVE, SCOPE_CALENDAR]
ALL_SCOPES = [SCOPE_DRIVE, SCOPE_GMAIL, SCOPE_CALENDAR]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# State token validity window (seconds)
_STATE_MAX_AGE = 1800  # 30 minutes

# #506: single-use OAuth state. ``verify_state_token`` records the SHA-256
# hash of every successfully verified state token here, paired with its
# expiry timestamp. A second verify of the same token (a replay) sees the
# hash already present and is rejected. The map is opportunistically
# pruned on each verify so memory stays bounded by max in-flight OAuth
# flows × 30 min × ~50 bytes per entry — tiny in practice.
_consumed_state_tokens: dict[str, float] = {}


def _state_token_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _prune_consumed_state_tokens(now: float | None = None) -> None:
    """Drop entries whose expiry has passed. O(N) sweep, called per verify."""
    now = time.time() if now is None else now
    expired = [h for h, exp in _consumed_state_tokens.items() if exp <= now]
    for h in expired:
        _consumed_state_tokens.pop(h, None)


def _make_state_token(patient_id: str) -> str:
    """Generate an HMAC-signed state token with embedded timestamp and patient_id.

    Format: {patient_id}:{timestamp}.{hmac} — HMAC covers "{patient_id}:{timestamp}".
    """
    ts = str(int(time.time()))
    if not MCP_BEARER_TOKEN:
        raise RuntimeError(
            "MCP_BEARER_TOKEN must be set when OAuth is configured — "
            "it is used as the HMAC signing key for state tokens."
        )
    key = oauth_state_key(MCP_BEARER_TOKEN)
    payload = f"{patient_id}:{ts}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{patient_id}:{ts}.{sig}"


def verify_state_token(state: str) -> tuple[bool, str]:
    """Verify an HMAC-signed state token. Returns (valid, patient_id).

    Supports both new format ({patient_id}:{ts}.{sig}) and legacy ({ts}.{sig}).

    Single-use semantics (#506): after a state token verifies successfully,
    its SHA-256 hash is stored in ``_consumed_state_tokens`` until expiry.
    A second verify of the same token returns (False, patient_id) — the
    OAuth callback handler already maps that to its standard error path.
    Tokens that fail HMAC verification are NOT recorded (no oracle for
    attackers probing valid tokens via replays).
    """
    if not state or "." not in state:
        return False, ""

    # Split signature from the rest: everything after last "."
    dot_idx = state.rfind(".")
    prefix, sig = state[:dot_idx], state[dot_idx + 1 :]

    # Parse patient_id and timestamp from prefix
    if ":" in prefix:
        # New format: {patient_id}:{timestamp}
        colon_idx = prefix.rfind(":")
        patient_id = prefix[:colon_idx]
        ts_str = prefix[colon_idx + 1 :]
    else:
        # Legacy format: {timestamp} only
        patient_id = ""
        ts_str = prefix

    try:
        ts = int(ts_str)
    except ValueError:
        return False, ""
    now = time.time()
    # Check expiry
    if now - ts > _STATE_MAX_AGE:
        return False, patient_id
    # Verify signature
    if not MCP_BEARER_TOKEN:
        return False, patient_id
    key = oauth_state_key(MCP_BEARER_TOKEN)
    # HMAC covers the full prefix (patient_id:ts or just ts for legacy)
    expected = hmac.new(key, prefix.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        return False, patient_id

    # HMAC valid + within window. Enforce single-use (#506).
    _prune_consumed_state_tokens(now)
    state_hash = _state_token_hash(state)
    if state_hash in _consumed_state_tokens:
        logger.warning(
            "OAuth state token replay rejected (patient_id=%s, age=%ds)",
            patient_id or "<legacy>",
            int(now - ts),
        )
        return False, patient_id
    # Record consumption — expire when the token would have expired anyway.
    _consumed_state_tokens[state_hash] = ts + _STATE_MAX_AGE
    return True, patient_id


def get_auth_url(state: str = "", patient_id: str = "") -> str:
    """Generate the Google OAuth 2.0 authorization URL for the user to visit.

    If no state is provided, an HMAC-signed state token is generated automatically
    for CSRF protection.  *patient_id* is embedded in the state token.
    """
    from urllib.parse import urlencode

    if not state:
        state = _make_state_token(patient_id=patient_id)

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "select_account consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def get_auth_url_for_scopes(scopes: list[str], state: str = "", patient_id: str = "") -> str:
    """Generate auth URL requesting specific scopes with incremental consent.

    Uses include_granted_scopes=true so existing grants are preserved.
    *patient_id* is embedded in the state token when no explicit state is given.
    """
    from urllib.parse import urlencode

    if not state:
        state = _make_state_token(patient_id=patient_id)

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "select_account consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def parse_granted_scopes(token_response: dict) -> list[str]:
    """Extract granted scope strings from a token exchange response."""
    scope_str = token_response.get("scope", "")
    return [s for s in scope_str.split() if s]


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
