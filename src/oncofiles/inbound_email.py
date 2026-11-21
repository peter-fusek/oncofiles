"""Documents by e-mail, phase 1 (#950): a private document address per patient.

Address
-------
``<gmail local part>-<token>@INBOUND_DOMAIN`` (maintainer decision, changed
2026-11-21): the prefix is the local part of the patient's caregiver address
when that address is ``@gmail.com`` (lowercase, dots kept, ``+tag`` dropped);
for a non-Gmail caregiver, a list whose first address is not Gmail, or no
caregiver, there is no prefix (token only), nothing is guessed. The prefix is
part of an e-mail address and is never logged. The token is 10
characters of lowercase Crockford base32 (no ``i l o u``, so nothing is
ambiguous), about 50 bits, derived and never stored::

    token = base32(HMAC(inbound_address_key(), f"{patient_id}:{version}"))[:10]

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
import time
from collections import Counter, OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

logger = logging.getLogger(__name__)

# D2 (the domain) is still open: the ONE place it is named.
INBOUND_DOMAIN = "docs.oncofiles.com"
OWN_DOMAINS = frozenset({"oncofiles.com", INBOUND_DOMAIN})
TERMS_URL = "https://oncofiles.com/terms"

TOKEN_LEN = 10
TOKEN_ALPHABET = "0123456789abcdefghjkmnpqrstvwxyz"  # Crockford base32, lowercase
_TOKEN_RE = re.compile(rf"^[{TOKEN_ALPHABET}]{{{TOKEN_LEN}}}$")
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


def derive_token(patient_id: str, version: int) -> str:
    """The 10-character token of ``patient_id`` at ``version`` (deterministic)."""
    from oncofiles.secrets_keys import inbound_address_key

    digest = hmac.new(
        inbound_address_key(), f"{patient_id}:{int(version)}".encode(), hashlib.sha256
    ).digest()
    return _b32(digest, TOKEN_LEN)


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


async def ensure_address(db, patient_id: str) -> dict:
    """The patient's active address, created (version 1, or next) when missing."""
    row = await db.get_active_inbound_address(patient_id=patient_id)
    if row is None:
        version = await db.max_inbound_address_version(patient_id=patient_id) + 1
        await db.activate_inbound_address(
            patient_id=patient_id,
            version=version,
            token_hash=token_hash(derive_token(patient_id, version)),
        )
        row = await db.get_active_inbound_address(patient_id=patient_id)
        logger.info("inbound: address created patient %s version=%d", patient_id[:4], version)
    return await _address_payload(db, patient_id, row)


async def rotate_address(db, patient_id: str) -> dict:
    """A new address (version + 1); the old one stops routing at once."""
    version = await db.max_inbound_address_version(patient_id=patient_id) + 1
    await db.activate_inbound_address(
        patient_id=patient_id,
        version=version,
        token_hash=token_hash(derive_token(patient_id, version)),
    )
    logger.info("inbound: address rotated patient %s version=%d", patient_id[:4], version)
    row = await db.get_active_inbound_address(patient_id=patient_id)
    return await _address_payload(db, patient_id, row)


async def _address_payload(db, patient_id: str, row: dict) -> dict:
    patient = await db.get_patient(patient_id)
    token = derive_token(patient_id, row["version"])
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
#   their own mail, and whether Brevo's MX adds (and strips foreign copies of)
#   its own line is NOT documented. So ``auth_pass`` is True only when
#   exactly one ``Authentication-Results`` header carries the authserv-id of
#   the runtime env ``INBOUND_BREVO_AUTHSERV_ID`` (two or more = a forged copy
#   may be among them = fail), the topmost such line is used, and it says
#   ``dmarc=pass`` with ``header.from`` equal to the From domain. Unset (the
#   default) = every sender fails the check = nothing is filed, everything is
#   listed as unmatched. Infra sets it only after reading a live payload.
# Not verified (needs a live payload): the exact ``Headers`` shape, which
# Authentication-Results line Brevo's MX writes, the ``auth`` header format.

