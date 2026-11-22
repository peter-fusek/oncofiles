"""Documents by e-mail, phase 1 (#950): a private document address per patient.

Address
-------
``<gmail local part>-<token>@INBOUND_DOMAIN`` (maintainer decision, changed
2026-11-21): the prefix is the local part of the patient's caregiver address
when that address is ``@gmail.com`` (lowercase, dots kept, ``+tag`` dropped);
for a non-Gmail caregiver, a list whose first address is not Gmail, or no
caregiver, there is no prefix (token only), nothing is guessed. The prefix is
part of an e-mail address and is never logged. The token is 6
characters of lowercase Crockford base32 (no ``i l o u``, so nothing is
ambiguous), about 30 bits, derived and never stored::

    token = base32(HMAC(inbound_address_key(), f"{patient_id}:{version}"))[:6]

Six characters (maintainer decision 2026-11-22, easier to dictate; it was 10)
are enough because a guessed address files nothing from a stranger: the
sender rules and our own DKIM verification still apply, so a guess only
reaches the unmatched list. Bad-token mail is counted globally per hour
(``bad_tokens_overview``, alert ``bad_tokens_high``) to see enumeration.

**Transition:** a row written before migration 100 holds a 10-character token
(``token_length`` NULL). It keeps routing until ``legacy_grace_end``: 30 days
from ``SHORT_TOKEN_SINCE``, or from the row's ``created_at`` when newer. The
first ``ensure_address`` (dashboard card, welcome reply) supersedes it with a
6-character row of the next version; the old row goes inactive with
``grace_ends_at`` set and still routes until then. After it, a 10-character
token lapses by a time check (``lapsed_token``); nothing is deleted. A rotation
stops every earlier address at once, a graced one included.

``inbound_address_key`` is the #502 purpose-bound key
(``oncofiles.inbound_address.v1``, override ``INBOUND_ADDRESS_SECRET``). The
table ``inbound_addresses`` (migration 095) keeps only ``token_hash(token)``,
an HMAC with ``secrets_keys.inbound_token_hash_key`` (not a bare digest);
the dashboard recomputes the address for display. Rotation = version + 1, the
old row inactive. The prefix is informational and never checked: only the
token routes, and the patient is determined ONLY by that token, never by From.

Webhook (``POST /inbound/email``, ``server.inbound_email_webhook``)
-------------------------------------------------------------------
Each adapter has its own secret and its own trust model (security review):
``INBOUND_MIME_HMAC_SECRET`` is the HMAC key of the raw-MIME route and
``INBOUND_BREVO_TOKEN`` the shared header token of the Brevo route. A route
whose secret is unset is inert: it answers the app's ordinary 404 (the same
as an unknown path), as it does for any request that fails its check. The
dashboard card is enabled when either adapter is. Contract of the raw-MIME
adapter:

* body: the raw RFC 5322 message, at most ``MAX_BODY_BYTES`` (checked from
  ``Content-Length`` and again while reading, before any parsing; 413);
* ``X-Original-To`` (optional): the envelope recipient as the provider saw it;
  without it the recipient is taken from the message's ``Delivered-To``, then
  ``To`` / ``Cc``;
* ``X-Inbound-Auth`` (optional): the provider's verdict on the sender, ``pass``
  when SPF or DKIM passed aligned with the ``From`` domain (DMARC pass);
  anything else, or absent, is a fail;
* ``X-Inbound-Signature: sha256=<hex>``: HMAC-SHA256 with
  ``INBOUND_MIME_HMAC_SECRET`` over
  ``X-Original-To + "\\n" + X-Inbound-Auth + "\\n" + body`` (absent headers are
  empty strings). The two trusted headers are inside the signature so a
  replayed body cannot be re-addressed or have its verdict flipped. Compared
  in constant time before the body is parsed.

After a valid signature the answer is always ``202 {"status": "accepted"}``,
whatever happened to the mail (unknown token, ignored loop, unmatched sender,
filed): the provider learns nothing it could leak, and nobody gets a reply
except the patient's caregiver. An unexpected server error is a 500 (the
provider retries; files already filed are recognised as duplicates).

Provider adapters
-----------------
The core (:func:`process_message`) takes a provider-neutral
:class:`InboundMessage` (recipients, From, auth verdict, the headers the loop
checks need, attachments as name + bytes). :func:`parse_raw_mime` is the raw
MIME adapter (``POST /inbound/email``). :func:`parse_brevo_payload` is the
Brevo inbound-parsing adapter (``POST /inbound/brevo``; D3 = Brevo): JSON
items, attachments by download token fetched only after the sender check,
the request authenticated by a custom header holding the shared secret
(Brevo does not sign its calls). What was verified in Brevo's documentation
and what still needs a live payload is listed above the adapter.

Rules (maintainer decisions 2026-11-21 + infra review)
-------------------------------------------------------
* Files are filed only when the From address is one of the patient's
  caregiver addresses AND the auth verdict passes. Mail from anyone else is
  not filed and gets no reply; it is counted and listed for the admin zone
  (keyed sender hash, counts, time), never the address.
* Filing goes through the dashboard upload backend (#935,
  ``dashboard_upload.handle_upload``), one file per call: the same type
  allowlist (``dashboard_upload.check_file``), caps, curated inbox / Drive-only
  choice and Drive write path. No new Drive write path. Before that each file
  also passes the #815 ``llm_input.extraction_exclusion`` rule with its
  declared content type (raw genotype exports never enter).
* Skipped: signature images (images under ``LOGO_MAX_BYTES`` or at most
  ``LOGO_MAX_PX`` px on both sides), archives, types outside the allowlist
  (checked by magic bytes), duplicates (sha256 within the mail and of recent
  mail filings, md5 against the patient's documents). At most
  ``MAX_FILES_PER_MAIL`` files and ``MAX_BYTES_PER_MAIL`` per mail; per address
  ``MAX_MAILS_PER_HOUR`` / ``MAX_MAILS_PER_DAY`` and ``MAX_FILES_PER_DAY`` for
  authenticated caregiver mail only (checked after the sender check), and a
  separate ``MAX_UNMATCHED_PER_HOUR`` / ``MAX_UNMATCHED_PER_DAY`` for mail that
  is not filed, so a flood of it never blocks the caregiver.
* Message bodies are never stored and never go to any model.
* Loop safety: mail with ``Auto-Submitted`` (other than ``no``),
  ``Precedence: bulk/list/junk``, a ``List-Id``, an empty ``Return-Path``, a
  delivery report (``multipart/report`` / ``message/delivery-status``), a
  ``MAILER-DAEMON`` / ``postmaster`` sender, or a From / Message-ID of our own
  domains is ignored (not filed, no reply). At most one receipt per address
  per ``REPLY_INTERVAL_S``.
* The receipt (``mailer.send_mail``, caregiver only) carries counts and reason
  codes and the consent-by-sending sentence with the link to /terms; no
  names, no file names (#834).
* Logs: counts, reason codes and ``patient_id[:4]`` only; never an address, a
  subject or a file name.

Counters and the unmatched-sender list live in memory (lost on restart,
bounded). The Brevo route does not process in the request: it writes each
mail to the durable queue ``inbound_jobs`` before answering 202 and the worker
in :mod:`oncofiles.inbound_queue` processes it (#952), so a deploy never loses
a mail. The raw-MIME route (disabled in production) still processes
synchronously before its 202; a failure there is a 500 the provider retries.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import re
import sqlite3
import time
from collections import Counter, OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

logger = logging.getLogger(__name__)

# D2 (the domain) is still open: the ONE place it is named.
INBOUND_DOMAIN = "docs.oncofiles.com"
OWN_DOMAINS = frozenset({"oncofiles.com", INBOUND_DOMAIN})
TERMS_URL = "https://oncofiles.com/terms"

TOKEN_LEN = 6  # maintainer decision 2026-11-22 (was 10), ~30 bits
LEGACY_TOKEN_LEN = 10  # rows written before migration 100 (token_length NULL)
TOKEN_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
_TOKEN_RE = re.compile(
    rf"^(?:[{TOKEN_ALPHABET}]{{{TOKEN_LEN}}}|[{TOKEN_ALPHABET}]{{{LEGACY_TOKEN_LEN}}})$"
)
# A 10-character address keeps working for LEGACY_GRACE_DAYS from this instant
# (the deploy of the short tokens), or from the row's created_at when newer.
SHORT_TOKEN_SINCE = datetime(2026, 11, 23, tzinfo=UTC)
LEGACY_GRACE_DAYS = 30
# A token-hash collision (UNIQUE token_hash) bumps the version and retries.
ACTIVATE_ATTEMPTS = 5
# Enumeration watch: mail to an unknown, inactive or lapsed token, all
# addresses together, in a sliding hour; above the threshold each further one
# raises the alert ``bad_tokens_high`` (counts only).
BAD_TOKENS_ALERT_PER_HOUR = 20
BAD_TOKENS_WINDOW_S = 3600
BAD_TOKENS_KEEP = 10_000  # memory bound of the sliding window
BAD_TOKEN_REASONS = ("unknown_token", "inactive_token", "lapsed_token")
GMAIL_DOMAIN = "gmail.com"
# Gmail local parts: letters, digits and single dots, not at either end.
_GMAIL_LOCAL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.]{0,62}[a-z0-9])?$")

# One secret per adapter (security review of #950): never share them.
MIME_SECRET_ENV = "INBOUND_MIME_HMAC_SECRET"  # HMAC key, /inbound/email
BREVO_TOKEN_ENV = "INBOUND_BREVO_TOKEN"  # shared header token, /inbound/brevo
SIGNATURE_HEADER = "x-inbound-signature"
RECIPIENT_HEADER = "x-original-to"
AUTH_HEADER = "x-inbound-auth"

# 25 MB of attachments grow by a third in base64, plus headers and bodies.
MAX_BODY_BYTES = 36 * 1024 * 1024
MAX_FILES_PER_MAIL = 20
MAX_BYTES_PER_MAIL = 25 * 1024 * 1024
MAX_PARTS = 500
MAX_EML_DEPTH = 3
MAX_MAILS_PER_HOUR = 20
MAX_MAILS_PER_DAY = 60
MAX_FILES_PER_DAY = 200
# Mail that is not filed (unknown sender, failed auth) has its own per-address
# cap, so a flood of it can never use up the caregiver's filing quota.
MAX_UNMATCHED_PER_HOUR = 30
MAX_UNMATCHED_PER_DAY = 100
REPLY_INTERVAL_S = 600
LOGO_MAX_BYTES = 10 * 1024
LOGO_MAX_PX = 200
UNMATCHED_MAX = 200
RECENT_MAX = 500

# ── address ──────────────────────────────────────────────────────────────


def _b32(data: bytes, length: int) -> str:
    bits = int.from_bytes(data, "big")
    total = len(data) * 8
    out = []
    for i in range(length):
        shift = total - 5 * (i + 1)
        out.append(TOKEN_ALPHABET[(bits >> shift) & 31])
    return "".join(out)


def derive_token(patient_id: str, version: int, length: int = TOKEN_LEN) -> str:
    """The token of ``patient_id`` at ``version`` (deterministic, ``TOKEN_LEN``
    characters; ``length=LEGACY_TOKEN_LEN`` gives a pre-migration-100 token)."""
    from oncofiles.secrets_keys import inbound_address_key

    digest = hmac.new(
        inbound_address_key(), f"{patient_id}:{int(version)}".encode(), hashlib.sha256
    ).digest()
    return _b32(digest, length)


def token_hash(token: str) -> str:
    """The stored routing key: HMAC of the token with a server-side key.

    Keyed (``secrets_keys.inbound_token_hash_key``), not a bare digest, so a
    copy of the table cannot be checked against guessed tokens offline.
    """
    from oncofiles.secrets_keys import inbound_token_hash_key

    return hmac.new(inbound_token_hash_key(), token.encode(), hashlib.sha256).hexdigest()


def gmail_prefix(caregiver_email: str | None) -> str:
    """The informational prefix: the Gmail local part of the caregiver, or ''.

    Only the FIRST address of a comma-separated caregiver list counts, and only
    when it is ``@gmail.com``: lowercase, dots kept as they are, a ``+tag``
    dropped. Anything else (another domain, characters Gmail does not allow in
    a local part, no caregiver) gives no prefix; nothing is guessed.
    """
    first = (caregiver_email or "").split(",", 1)[0]
    addr = parseaddr(first.strip())[1].strip().lower()
    local, at, domain = addr.rpartition("@")
    if not at or domain != GMAIL_DOMAIN:
        return ""
    local = local.split("+", 1)[0]
    if not _GMAIL_LOCAL_RE.match(local):
        return ""
    return local


def format_address(token: str, caregiver_email: str | None = None) -> str:
    prefix = gmail_prefix(caregiver_email)
    local = f"{prefix}-{token}" if prefix else token
    return f"{local}@{INBOUND_DOMAIN}"


def token_from_address(address: str) -> str | None:
    """The token of a document address, or None when it is not one of ours.

    Only the part after the last ``-`` of the local part counts (a ``+tag`` is
    dropped); the prefix is never checked. Crockford's reading rules
    map ``o`` → ``0`` and ``i`` / ``l`` → ``1``.
    """
    _name, addr = parseaddr(address or "")
    local, at, domain = addr.strip().rpartition("@")
    if not at or domain.lower().rstrip(".") != INBOUND_DOMAIN:
        return None
    local = local.split("+", 1)[0].lower()
    candidate = local.rsplit("-", 1)[-1]
    candidate = candidate.translate(str.maketrans({"o": "0", "i": "1", "l": "1"}))
    return candidate if _TOKEN_RE.match(candidate) else None


def _utcnow() -> datetime:
    """The clock of the legacy-grace checks (one place, patched by tests)."""
    return datetime.now(UTC)


def _parse_instant(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_legacy(row: dict) -> bool:
    """True for a row holding a 10-character token (written before migration 100)."""
    return int(row.get("token_length") or LEGACY_TOKEN_LEN) != TOKEN_LEN


def legacy_grace_end(row: dict) -> datetime:
    """Until when a 10-character row routes: its ``grace_ends_at`` when an
    upgrade set it, else 30 days from ``SHORT_TOKEN_SINCE`` or from the row's
    ``created_at`` when that is newer."""
    graced = _parse_instant(row["grace_ends_at"]) if row.get("grace_ends_at") else None
    if graced is not None:
        return graced
    created = _parse_instant(row.get("created_at")) or SHORT_TOKEN_SINCE
    return max(SHORT_TOKEN_SINCE, created) + timedelta(days=LEGACY_GRACE_DAYS)


def address_routes(row: dict | None, now: datetime | None = None) -> str | None:
    """None when mail to this row is routed, else the drop reason.

    ``unknown_token`` (no row), ``inactive_token`` (rotated away), or
    ``lapsed_token`` (a 10-character row past ``legacy_grace_end``). An
    inactive 10-character row with ``grace_ends_at`` (superseded by its short
    successor, not rotated) still routes until then.
    """
    if row is None:
        return "unknown_token"
    if not is_legacy(row):
        return None if row["active"] else "inactive_token"
    if not row["active"] and not row.get("grace_ends_at"):
        return "inactive_token"
    if (now or _utcnow()) >= legacy_grace_end(row):
        return "lapsed_token"
    return None


def note_bad_token(reason: str, now: float | None = None) -> None:
    """Count one mail to a token that does not route (global, per hour).

    Codes and counts only. Above ``BAD_TOKENS_ALERT_PER_HOUR`` in the last
    hour each further one raises ``bad_tokens_high`` (enumeration watch).
    """
    now = time.time() if now is None else now
    _state.counters[reason] += 1
    book = _state.bad_tokens
    book.append(now)
    while book and now - book[0] >= BAD_TOKENS_WINDOW_S:
        book.popleft()
    if len(book) > BAD_TOKENS_ALERT_PER_HOUR:
        note_alert("bad_tokens_high")


def bad_tokens_overview(now: float | None = None) -> dict:
    """Bad-token mails in the last hour, all addresses (operator ``/readiness``
    and the admin inbound panel; never ``/health``)."""
    now = time.time() if now is None else now
    last_hour = sum(1 for t in _state.bad_tokens if now - t < BAD_TOKENS_WINDOW_S)
    return {
        "last_hour": last_hour,
        "alert_above": BAD_TOKENS_ALERT_PER_HOUR,
        "alerting": last_hour > BAD_TOKENS_ALERT_PER_HOUR,
    }


async def lookup_address(db, token: str | None, *, count: bool = True):
    """``(row, None)`` when mail to ``token`` routes, else ``(None, reason)``.

    The patient is resolved ONLY here, from the keyed token hash. ``count``
    records a drop in the bad-token counter (the paths that drop the mail).
    """
    row = await db.find_inbound_address(token_hash(token)) if token else None
    reason = address_routes(row)
    if reason is not None:
        if count:
            note_bad_token(reason)
        return None, reason
    return row, None


async def _activate_next(db, patient_id: str, keep_previous_until: str | None = None) -> int:
    """Activate a 6-character address at the next free version.

    A ``token_hash`` collision (or a version taken meanwhile) violates a
    UNIQUE constraint and rolls the transaction back; the version is bumped
    and tried again, at most ``ACTIVATE_ATTEMPTS`` times.
    """
    version = await db.max_inbound_address_version(patient_id=patient_id) + 1
    for attempt in range(ACTIVATE_ATTEMPTS):
        try:
            await db.activate_inbound_address(
                patient_id=patient_id,
                version=version,
                token_hash=token_hash(derive_token(patient_id, version)),
                token_length=TOKEN_LEN,
                keep_previous_until=keep_previous_until,
            )
            return version
        except sqlite3.IntegrityError:
            logger.warning(
                "inbound: address collision patient %s version=%d attempt=%d",
                patient_id[:4],
                version,
                attempt + 1,
            )
            version += 1
    raise RuntimeError("inbound: no free address token after retries")


async def ensure_address(db, patient_id: str) -> dict:
    """The patient's active address, created (version 1, or next) when missing.

    An active 10-character row is superseded lazily here by a 6-character row
    of the next version; the old row keeps routing until ``legacy_grace_end``.
    """
    row = await db.get_active_inbound_address(patient_id=patient_id)
    if row is None:
        version = await _activate_next(db, patient_id)
        row = await db.get_active_inbound_address(patient_id=patient_id)
        logger.info("inbound: address created patient %s version=%d", patient_id[:4], version)
    elif is_legacy(row):
        grace = legacy_grace_end(row)
        keep = _iso(grace) if _utcnow() < grace else None
        version = await _activate_next(db, patient_id, keep_previous_until=keep)
        row = await db.get_active_inbound_address(patient_id=patient_id)
        logger.info(
            "inbound: short address patient %s version=%d old_kept=%s",
            patient_id[:4],
            version,
            "yes" if keep else "no",
        )
    return await _address_payload(db, patient_id, row)


async def rotate_address(db, patient_id: str) -> dict:
    """A new address (next version); every earlier one stops routing at once."""
    version = await _activate_next(db, patient_id)
    logger.info("inbound: address rotated patient %s version=%d", patient_id[:4], version)
    row = await db.get_active_inbound_address(patient_id=patient_id)
    return await _address_payload(db, patient_id, row)


async def _address_payload(db, patient_id: str, row: dict) -> dict:
    patient = await db.get_patient(patient_id)
    token = derive_token(patient_id, row["version"], TOKEN_LEN)
    return {
        "enabled": True,
        "address": format_address(token, getattr(patient, "caregiver_email", None)),
        "domain": INBOUND_DOMAIN,
        "version": row["version"],
        "created_at": row["created_at"],
        "terms_url": TERMS_URL,
    }


def mime_secret() -> str:
    """HMAC key of the raw-MIME route ('' = that route is inert)."""
    return os.environ.get(MIME_SECRET_ENV, "")


def brevo_token() -> str:
    """Shared header token of the Brevo route ('' = that route is inert)."""
    return os.environ.get(BREVO_TOKEN_ENV, "")


def is_enabled() -> bool:
    """True when at least one adapter can receive mail."""
    return bool(mime_secret() or brevo_token())


# ── webhook signature ────────────────────────────────────────────────────


def sign(secret: str, original_to: str, auth: str, body: bytes | bytearray) -> str:
    """``sha256=<hex>`` over ``original_to + "\\n" + auth + "\\n" + body``.

    Fed incrementally, so the (up to 36 MiB) body is never copied.
    """
    mac = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    mac.update(original_to.encode() + b"\n" + auth.encode() + b"\n")
    mac.update(body)
    return "sha256=" + mac.hexdigest()


def signature_ok(secret: str, header: str, original_to: str, auth: str, body: bytes) -> bool:
    """Constant-time check of ``X-Inbound-Signature``; False when anything is missing."""
    if not secret or not header:
        return False
    expected = sign(secret, original_to, auth, body)
    return hmac.compare_digest(expected.encode(), header.strip().lower().encode())


# ── provider-neutral message ─────────────────────────────────────────────


@dataclass
class InboundAttachment:
    name: str  # as sent (may be empty); never logged
    data: bytes
    content_type: str = "application/octet-stream"
    inline: bool = False
    # Adapters whose provider delivers attachments by reference (Brevo) set
    # ``fetch`` (an async callable returning the bytes or None) and leave
    # ``data`` empty: the core downloads only after the sender check passed.
    fetch: Callable[[], Awaitable[bytes | None]] | None = None
    declared_size: int | None = None
    # The provider's download reference (Brevo ``DownloadToken``), kept so the
    # durable queue (#952) can rebuild ``fetch`` after a restart. Never logged.
    ref: str = ""


@dataclass
class InboundMessage:
    recipients: list[str]  # candidates, most trusted first
    sender: str  # the From address
    auth_pass: bool  # provider verdict: SPF/DKIM aligned with From
    message_id: str = ""
    auto_submitted: str = ""
    precedence: str = ""
    list_id: str = ""
    return_path: str | None = None  # None = header absent; "<>" = bounce
    is_report: bool = False  # multipart/report or message/delivery-status inside
    attachments: list[InboundAttachment] = field(default_factory=list)
    parse_skips: Counter = field(default_factory=Counter)
    # #950 phase 2: admitted through welcome@ (``inbound_welcome.admit``): the
    # reply is the welcome text instead of the receipt.
    welcome: bool = False
    # #950 (Brevo): ``auth_mode == "dkim"`` = the verdict is not known yet
    # (``auth_pass`` stays False) and is decided by the queue worker on the
    # raw message (``inbound_dkim``); ``eml_ref`` is Brevo's
    # ``EMLDownloadToken``; ``dkim_prefilter`` is the refusal code the
    # payload's own ``DKIM-Signature`` headers already give (``dkim_none`` /
    # ``dkim_not_aligned``; "" = worth verifying); ``delivery_id`` is Brevo's
    # per-delivery ``Uuid`` (part of the claimed dedupe key). Never logged.
    auth_mode: str = ""
    eml_ref: str = ""
    dkim_prefilter: str = ""
    delivery_id: str = ""


class UnparseableMessageError(ValueError):
    """The body is not a readable e-mail."""


# Reason codes of a TEMPORARY failure (#952 infra review): a queued job that
# ends on one of them goes back to ``pending`` (with the retry backoff) instead
# of ``done``; after the last attempt it ends ``failed`` with that code.
TEMPORARY_CODES = frozenset({"rate_limited", "fetch_failed", "store_failed", "no_api_key"})


class TemporaryFetchError(Exception):
    """An attachment download that may work later (Brevo 5xx / 429 / auth,
    a timeout or transport error, or ``BREVO_API_KEY`` unset). ``code`` is a
    member of ``TEMPORARY_CODES``. A permanent miss (404, oversize) is a
    ``None`` from the fetcher instead."""

    def __init__(self, code: str = "fetch_failed") -> None:
        super().__init__(code)
        self.code = code


def _header(msg, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # malformed header: treat as absent
        return ""
    return str(value or "").strip()


def _collect(msg, out: list[InboundAttachment], skips: Counter, state: dict, depth: int) -> None:
    for part in msg.walk():
        state["parts"] += 1
        if state["parts"] > MAX_PARTS:
            skips["too_many_parts"] += 1
            return
        ctype = part.get_content_type()
        if ctype == "message/delivery-status" or ctype == "multipart/report":
            state["report"] = True
        if part.is_multipart():
            continue
        try:
            disp = part.get_content_disposition()
            filename = part.get_filename() or ""
        except Exception:
            disp, filename = None, ""
        main = part.get_content_maintype()
        if not filename and disp != "attachment" and main not in ("image", "application"):
            continue  # a message body (text/plain, text/html …): never stored
        try:
            data = part.get_payload(decode=True)
        except Exception:
            data = None
        if not isinstance(data, bytes):
            continue
        if filename.lower().endswith(".eml") or ctype == "message/rfc822":
            if depth >= MAX_EML_DEPTH:
                skips["nested_too_deep"] += 1
                continue
            try:
                inner = BytesParser(policy=policy.default).parsebytes(data)
            except Exception:
                skips["unparseable"] += 1
                continue
            _collect(inner, out, skips, state, depth + 1)
            continue
        out.append(
            InboundAttachment(
                name=filename,
                data=data,
                content_type=ctype,
                inline=disp == "inline" or (not filename and main == "image"),
            )
        )


def parse_raw_mime(raw: bytes, *, original_to: str = "", auth: str = "") -> InboundMessage:
    """Raw-MIME adapter: an :class:`InboundMessage` from the signed request."""
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise UnparseableMessageError(type(exc).__name__) from exc
    recipients: list[str] = []
    if original_to.strip():
        recipients.append(original_to.strip())
    for name in ("Delivered-To", "To", "Cc"):
        try:
            values = [str(v) for v in (msg.get_all(name) or [])]
        except Exception:
            values = []
        recipients.extend(addr for _n, addr in getaddresses(values) if addr)
    sender = parseaddr(_header(msg, "From"))[1]
    attachments: list[InboundAttachment] = []
    skips: Counter = Counter()
    state = {"parts": 0, "report": False}
    _collect(msg, attachments, skips, state, 0)
    return_path = _header(msg, "Return-Path") if msg.get("Return-Path") is not None else None
    return InboundMessage(
        recipients=recipients,
        sender=sender,
        auth_pass=auth.strip().lower() == "pass",
        message_id=_header(msg, "Message-ID"),
        auto_submitted=_header(msg, "Auto-Submitted"),
        precedence=_header(msg, "Precedence"),
        list_id=_header(msg, "List-Id"),
        return_path=return_path,
        is_report=state["report"] or msg.get_content_type() == "multipart/report",
        attachments=attachments,
        parse_skips=skips,
    )


async def _load_referenced(
    msg: InboundMessage,
    skipped: Counter,
    progress: JobProgress | None = None,
    temporary: set[str] | None = None,
) -> None:
    """Download attachments delivered by reference, only for a filed sender.

    At most ``MAX_FILES_PER_MAIL`` downloads; a declared size above the #935
    cap or a running total above ``MAX_BYTES_PER_MAIL`` is not downloaded.

    Queue mode (#952, ``progress`` given): the job's lease is refreshed before
    every download (``progress.heartbeat``), and a temporary download failure
    is collected in ``temporary`` (the job is retried) instead of being a
    ``fetch_failed`` skip; only the last attempt (``progress.final``) also
    reports it as a skip in the receipt. A ``None`` from the fetcher (404,
    oversize) is permanent in both modes.
    """
    from oncofiles.dashboard_upload import MAX_FILE_BYTES

    fetched = 0
    total = 0
    for att in msg.attachments:
        if att.fetch is None or att.data:
            continue
        if att.declared_size is not None and att.declared_size > MAX_FILE_BYTES:
            att.fetch = None
            skipped["file_too_large"] += 1
            continue
        if fetched >= MAX_FILES_PER_MAIL:
            att.fetch = None
            skipped["too_many_files"] += 1
            continue
        if total + (att.declared_size or 0) > MAX_BYTES_PER_MAIL:
            att.fetch = None
            skipped["mail_too_large"] += 1
            continue
        if progress is not None and progress.heartbeat is not None:
            await progress.heartbeat()  # outside the try: a lost lease stops the job
        temp_code: str | None = None
        try:
            data = await att.fetch()
        except TemporaryFetchError as exc:
            logger.info("inbound: attachment fetch failed temporarily (%s)", exc.code)
            data, temp_code = None, exc.code
        except Exception as exc:
            logger.info("inbound: attachment fetch failed (%s)", type(exc).__name__)
            data, temp_code = None, "fetch_failed"
        att.fetch = None
        fetched += 1
        if not data:
            if temp_code and temporary is not None:
                temporary.add(temp_code)
                if progress is not None and not progress.final:
                    continue  # retried later: not a skip of this mail
            skipped["fetch_failed"] += 1
            continue
        att.data = data
        total += len(data)
    # anything still without bytes is dropped before screening
    msg.attachments = [a for a in msg.attachments if a.data]


# ── Brevo inbound parsing adapter (D3 = Brevo, maintainer 2026-11-21) ────
#
# Verified from Brevo's public documentation (developers.brevo.com, read
# 2026-11-21):
# * the inbound webhook POSTs JSON ``{"items": [...]}``; an item has ``Uuid``,
#   ``MessageId``, ``InReplyTo``, ``From`` {Name, Address}, ``To`` / ``Cc``
#   [{Name, Address}], ``Recipients`` [RCPT TO addresses], ``ReplyTo``,
#   ``SentAtDate``, ``Subject``, ``RawHtmlBody``, ``RawTextBody``,
#   ``ExtractedMarkdownMessage``, ``ExtractedMarkdownSignature``,
#   ``SpamScore``, ``Attachments`` and ``Headers`` (the docs show an object of
#   header name -> value in the sample and call it an array in the table);
# * attachments are NOT in the payload: each has ``Name``, ``ContentType``,
#   ``ContentLength``, ``ContentID`` and ``DownloadToken``, fetched with
#   ``GET https://api.brevo.com/v3/inbound/attachments/{downloadToken}`` and
#   the ``api-key`` header (``BREVO_API_KEY``);
# * Brevo does NOT sign webhook calls. ``createWebhook`` takes an optional
#   ``auth`` {token, type} (allowed types and the header it produces are not
#   documented) and a list of custom ``headers`` {key, value} sent with every
#   call. We use the documented custom header: the webhook is created with
#   ``headers: [{"key": "X-Inbound-Token", "value": <INBOUND_BREVO_TOKEN>}]``
#   and the route compares it in constant time (``hmac.compare_digest``).
#   This is a shared secret over TLS, not a body signature: it does not
#   protect against a replay by someone who saw a request, so the route stays
#   behind Traefik/TLS and the token must be rotated like a password.
# * There is no SPF/DKIM/DMARC field. The original headers are in
#   ``Headers``, but any sender can put an ``Authentication-Results`` line in
#   their own mail.
#
# Production finding (2026-11-21): Brevo's payload has NO
# Authentication-Results header at all, so any such header in a payload was
# written by the sender. The verdict is therefore DKIM ONLY (infra review of
# #966): the queue worker downloads the raw message (item key
# ``EMLDownloadToken``) and verifies its DKIM signatures itself
# (``inbound_dkim``; the design is in ``inbound_queue``). The earlier
# Authentication-Results mode was removed: ``INBOUND_BREVO_AUTHSERV_ID`` is
# ignored (one ERROR when it is set; the verdict stays DKIM, fail closed).
# Authentication-Results headers are still read for the go-live diagnostics
# (authserv-id candidates), never for a verdict.

BREVO_ATTACHMENT_URL = "https://api.brevo.com/v3/inbound/attachments/{token}"
BREVO_TOKEN_HEADER = "x-inbound-token"
BREVO_API_KEY_ENV = "BREVO_API_KEY"
# Ignored on purpose (see above); its presence is reported once as an ERROR.
BREVO_AUTHSERV_ENV = "INBOUND_BREVO_AUTHSERV_ID"
VERIFICATION_MODE = "dkim"
_warned_authserv = False


def verification_mode() -> str:
    """Always ``dkim``. A set ``INBOUND_BREVO_AUTHSERV_ID`` is ignored and
    logged once as an ERROR: an Authentication-Results header in a Brevo
    payload comes from the sender and must never pass a mail."""
    global _warned_authserv
    if os.environ.get(BREVO_AUTHSERV_ENV, "").strip() and not _warned_authserv:
        _warned_authserv = True
        logger.error(
            "inbound: INBOUND_BREVO_AUTHSERV_ID is set but ignored: Brevo sends no "
            "Authentication-Results, the sender verdict is DKIM only; unset it"
        )
    return VERIFICATION_MODE


def verification_ready() -> bool:
    """The DKIM verification needs nothing extra (the EML is fetched with the
    same ``BREVO_API_KEY`` as the attachments)."""
    return verification_mode() == VERIFICATION_MODE


BREVO_MAX_BODY_BYTES = 8 * 1024 * 1024  # JSON only: bodies and headers, no files
BREVO_MAX_ITEMS = 20
_DOWNLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")  # no dots, no slashes
_DELIVERY_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")


def brevo_token_ok(secret: str, header: str) -> bool:
    """Constant-time check of the custom ``X-Inbound-Token`` header."""
    if not secret or not header:
        return False
    return hmac.compare_digest(secret.encode(), header.strip().encode())


def _brevo_headers(raw) -> dict[str, list[str]]:
    """``Headers`` as {lower name: [values]}, for both documented shapes."""
    out: dict[str, list[str]] = {}

    def add(name, value):
        if not isinstance(name, str):
            return
        values = value if isinstance(value, list) else [value]
        for v in values:
            if isinstance(v, (str, int, float)):
                out.setdefault(name.strip().lower(), []).append(str(v))

    if isinstance(raw, dict):
        for k, v in raw.items():
            add(k, v)
    elif isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                if "Name" in entry or "name" in entry:
                    add(
                        entry.get("Name", entry.get("name")), entry.get("Value", entry.get("value"))
                    )
                else:
                    for k, v in entry.items():
                        add(k, v)
    return out


# Why the sender verdict refused a Brevo item (#950): one code per refusal,
# the DKIM codes of ``inbound_dkim.DKIM_REFUSAL_REASONS`` (same order).
# Counted in memory and shown in the admin overview as ``auth_refusals``;
# never a header value.
AUTH_REFUSAL_REASONS = (
    "eml_unavailable",
    "eml_too_large",
    "from_repeated",
    "from_mismatch",
    "dkim_none",
    "dkim_not_aligned",
    "dkim_malformed",
    "dkim_l_tag",
    "dkim_weak_alg",
    "dkim_weak_key",
    "dkim_expired",
    "dkim_invalid",
    "dkim_h_incomplete",
    "recipient_not_signed",
    "stale_date",
)


ALERT_CODES = ("claimed_address_refusals_high", "bad_tokens_high")


def note_alert(code: str) -> None:
    """Count one alert (admin inbound panel and operator ``/readiness``)."""
    _state.alerts[code] += 1


def alerts_overview() -> dict:
    """Alert codes with counts, in memory only (never on ``/health``)."""
    return {code: _state.alerts[code] for code in ALERT_CODES}


def note_auth_refusal(reason: str) -> None:
    """Count one refused verdict by its code (admin overview ``auth_refusals``)."""
    _state.auth_refusals[reason] += 1


# Authserv-id candidates (go-live diagnostics, #950): infra must learn which
# authserv-id Brevo's MX writes before setting INBOUND_BREVO_AUTHSERV_ID. For
# every item of a payload that passed the token check, the part before the
# first ";" of each Authentication-Results header is kept ONLY when it looks
# like a hostname; anything else is just counted as invalid. Nothing else of
# the header (nor addresses, subjects, bodies) is kept or logged.
AUTHSERV_CANDIDATES_MAX = 20
_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]{1,253}$")


def _authserv_ids(headers: dict[str, list[str]]) -> tuple[Counter, int]:
    """Hostname-shaped authserv-ids of one item with the number of A-R lines
    carrying each (header order kept), and the number of A-R headers whose
    authserv-id is not hostname-shaped."""
    hosts: Counter = Counter()
    invalid = 0
    for line in headers.get("authentication-results", []):
        candidate = line.partition(";")[0].strip().lower()
        if _HOSTNAME_RE.match(candidate):
            hosts[candidate] += 1
        else:
            invalid += 1
    return hosts, invalid


LINES_BUCKETS = ("1", "2", "3+")  # A-R lines carrying one id within one item


def _lines_bucket(lines: int) -> str:
    return str(lines) if lines < 3 else "3+"


def _note_authserv_candidates(hosts: Counter) -> None:
    """Count one item's hostnames, with a lines-per-item histogram (``2`` =
    the provider's own line plus a planted copy, ``1`` = one line only). At
    most ``AUTHSERV_CANDIDATES_MAX`` are kept: a new one replaces the one on
    the fewest items (oldest on a tie), so a burst of planted one-off values
    never pushes out the id seen on every mail."""
    book = _state.authserv_hosts
    now = _now_iso()
    for host, lines in hosts.items():
        entry = book.get(host)
        if entry is None:
            if len(book) >= AUTHSERV_CANDIDATES_MAX:
                victim = min(book, key=lambda h: (book[h]["items"], book[h]["last_at"]))
                del book[victim]
                _state.authserv_stats["evicted"] += 1
            entry = book[host] = {
                "items": 0,
                "lines_per_item": dict.fromkeys(LINES_BUCKETS, 0),
                "last_at": now,
            }
        entry["items"] += 1
        entry["lines_per_item"][_lines_bucket(lines)] += 1
        entry["last_at"] = now


def authserv_candidates() -> dict:
    """Admin overview: hostname candidates with counts, plus totals."""
    hosts = sorted(
        (
            {"host": h, **e, "lines_per_item": dict(e["lines_per_item"])}
            for h, e in _state.authserv_hosts.items()
        ),
        key=lambda e: (-e["items"], e["host"]),
    )
    stats = _state.authserv_stats
    return {
        "hosts": hosts,
        "items_seen": stats["items"],
        "items_without_header": stats["no_header"],
        "invalid": stats["invalid"],
        "evicted": stats["evicted"],
    }


def _log_authserv_candidates(items: list) -> None:
    """Collect the candidates of one payload; one INFO line per item with the
    hostnames and how many A-R lines carry each (hostnames and counts only)."""
    for item in items:
        if not isinstance(item, dict):
            continue
        headers = _brevo_headers(item.get("Headers"))
        _state.authserv_stats["items"] += 1
        if not headers.get("authentication-results"):
            _state.authserv_stats["no_header"] += 1
            logger.info("inbound: authserv-id candidates: none (no Authentication-Results)")
            continue
        hosts, invalid = _authserv_ids(headers)
        _state.authserv_stats["invalid"] += invalid
        _note_authserv_candidates(hosts)
        listed = ", ".join(f"{h}(lines={n})" for h, n in hosts.items()) or "none"
        if invalid:
            listed += f"; invalid={invalid}"
        logger.info("inbound: authserv-id candidates: %s", listed)


# Payload field names (go-live diagnostics, #950): production payloads carry
# no Authentication-Results header, so infra must learn which authentication
# evidence Brevo does deliver. For every item of a payload that passed the
# token check only the NAMES are kept: of the entries in ``Headers`` (both
# documented shapes), of the item's own keys and of the payload's keys. A
# name must match ``FIELD_NAME_RE``; anything else is only counted as invalid.
# Never a header value, an address, a subject or a body.
FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
FIELD_NAMES_PER_ITEM_MAX = 50  # per item and kind; more are counted as truncated
FIELD_NAMES_MAX = 200  # distinct names kept per kind; the least-seen is evicted

# Authentication evidence as presence flags (booleans, never values). Header
# flags: exact header names, or a name prefix ("x-brevo-"); item-key flags:
# exact key or substring, case-insensitive.
_EVIDENCE_HEADERS = {
    "arc_authentication_results": "arc-authentication-results",
    "arc_seal": "arc-seal",
    "arc_message_signature": "arc-message-signature",
    "received_spf": "received-spf",
    "dkim_signature": "dkim-signature",
    "authentication_results": "authentication-results",
}
_EVIDENCE_HEADER_PREFIXES = {
    "header_x_brevo": "x-brevo-",
    "header_x_sib": "x-sib-",
    "header_x_mailin": "x-mailin-",
}
_EVIDENCE_KEYS_EXACT = {"key_spam_score": "spamscore"}
_EVIDENCE_KEYS_SUBSTRING = {
    "key_spam": "spam",
    "key_spf": "spf",
    "key_dkim": "dkim",
    "key_dmarc": "dmarc",
}
AUTH_EVIDENCE_FLAGS = (
    *_EVIDENCE_HEADERS,
    *_EVIDENCE_HEADER_PREFIXES,
    *_EVIDENCE_KEYS_EXACT,
    *_EVIDENCE_KEYS_SUBSTRING,
)


def _header_names(raw) -> list[str]:
    """The names of ``Headers`` entries in order, for both documented shapes
    (an object, or a list of ``{Name, Value}`` / ``{name: value}`` entries).
    Values are never read."""
    names: list[str] = []
    if isinstance(raw, dict):
        names.extend(k for k in raw if isinstance(k, str))
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            if "Name" in entry or "name" in entry:
                name = entry.get("Name", entry.get("name"))
                if isinstance(name, str):
                    names.append(name)
            else:
                names.extend(k for k in entry if isinstance(k, str))
    return names


def _valid_names(names) -> list[str]:
    """Distinct valid names (first-seen order), at most
    ``FIELD_NAMES_PER_ITEM_MAX``; invalid and truncated ones are counted."""
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        if not isinstance(name, str) or not FIELD_NAME_RE.match(name):
            _state.field_stats["invalid_names"] += 1
            continue
        if name in seen:
            continue
        if len(out) >= FIELD_NAMES_PER_ITEM_MAX:
            _state.field_stats["truncated_names"] += 1
            continue
        seen.add(name)
        out.append(name)
    return out


def _auth_evidence(header_names: list[str], item_keys: list[str]) -> dict[str, bool]:
    """Presence flags over ALL names of one item (not only the capped list)."""
    headers = {n.strip().lower() for n in header_names if isinstance(n, str)}
    keys = {k.strip().lower() for k in item_keys if isinstance(k, str)}
    flags = {flag: name in headers for flag, name in _EVIDENCE_HEADERS.items()}
    for flag, prefix in _EVIDENCE_HEADER_PREFIXES.items():
        flags[flag] = any(h.startswith(prefix) for h in headers)
    for flag, name in _EVIDENCE_KEYS_EXACT.items():
        flags[flag] = name in keys
    for flag, part in _EVIDENCE_KEYS_SUBSTRING.items():
        flags[flag] = any(part in k for k in keys)
    return flags


def _note_names(book: dict, names: list[str]) -> None:
    """Count one item's (or payload's) names; at most ``FIELD_NAMES_MAX``
    are kept, a new one replaces the one seen on the fewest items (oldest on
    a tie)."""
    for name in names:
        _state.field_seq += 1
        entry = book.get(name)
        if entry is None:
            if len(book) >= FIELD_NAMES_MAX:
                victim = min(book, key=lambda n: (book[n]["items"], book[n]["seq"]))
                del book[victim]
                _state.field_stats["evicted"] += 1
            entry = book[name] = {"items": 0, "seq": 0}
        entry["items"] += 1
        entry["seq"] = _state.field_seq


def _ranked(book: dict) -> list[dict]:
    return [
        {"name": n, "items": e["items"]}
        for n, e in sorted(book.items(), key=lambda kv: (-kv[1]["items"], kv[0]))
    ]


def field_names_overview() -> dict:
    """Admin overview: header names, item keys and payload keys with the
    number of items (payloads) carrying each, auth evidence flag counts and
    totals. Names and counts only."""
    stats = _state.field_stats
    return {
        "header_names": _ranked(_state.header_names),
        "item_keys": _ranked(_state.item_keys),
        "payload_keys": _ranked(_state.payload_keys),
        "auth_evidence": {f: _state.auth_evidence[f] for f in AUTH_EVIDENCE_FLAGS},
        "field_names_stats": {
            "items_seen": stats["items"],
            "payloads_seen": stats["payloads"],
            "invalid_names": stats["invalid_names"],
            "truncated_names": stats["truncated_names"],
            "evicted": stats["evicted"],
        },
    }


def _log_field_names(payload: dict, items: list) -> None:
    """Collect the names of one payload; one INFO line per item with its
    header names and item keys, and one with its auth evidence flags (names
    and booleans only)."""
    _state.field_stats["payloads"] += 1
    payload_keys = _valid_names(list(payload))
    _note_names(_state.payload_keys, payload_keys)
    logger.info("inbound: payload keys: %s", ", ".join(payload_keys) or "none")
    for item in items:
        if not isinstance(item, dict):
            continue
        _state.field_stats["items"] += 1
        all_headers = _header_names(item.get("Headers"))
        all_keys = list(item)
        header_names = _valid_names(all_headers)
        item_keys = _valid_names(all_keys)
        _note_names(_state.header_names, header_names)
        _note_names(_state.item_keys, item_keys)
        flags = _auth_evidence(all_headers, all_keys)
        present = [f for f in AUTH_EVIDENCE_FLAGS if flags[f]]
        for flag in present:
            _state.auth_evidence[flag] += 1
        logger.info(
            "inbound: header names: %s; item keys: %s",
            ", ".join(header_names) or "none",
            ", ".join(item_keys) or "none",
        )
        logger.info("inbound: auth evidence: %s", ", ".join(present) or "none")


def _mailbox(value) -> str:
    if isinstance(value, dict):
        return str(value.get("Address") or "").strip()
    return ""


# Brevo answers that may succeed later (#952): server errors, throttling, a
# request timeout, and an API key Brevo refuses (a configuration fix, not a
# property of the mail). Any other non-200 (404 "download token not found",
# 410, other 4xx) is permanent.
_BREVO_TEMPORARY_STATUS = frozenset({401, 403, 408, 425, 429})


def _missing_key_fetcher():
    async def fetch() -> bytes | None:
        raise TemporaryFetchError("no_api_key")

    return fetch


def _brevo_fetcher(token: str, api_key: str, limit: int):
    async def fetch() -> bytes | None:
        import httpx

        url = BREVO_ATTACHMENT_URL.format(token=token)
        try:
            async with (
                httpx.AsyncClient(timeout=30.0) as client,
                client.stream("GET", url, headers={"api-key": api_key}) as resp,
            ):
                if resp.status_code != 200:
                    logger.info("inbound: brevo attachment fetch status=%d", resp.status_code)
                    if resp.status_code >= 500 or resp.status_code in _BREVO_TEMPORARY_STATUS:
                        raise TemporaryFetchError("fetch_failed")
                    return None
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > limit:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.TransportError as exc:  # timeouts, connection errors
            raise TemporaryFetchError("fetch_failed") from exc

    return fetch


def parse_brevo_item(item: dict) -> InboundMessage:
    """Brevo adapter: one ``items[]`` entry → :class:`InboundMessage`.

    Bodies (``RawHtmlBody`` / ``RawTextBody`` / ``ExtractedMarkdown*``) are
    never read. Attachments are references, downloaded by the core only after
    the sender check passed (``BREVO_API_KEY``; without it nothing is fetched).
    """
    from oncofiles.dashboard_upload import MAX_FILE_BYTES

    if not isinstance(item, dict):
        raise UnparseableMessageError("item")
    headers = _brevo_headers(item.get("Headers"))
    recipients = [str(r) for r in item.get("Recipients") or [] if isinstance(r, str)]
    for key in ("To", "Cc"):
        recipients.extend(a for a in (_mailbox(m) for m in item.get(key) or []) if a)
    api_key = os.environ.get(BREVO_API_KEY_ENV, "")
    attachments: list[InboundAttachment] = []
    skips: Counter = Counter()
    for att in item.get("Attachments") or []:
        if not isinstance(att, dict):
            continue
        token = str(att.get("DownloadToken") or "")
        if not _DOWNLOAD_TOKEN_RE.match(token):
            skips["fetch_failed"] += 1
            continue
        try:
            size = int(att.get("ContentLength")) if att.get("ContentLength") is not None else None
        except (TypeError, ValueError):
            size = None
        attachments.append(
            InboundAttachment(
                name=str(att.get("Name") or ""),
                data=b"",
                content_type=str(att.get("ContentType") or "application/octet-stream").lower(),
                inline=bool(att.get("ContentID")) and not att.get("Name"),
                # Without BREVO_API_KEY the reference is kept (#952): the queue
                # retries it once the key is set; a direct call reports it as
                # fetch_failed, as before.
                fetch=(
                    _brevo_fetcher(token, api_key, MAX_FILE_BYTES + 1)
                    if api_key
                    else _missing_key_fetcher()
                ),
                declared_size=size,
                ref=token,
            )
        )
    return_path = headers.get("return-path")

    def first(name: str) -> str:
        """The value the loop checks see: any non-trivial one wins."""
        values = [v.strip() for v in headers.get(name) or [] if v.strip()]
        if name == "auto-submitted":
            values = [v for v in values if v.lower() != "no"] or values
        return values[0] if values else ""

    ctype = first("content-type").lower()
    sender = _mailbox(item.get("From"))
    eml_ref = str(item.get("EMLDownloadToken") or "")
    if not _DOWNLOAD_TOKEN_RE.match(eml_ref):
        eml_ref = ""
    uuids = item.get("Uuid")
    delivery = uuids[0] if isinstance(uuids, list) and uuids else uuids
    delivery = str(delivery) if isinstance(delivery, str) else ""
    from oncofiles.inbound_dkim import payload_prefilter

    verification_mode()  # reports a set INBOUND_BREVO_AUTHSERV_ID once
    return InboundMessage(
        recipients=recipients,
        sender=sender,
        # DKIM only: decided later by the queue worker on the raw message
        auth_pass=False,
        auth_mode="dkim",
        eml_ref=eml_ref,
        dkim_prefilter=payload_prefilter(headers.get("dkim-signature") or [], sender) or "",
        delivery_id=delivery if _DELIVERY_ID_RE.match(delivery) else "",
        message_id=str(item.get("MessageId") or "").strip(),
        auto_submitted=first("auto-submitted"),
        precedence=first("precedence"),
        list_id=first("list-id"),
        return_path=return_path[0].strip() if return_path else None,
        is_report=ctype.startswith("multipart/report"),
        attachments=attachments,
        parse_skips=skips,
    )


def parse_brevo_payload(payload) -> list[InboundMessage]:
    """All messages of one Brevo webhook call (at most ``BREVO_MAX_ITEMS``)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise UnparseableMessageError("payload")
    items = payload["items"][:BREVO_MAX_ITEMS]
    if items:
        _log_field_names(payload, items)
        _log_authserv_candidates(items)
    out = []
    for item in items:
        try:
            out.append(parse_brevo_item(item))
        except UnparseableMessageError:
            continue
    return out


