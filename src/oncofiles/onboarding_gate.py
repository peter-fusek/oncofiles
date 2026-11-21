"""Server-side onboarding gate (#852).

The landing page says that connecting your own Google Drive opens later. This
module is what makes that true on the server, and it is also the main rollback
switch once self-service onboarding opens (#858).

``ONBOARDING_MODE`` (runtime env, default ``closed``):

=========  ===================================================================
closed     non-admins cannot create a patient, start Google OAuth for a
           patient that has no Google link yet, or set/change a Drive folder
invite     as closed, except for e-mails in ``ONBOARDING_INVITE_EMAILS``
open       every signed-in caller
=========  ===================================================================

Any other value is ``closed`` (fail closed, one warning logged).

Admins always pass: the static bearer / stdio operator, a dashboard session or
MCP OAuth caller whose e-mail is in ``DASHBOARD_ADMIN_EMAILS``.

**Legacy triggers are admin-only in every mode** (``legacy_refusal``): the
legacy Drive sync / import / export / reconcile / rename / folder-setup paths
and the legacy enhance trigger. A new user's folder must only ever be touched
by the curated pipeline; self-service curated enrolment is #853. Re-opening the
legacy path for non-admins is a code change after #853/#784, not an env flip.

Not gated: sign-in, ``/demo``, read tools, the curated pipeline, the old
waitlist route (no longer on the landing), ``/health``. An existing caregiver
of an existing patient keeps working; only the actions above are refused.

Refusals are logged at INFO with a keyed hash of the caller (never the e-mail),
at most once per minute per action.
"""

from __future__ import annotations

import json
import logging
import time

from oncofiles import config

logger = logging.getLogger(__name__)

MODES = ("closed", "invite", "open")

ONBOARDING_CLOSED = "onboarding_closed"
LEGACY_ADMIN_ONLY = "legacy_sync_admin_only"
# The key is still called ``waitlist_url`` in every body (compatibility), but
# the waitlist is gone: ``/#join`` is the landing's "try it" section now
# (demo, sign in, how to get an invitation).
WAITLIST_URL = "/#join"

# Action names (log + tests). Kept short and stable.
ACTION_CREATE_PATIENT = "create_patient"
ACTION_GOOGLE_CONNECT = "google_connect"
ACTION_SET_FOLDER = "set_folder"
ACTION_LEGACY_SYNC = "legacy_sync"

_MESSAGES = {
    ONBOARDING_CLOSED: (
        "Pripojenie vlastného Google Drive je zatiaľ len na pozvanie. "
        "Pozrite si ukážku alebo nám napíšte o prístup.",
        "Connecting your own Google Drive is by invitation for now. "
        "See the demo, or write to us for access.",
    ),
    LEGACY_ADMIN_ONLY: (
        "Staršia synchronizácia Google Drive je vypnutá. Dokumenty spracúva iba "
        "kurátorovaný postup.",
        "The legacy Google Drive sync is turned off. Documents are processed "
        "only by the curated pipeline.",
    ),
}

_warned_modes: set[str] = set()
_LOG_INTERVAL_S = 60.0
_last_log: dict[str, float] = {}
_suppressed: dict[str, int] = {}


def onboarding_mode() -> str:
    """The current mode; unknown values fail closed (one warning per value)."""
    raw = str(getattr(config, "ONBOARDING_MODE", "") or "").strip().lower()
    if raw in MODES:
        return raw
    if raw not in _warned_modes:
        _warned_modes.add(raw)
        logger.warning(
            "ONBOARDING_MODE=%r is not one of %s; treating it as 'closed'",
            raw[:20],
            "/".join(MODES),
        )
    return "closed"


def _invited(email: str | None) -> bool:
    normalised = (email or "").strip().lower()
    if not normalised:
        return False
    invites = getattr(config, "ONBOARDING_INVITE_EMAILS", frozenset()) or frozenset()
    return normalised in {str(e).strip().lower() for e in invites}


def onboarding_allowed(email: str | None, *, is_admin: bool) -> bool:
    """May this caller create a patient / connect Google / set a folder?"""
    if is_admin:
        return True
    mode = onboarding_mode()
    if mode == "open":
        return True
    if mode == "invite":
        return _invited(email)
    return False


def onboarding_allowed_for_hash(email_hash: str | None, *, is_admin: bool) -> bool:
    """Same rule for a caller known only by its keyed e-mail hash.

    An MCP-initiated OAuth state carries the hash, never the e-mail
    (``oauth.mcp_principal``), so ``/oauth/callback`` checks invites this way.
    """
    if is_admin:
        return True
    mode = onboarding_mode()
    if mode == "open":
        return True
    if mode != "invite" or not email_hash:
        return False
    from oncofiles.google_link import email_hash as _hash

    invites = getattr(config, "ONBOARDING_INVITE_EMAILS", frozenset()) or frozenset()
    return email_hash in {_hash(e) for e in invites}


