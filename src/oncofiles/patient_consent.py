"""Consent recorded when a patient is created (#857, GDPR Art. 9).

``POST /api/patients`` refuses a body without ``consent: true`` and the current
``consent_version``. The version names the privacy policy + terms text the
caller agreed to; the dashboard wizard sends the same constant
(``dashboard.js`` ``CONSENT_VERSION``, kept equal by a test). Bump it, in the
same PR, whenever the consent text in the wizard or the meaning of /privacy or
/terms changes: an old page then gets a 400 and must be reloaded.

Stored per patient in ``patient_consents``: version, UTC time, whether an
admin/operator created the patient, and a keyed hash of the caller's e-mail
(``secrets_keys.caller_email_hash``), never the e-mail itself. Existing
patients get no row (no backfill).
"""

from __future__ import annotations

from typing import Any

CONSENT_VERSION = "2026-11-26"  # #856: self-service export + Drive disconnect, erasure scope

# ``patient_consents.kind`` (migration 097): ``wizard`` = the #857 checkbox,
# ``by_sending`` = consent by sending documents to welcome@ (#950 phase 2,
# maintainer decision 2026-11-21: the terms say that whoever sends documents
# agrees to them; the sender is told so in the reply).
CONSENT_KIND_WIZARD = "wizard"
CONSENT_KIND_BY_SENDING = "by_sending"
CONSENT_KINDS = (CONSENT_KIND_WIZARD, CONSENT_KIND_BY_SENDING)

CONSENT_REQUIRED = "consent_required"
CONSENT_VERSION_MISMATCH = "consent_version_mismatch"

_MESSAGES = {
    CONSENT_REQUIRED: (
        "Potvrďte prosím súhlas so spracovaním zdravotných údajov tejto osoby.",
        "Please confirm consent to the processing of this person's health data.",
    ),
    CONSENT_VERSION_MISMATCH: (
        "Text súhlasu sa zmenil. Obnovte prosím stránku a potvrďte ho znova.",
        "The consent text has changed. Please reload the page and confirm it again.",
    ),
}


def consent_error(body: dict[str, Any]) -> str | None:
    """The refusal code for a create-patient body, or None when consent is valid.

    ``consent`` must be the JSON boolean ``true`` (not ``"true"`` or ``1``) and
    ``consent_version`` must equal :data:`CONSENT_VERSION` exactly.
    """
    if body.get("consent") is not True:
        return CONSENT_REQUIRED
    if body.get("consent_version") != CONSENT_VERSION:
        return CONSENT_VERSION_MISMATCH
    return None


def refusal_payload(code: str) -> dict[str, str]:
    """400 body: bilingual message, machine code and the version the server expects."""
    sk, en = _MESSAGES[code]
    return {
        "error": en,
        "error_sk": sk,
        "error_en": en,
        "code": code,
        "consent_version": CONSENT_VERSION,
    }
