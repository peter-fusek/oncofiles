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


_MCP_CONSENT_PURPOSE = b"oncofiles.mcp_oauth.consent.v1"


def mcp_consent_key(bearer: str | None = None) -> bytes:
    """Key for the MCP OAuth consent-page CSRF nonce (#898).

    Override with ``MCP_CONSENT_SECRET`` (raw string, any length).
    """
    override = os.environ.get("MCP_CONSENT_SECRET", "")
    if override:
        return override.encode()
    return _derive(_MCP_CONSENT_PURPOSE, bearer)


_AUDIT_EMAIL_PURPOSE = b"oncofiles.audit.caller_email.v1"


def audit_email_hash_key(bearer: str | None = None) -> bytes:
    """Key for the caller-email hash stored in ``activity_log`` (#590/#604).

    A keyed HMAC, not a bare SHA-256: a plain hash of an email address is
    reversed by hashing a list of candidate addresses. Override with
    ``AUDIT_EMAIL_HASH_SECRET`` to keep hashes stable across a rotation of
    ``MCP_BEARER_TOKEN`` (the derived key changes with it).
    """
    override = os.environ.get("AUDIT_EMAIL_HASH_SECRET", "")
    if override:
        return override.encode()
    return _derive(_AUDIT_EMAIL_PURPOSE, bearer)


def caller_email_hash(email: str, bearer: str | None = None) -> str | None:
    """HMAC-SHA256 hex of the normalised (stripped, lower-case) email, or None.

    None when there is no email or no key can be derived (no
    ``MCP_BEARER_TOKEN`` and no override), so the audit write never fails.
    """
    normalised = (email or "").strip().lower()
    if not normalised:
        return None
    try:
        key = audit_email_hash_key(bearer)
    except RuntimeError:
        return None
    return hmac.new(key, normalised.encode(), hashlib.sha256).hexdigest()


_INBOUND_ADDRESS_PURPOSE = b"oncofiles.inbound_address.v1"


def inbound_address_key(bearer: str | None = None) -> bytes:
    """Key for the patient document e-mail address token (#950).

    ``token = base32(HMAC(key, f"{patient_id}:{version}"))[:10]``; the DB keeps
    only a keyed hash of it. Override with ``INBOUND_ADDRESS_SECRET`` so the
    addresses survive a rotation of ``MCP_BEARER_TOKEN`` (the derived key
    changes with it, and with it every patient's address).
    """
    override = os.environ.get("INBOUND_ADDRESS_SECRET", "")
    if override:
        return override.encode()
    return _derive(_INBOUND_ADDRESS_PURPOSE, bearer)


_INBOUND_TOKEN_HASH_PURPOSE = b"oncofiles.inbound_token_hash.v1"


def inbound_token_hash_key(bearer: str | None = None) -> bytes:
    """Key for the stored routing hash of an inbound address token (#950).

    Derived from the address key material (``INBOUND_ADDRESS_SECRET`` when
    set, else ``MCP_BEARER_TOKEN``) with its own purpose string, so the hash
    and the token never share a key and no extra env is needed.
    """
    return hmac.new(
        inbound_address_key(bearer), _INBOUND_TOKEN_HASH_PURPOSE, hashlib.sha256
    ).digest()


_INBOUND_JOBS_PURPOSE = b"oncofiles.inbound_jobs.v1"
_INBOUND_JOBS_MESSAGE_ID_PURPOSE = b"oncofiles.inbound_jobs.message_id.v1"
_INBOUND_JOBS_SENDER_PURPOSE = b"oncofiles.inbound_jobs.sender.v1"
INBOUND_JOBS_SECRET_ENV = "INBOUND_JOBS_SECRET"


class InboundJobsSecretMissingError(RuntimeError):
    """``INBOUND_JOBS_SECRET`` is unset: the inbound queue fails closed."""


def inbound_jobs_secret_set() -> bool:
    return bool(os.environ.get(INBOUND_JOBS_SECRET_ENV, ""))