def legacy_allowed(*, is_admin: bool) -> bool:
    """Legacy Drive sync & co.: admin-only in every mode until #853."""
    return bool(is_admin)


def refusal_payload(code: str = ONBOARDING_CLOSED) -> dict:
    sk, en = _MESSAGES[code]
    return {
        "error": code,
        "message_sk": sk,
        "message_en": en,
        "waitlist_url": WAITLIST_URL,
    }


def _caller_ref(email: str | None) -> str:
    """A keyed hash prefix of the caller's e-mail, never the e-mail itself."""
    if not email:
        return "no-email"
    try:
        from oncofiles.secrets_keys import caller_email_hash

        digest = caller_email_hash(email)
    except Exception:
        digest = None
    return digest[:12] if digest else "unhashable"


def log_refusal(action: str, email: str | None, *, surface: str, code: str) -> None:
    """INFO log of a refusal, throttled to one line per action and minute."""
    key = f"{surface}:{action}"
    now = time.monotonic()
    last = _last_log.get(key)
    if last is not None and now - last < _LOG_INTERVAL_S:
        _suppressed[key] = _suppressed.get(key, 0) + 1
        return
    _last_log[key] = now
    suppressed = _suppressed.pop(key, 0)
    logger.info(
        "onboarding gate refused %s via %s (%s, mode=%s, caller=%s, suppressed=%d)",
        action,
        surface,
        code,
        onboarding_mode(),
        _caller_ref(email),
        suppressed,
    )


def status_for(email: str | None, *, is_admin: bool) -> dict:
    """What the dashboard may know: mode + booleans, never the invite list."""
    return {
        "mode": onboarding_mode(),
        "allowed": onboarding_allowed(email, is_admin=is_admin),
        "legacy_sync_allowed": legacy_allowed(is_admin=is_admin),
        "waitlist_url": WAITLIST_URL,
    }


# ── MCP side ────────────────────────────────────────────────────────────────


def mcp_caller() -> tuple[str, bool]:
    """(email, is_admin) of the current MCP caller.

    Admin = ``_is_admin_caller()`` (static bearer, or OAuth e-mail in
    DASHBOARD_ADMIN_EMAILS) or the stdio transport (local IPC, the operator's
    own process — the same trust ``_check_bearer`` gives stdio).
    """
    from oncofiles.tools._helpers import _caller_email, _is_admin_caller

    is_admin = _is_admin_caller() or getattr(config, "MCP_TRANSPORT", "") == "stdio"
    return _caller_email(), is_admin


def mcp_onboarding_refusal(action: str) -> str | None:
    """JSON refusal for an MCP onboarding action, or None when allowed."""
    email, is_admin = mcp_caller()
    if onboarding_allowed(email, is_admin=is_admin):
        return None
    log_refusal(action, email, surface="mcp", code=ONBOARDING_CLOSED)
    return json.dumps(refusal_payload(ONBOARDING_CLOSED))


def mcp_legacy_refusal(tool_name: str) -> str | None:
    """JSON refusal for a legacy Drive trigger called by a non-admin, or None."""
    email, is_admin = mcp_caller()
    if legacy_allowed(is_admin=is_admin):
        return None
    log_refusal(f"{ACTION_LEGACY_SYNC}:{tool_name}", email, surface="mcp", code=LEGACY_ADMIN_ONLY)
    return json.dumps(refusal_payload(LEGACY_ADMIN_ONLY))


async def mcp_google_connect_refusal(db, patient_id: str) -> str | None:
    """Gate for starting/completing Google OAuth from MCP.

    A patient that already has a Google link may re-consent (token refresh,
    added scope); only a first link is gated.
    """
    email, is_admin = mcp_caller()
    if onboarding_allowed(email, is_admin=is_admin):
        return None
    if await has_google_link(db, patient_id):
        return None
    log_refusal(ACTION_GOOGLE_CONNECT, email, surface="mcp", code=ONBOARDING_CLOSED)
    return json.dumps(refusal_payload(ONBOARDING_CLOSED))


async def has_google_link(db, patient_id: str) -> bool:
    """True when the patient already has a stored Google OAuth token."""
    if not patient_id:
        return False
    try:
        return (await db.get_oauth_token(patient_id=patient_id)) is not None
    except Exception:
        # Fail closed: an error reads as "no link", so the gate applies.
        logger.warning("onboarding gate: oauth token lookup failed", exc_info=True)
        return False
