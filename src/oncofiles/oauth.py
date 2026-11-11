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

# State token validity window (seconds). #591: short — the user only has to
# click through Google's consent screen.
_STATE_MAX_AGE = 600  # 10 minutes

# #591: v2 state, bound to the initiating principal, the patient and the
# service. Signed with a key derived from the purpose-bound
# ``secrets_keys.oauth_state_key`` plus a versioned purpose, so a v1 state
# (``{pid}:{ts}.{sig}``) never verifies.
_STATE_V2_PREFIX = "v2."
_STATE_V2_PURPOSE = b"oncofiles.oauth.state.v2"

# Services a state may name, and the scope each one must come back with.
STATE_SERVICES = {"drive", "gmail", "calendar", "all"}

# #506: single-use OAuth state. ``verify_state_token`` records the SHA-256
# hash of every successfully verified state token here, paired with its
# expiry timestamp. A second verify of the same token (a replay) sees the
# hash already present and is rejected. The map is opportunistically
# pruned on each verify so memory stays bounded by max in-flight OAuth
# flows × the validity window × ~50 bytes per entry — tiny in practice.
_consumed_state_tokens: dict[str, float] = {}


def _state_token_hash(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _prune_consumed_state_tokens(now: float | None = None) -> None:
    """Drop entries whose expiry has passed. O(N) sweep, called per verify."""
    now = time.time() if now is None else now
    expired = [h for h, exp in _consumed_state_tokens.items() if exp <= now]
    for h in expired:
        _consumed_state_tokens.pop(h, None)


def _state_key() -> bytes:
    if not MCP_BEARER_TOKEN:
        raise RuntimeError(
            "MCP_BEARER_TOKEN must be set when OAuth is configured — "
            "it is used as the HMAC signing key for state tokens."
        )
    return hmac.new(oauth_state_key(MCP_BEARER_TOKEN), _STATE_V2_PURPOSE, hashlib.sha256).digest()


def service_for_scopes(scopes: list[str]) -> str:
    """The state ``svc`` for a requested scope set."""
    gmail, calendar = SCOPE_GMAIL in scopes, SCOPE_CALENDAR in scopes
    if gmail and calendar:
        return "all"
    if gmail:
        return "gmail"
    if calendar:
        return "calendar"
    return "drive"


def required_scopes(service: str) -> set[str]:
    """Scopes a completed link for *service* must have been granted."""
    return {
        "drive": {SCOPE_DRIVE},
        "gmail": {SCOPE_GMAIL},
        "calendar": {SCOPE_CALENDAR},
        "all": {SCOPE_GMAIL, SCOPE_CALENDAR},
    }.get(service, {SCOPE_DRIVE})


def mcp_principal() -> dict:
    """Principal of an MCP tool call that starts a link (from the verified token).

    Carries an admin flag and the keyed hash of the bound Google email — never
    the email itself, since the state travels through Google and browser
    history. stdio is the local operator.
    """
    from oncofiles import config
    from oncofiles.google_link import email_hash
    from oncofiles.persistent_oauth import _verified_caller_email, _verified_caller_is_admin

    admin = _verified_caller_is_admin.get() or config.MCP_TRANSPORT == "stdio"
    return {"k": "m", "adm": bool(admin), "eh": email_hash(_verified_caller_email.get()) or ""}


def _make_state_token(
    patient_id: str, *, service: str = "drive", principal: dict | None = None
) -> str:
    """Sign a v2 state: ``v2.<base64url(json)>.<hmac>``.

    The payload names the patient, the service and the initiating principal:
    ``{"k": "o"}`` operator bearer, ``{"k": "s", "tid": …}`` dashboard session,
    ``{"k": "m", "adm": …, "eh": …}`` MCP tool caller (default).
    """
    import base64
    import json
    import secrets

    key = _state_key()
    if service not in STATE_SERVICES:
        raise ValueError(f"unknown OAuth service: {service!r}")
    payload = {
        "pid": patient_id,
        "svc": service,
        "pr": principal if principal is not None else mcp_principal(),
        "ts": int(time.time()),
        "n": secrets.token_hex(8),
    }
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode())
    body_s = body.decode().rstrip("=")
    sig = hmac.new(key, f"{_STATE_V2_PREFIX}{body_s}".encode(), hashlib.sha256).hexdigest()
    return f"{_STATE_V2_PREFIX}{body_s}.{sig}"