def inbound_jobs_key() -> bytes:
    """32-byte AES-GCM key for the encrypted ``inbound_jobs.payload_enc`` (#952).

    From the MANDATORY runtime env ``INBOUND_JOBS_SECRET`` only (raw string,
    any length, stretched to 32 bytes with the purpose string). Never derived
    from ``MCP_BEARER_TOKEN`` (infra review): a bearer rotation must not touch
    queued mail, and the queue must not depend on a secret reused for HTTP
    auth. Unset → ``InboundJobsSecretMissingError`` (fail closed: the Brevo
    route answers 503 and queues nothing, the worker leaves jobs untouched).
    Rotating it makes jobs still queued at that moment ``undecryptable``.
    """
    secret = os.environ.get(INBOUND_JOBS_SECRET_ENV, "")
    if not secret:
        raise InboundJobsSecretMissingError(INBOUND_JOBS_SECRET_ENV)
    return hmac.new(secret.encode(), _INBOUND_JOBS_PURPOSE, hashlib.sha256).digest()


def inbound_message_id_hash_key() -> bytes:
    """Key for ``inbound_jobs.message_id_hash`` (#952): derived from the same
    mandatory secret with its own purpose string, never the AES key itself."""
    return hmac.new(inbound_jobs_key(), _INBOUND_JOBS_MESSAGE_ID_PURPOSE, hashlib.sha256).digest()


def inbound_sender_hash_key() -> bytes:
    """Key for ``inbound_jobs.sender_hash`` (#952 security review): same
    derivation pattern, its own purpose string."""
    return hmac.new(inbound_jobs_key(), _INBOUND_JOBS_SENDER_PURPOSE, hashlib.sha256).digest()


_HOLDING_STORE_PURPOSE = b"oncofiles.holding_store.v1"
HOLDING_STORE_SECRET_ENV = "HOLDING_STORE_SECRET"


class HoldingStoreSecretMissingError(RuntimeError):
    """``HOLDING_STORE_SECRET`` is unset: the holding store fails closed (#950)."""


def holding_store_secret_set() -> bool:
    return bool(os.environ.get(HOLDING_STORE_SECRET_ENV, ""))


def holding_store_key() -> bytes:
    """32-byte AES-GCM key of the encrypted holding store (#950 phase 2).

    From the MANDATORY runtime env ``HOLDING_STORE_SECRET`` only (raw string,
    stretched with the purpose string), like ``INBOUND_JOBS_SECRET`` (#952):
    never derived from ``MCP_BEARER_TOKEN``, so a bearer rotation never makes
    held documents unreadable. Unset → ``HoldingStoreSecretMissingError``:
    nothing is stored (welcome@ mail is not admitted) and nothing is read.
    Rotating it makes every held file unreadable: rotate only with an empty
    store.
    """
    secret = os.environ.get(HOLDING_STORE_SECRET_ENV, "")
    if not secret:
        raise HoldingStoreSecretMissingError(HOLDING_STORE_SECRET_ENV)
    return hmac.new(secret.encode(), _HOLDING_STORE_PURPOSE, hashlib.sha256).digest()


# #950 quarantine (maintainer decision 2026-11-22): attachments from a sender
# other than the caregiver wait encrypted until the caregiver approves them.
# Every key derives from the holding store key (the MANDATORY
# ``HOLDING_STORE_SECRET``) with its own purpose string: unset = no quarantine.
_QUARANTINE_PURPOSE = b"oncofiles.inbound_quarantine.v1"
_QUARANTINE_TOKEN_PURPOSE = b"oncofiles.inbound_quarantine.token.v1"
_QUARANTINE_CSRF_PURPOSE = b"oncofiles.inbound_quarantine.csrf.v1"
_QUARANTINE_SENDER_PURPOSE = b"oncofiles.inbound_quarantine.sender.v1"


def quarantine_key() -> bytes:
    """32-byte AES-GCM key of quarantined files and their encrypted metadata."""
    return hmac.new(holding_store_key(), _QUARANTINE_PURPOSE, hashlib.sha256).digest()


def quarantine_token_key() -> bytes:
    """HMAC key of the action-link token hashes (only the hash is stored)."""
    return hmac.new(holding_store_key(), _QUARANTINE_TOKEN_PURPOSE, hashlib.sha256).digest()


def quarantine_csrf_key() -> bytes:
    """HMAC key of the confirm form's CSRF value (bound to the token)."""
    return hmac.new(holding_store_key(), _QUARANTINE_CSRF_PURPOSE, hashlib.sha256).digest()


def quarantine_sender_key() -> bytes:
    """HMAC key of the quarantine sender hash (throttle per sender, dedupe)."""
    return hmac.new(holding_store_key(), _QUARANTINE_SENDER_PURPOSE, hashlib.sha256).digest()
