"""Storing a Google account link for a patient, with an overwrite guard (#591).

Both ways of completing a Google link go through ``store_google_link``: the
``/oauth/callback`` redirect and the ``gdrive_auth_callback`` MCP tool.

- **Overwrite guard.** If the patient already has a token whose
  ``owner_email`` differs from the new account's email (or the new email is
  unknown), only a privileged initiator may replace it: the operator, an
  admin, or the patient's caregiver. Anyone else is refused and nothing is
  written.
- **Audit.** Every link / re-link attempt, stored or refused, writes an
  ``activity_log`` row with ids and keyed email hashes only, never an email.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import UTC, datetime, timedelta

from oncofiles import secrets_keys

logger = logging.getLogger(__name__)

# Same purpose / override as #737's ``secrets_keys.caller_email_hash`` so the
# hashes line up once that helper is on main; switch to it then.
_AUDIT_EMAIL_PURPOSE = b"oncofiles.audit.caller_email.v1"


def email_hash(email: str | None) -> str | None:
    """Keyed HMAC-SHA256 hex of the normalised email, or None (no email / no key)."""
    normalised = (email or "").strip().lower()
    if not normalised:
        return None
    override = os.environ.get("AUDIT_EMAIL_HASH_SECRET", "")
    try:
        key = override.encode() if override else secrets_keys._derive(_AUDIT_EMAIL_PURPOSE, None)
    except RuntimeError:
        return None
    return hmac.new(key, normalised.encode(), hashlib.sha256).hexdigest()


def caregiver_hashes(caregiver_email: str | None) -> set[str]:
    """Hashes of each address in a (comma-separated) caregiver_email."""
    out = set()
    for part in (caregiver_email or "").split(","):
        h = email_hash(part)
        if h:
            out.add(h)
    return out


def mcp_principal_privileged(principal: dict, caregiver_email: str | None) -> bool:
    """An MCP-initiated link is privileged for an admin or the patient's caregiver."""
    if principal.get("adm"):
        return True
    eh = principal.get("eh") or ""
    return bool(eh) and eh in caregiver_hashes(caregiver_email)


async def store_google_link(
    db,
    *,
    patient_id: str,
    tokens: dict,
    service: str,
    principal_kind: str,
    privileged: bool,
    source: str,
) -> tuple[bool, str]:
    """Store the exchanged Google tokens for *patient_id*, or refuse.

    Returns ``(stored, reason)``. Writes one activity_log row either way.
    """
    from oncofiles.models import ActivityLogEntry, OAuthToken
    from oncofiles.oauth import account_email, parse_granted_scopes, required_scopes

    new_email = account_email(tokens)
    existing = await db.get_oauth_token(patient_id=patient_id)
    old_email = (existing.owner_email or "").strip().lower() if existing else ""
    event = "relink" if existing else "link"
    same_account = bool(old_email) and new_email == old_email

    new_scopes = parse_granted_scopes(tokens)
    reason = ""
    if not required_scopes(service) <= set(new_scopes):
        reason = "requested scope not granted"
    elif old_email and not same_account and not privileged:
        reason = "a different Google account is already linked; only the caregiver may replace it"

    if not reason:
        if existing and existing.granted_scopes:
            merged = sorted(set(json.loads(existing.granted_scopes)) | set(new_scopes))
        else:
            merged = new_scopes
        await db.upsert_oauth_token(
            OAuthToken(
                patient_id=patient_id,
                access_token=tokens["access_token"],
                # An old refresh token is kept only for the same account.
                refresh_token=tokens.get("refresh_token", "")
                or (existing.refresh_token if existing and same_account else ""),
                token_expiry=datetime.now(UTC) + timedelta(seconds=tokens.get("expires_in", 3600)),
                granted_scopes=json.dumps(merged),
                gdrive_folder_id=existing.gdrive_folder_id if existing else "",
                owner_email=existing.owner_email if existing else "",
            )
        )

    detail = {
        "event": event,
        "service": service,
        "principal": principal_kind,
        "privileged": privileged,
        "new_email_hash": email_hash(new_email),
        "old_email_hash": email_hash(old_email),
        "same_account": same_account,
    }
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=patient_id,
                session_id="google-link",
                agent_id=source,
                tool_name="google_link",
                input_summary=json.dumps(detail, sort_keys=True),
                output_summary="stored" if not reason else "refused",
                status="ok" if not reason else "error",
                error_message=reason or None,
            )
        )
    except Exception:
        logger.warning("google_link: activity_log write failed", exc_info=True)
    if reason:
        logger.warning("Google link refused for patient %s: %s", patient_id[:8], reason)
    return (not reason), reason