def _parse_state(state: str) -> tuple[str, str, dict] | None:
    """(signed part, signature, payload) of a structurally valid v2 state."""
    import base64
    import json

    if not state or not state.startswith(_STATE_V2_PREFIX) or len(state) > 2048:
        return None
    signed, _, sig = state.rpartition(".")
    body = signed[len(_STATE_V2_PREFIX) :]
    try:
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except Exception:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("pr"), dict):
        return None
    return signed, sig, payload


def verify_link_state(state: str) -> dict | None:
    """Verify and consume a v2 state. Returns its payload, or None.

    None for a missing, malformed, tampered, expired, replayed or v1 state, or
    one naming an unknown service. Single use (#506): only a state that passes
    every check is recorded as consumed.
    """
    parsed = _parse_state(state)
    if parsed is None or not MCP_BEARER_TOKEN:
        return None
    signed, sig, payload = parsed
    expected = hmac.new(_state_key(), signed.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    ts = payload.get("ts")
    now = time.time()
    if not isinstance(ts, int) or not (0 <= now - ts <= _STATE_MAX_AGE):
        return None
    if payload.get("svc") not in STATE_SERVICES or not payload.get("pid"):
        return None
    _prune_consumed_state_tokens(now)
    state_hash = _state_token_hash(state)
    if state_hash in _consumed_state_tokens:
        logger.warning("OAuth state token replay rejected (age=%ds)", int(now - ts))
        return None
    _consumed_state_tokens[state_hash] = ts + _STATE_MAX_AGE
    return payload


def verify_state_token(state: str) -> tuple[bool, str]:
    """Back-compat wrapper: ``(valid, patient_id)`` for a v2 state.

    The patient id is surfaced even when the state is rejected (expired,
    replayed) so a caller can log which flow failed; ``valid`` is what counts.
    """
    payload = verify_link_state(state)
    if payload is not None:
        return True, payload["pid"]
    parsed = _parse_state(state or "")
    return False, (str(parsed[2].get("pid", "")) if parsed else "")


# #591: identity scopes so the token response carries an ID token with the
# Google account's email (the callback's overwrite guard compares it).
IDENTITY_SCOPES = ["openid", "https://www.googleapis.com/auth/userinfo.email"]


def account_email(token_response: dict) -> str | None:
    """Verified email of the Google account from the token response's ID token.

    The ID token comes straight from Google's token endpoint over TLS, so its
    claims are trusted without a signature check (OIDC Core 3.1.3.7).
    """
    import base64
    import json

    id_token = token_response.get("id_token") or ""
    parts = id_token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return None
    email = (claims.get("email") or "").strip().lower()
    if not email or claims.get("email_verified") not in (True, "true"):
        return None
    return email


def get_auth_url(state: str = "", patient_id: str = "") -> str:
    """Generate the Google OAuth 2.0 authorization URL for the user to visit.

    If no state is provided, an HMAC-signed state token is generated automatically
    for CSRF protection.  *patient_id* is embedded in the state token.
    """
    from urllib.parse import urlencode

    if not state:
        state = _make_state_token(patient_id=patient_id, service="drive")

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join([*SCOPES, *IDENTITY_SCOPES]),
        "access_type": "offline",
        "prompt": "select_account consent",
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


_GMAIL_CALENDAR_SCOPES = frozenset({SCOPE_GMAIL, SCOPE_CALENDAR})


def allowed_scopes(scopes: list[str]) -> list[str]:
    """Drop the Gmail/Calendar scopes unless GMAIL_CALENDAR_ENABLED (#618/#699).

    Single chokepoint for every OAuth URL that requests explicit scopes, so no
    flow (per-service or the combined ``all`` set) can ask Google for mail or
    calendar access while the integration is off.
    """
    from oncofiles.config import gmail_calendar_enabled

    if gmail_calendar_enabled():
        return list(scopes)
    kept = [s for s in scopes if s not in _GMAIL_CALENDAR_SCOPES]
    if len(kept) != len(scopes):
        logger.info("OAuth: Gmail/Calendar scopes dropped (GMAIL_CALENDAR_ENABLED=false)")
    return kept or list(SCOPES)


def get_auth_url_for_scopes(scopes: list[str], state: str = "", patient_id: str = "") -> str:
    """Generate auth URL requesting specific scopes with incremental consent.

    Uses include_granted_scopes=true so existing grants are preserved.
    *patient_id* is embedded in the state token when no explicit state is given.
    """
    from urllib.parse import urlencode

    scopes = allowed_scopes(scopes)
    if not state:
        state = _make_state_token(patient_id=patient_id, service=service_for_scopes(scopes))

    params = {
        "client_id": GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join([*scopes, *IDENTITY_SCOPES]),
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