# ── attachment screening ─────────────────────────────────────────────────

_ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".gz", ".tgz", ".tar", ".bz2", ".xz", ".zipx", ".cab")
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"heif"}


def _is_archive(data: bytes, name: str) -> bool:
    if name.lower().endswith(_ARCHIVE_EXTS):
        return True
    return (
        data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
        or data.startswith((b"Rar!\x1a\x07", b"7z\xbc\xaf\x27\x1c", b"\x1f\x8b", b"BZh"))
        or data.startswith(b"\xfd7zXZ\x00")
        or data[257:262] == b"ustar"
    )


def sniff(data: bytes, name: str) -> str | None:
    """The canonical extension the content proves (magic bytes), or None."""
    if b"%PDF-" in data[:1024]:
        return ".pdf"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp" and data[8:12] in _HEIF_BRANDS:
        return ".heic"
    low = name.lower()
    if low.endswith((".txt", ".md")) and b"\x00" not in data:
        try:
            data.decode("utf-8-sig")
        except UnicodeDecodeError:
            return None
        return ".md" if low.endswith(".md") else ".txt"
    return None


def image_size(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG / GIF / JPEG from its header, else None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = int.from_bytes(data[i + 2 : i + 4], "big")
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                return int.from_bytes(data[i + 7 : i + 9], "big"), int.from_bytes(
                    data[i + 5 : i + 7], "big"
                )
            i += 2 + seg
    return None


def is_signature_image(att: InboundAttachment) -> bool:
    is_image = att.content_type.startswith("image/") or att.data[:6] in (b"GIF87a", b"GIF89a")
    is_image = is_image or att.data.startswith((b"\xff\xd8\xff", b"\x89PNG"))
    if not is_image:
        return False
    if len(att.data) < LOGO_MAX_BYTES:
        return True
    size = image_size(att.data)
    return bool(size) and size[0] <= LOGO_MAX_PX and size[1] <= LOGO_MAX_PX


def _same_type(ext_a: str, ext_b: str) -> bool:
    from oncofiles.dashboard_upload import ALLOWED_TYPES

    alias = {"image/heif": "image/heic"}
    a, b = ALLOWED_TYPES.get(ext_a), ALLOWED_TYPES.get(ext_b)
    return a is not None and alias.get(a, a) == alias.get(b, b)


def screen(att: InboundAttachment, index: int) -> tuple[str, str] | str:
    """``(name, mime)`` for a file that may be filed, else a skip reason code.

    The final word is the #935 dashboard check (``dashboard_upload.check_file``:
    the same type allowlist, size cap and raw-genotype rule), preceded by the
    archive / signature-image skips and the #815 exclusion with the declared
    content type.
    """
    from oncofiles.curated.llm_input import extraction_exclusion
    from oncofiles.dashboard_upload import _ext, check_file
    from oncofiles.files_api import sanitize_filename

    if not att.data:
        return "empty_file"
    raw_name = sanitize_filename(att.name) if (att.name or "").strip() else ""
    if _is_archive(att.data, raw_name):
        return "archive_refused"
    if is_signature_image(att):
        return "signature_image"
    sniffed = sniff(att.data, raw_name)
    if sniffed is None:
        if raw_name.lower().endswith((".vcf", ".vcf.gz", ".csv", ".tsv")):
            return "raw_genotype_excluded"
        return "type_not_allowed"
    ext = _ext(raw_name)
    stem = raw_name[: -len(ext)] if ext else raw_name
    if not stem:
        stem = f"email-{'image' if att.inline else 'attachment'}-{index + 1}"
    if not ext:
        name = stem + sniffed
    elif _same_type(ext, sniffed):
        name = raw_name
    else:
        return "content_mismatch"
    pages = []
    if sniffed in (".txt", ".md"):
        pages = [(1, att.data.decode("utf-8-sig", "replace"))]
    doc = {"filename": raw_name or name, "mime_type": att.content_type}
    if extraction_exclusion(doc, pages):
        return "raw_genotype_excluded"
    checked = check_file(name, att.data)
    return checked


# ── in-memory state ──────────────────────────────────────────────────────


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.counters: Counter = Counter()  # mail outcomes and file reasons
        self.unmatched: OrderedDict[tuple[str, str], dict] = OrderedDict()
        self.mails: dict[str, list[float]] = {}  # token hash -> filed-mail times (24 h)
        self.unmatched_mails: dict[str, list[float]] = {}  # token hash -> not-filed times
        self.files_day: dict[str, list[tuple[float, int]]] = {}
        # #950 phase 2: mails of a trial patient in holding mode, both routes
        # (token hash -> times), next to the durable count of queued jobs
        self.trial_mails: dict[str, list[float]] = {}
        self.last_reply: dict[str, float] = {}
        self.done_ids: OrderedDict[str, None] = OrderedDict()
        self.recent: dict[str, OrderedDict[str, None]] = {}  # patient -> sha256 filed
        self.locks: dict[str, asyncio.Lock] = {}
        # go-live diagnostics (#950): auth refusal codes, authserv candidates
        self.auth_refusals: Counter = Counter()
        # alert codes for the admin panel and operator /readiness (codes and
        # counts only), and the claimed-bucket batch-503s per token hash
        self.alerts: Counter = Counter()
        self.claimed_503: dict[str, list[float]] = {}
        # #950 short tokens: times of bad-token mails, all addresses (1 h)
        self.bad_tokens: deque[float] = deque(maxlen=BAD_TOKENS_KEEP)
        self.authserv_hosts: dict[str, dict] = {}
        self.authserv_stats: Counter = Counter()
        # header / item / payload field names and auth evidence flags (#950)
        self.header_names: dict[str, dict] = {}
        self.item_keys: dict[str, dict] = {}
        self.payload_keys: dict[str, dict] = {}
        self.auth_evidence: Counter = Counter()
        self.field_stats: Counter = Counter()
        self.field_seq = 0

    def lock(self, key: str) -> asyncio.Lock:
        lk = self.locks.get(key)
        if lk is None:
            lk = self.locks[key] = asyncio.Lock()
        return lk


_state = _State()


def reset_state() -> None:
    """Tests only."""
    _state.reset()


def _remember(od: OrderedDict, key: str, limit: int) -> None:
    od[key] = None
    od.move_to_end(key)
    while len(od) > limit:
        od.popitem(last=False)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_unmatched(sender: str, patient_id: str, reason: str, files: int) -> None:
    from oncofiles.secrets_keys import caller_email_hash

    h = (caller_email_hash(sender) or "unknown")[:16]
    key = (h, patient_id[:4])
    entry = _state.unmatched.get(key)
    now = _now_iso()
    if entry is None:
        entry = {
            "sender_hash": h,
            "patient": patient_id[:4],
            "mails": 0,
            "files": 0,
            "reasons": Counter(),
            "first_at": now,
            "last_at": now,
        }
        _state.unmatched[key] = entry
    entry["mails"] += 1
    entry["files"] += files
    entry["reasons"][reason] += 1
    entry["last_at"] = now
    _state.unmatched.move_to_end(key)
    while len(_state.unmatched) > UNMATCHED_MAX:
        _state.unmatched.popitem(last=False)


def unmatched_overview() -> dict:
    """Admin zone: unmatched senders (keyed hash, counts, times) and totals."""
    senders = [
        {**e, "reasons": dict(e["reasons"])}
        for e in sorted(_state.unmatched.values(), key=lambda e: e["last_at"], reverse=True)
    ]
    return {
        "enabled": is_enabled(),
        "verification_mode": verification_mode(),
        "senders": senders,
        "totals": dict(_state.counters),
        "auth_refusals": {r: _state.auth_refusals[r] for r in AUTH_REFUSAL_REASONS},
        "alerts": alerts_overview(),
        "bad_tokens": bad_tokens_overview(),
        "authserv_candidates": authserv_candidates(),
        **field_names_overview(),
    }


def _within(times: list[float], seconds: float, now: float) -> int:
    return sum(1 for t in times if now - t < seconds)


def _over_cap(book: dict, th: str, now: float, per_hour: int, per_day: int) -> bool:
    """Count one mail in ``book`` for ``th``; True when it is over the caps."""
    times = [t for t in book.get(th, []) if now - t < 86400]
    book[th] = times
    if _within(times, 3600, now) >= per_hour or len(times) >= per_day:
        return True
    times.append(now)
    return False


def _rate_limited(th: str, now: float) -> bool:
    """Filing quota: authenticated caregiver mail only."""
    return _over_cap(_state.mails, th, now, MAX_MAILS_PER_HOUR, MAX_MAILS_PER_DAY)


def quota_frees_at(th: str, now: float) -> tuple[float, str]:
    """When the filing quota of address ``th`` admits a mail again (#952 follow-up).

    Returns ``(epoch seconds, basis)``. ``basis == "window"``: computed from the
    address's own mail times, the moment the oldest mail that keeps the hourly
    and/or the daily cap full leaves its window (the later of the two when both
    are full). ``basis == "next_hour"``: the window end is not knowable from the
    counters (nothing over a cap is recorded, e.g. the in-memory counters of
    another process); then the next full hour. Never counts a mail.
    """
    times = sorted(t for t in _state.mails.get(th, []) if now - t < 86400)
    hour = [t for t in times if now - t < 3600]
    frees: list[float] = []
    if len(hour) >= MAX_MAILS_PER_HOUR:
        frees.append(hour[len(hour) - MAX_MAILS_PER_HOUR] + 3600)
    if len(times) >= MAX_MAILS_PER_DAY:
        frees.append(times[len(times) - MAX_MAILS_PER_DAY] + 86400)
    if frees:
        # one second past the edge: the windows compare with ``now - t < N``
        return max(frees) + 1, "window"
    return float((int(now) // 3600 + 1) * 3600), "next_hour"


def _unmatched_capped(th: str, now: float) -> bool:
    """Separate cap for mail that is not filed; never touches the filing quota."""
    return _over_cap(_state.unmatched_mails, th, now, MAX_UNMATCHED_PER_HOUR, MAX_UNMATCHED_PER_DAY)


def _files_today(th: str, now: float) -> int:
    entries = [(t, n) for t, n in _state.files_day.get(th, []) if now - t < 86400]
    _state.files_day[th] = entries
    return sum(n for _t, n in entries)


# ── loop safety ──────────────────────────────────────────────────────────


def own_domains() -> set[str]:
    domains = set(OWN_DOMAINS)
    mail_from = parseaddr(os.environ.get("MAIL_FROM", ""))[1]
    if "@" in mail_from:
        domains.add(mail_from.rpartition("@")[2].lower())
    return domains


def _domain(addr: str) -> str:
    return addr.rpartition("@")[2].strip().strip(">").lower().rstrip(".")


def _is_own(domain: str, own: set[str]) -> bool:
    return bool(domain) and any(domain == d or domain.endswith("." + d) for d in own)


def loop_reason(msg: InboundMessage) -> str | None:
    """Why a mail must be ignored to avoid loops and backscatter, or None."""
    auto = msg.auto_submitted.strip().lower()
    if auto and auto != "no":
        return "auto_submitted"
    if msg.precedence.strip().lower() in ("bulk", "list", "junk"):
        return "bulk"
    if msg.list_id:
        return "bulk"
    if msg.return_path is not None and msg.return_path.replace(" ", "") in ("<>", ""):
        return "bounce"
    if msg.is_report:
        return "bounce"
    local = msg.sender.rpartition("@")[0].lower()
    if local in ("mailer-daemon", "postmaster") or not msg.sender:
        return "bounce"
    own = own_domains()
    if _is_own(_domain(msg.sender), own):
        return "own_domain"
    if _is_own(_domain(msg.message_id), own):
        return "own_message"
    return None


# ── filing through the #935 backend ──────────────────────────────────────


class _BytesUpload:
    """The one ``UploadFile`` surface ``handle_upload`` uses: filename, read, close."""

    def __init__(self, filename: str, data: bytes) -> None:
        self.filename = filename
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        return self._data if size is None or size < 0 else self._data[:size]

    async def close(self) -> None:
        self._data = b""


# Request-level refusals of handle_upload: they stop the mail's filing.
_STOPPING = {
    "no_drive",
    "folder_not_writable",
    "inbox_unavailable",
    "document_cap",
    # #950 phase 2: holding-store refusals that no later file of the mail
    # can pass either (a smaller file may still fit under trial_bytes_cap)
    "trial_ended",
    "holding_full",
    "disk_low",
}


async def _file_one(db, patient_id: str, name: str, data: bytes) -> str:
    """File ONE file; returns ``uploaded`` or a reason code.

    A welcome@ trial patient without Drive (#950 phase 2,
    ``holding_store.holding_mode``) gets the file in the encrypted holding
    store instead, and its local reading starts at once.
    """
    from oncofiles import holding_store
    from oncofiles.dashboard_upload import handle_upload

    if await holding_store.holding_mode(db, patient_id):
        return await _hold_one(db, patient_id, name, data)
    outcome = await handle_upload(
        db, patient_id, [_BytesUpload(name, data)], log_pid=patient_id[:4]
    )
    if outcome.status != 200:
        return str(outcome.payload.get("error") or "store_failed")
    entry = (outcome.payload.get("files") or [{}])[0]
    if entry.get("status") == "uploaded":
        return "uploaded"
    if entry.get("status") == "refused" and entry.get("code"):
        return str(entry["code"])
    return "store_failed"


async def _hold_one(db, patient_id: str, name: str, data: bytes) -> str:
    """Store ONE file in the holding store (#950 phase 2); ``uploaded`` or a code.

    ``name`` already passed ``screen`` (the #935 type check), so its extension
    names an allowed type.
    """
    from oncofiles import holding_extract, holding_store
    from oncofiles.dashboard_upload import ALLOWED_TYPES, _ext

    mime = ALLOWED_TYPES.get(_ext(name), "application/octet-stream")

    try:
        await holding_store.store_document(db, patient_id, name, data, mime)
    except holding_store.HoldingRefusedError as refused:
        return refused.code  # a limit refused it before any write (infra review)
    except ValueError:
        return "document_cap"  # insert_document's limit
    except Exception as exc:
        logger.warning("inbound: holding store failed (%s)", type(exc).__name__)
        return "store_failed"
    holding_extract.kick(db)
    return "uploaded"


# ── receipt ──────────────────────────────────────────────────────────────

REASON_TEXT: dict[str, tuple[str, str]] = {
    "duplicate": ("už je medzi dokumentmi", "already among the documents"),
    "type_not_allowed": ("nepodporovaný typ súboru", "file type not accepted"),
    "content_mismatch": ("obsah nezodpovedá prípone", "content does not match its extension"),
    "archive_refused": ("archív (zip a pod.) sa neprijíma", "archives (zip etc.) are not accepted"),
    "signature_image": ("malý obrázok z podpisu", "small signature image"),
    "file_too_large": ("súbor nad 25 MB", "file over 25 MB"),
    "empty_file": ("prázdny súbor", "empty file"),
    "raw_genotype_excluded": ("surové genetické dáta", "raw genetic data"),
    "too_many_files": ("viac ako 20 súborov v správe", "more than 20 files in one message"),
    "mail_too_large": ("prílohy spolu nad 25 MB", "attachments over 25 MB in total"),
    "daily_file_cap": ("denný limit súborov", "daily file limit"),
    "no_drive": (
        "pacient nemá pripojený Google Drive",
        "the patient has no Google Drive connected",
    ),
    "folder_not_writable": (
        "do priečinka sa teraz nedá zapisovať",
        "the folder cannot take files now",
    ),
    "inbox_unavailable": ("priečinok nemá použiteľný inbox", "the folder has no usable inbox"),
    "document_cap": ("limit počtu dokumentov", "document limit reached"),
    "store_failed": ("uloženie zlyhalo", "storing failed"),
    "not_attempted": ("neodoslané po chybe", "not attempted after an error"),
    "fetch_failed": ("prílohu sa nepodarilo stiahnuť", "the attachment could not be downloaded"),
    # #950 phase 2: holding-store limits of a welcome@ trial (infra review)
    "trial_ended": (
        "skúšobná doba skončila: prihláste sa a pripojte Google Drive",
        "the trial has ended: sign in and connect Google Drive",
    ),
    "trial_bytes_cap": ("limit veľkosti skúšobného účtu", "trial storage limit reached"),
    "holding_full": ("úložisko je dočasne plné", "the storage is full for now"),
    "disk_low": ("úložisko je dočasne plné", "the storage is full for now"),
}

TERMS_SENTENCE_SK = (
    "Poslaním dokumentov na túto adresu súhlasíte s podmienkami používania "
    "a so spracovaním týchto údajov: " + TERMS_URL
)
TERMS_SENTENCE_EN = (
    "By sending documents to this address you agree to the terms of service "
    "and to the processing of this data: " + TERMS_URL
)


def receipt_text(accepted: int, skipped: Counter) -> tuple[str, str]:
    """(subject, body): counts and reason codes only, never a name or file name."""
    subject = "Oncofiles: dokumenty prijaté / documents received"
    lines_sk = [
        "Dobrý deň,",
        "",
        f"prijali sme {accepted} súbor(ov) na spracovanie.",
    ]
    lines_en = ["Hello,", "", f"we received {accepted} file(s) for processing."]
    if skipped:
        lines_sk.append(f"Preskočené: {sum(skipped.values())}")
        lines_en.append(f"Skipped: {sum(skipped.values())}")
        for code, n in sorted(skipped.items()):
            sk, en = REASON_TEXT.get(code, (code, code))
            lines_sk.append(f"  - {sk} ({code}): {n}")
            lines_en.append(f"  - {en} ({code}): {n}")
    lines_sk += [
        "",
        "Prijaté súbory idú bez zmeny do priečinka pacienta a spracujú sa automaticky.",
        TERMS_SENTENCE_SK,
    ]
    lines_en += [
        "",
        "Accepted files go unchanged into the patient folder and are processed automatically.",
        TERMS_SENTENCE_EN,
    ]
    body = "\n".join(lines_sk) + "\n\n---\n\n" + "\n".join(lines_en) + "\n\nOncofiles\n"
    return subject, body


async def _send_receipt(
    th: str,
    to: str,
    accepted: int,
    skipped: Counter,
    now: float,
    *,
    welcome_address: str | None = None,
) -> bool:
    """The receipt, or with ``welcome_address`` the welcome@ reply (#950 phase
    2, ``inbound_welcome.welcome_text``); one per address per 10 min."""
    from oncofiles import mailer

    last = _state.last_reply.get(th)
    if last is not None and now - last < REPLY_INTERVAL_S:
        _state.counters["reply_throttled"] += 1
        return False
    _state.last_reply[th] = now
    if welcome_address is not None:
        from oncofiles.inbound_welcome import welcome_text

        subject, body = welcome_text(accepted, skipped, welcome_address)
    else:
        subject, body = receipt_text(accepted, skipped)
    sent = await mailer.send_mail(
        [to],
        subject,
        body,
        tags=["inbound_welcome" if welcome_address is not None else "inbound_receipt"],
        headers={"Auto-Submitted": "auto-replied"},  # RFC 3834: never answered by robots
    )
    _state.counters["reply_sent" if sent else "reply_failed"] += 1
    return sent


# ── concurrency ──────────────────────────────────────────────────────────

# At most two inbound bodies are read at a time PER ROUTE (infra review): a
# third request is answered 503 before a byte of its body is read. Separate
# slots per route, because the raw-MIME route reads its body before the HMAC
# can be checked, so unauthenticated callers there must never starve the
# Brevo route (whose token is checked before any slot is taken). Every read
# also has a total deadline, so a slow sender cannot hold a slot.
MIME_READ_SLOTS = asyncio.Semaphore(2)
BREVO_READ_SLOTS = asyncio.Semaphore(2)
READ_DEADLINE_S = 30
# Every inbound mail is processed under ONE lock, one mail after another:
# the app holds a single DB connection, and processing interleaves DB writes
# (address rows, Drive-only rows of the upload) with Drive and SMTP awaits.
# A dedicated lock, not the scheduler semaphore: a long scheduler job must not
# hold mail back for hours.
PROCESS_LOCK = asyncio.Lock()
# Background tasks of this feature (the #952 queue worker kicked by the Brevo
# route); a reference is held until each task ends.
_BACKGROUND: set[asyncio.Task] = set()


def parse_json_strict(raw: bytes | bytearray):
    """``json.loads`` that refuses duplicate keys anywhere (fail closed)."""
    import json

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise UnparseableMessageError("duplicate key")
            out[key] = value
        return out

    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except UnparseableMessageError:
        raise
    except ValueError as exc:
        raise UnparseableMessageError(type(exc).__name__) from exc


# ── core ─────────────────────────────────────────────────────────────────


@dataclass
class JobProgress:
    """Durable progress of one queued mail (#952), so a retry never files twice.

    ``filed`` holds the sha256 (and, for a provider reference, the download
    token) of every file already filed by an earlier attempt;
    ``receipt_sent`` says the caregiver already got the receipt.
    ``save`` (set by the queue worker) persists the change on the event loop
    before the next file is filed.
    """

    filed: set[str] = field(default_factory=set)
    receipt_sent: bool = False
    # The mail already passed the filing quota in an earlier attempt: a retry
    # (after a temporary failure) does not count against it a second time.
    admitted: bool = False
    save: Callable[[], Awaitable[None]] | None = None
    # Refreshes the job's lease; called before every attachment download.
    heartbeat: Callable[[], Awaitable[None]] | None = None
    # A job claimed before: every file is first checked read-only against what
    # is already stored (``_stored_md5s``), so a write whose progress save was
    # lost is never repeated.
    retry: bool = False
    # The job's last attempt: a temporary failure is then reported like a
    # permanent one (skip + receipt) and the worker ends the job ``failed``.
    final: bool = False

    def mark_admitted(self) -> None:
        # Persisted with the next save (a filed file, the receipt, or the
        # worker's save before it puts the job back to pending).
        self.admitted = True

    async def mark_filed(self, sha: str, ref: str = "") -> None:
        self.filed.add(sha)
        if ref:
            self.filed.add(ref)
        if self.save is not None:
            await self.save()

    async def mark_receipt_sent(self) -> None:
        self.receipt_sent = True
        if self.save is not None:
            await self.save()


async def _stored_md5s(db, patient_id: str) -> set[str]:
    """md5s already stored for the patient that the document md5 check misses.

    The curated mirror rows' ``source_md5`` (a file the pipeline already filed
    out of the inbox) and one read-only listing of the curated inbox
    (``dashboard_upload.stored_inbox_md5s``). A failed listing raises: the
    attempt fails and is retried, never risking a second copy.
    """
    from oncofiles.dashboard_upload import stored_inbox_md5s

    state = await db.curated_index_state(patient_id=patient_id)
    md5s = {s["source_md5"] for s in state.values() if s.get("source_md5") and not s["deleted"]}
    return md5s | await stored_inbox_md5s(db, patient_id, log_pid=patient_id[:4])


@dataclass
class Result:
    outcome: str  # filed | unknown_token | ignored_loop | unmatched | rate_limited | …
    patient_prefix: str = ""
    accepted: int = 0
    skipped: Counter = field(default_factory=Counter)
    replied: bool = False
    # Queue mode only (#952): the first temporary failure code (a member of
    # ``TEMPORARY_CODES``) when the mail is not finished; the worker retries
    # it, or ends the job ``failed:<code>`` after its last attempt.
    temporary: str | None = None
    # Queue mode, ``rate_limited`` only: when the filing quota frees up again
    # (epoch seconds, ``time.time`` clock) and how that was found
    # (``quota_frees_at``: ``window`` | ``next_hour``).
    retry_at: float | None = None
    retry_basis: str = ""


async def sender_refusal(db, patient_id: str, msg: InboundMessage) -> str | None:
    """Why this mail is not filed for the patient, or None (a trusted mail).

    The ONE sender rule: the From must match the patient's caregiver and the
    auth verdict must pass. Used by the processing core and, at enqueue, by
    the #952 queue to mark a job ``trusted`` (separate queue caps).
    """
    from oncofiles.persistent_oauth import _email_matches_caregiver

    patient = await db.get_patient(patient_id)
    caregiver = getattr(patient, "caregiver_email", None) if patient else None
    if patient is None or not _email_matches_caregiver(msg.sender, caregiver):
        return "unknown_sender"
    if not msg.auth_pass:
        return "auth_failed"
    return None


def _find_token(msg: InboundMessage) -> str | None:
    for addr in msg.recipients:
        token = token_from_address(addr)
        if token:
            return token
    return None


async def process_message(db, msg: InboundMessage) -> Result:
    """Route, check and file one parsed mail (provider-neutral core).

    Serialized through ``PROCESS_LOCK`` (one mail at a time).
    """
    async with PROCESS_LOCK:
        return await _process_message(db, msg)


async def _process_message(db, msg: InboundMessage, progress: JobProgress | None = None) -> Result:
    token = _find_token(msg)
    if token is None:
        # #950 phase 2: welcome@ (raw-MIME route; the Brevo route admits at
        # enqueue and queues the mail for the trial patient's own token).
        from oncofiles import inbound_welcome

        if inbound_welcome.is_welcome(msg):
            admitted = await inbound_welcome.admit(db, msg)
            if admitted is None:
                return Result("welcome_refused")
            token = admitted[1]
            msg.welcome = admitted[2]
    row, reason = await lookup_address(db, token)
    if row is None:
        logger.info("inbound: dropped reason=%s", reason)
        return Result(reason)
    patient_id = row["patient_id"]
    pp = patient_id[:4]
    th = token_hash(token)
    loop = loop_reason(msg)
    if loop:
        _state.counters[f"ignored_{loop}"] += 1
        logger.info("inbound: patient %s ignored reason=%s", pp, loop)
        return Result("ignored_loop", pp)

    async with _state.lock(patient_id):
        # Bound to the sender and the verdict (security review): an unmatched
        # or forged mail with a real mail's Message-ID never blocks the real one.
        mid_key = hashlib.sha256(
            "\n".join(
                (th, msg.message_id, msg.sender.strip().lower(), str(bool(msg.auth_pass)))
            ).encode()
        ).hexdigest()
        if msg.message_id and mid_key in _state.done_ids:
            _state.counters["duplicate_delivery"] += 1
            logger.info("inbound: patient %s duplicate delivery ignored", pp)
            return Result("duplicate_delivery", pp)
        now = time.time()

        reason = await sender_refusal(db, patient_id, msg)
        if reason:
            # Its own cap (security review): unmatched mail is counted and,
            # beyond the cap, dropped; it never uses the filing quota below.
            if _unmatched_capped(th, now):
                _state.counters["unmatched_capped"] += 1
                logger.info("inbound: patient %s dropped reason=unmatched_capped", pp)
                return Result("unmatched_capped", pp)
            _record_unmatched(msg.sender, patient_id, reason, len(msg.attachments))
            _state.counters[f"unmatched_{reason}"] += 1
            logger.info(
                "inbound: patient %s not filed reason=%s files=%d",
                pp,
                reason,
                len(msg.attachments),
            )
            if msg.message_id:
                _remember(_state.done_ids, mid_key, RECENT_MAX)
            return Result("unmatched", pp)

        queued = progress is not None
        final = queued and progress.final
        # #950 phase 2 (infra + security review): while a welcome@ trial is in
        # holding mode (no Drive yet) its mail ALSO meets the per-sender welcome
        # caps, on top of the phase-1 caps below: the durable count of its
        # queued jobs (Brevo) and an in-memory count of its mails on both routes
        # (raw MIME included); over either: counted, dropped, no reply. Once
        # Drive is connected only the phase-1 caps apply.
        from oncofiles import holding_store, inbound_welcome

        holding = await holding_store.holding_mode(db, patient_id)
        if holding and not (queued and progress.admitted):
            cap = inbound_welcome.MAX_MAILS_PER_SENDER_DAY
            if await inbound_welcome.trial_mails_over_cap(
                db, patient_id, including_this=queued
            ) or _over_cap(_state.trial_mails, th, now, cap, cap):
                _state.counters["trial_capped"] += 1
                logger.info("inbound: patient %s dropped reason=trial_capped", pp)
                return Result("trial_capped", pp)
        # Filing quota: only authenticated caregiver mail counts against it,
        # and a queued mail only once (not again on each retry).
        if not (queued and progress.admitted) and _rate_limited(th, now):
            _state.counters["rate_limited"] += 1
            if queued:
                # #952: a temporary refusal; the queue retries the mail later
                # until the quota window frees up (#952 follow-up).
                logger.info("inbound: patient %s deferred reason=rate_limited", pp)
                retry_at, basis = quota_frees_at(th, now)
                return Result(
                    "rate_limited",
                    pp,
                    temporary="rate_limited",
                    retry_at=retry_at,
                    retry_basis=basis,
                )
            logger.info("inbound: patient %s dropped reason=rate_limited", pp)
            return Result("rate_limited", pp)
        if queued and not progress.admitted:
            progress.mark_admitted()

        # temporary failures of this attempt (queue mode only)
        temporary: set[str] | None = set() if queued else None
        skipped: Counter = Counter(msg.parse_skips)
        prefiled = 0
        if progress is not None and progress.filed:
            # A retried queue job (#952): what an earlier attempt filed is not
            # downloaded or filed again; it counts as accepted.
            keep = []
            for a in msg.attachments:
                if a.ref and a.ref in progress.filed:
                    prefiled += 1
                else:
                    keep.append(a)
            msg.attachments = keep
        await _load_referenced(msg, skipped, progress, temporary)
        recent = _state.recent.setdefault(patient_id, OrderedDict())
        seen: set[str] = set()
        candidates: list[tuple[str, bytes, str, str]] = []
        stored: set[str] | None = None  # md5s already stored (retried jobs only)
        total = 0
        for i, att in enumerate(msg.attachments):
            checked = screen(att, i)
            if isinstance(checked, str):
                skipped[checked] += 1
                continue
            name, _mime = checked
            sha = hashlib.sha256(att.data).hexdigest()
            if progress is not None and sha in progress.filed:
                prefiled += 1
                seen.add(sha)
                continue
            md5 = hashlib.md5(att.data, usedforsecurity=False).hexdigest()
            if progress is not None and progress.retry:
                # #952 infra review: an earlier attempt may have been killed
                # after Drive confirmed the write but before the progress
                # save. Read-only check of what is stored; a hit is filed.
                if stored is None:
                    stored = await _stored_md5s(db, patient_id)
                if md5 in stored or await db.document_md5_exists(md5, patient_id=patient_id):
                    prefiled += 1
                    seen.add(sha)
                    await progress.mark_filed(sha, att.ref)
                    _state.counters["retry_already_stored"] += 1
                    continue
            if (
                sha in seen
                or sha in recent
                or await db.document_md5_exists(md5, patient_id=patient_id)
            ):
                skipped["duplicate"] += 1
                continue
            seen.add(sha)
            if len(candidates) >= MAX_FILES_PER_MAIL:
                skipped["too_many_files"] += 1
                continue
            if total + len(att.data) > MAX_BYTES_PER_MAIL:
                skipped["mail_too_large"] += 1
                continue
            total += len(att.data)
            candidates.append((name, att.data, sha, att.ref))

        accepted = prefiled
        stop_code: str | None = None
        today = _files_today(th, now)
        file_cap = MAX_FILES_PER_DAY
        if holding:
            # the welcome per-sender file cap: every document the patient got in
            # the last 24 h, whatever its route (DB), or this token's files in
            # memory, whichever is higher; the files an earlier attempt of this
            # job filed are already in both and counted through ``accepted``
            stored = await inbound_welcome.trial_files_today(db, patient_id) - prefiled
            today = max(today, stored, 0)
            file_cap = inbound_welcome.MAX_FILES_PER_SENDER_DAY
        for name, data, sha, ref in candidates:
            if stop_code == "retry_later":
                continue  # left for the queue's next attempt, not a skip
            if stop_code:
                skipped[stop_code if stop_code in _STOPPING else "not_attempted"] += 1
                continue
            if today + accepted >= file_cap:
                skipped["daily_file_cap"] += 1
                continue
            code = await _file_one(db, patient_id, name, data)
            if code == "uploaded":
                accepted += 1
                _remember(recent, sha, RECENT_MAX)
                if progress is not None:
                    await progress.mark_filed(sha, ref)
                continue
            if code == "store_failed" and temporary is not None:
                # #952: a Drive failure is temporary; what is left is filed by
                # the retry (the files filed so far are in the progress).
                temporary.add(code)
                if not final:
                    stop_code = "retry_later"
                    continue
            skipped[code] += 1
            if code in _STOPPING or code == "store_failed":
                stop_code = code
        if accepted - prefiled:
            _state.files_day.setdefault(th, []).append((now, accepted - prefiled))
            _state.counters["files_accepted"] += accepted - prefiled
        if temporary and not final:
            # Not finished: no receipt, not remembered as delivered, the skips
            # are reported once the mail is done (or on its last attempt).
            code = sorted(temporary)[0]
            logger.info("inbound: patient %s retry later accepted=%d reason=%s", pp, accepted, code)
            return Result("retry", pp, accepted, skipped, temporary=code)
        _state.counters["mails_filed"] += 1
        for code, n in skipped.items():
            _state.counters[f"skipped_{code}"] += n
        if msg.message_id:
            _remember(_state.done_ids, mid_key, RECENT_MAX)
        logger.info(
            "inbound: patient %s filed accepted=%d skipped=%s",
            pp,
            accepted,
            dict(sorted(skipped.items())),
        )
        replied = False
        if (accepted or skipped) and not (progress is not None and progress.receipt_sent):
            welcome_address = None
            if msg.welcome:
                welcome_address = (await ensure_address(db, patient_id))["address"]
            replied = await _send_receipt(
                th, msg.sender, accepted, skipped, now, welcome_address=welcome_address
            )
            if replied and msg.welcome:
                # #950 phase 3: time-to-value funnel (first welcome reply)
                from oncofiles import trial_funnel

                await trial_funnel.note_event(db, "first_reply_at", patient_id)
            if progress is not None:
                await progress.mark_receipt_sent()
        # On the last attempt a temporary failure was reported in the receipt;
        # the worker still ends the job ``failed:<code>`` (#952).
        last_temporary = sorted(temporary)[0] if temporary else None
        return Result("filed", pp, accepted, skipped, replied, temporary=last_temporary)