BREVO_ATTACHMENT_URL = "https://api.brevo.com/v3/inbound/attachments/{token}"
BREVO_TOKEN_HEADER = "x-inbound-token"
BREVO_API_KEY_ENV = "BREVO_API_KEY"
BREVO_AUTHSERV_ENV = "INBOUND_BREVO_AUTHSERV_ID"
BREVO_MAX_BODY_BYTES = 8 * 1024 * 1024  # JSON only: bodies and headers, no files
BREVO_MAX_ITEMS = 20
_DOWNLOAD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")  # no dots, no slashes


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


_SINGLE_VALUED_AUTH_HEADERS = ("from", "sender", "return-path")
_AR_HEADER_FROM = re.compile(r"(?:^|[\s;])header\.from=([^\s;]+)")
_AR_DMARC_PASS = re.compile(r"(?:^|[\s;])dmarc=pass\b")


# Why the trusted verdict refused a Brevo item (go-live diagnostics, #950):
# one code per refusal, in the order of the checks. Counted in memory and
# shown in the admin overview as ``auth_refusals``; never a header value.
AUTH_REFUSAL_REASONS = (
    "authserv_unset",
    "single_header_repeated",
    "from_header_mismatch",
    "no_trusted_line",
    "multiple_trusted_lines",
    "dmarc_not_pass",
    "header_from_missing",
    "header_from_mismatch",
)


def _brevo_auth_refusal(headers: dict[str, list[str]], sender: str) -> str | None:
    """The trusted Authentication-Results verdict for ``sender`` (fail closed).

    Returns ``None`` on a pass, otherwise the first failing check as a code of
    :data:`AUTH_REFUSAL_REASONS`.

    Only lines whose authserv-id equals ``INBOUND_BREVO_AUTHSERV_ID`` count.
    More than one such line = refused (a sender may have planted a copy with
    the trusted id below the real one). The topmost (= only) trusted line must
    say ``dmarc=pass`` AND carry ``header.from`` equal to the From domain,
    case-insensitive.
    """
    authserv = os.environ.get(BREVO_AUTHSERV_ENV, "").strip().lower()
    if not authserv:
        return "authserv_unset"
    # A header the verdict leans on that should occur once but occurs more
    # often: refused (fail closed).
    if any(len(headers.get(name, [])) > 1 for name in _SINGLE_VALUED_AUTH_HEADERS):
        return "single_header_repeated"
    header_from = headers.get("from")
    if header_from and _domain(parseaddr(header_from[0])[1]) != _domain(sender):
        return "from_header_mismatch"
    trusted = [
        line.partition(";")[2].lower()
        for line in headers.get("authentication-results", [])
        if line.partition(";")[0].strip().lower() == authserv
    ]
    if not trusted:
        return "no_trusted_line"
    if len(trusted) > 1:
        return "multiple_trusted_lines"
    line = trusted[0]
    if not _AR_DMARC_PASS.search(line):
        return "dmarc_not_pass"
    match = _AR_HEADER_FROM.search(line)
    if not match:
        return "header_from_missing"
    domain = match.group(1).strip().strip('"').rstrip(".").lower()
    if not domain or domain != _domain(sender):
        return "header_from_mismatch"
    return None


def _brevo_auth_pass(headers: dict[str, list[str]], sender: str) -> bool:
    """True only when :func:`_brevo_auth_refusal` finds nothing; a refusal is
    counted by reason code (admin overview ``auth_refusals``)."""
    reason = _brevo_auth_refusal(headers, sender)
    if reason is None:
        return True
    _state.auth_refusals[reason] += 1
    return False


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
    return InboundMessage(
        recipients=recipients,
        sender=_mailbox(item.get("From")),
        auth_pass=_brevo_auth_pass(headers, _mailbox(item.get("From"))),
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
        self.authserv_hosts: dict[str, dict] = {}
        self.authserv_stats: Counter = Counter()

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
        "senders": senders,
        "totals": dict(_state.counters),
        "auth_refusals": {r: _state.auth_refusals[r] for r in AUTH_REFUSAL_REASONS},
        "authserv_candidates": authserv_candidates(),
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
    row = await db.find_inbound_address(token_hash(token)) if token else None
    if row is None or not row["active"]:
        reason = "unknown_token" if row is None else "inactive_token"
        _state.counters[reason] += 1
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
