"""Durable queue of the Brevo inbound route (#952; table ``inbound_jobs``, migration 096).

Why: ``POST /inbound/brevo`` answers 202 at once and Brevo never retries a
202. Processing used to run in an in-memory task, so a deploy or restart in
between lost the mail. Now the route calls :func:`enqueue` BEFORE it answers
(an enqueue failure is a 500, so Brevo retries) and then :func:`kick`; the
worker :func:`run_pending` does the processing.

Enqueue
-------
Per parsed mail: the address token is found and resolved to its patient
(``inbound_addresses``). An unknown or inactive token is dropped right there,
silently, exactly as the processing core would drop it (no row: nothing to
process, no patient to own it). Otherwise one row: ``patient_id``,
``token_hash``, a keyed hash of the Message-ID and a keyed hash of the
lowercased From + auth verdict (``sender_hash``; UNIQUE together with the
token hash, so a genuine provider retry is not queued twice, while a forged or
unmatched mail sent first with a real mail's Message-ID cannot block it) and
``payload_enc``: AES-GCM (``cryptography``) with
``secrets_keys.inbound_jobs_key()`` (purpose ``oncofiles.inbound_jobs.v1``),
a random 96-bit nonce and ``patient_id|token_hash|job_uuid`` as associated
data (``job_uuid`` is a random uuid per row, so a payload copied onto any other
row does not decrypt). The plaintext is the
minimum the core needs: the address token, the From address and the auth
verdict computed at enqueue, the loop-check header values, the Message-ID,
the adapter's parse skips, and per attachment the Brevo download token, name,
content type, declared size and inline flag (at most ``MAX_FILES_PER_MAIL``
references; the rest count as ``too_many_files``); plus the progress below.
Never a subject or a body. A mail without a Message-ID is deduplicated on a
keyed hash of its payload digest instead (never NULL, which UNIQUE would treat
as distinct).

Caps, counted per trust class over open (pending + processing) jobs; each
row is marked ``trusted`` at enqueue with the core's sender rule
(``inbound_email.sender_refusal``: caregiver From AND auth pass). Trusted:
at most ``MAX_OPEN_JOBS_PER_TOKEN`` per address and ``MAX_OPEN_JOBS_TOTAL`` in
all; a batch that would pass either is refused whole
(``InboundQueueFullError``): the route answers 503 + ``Retry-After``, nothing
is written, only the ``jobs_queue_full`` counter. Untrusted:
``MAX_UNTRUSTED_JOBS_PER_TOKEN`` / ``MAX_UNTRUSTED_JOBS_TOTAL``; a mail over
them is dropped (counter ``jobs_untrusted_dropped``, still 202), so a forged
flood never blocks the caregiver's mail.

The key material is the MANDATORY runtime env ``INBOUND_JOBS_SECRET``, never
derived from ``MCP_BEARER_TOKEN`` (infra review). Unset = fail closed: the
route answers 503 + ``Retry-After`` before reading the body and queues
nothing (Brevo retries), and the worker leaves every job untouched and logs
one warning. The Message-ID and sender hash keys derive from the same secret
with their own purpose strings.

Worker
------
* One job at a time, claim + process + finish all under
  ``inbound_email.PROCESS_LOCK`` (the lock of every inbound mail).
* Claimable: ``pending`` with ``next_attempt_at <= now`` (earliest first), or
  ``processing`` not touched for ``STALE_PROCESSING_MIN`` (a crash or kill
  mid-way). The claim is a conditional UPDATE (state and updated_at
  unchanged), so two containers of a rolling deploy sharing the volume never
  take the same job.
* Lease: the ``updated_at`` the claim set. It is refreshed before every
  attachment download and by every progress save, and every later write
  (progress, finish) is conditional on ``state='processing' AND
  updated_at=<lease>``: a worker whose job was taken over as stale changes
  nothing (``lease_lost``).
* Each claim counts an attempt. A job that reaches ``MAX_ATTEMPTS`` claims
  without finishing ends ``failed`` / ``max_attempts``.
* ``rate_limited`` (the caregiver's hourly / daily filing quota, #952
  follow-up) is a wait, not an attempt: the claim's attempt is given back
  and ``next_attempt_at`` is the moment the quota frees up
  (``inbound_email.quota_frees_at``: the end of the per-address window when
  the counters know it, else the next full hour; logged as ``basis``). Upper
  bound: a job still waiting ``RATE_LIMIT_MAX_WAIT_S`` (48 h) after its
  ``created_at`` (its first attempt: ``kick`` runs right after the enqueue)
  ends ``failed`` / ``rate_limited``; a job with attachments ends so already
  when the quota would free up only after ``BREVO_ATTACHMENT_RETENTION_S``,
  since it could not be downloaded any more.
* Other temporary failures never end ``done``: ``fetch_failed``
  (Brevo 5xx / 429 / auth / timeout), ``store_failed`` (Drive) and
  ``no_api_key`` (``BREVO_API_KEY`` unset) put the job back to ``pending``
  with ``next_attempt_at`` = now + ``RETRY_BASE_MIN * 2**(attempts-1)``
  minutes; files filed before the failure stay in the progress, no receipt is
  sent yet. The last attempt reports such a failure in the receipt and ends
  ``failed`` / ``<code>``. ``done`` = success, or a deliberate permanent drop
  (unknown sender, failed auth, loop mail, disallowed type, duplicate).
* A job with attachments older than ``BREVO_ATTACHMENT_RETENTION_S`` ends
  ``failed`` / ``download_expired`` without any download, and no receipt is
  sent (there is no content to report on). It shows in the admin panel
  ``GET /api/inbound-unmatched`` (``jobs.failed_reasons``).
* Progress is durable: after each filed file its sha256 and download token go
  into the encrypted payload (``inbound_email.JobProgress``) before the next
  file; a retry skips them (counted as accepted, not downloaded or filed
  again) and does not send a second receipt. The window between Drive
  confirming a write and that progress write is closed on every retried job
  (a job claimed a second time, from ``pending`` or a stale ``processing``):
  before a file is filed, its md5 is looked up read-only in what is already
  stored for the patient (the md5 of the patient's active documents, the
  ``source_md5`` of the curated mirror rows, and one listing of the curated
  inbox, ``inbox_upload.inbox_md5s``); a hit is recorded as filed and counted
  as accepted, never uploaded again. A failed listing fails the attempt
  (retried later) rather than risk a second copy.
* Runs: right after an enqueue (:func:`kick`), at startup after
  ``STARTUP_GRACE_S`` and every ``WORKER_INTERVAL_MIN`` minutes (the
  ``inbound_queue`` job in the one APScheduler, under ``_sync_semaphore`` and
  ``local_only_task``). All DB calls are on the event loop; only the Drive
  writes and the SMTP receipt inside the core use worker threads.

Sender verification by DKIM (#950, the "claimed" bucket)
-------------------------------------------------------
Production finding: Brevo's payload carries no ``Authentication-Results``
header, so the verdict is our own and DKIM is its only source: the raw
message (Brevo ``EMLDownloadToken``) is downloaded and its DKIM signatures
verified (``inbound_dkim``). That costs a download and DNS lookups, so it runs
here in the WORKER, never in the webhook request, which still answers at once.

At enqueue the verdict is therefore unknown. A mail goes:

* straight into ``inbound_jobs`` as untrusted (verdict fail), exactly as
  before, when its From does not match the address's caregiver (it is never
  filed whatever its DKIM says, so nothing is downloaded for it), when it is
  loop mail, or when none of the payload's ``DKIM-Signature`` headers has a
  ``d=`` aligned with the From domain (``dkim_none`` / ``dkim_not_aligned`` at
  once; a payload header can only REFUSE a mail, never pass one);
* otherwise (a From that CLAIMS to be the caregiver, or a welcome@ mail) into
  the separate ``claimed`` bucket, table ``inbound_claimed_jobs`` (migration
  099), with its own small caps over open rows: ``MAX_CLAIMED_JOBS_PER_TOKEN``
  per address, ``MAX_CLAIMED_JOBS_TOTAL`` for all address buckets,
  ``MAX_CLAIMED_WELCOME_JOBS`` for welcome@ (a separate pool: a welcome@
  flood never uses the patients' slots), ``MAX_CLAIMED_PER_SENDER`` per
  bucket and canonical From (forgeries of the caregiver's From to welcome@
  never touch the caregiver's slots at the patient address) and
  ``MAX_CLAIMED_PER_DOMAIN`` per From organisational domain within an address
  bucket (never in the welcome bucket: every welcome@ sender is a Gmail user).
  A mail over an open cap is stored ``deferred`` (no download, no DNS;
  counters ``jobs_claimed_deferred_<cap>``) and promoted to ``pending``,
  oldest first, as soon as a finished row frees a slot (``claimed_deferred_
  promoted``); a deferred row older than ``BREVO_ATTACHMENT_RETENTION_S``
  ends ``failed`` / ``download_expired``. Only beyond the deferred bounds
  (``MAX_DEFERRED_PER_TOKEN``, ``MAX_DEFERRED_PER_SENDER``,
  ``MAX_DEFERRED_TOTAL`` for the address buckets, ``MAX_DEFERRED_WELCOME``
  for welcome@, again separate pools): a welcome@ mail is then dropped and
  counted (``jobs_claimed_dropped_<cap>``, the batch still gets 202), while
  a mail to a patient address makes the batch answer 503 + Retry-After
  (``jobs_claimed_full``; nothing written, Brevo retries, rows it already
  holds are no-ops on the retry), so a patient's real mail is never dropped.
  A real mail that arrives during a forged flood waits instead. The
  dedupe key includes a digest of the delivery's Brevo ``Uuid`` and download
  tokens, so a forged or refused copy with the same Message-ID and From never
  suppresses a later real delivery.

The worker (:func:`run_pending`) first verifies claimed rows, one at a time,
under the same lease rules as the jobs (conditional claim, lease refreshed
before the download, every write conditional on it). The verification itself
(EML download + DNS, network only) runs OUTSIDE ``PROCESS_LOCK``; the lock is
held only for the claim and for the promotion and its DB writes, so a slow
DNS server never stalls the filing of other mail. All DB calls stay on the
event loop. A verified row is promoted: it is queued into ``inbound_jobs``
through :func:`enqueue` with the real verdict, where the core's own
``sender_refusal`` marks it trusted (the trusted caps apply from there on)
and, for welcome@, ``inbound_welcome.admit`` runs only now, so a forged mail
never creates a trial patient nor gets a reply. A refused verdict (see
``inbound_dkim.DKIM_REFUSAL_REASONS``) is queued as untrusted (listed in the
admin panel, counted in ``auth_refusals``); both end the claimed row ``done``
with the code. Temporary failures (EML 5xx / 429 / timeout ``fetch_failed``,
DNS ``dns_failed``, ``no_api_key``, a full trusted queue ``queue_full``) go
back to ``pending`` with the job backoff; after ``MAX_ATTEMPTS`` the row ends
``failed``; a welcome@ row gets ``WELCOME_DNS_RETRIES`` retry after
``dns_failed``, then ``failed``. A forged flood claiming the caregiver's From
can therefore only fill the small claimed bucket, and each forged row leaves
it after one quick verification.

Why a separate table and not a third ``trusted`` value in ``inbound_jobs``: a
welcome@ mail has no patient until ``admit`` creates the trial, and
``inbound_jobs.patient_id`` is NOT NULL with an FK; creating the trial before
the verification would let a forged mail create trial patients (and their
by-sending consent rows) and use up ``MAX_NEW_TRIALS_PER_DAY``. One claimed
table for both kinds keeps one verification path, and ``inbound_jobs``
keeps its meaning (every row there has its final verdict).

Retention: nothing here deletes a row (#734). TODO: removing ``done`` rows
older than 30 days needs a RUNBOOK lifecycle schedule and a reviewed
``tests/test_no_automatic_deletion.py`` allowlist entry (maintainer decision).

Logs: counts, reason codes and ``patient_id[:4]`` only.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from oncofiles import inbound_email, inbound_welcome
from oncofiles.database._inbound_jobs import InboundQueueFullError
from oncofiles.email_identity import canonical_email

__all__ = ["InboundQueueFullError", "enqueue", "kick", "run_pending"]

logger = logging.getLogger(__name__)

STALE_PROCESSING_MIN = 10
MAX_ATTEMPTS = 5
WORKER_INTERVAL_MIN = 2
MAX_JOBS_PER_RUN = 50
# After a failed attempt: 2, 4, 8, 16 minutes before the next one, so a short
# Drive or Brevo outage does not use up MAX_ATTEMPTS in one run.
RETRY_BASE_MIN = 2
# Enqueue caps (#952 infra + security review), over open jobs (pending +
# processing), counted per trust class. Trusted = caregiver From + auth pass
# (``inbound_email.sender_refusal``). Over a trusted cap the Brevo route
# answers 503 + Retry-After and writes nothing (Brevo retries later); only a
# counter moves.
MAX_OPEN_JOBS_PER_TOKEN = 50
MAX_OPEN_JOBS_TOTAL = 500
# Untrusted mail (anyone else, or a failed verdict) has its own small caps and
# is DROPPED over them (counted, nothing written, still 202): a stranger who
# knows the address can never use up the caregiver's slots nor make the
# address answer 503.
MAX_UNTRUSTED_JOBS_PER_TOKEN = 10
MAX_UNTRUSTED_JOBS_TOTAL = 100
# The claimed bucket (#950 DKIM mode): mail that claims the caregiver's From
# (or a welcome@ mail) waits here for its DKIM verification by the worker.
# Small on purpose: a forged flood fills only these open slots; a mail over
# them is deferred (below), never a 503 for the whole batch (infra review).
MAX_CLAIMED_JOBS_PER_TOKEN = 5
MAX_CLAIMED_WELCOME_JOBS = 20
MAX_CLAIMED_JOBS_TOTAL = 50  # all ADDRESS buckets; welcome@ has its own pool
# Per (bucket, canonical From): forgeries carrying the caregiver's From sent to
# welcome@ never use the slots of the caregiver's mail to the patient address.
MAX_CLAIMED_PER_SENDER = 2
# Per From organisational domain within one ADDRESS bucket. Not applied to the
# welcome bucket (maintainer decision on #966): every welcome@ sender is a
# Gmail user, so it would cap all of gmail.com; there the per-sender cap and
# MAX_CLAIMED_WELCOME_JOBS apply, and slow DNS stays bounded by the DNS budget
# and WELCOME_DNS_RETRIES (gmail.com keys resolve fast).
MAX_CLAIMED_PER_DOMAIN = 2
# Over an open cap a mail is stored ``deferred`` (security review of #966: no
# download, no DNS) and promoted to pending when a slot frees up; only beyond
# these bounds is it dropped and counted. Per canonical sender; per address
# token and for all address buckets together; welcome@ is a separate pool
# (security review of #966: a welcome@ flood from many Gmail senders never
# uses up the deferred slots of the patients' addresses).
MAX_DEFERRED_PER_TOKEN = 10
MAX_DEFERRED_PER_SENDER = 10
MAX_DEFERRED_TOTAL = 50
MAX_DEFERRED_WELCOME = 30
# An address mail beyond its deferred bounds always makes the batch answer
# 503 (fail closed; the rest of the batch is committed first). More than this
# many such refusals for one token within the window raise an admin alert.
REFUSAL_ALERT_PER_TOKEN = 3
REFUSAL_ALERT_WINDOW_S = 3600
# A welcome@ row whose key lookup failed (dns_failed) is retried this often,
# then ends failed (infra review: a slow DNS server must not hold the bucket).
WELCOME_DNS_RETRIES = 1
_CLAIMED_SCOPE = "claimed"  # AES-GCM associated data: claimed|bucket|job_uuid
# How long Brevo keeps an inbound attachment downloadable is NOT documented
# (checked 2026-11-21): neither the inbound parsing guide
# https://developers.brevo.com/docs/inbound-parse-webhooks nor the download
# endpoint https://developers.brevo.com/reference/get-inbound-email-attachment
# (which documents only "404 Download token not found") states a retention or
# expiry; the inbound events endpoint
# https://developers.brevo.com/reference/get-inbound-email-events only limits
# its query window to 30 days. Conservative constant: a job whose attachments
# have waited longer than 24 hours is not attempted (``download_expired``).
BREVO_ATTACHMENT_RETENTION_S = 24 * 3600
# A job deferred by the filing quota (``rate_limited``) waits for the quota
# window without using attempts; after this long from its ``created_at`` it
# ends ``failed`` / ``rate_limited`` so it can never loop forever.
RATE_LIMIT_MAX_WAIT_S = 48 * 3600

PAYLOAD_VERSION = 1
_ENC_PREFIX = "v1:"


class UndecryptablePayloadError(ValueError):
    """The payload does not decrypt with the current key (key rotated?)."""


class LeaseLostError(RuntimeError):
    """Another worker took the job over (its lease ``updated_at`` changed)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# ── encryption ───────────────────────────────────────────────────────────


def _aad(patient_id: str, token_hash: str, job_uuid: str) -> bytes:
    """Associated data: the row's patient, address token hash and random job
    uuid (#952 infra review), so a payload copied onto any other row (even of
    the same patient and address) does not decrypt."""
    if not job_uuid:
        raise ValueError("job_uuid")
    return f"{patient_id}|{token_hash}|{job_uuid}".encode()


def encrypt_payload(payload: dict, *, patient_id: str, token_hash: str, job_uuid: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import inbound_jobs_key

    nonce = os.urandom(12)
    plain = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ct = AESGCM(inbound_jobs_key()).encrypt(nonce, plain, _aad(patient_id, token_hash, job_uuid))
    return _ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_payload(blob: str, *, patient_id: str, token_hash: str, job_uuid: str) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import inbound_jobs_key

    try:
        if not blob.startswith(_ENC_PREFIX):
            raise ValueError("prefix")
        raw = base64.b64decode(blob[len(_ENC_PREFIX) :], validate=True)
        aad = _aad(patient_id, token_hash, job_uuid)
        plain = AESGCM(inbound_jobs_key()).decrypt(raw[:12], raw[12:], aad)
        payload = json.loads(plain)
    except (InvalidTag, ValueError) as exc:
        raise UndecryptablePayloadError(type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
        raise UndecryptablePayloadError("version")
    return payload


def payload_digest(payload: dict) -> str:
    """sha256 of the payload's canonical JSON without its progress."""
    body = {k: v for k, v in payload.items() if k != "progress"}
    return hashlib.sha256(
        json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()


def message_id_hash(message_id: str, payload: dict | None = None) -> str:
    """Keyed hash of the Message-ID; never None (#952 infra review).

    A mail without a Message-ID gets a keyed hash of its payload digest
    (``payload_digest``: download tokens, sender, headers, names) under its
    own domain prefix: NULL is distinct in a UNIQUE key, so a provider retry
    of such a mail would otherwise be queued twice.
    """
    from oncofiles.secrets_keys import inbound_message_id_hash_key

    mid = (message_id or "").strip()
    if mid:
        value = b"mid|" + mid.encode()
    else:
        if payload is None:
            raise ValueError("payload")
        value = b"digest|" + payload_digest(payload).encode()
    return hmac.new(inbound_message_id_hash_key(), value, hashlib.sha256).hexdigest()


def sender_hash(sender: str, auth_pass: bool) -> str:
    """Keyed hash of the lowercased From address + the auth verdict (dedupe key)."""
    from oncofiles.secrets_keys import inbound_sender_hash_key

    value = f"{(sender or '').strip().lower()}|{'pass' if auth_pass else 'fail'}"
    return hmac.new(inbound_sender_hash_key(), value.encode(), hashlib.sha256).hexdigest()


def domain_hash(sender: str) -> str:
    """Keyed hash of the From's organisational domain (claimed-bucket cap)."""
    from oncofiles import inbound_dkim
    from oncofiles.secrets_keys import inbound_sender_hash_key

    domain = (sender or "").rpartition("@")[2].strip().rstrip(".").lower()
    org = inbound_dkim.org_domain(domain) or domain
    return hmac.new(inbound_sender_hash_key(), f"domain|{org}".encode(), hashlib.sha256).hexdigest()


def delivery_hash(msg: inbound_email.InboundMessage) -> str:
    """Keyed hash of one Brevo delivery: its ``Uuid`` and download tokens (EML
    and attachments). A Brevo retry of that delivery hashes the same; another
    delivery of a mail with the same Message-ID and From does not (security
    review of #966)."""
    from oncofiles.secrets_keys import inbound_message_id_hash_key

    parts = [msg.delivery_id, msg.eml_ref, *sorted(a.ref for a in msg.attachments if a.ref)]
    value = "delivery|" + "\n".join(parts)
    return hmac.new(inbound_message_id_hash_key(), value.encode(), hashlib.sha256).hexdigest()


def recipient_matcher(payload: dict):
    """``recipient_ok(address)`` for the DKIM replay guard: the address token of
    the job, or a welcome@ address for a welcome@ row."""
    token = payload.get("token") or ""
    if token:
        return lambda address: inbound_email.token_from_address(address) == token
    return inbound_welcome.is_welcome_address


def configured() -> bool:
    """True when the mandatory ``INBOUND_JOBS_SECRET`` is set (else fail closed)."""
    from oncofiles.secrets_keys import inbound_jobs_secret_set

    return inbound_jobs_secret_set()


_warned_unconfigured = False


def _warn_unconfigured_once() -> None:
    global _warned_unconfigured
    if not _warned_unconfigured:
        _warned_unconfigured = True
        logger.warning(
            "inbound: INBOUND_JOBS_SECRET is not set; the queue is closed "
            "(Brevo route answers 503, queued jobs are left untouched)"
        )


# ── payload <-> message ──────────────────────────────────────────────────


def to_payload(msg: inbound_email.InboundMessage, token: str) -> dict:
    """The minimal plaintext of a job (encrypted before it is stored).

    At most ``MAX_FILES_PER_MAIL`` attachment references are kept (#952 infra
    review: a bounded payload); the rest are counted as ``too_many_files``, so
    the receipt still reports them.
    """
    refs = [a for a in msg.attachments if a.ref]
    skips = Counter(msg.parse_skips)
    if len(refs) > inbound_email.MAX_FILES_PER_MAIL:
        skips["too_many_files"] += len(refs) - inbound_email.MAX_FILES_PER_MAIL
        refs = refs[: inbound_email.MAX_FILES_PER_MAIL]
    return {
        "v": PAYLOAD_VERSION,
        "token": token,
        "sender": msg.sender,
        "auth_pass": bool(msg.auth_pass),
        "message_id": msg.message_id,
        "auto_submitted": msg.auto_submitted,
        "precedence": msg.precedence,
        "list_id": msg.list_id,
        "return_path": msg.return_path,
        "is_report": bool(msg.is_report),
        "parse_skips": dict(skips),
        "attachments": [
            {
                "ref": a.ref,
                "name": a.name,
                "content_type": a.content_type,
                "declared_size": a.declared_size,
                "inline": bool(a.inline),
            }
            for a in refs
        ],
        "progress": {"filed": [], "receipt_sent": False, "admitted": False},
        # #950 phase 2: admitted through welcome@ (reply = the welcome text)
        "welcome": bool(msg.welcome),
        # #950 DKIM: only meaningful on a claimed row (verdict still open)
        "auth_mode": msg.auth_mode,
        "eml_ref": msg.eml_ref,
        "dkim_prefilter": msg.dkim_prefilter,
        "delivery_id": msg.delivery_id,
    }


def from_payload(payload: dict) -> inbound_email.InboundMessage:
    """Rebuild the message; download callables are made now, with the current key."""
    from oncofiles.dashboard_upload import MAX_FILE_BYTES

    api_key = os.environ.get(inbound_email.BREVO_API_KEY_ENV, "")
    skips = Counter({str(k): int(v) for k, v in (payload.get("parse_skips") or {}).items()})
    attachments = []
    for a in payload.get("attachments") or []:
        ref = str(a.get("ref") or "")
        if not ref:
            skips["fetch_failed"] += 1
            continue
        # BREVO_API_KEY unset: the download raises a temporary failure
        # (``no_api_key``), so the job waits for the key instead of ending done.
        fetch = (
            inbound_email._brevo_fetcher(ref, api_key, MAX_FILE_BYTES + 1)
            if api_key
            else inbound_email._missing_key_fetcher()
        )
        attachments.append(
            inbound_email.InboundAttachment(
                name=str(a.get("name") or ""),
                data=b"",
                content_type=str(a.get("content_type") or "application/octet-stream"),
                inline=bool(a.get("inline")),
                fetch=fetch,
                declared_size=a.get("declared_size"),
                ref=ref,
            )
        )
    # A token-only address routes exactly like the full one (the prefix is
    # never checked), so the Gmail prefix is never stored. A claimed welcome@
    # row has no token yet (#950 DKIM mode): it routes to the welcome address.
    if payload.get("token"):
        recipient = f"{payload['token']}@{inbound_email.INBOUND_DOMAIN}"
    else:
        recipient = sorted(inbound_welcome.WELCOME_ADDRESSES)[0]
    return inbound_email.InboundMessage(
        recipients=[recipient],
        sender=str(payload.get("sender") or ""),
        auth_pass=bool(payload.get("auth_pass")),
        message_id=str(payload.get("message_id") or ""),
        auto_submitted=str(payload.get("auto_submitted") or ""),
        precedence=str(payload.get("precedence") or ""),
        list_id=str(payload.get("list_id") or ""),
        return_path=payload.get("return_path"),
        is_report=bool(payload.get("is_report")),
        attachments=attachments,
        parse_skips=skips,
        welcome=bool(payload.get("welcome")),
        auth_mode=str(payload.get("auth_mode") or ""),
        eml_ref=str(payload.get("eml_ref") or ""),
        dkim_prefilter=str(payload.get("dkim_prefilter") or ""),
        delivery_id=str(payload.get("delivery_id") or ""),
    )


# ── enqueue ──────────────────────────────────────────────────────────────


async def _claimed_bucket(db, msg: inbound_email.InboundMessage) -> str | None:
    """The claimed bucket of a DKIM-mode mail whose verdict is still open
    (the address token hash, or ``welcome``), or None: queued straight away as
    untrusted (#950; see the module docstring)."""
    from oncofiles.persistent_oauth import _email_matches_caregiver

    if msg.auth_mode != "dkim" or inbound_email.loop_reason(msg):
        return None
    token = inbound_email._find_token(msg)
    if token is None:
        if not inbound_welcome.is_welcome(msg):
            return None
        bucket = "welcome"
    else:
        th = inbound_email.token_hash(token)
        row = await db.find_inbound_address(th)
        if row is None or not row["active"]:
            return None  # dropped by the normal path
        patient = await db.get_patient(row["patient_id"])
        caregiver = getattr(patient, "caregiver_email", None) if patient else None
        if patient is None or not _email_matches_caregiver(msg.sender, caregiver):
            return None  # never filed whatever its DKIM says
        bucket = th
    if msg.dkim_prefilter:
        # A payload header can only refuse: no DKIM-Signature at all, or none
        # with a d= aligned with the From domain. No download.
        inbound_email.note_auth_refusal(msg.dkim_prefilter)
        return None
    return bucket


async def _enqueue_claimed(
    db, claimed: list[tuple[str, inbound_email.InboundMessage]]
) -> tuple[int, list[str]]:
    """Write claimed mail in ONE transaction (committed); returns (inserted,
    over-bound address buckets). A welcome@ mail beyond its bounds is dropped
    and counted (``jobs_claimed_dropped``, ``jobs_claimed_dropped_<cap>``);
    an address mail beyond them is not written, and once the rest of the
    batch is committed the batch answers 503 (:func:`_refuse_over_bound`)."""
    rows = []
    for bucket, msg in claimed:
        payload = to_payload(msg, inbound_email._find_token(msg) or "")
        job_uuid = str(uuid.uuid4())
        rows.append(
            {
                "job_uuid": job_uuid,
                "bucket": bucket,
                "message_id_hash": message_id_hash(msg.message_id, payload),
                # canonical sender (Gmail dots / +tag folded): the per-sender cap
                # counts the variants of one mailbox as one sender
                "sender_hash": sender_hash(canonical_email(msg.sender), False),
                "domain_hash": domain_hash(msg.sender),
                "delivery_hash": delivery_hash(msg),
                "payload_enc": encrypt_payload(
                    payload, patient_id=_CLAIMED_SCOPE, token_hash=bucket, job_uuid=job_uuid
                ),
            }
        )
    result = await db.insert_inbound_claimed_jobs(rows, now=_iso(_now()), caps=_claimed_caps())
    counters = inbound_email._state.counters
    counters["jobs_claimed"] += result["inserted"]
    counters["jobs_claimed_duplicate"] += result["duplicate"]
    for cap, n in result["deferred"].items():
        counters["jobs_claimed_deferred"] += n
        counters[f"jobs_claimed_deferred_{cap}"] += n
    for cap, n in result["dropped"].items():
        counters["jobs_claimed_dropped"] += n
        counters[f"jobs_claimed_dropped_{cap}"] += n
    if result["deferred"] or result["dropped"]:
        logger.warning(
            "inbound: claimed bucket over its caps, deferred %s dropped %s",
            dict(sorted(result["deferred"].items())),
            dict(sorted(result["dropped"].items())),
        )
    return result["inserted"], list(result["over_bound"])


def _refuse_over_bound(buckets: list[str]) -> None:
    """Count the batch-503 for over-bound address rows and raise the alert.

    Over-bound address rows ALWAYS make the batch answer 503 + Retry-After
    (fail closed: a patient's possibly real mail is never discarded). Every
    other row of the batch is already committed, so other patients are never
    held back, and Brevo's retry finds those rows as no-ops through the
    delivery hash. Per address token the refusals within
    ``REFUSAL_ALERT_WINDOW_S`` are counted in memory; above
    ``REFUSAL_ALERT_PER_TOKEN`` each further refusal raises the alert code
    ``claimed_address_refusals_high`` (admin inbound panel and operator
    ``/readiness``; counts only, never an address).
    """
    now = time.time()
    book = inbound_email._state.claimed_503
    counters = inbound_email._state.counters
    counters["jobs_claimed_full"] += 1
    for th in sorted(set(buckets)):
        times = [t for t in book.get(th, []) if now - t < REFUSAL_ALERT_WINDOW_S]
        times.append(now)
        book[th] = times
        if len(times) > REFUSAL_ALERT_PER_TOKEN:
            inbound_email.note_alert("claimed_address_refusals_high")
            logger.warning(
                "inbound: claimed address bucket refused %d times within the hour "
                "alert=claimed_address_refusals_high",
                len(times),
            )
    logger.warning("inbound: claimed address bucket full, batch answers 503 (rest committed)")


def _claimed_caps() -> dict[str, int]:
    """The open and deferred caps of the claimed bucket (read at call time)."""
    return {
        "address": MAX_CLAIMED_JOBS_PER_TOKEN,
        "welcome": MAX_CLAIMED_WELCOME_JOBS,
        "total": MAX_CLAIMED_JOBS_TOTAL,
        "sender": MAX_CLAIMED_PER_SENDER,
        "domain": MAX_CLAIMED_PER_DOMAIN,
        "deferred_bucket": MAX_DEFERRED_PER_TOKEN,
        "deferred_sender": MAX_DEFERRED_PER_SENDER,
        "deferred_total": MAX_DEFERRED_TOTAL,
        "deferred_welcome": MAX_DEFERRED_WELCOME,
    }


async def enqueue(
    db, messages: list[inbound_email.InboundMessage], *, verified: bool = False
) -> dict:
    """Queue every routable message in ONE transaction; raises on a DB error.

    Returns counts: ``queued``, ``duplicate`` (already queued, a provider
    retry), ``dropped`` (unknown / inactive token, silent as before),
    ``flood_dropped`` (untrusted mail over its own cap, counter
    ``jobs_untrusted_dropped``). Each job is marked ``trusted`` with the
    core's own sender rule (``inbound_email.sender_refusal``). Raises
    ``InboundQueueFullError`` (nothing written, counter ``jobs_queue_full``)
    when trusted mail would pass ``MAX_OPEN_JOBS_PER_TOKEN`` or
    ``MAX_OPEN_JOBS_TOTAL``; untrusted mail never causes it.

    #950 DKIM mode: a mail whose verdict is still open and that claims the
    caregiver's From (or goes to welcome@) is written to the claimed bucket
    instead (``claimed`` count; over its caps that mail is dropped and
    counted); the worker verifies it and calls this again with
    ``verified=True`` and the real verdict.
    """
    rows = []
    dropped = 0
    claimed: list[tuple[str, inbound_email.InboundMessage]] = []
    direct: list[inbound_email.InboundMessage] = []
    for msg in messages:
        bucket = None if verified else await _claimed_bucket(db, msg)
        if bucket is None:
            msg.auth_mode = ""  # the verdict is final: pass or fail
            direct.append(msg)
        else:
            claimed.append((bucket, msg))
    # The claimed rows within bounds are committed first; over-bound address
    # rows are decided at the end, after the rest of the batch is committed.
    n_claimed, over_bound = await _enqueue_claimed(db, claimed) if claimed else (0, [])
    for msg in direct:
        token = inbound_email._find_token(msg)
        if token is None and inbound_welcome.is_welcome(msg):
            # #950 phase 2: welcome@ is admitted here (auth, Gmail, caps, the
            # onboarding gate, the trial patient) and queued for the trial
            # patient's own address token; refused mail is never queued.
            admitted = await inbound_welcome.admit(db, msg)
            if admitted is None:
                dropped += 1
                continue
            token = admitted[1]
            msg.welcome = admitted[2]  # False: handed to an existing caregiver's patient
        th = inbound_email.token_hash(token) if token else None
        row = await db.find_inbound_address(th) if th else None
        if row is None or not row["active"]:
            reason = "unknown_token" if row is None else "inactive_token"
            inbound_email._state.counters[reason] += 1
            dropped += 1
            continue
        pid = row["patient_id"]
        trusted = await inbound_email.sender_refusal(db, pid, msg) is None
        job_uuid = str(uuid.uuid4())
        payload = to_payload(msg, token)
        rows.append(
            {
                "job_uuid": job_uuid,
                "patient_id": pid,
                "token_hash": th,
                "message_id_hash": message_id_hash(msg.message_id, payload),
                "sender_hash": sender_hash(msg.sender, msg.auth_pass),
                "trusted": trusted,
                "payload_enc": encrypt_payload(
                    payload, patient_id=pid, token_hash=th, job_uuid=job_uuid
                ),
            }
        )
    try:
        queued, duplicate, flood_dropped = (
            await db.insert_inbound_jobs(
                rows,
                now=_iso(_now()),
                caps={
                    True: (MAX_OPEN_JOBS_PER_TOKEN, MAX_OPEN_JOBS_TOTAL),
                    False: (MAX_UNTRUSTED_JOBS_PER_TOKEN, MAX_UNTRUSTED_JOBS_TOTAL),
                },
            )
            if rows
            else (0, 0, 0)
        )
    except InboundQueueFullError as exc:
        inbound_email._state.counters["jobs_queue_full"] += 1
        logger.warning("inbound: queue full (%s cap), nothing queued", exc)
        raise
    inbound_email._state.counters["jobs_queued"] += queued
    inbound_email._state.counters["jobs_duplicate"] += duplicate
    inbound_email._state.counters["jobs_untrusted_dropped"] += flood_dropped
    logger.info(
        "inbound: queued=%d duplicate=%d dropped=%d flood_dropped=%d claimed=%d patients=%s",
        queued,
        duplicate,
        dropped,
        flood_dropped,
        n_claimed,
        ",".join(sorted({r["patient_id"][:4] for r in rows})) or "-",
    )
    if over_bound:
        _refuse_over_bound(over_bound)
        raise InboundQueueFullError("claimed")
    return {
        "queued": queued,
        "duplicate": duplicate,
        "dropped": dropped,
        "flood_dropped": flood_dropped,
        "claimed": n_claimed,
    }


# ── worker ───────────────────────────────────────────────────────────────

_drain_task: asyncio.Task | None = None
_rekick = False


def kick(db) -> None:
    """Start draining the queue in the background (one drain at a time).

    A kick while a drain is running makes it look again before it ends, so a
    job queued during its last claim is not left for the periodic run.
    """
    global _drain_task, _rekick
    if (
        _drain_task is not None
        and not _drain_task.done()
        and _drain_task.get_loop() is asyncio.get_running_loop()
    ):
        _rekick = True
        return
    _drain_task = asyncio.create_task(_drain(db))
    inbound_email._BACKGROUND.add(_drain_task)
    _drain_task.add_done_callback(inbound_email._BACKGROUND.discard)


async def _drain(db) -> None:
    global _rekick
    try:
        while True:
            _rekick = False
            await run_pending(db)
            if not _rekick:
                return
    except Exception as exc:  # noqa: BLE001 - a background task never raises
        logger.error("inbound: queue drain failed (%s)", type(exc).__name__)


async def run_pending(db, *, limit: int = MAX_JOBS_PER_RUN) -> Counter:
    """Process claimable jobs one at a time; returns outcome counts.

    Without ``INBOUND_JOBS_SECRET`` nothing is touched (one warning per process).
    """
    counts: Counter = Counter()
    if not configured():
        _warn_unconfigured_once()
        return counts
    # #950: verify the claimed bucket first, so a verified mail is processed
    # in this same run. ``_verify_one`` takes PROCESS_LOCK itself, only around
    # its DB steps: the EML download and DNS lookups run outside it.
    for _ in range(limit):
        outcome = await _verify_one(db)
        if outcome is None:
            break
        counts[f"claimed:{outcome}"] += 1
    for _ in range(limit):
        async with inbound_email.PROCESS_LOCK:
            outcome = await _run_one(db)
        if outcome is None:
            break
        counts[outcome] += 1
    if counts:
        logger.info("inbound: queue run %s", dict(sorted(counts.items())))
        # #950 phase 3: a trial's first batch may be complete now that its mail
        # is filed (the reader may have finished before the job ended)
        from oncofiles import trial_funnel

        try:
            await trial_funnel.send_due_ready_mails(db)
        except Exception as exc:  # noqa: BLE001 - mail never stops the queue
            logger.warning("inbound: ready mails failed (%s)", type(exc).__name__)
    return counts


async def _verify_one(db) -> str | None:
    """Claim, DKIM-verify and promote the next claimed row. None = no row.

    Three steps (infra review of #966): (1) under ``PROCESS_LOCK`` the claim,
    the payload checks and a lease refresh; (2) WITHOUT the lock the
    verification (EML download + DNS: network only, no DB call); (3) under the
    lock again the promotion (``enqueue(verified=True)``) and the finish, both
    conditional on the lease. See the module docstring.
    """
    from oncofiles import inbound_dkim

    lease = {"value": ""}
    ctx: dict = {}

    def lease_lost() -> str:
        inbound_email._state.counters["jobs_lease_lost"] += 1
        logger.warning("inbound: claimed job lease lost, left to its new owner")
        return "lease_lost"

    async def finish(state: str, reason: str, *, retry_in: timedelta | None = None) -> str:
        at = _now()
        ok = await db.finish_inbound_claimed_job(
            ctx["id"],
            state=state,
            reason=reason,
            now=_iso(at),
            lease=lease["value"],
            next_attempt_at=_iso(at + retry_in) if retry_in is not None else None,
        )
        if not ok:
            return lease_lost()
        inbound_email._state.counters[f"claimed_{state}"] += 1
        logger.info("inbound: claimed job state=%s reason=%s", state, reason)
        return f"{state}:{reason}"

    async def retry_or_fail(code: str) -> str:
        """A temporary failure: back to ``pending`` with the job backoff, or
        ``failed:<code>`` once the attempts are used up (a welcome@ row after
        ``WELCOME_DNS_RETRIES`` retries of ``dns_failed``)."""
        attempts = ctx["attempts"]
        welcome_dns = ctx["bucket"] == "welcome" and code == "dns_failed"
        if attempts >= MAX_ATTEMPTS or (welcome_dns and attempts > WELCOME_DNS_RETRIES):
            return await finish("failed", code)
        return await finish("pending", code, retry_in=_backoff(attempts))

    # (1) claim, under the lock
    async with inbound_email.PROCESS_LOCK:
        now = _now()
        # deferred rows take the slots finished rows freed (oldest first);
        # a deferred row older than the attachment retention ends failed
        moved = await db.promote_deferred_claimed(
            caps=_claimed_caps(),
            now=_iso(now),
            expired_before=_iso(now - timedelta(seconds=BREVO_ATTACHMENT_RETENTION_S)),
        )
        for key, n in moved.items():
            inbound_email._state.counters[f"claimed_deferred_{key}"] += n
        job = await db.next_inbound_claimed_job(
            now=_iso(now), stale_before=_iso(now - timedelta(minutes=STALE_PROCESSING_MIN))
        )
        if job is None:
            return None
        lease["value"] = _iso(now)
        if not await db.claim_inbound_claimed_job(job, now=lease["value"]):
            return "lost_claim"
        ctx.update(id=job["id"], bucket=job["bucket"], attempts=job["attempts"] + 1)
        if job["attempts"] >= MAX_ATTEMPTS:
            return await finish("failed", "max_attempts")
        try:
            payload = decrypt_payload(
                job["payload_enc"],
                patient_id=_CLAIMED_SCOPE,
                token_hash=job["bucket"],
                job_uuid=job["job_uuid"],
            )
        except UndecryptablePayloadError:
            return await finish("failed", "undecryptable")
        if (now - _parse_iso(job["created_at"])).total_seconds() > BREVO_ATTACHMENT_RETENTION_S:
            return await finish("failed", "download_expired")
        msg = from_payload(payload)
        new = _iso(_now())
        if not await db.touch_inbound_claimed_job(ctx["id"], lease=lease["value"], now=new):
            return lease_lost()
        lease["value"] = new

    # (2) verify, WITHOUT the lock (network only)
    api_key = os.environ.get(inbound_email.BREVO_API_KEY_ENV, "")
    temporary: str | None = None
    verdict = None
    try:
        verdict = await inbound_dkim.verify_message(
            msg.eml_ref, msg.sender, api_key, recipient_matcher(payload)
        )
    except inbound_dkim.TemporaryVerifyError as exc:
        temporary = exc.code
    except Exception as exc:  # noqa: BLE001 - one job never stops the queue
        temporary = f"error:{type(exc).__name__}"[:64]

    # (3) promote and finish, under the lock
    async with inbound_email.PROCESS_LOCK:
        if temporary is not None:
            return await retry_or_fail(temporary)
        # infra review of #966: the verification ran without the lock; make
        # sure this worker still holds the row before promoting it
        new = _iso(_now())
        if not await db.touch_inbound_claimed_job(ctx["id"], lease=lease["value"], now=new):
            return lease_lost()
        lease["value"] = new
        msg.auth_mode = ""
        msg.auth_pass = verdict.ok
        reason = "verified" if verdict.ok else (verdict.reason or "dkim_invalid")
        try:
            await enqueue(db, [msg], verified=True)
        except InboundQueueFullError:
            return await retry_or_fail("queue_full")
        except Exception as exc:  # noqa: BLE001
            return await retry_or_fail(f"error:{type(exc).__name__}"[:64])
        if not verdict.ok:
            inbound_email.note_auth_refusal(reason)
        return await finish("done", reason)


def _backoff(attempts: int) -> timedelta:
    """After the n-th failed attempt: ``RETRY_BASE_MIN * 2**(n-1)`` minutes."""
    return timedelta(minutes=RETRY_BASE_MIN * 2 ** max(attempts - 1, 0))


async def _run_one(db) -> str | None:
    """Claim and process the next job (caller holds PROCESS_LOCK). None = no job.

    Every write after the claim is conditional on this worker's lease (the
    ``updated_at`` it set; refreshed by each progress save and before each
    download): a worker whose job was taken over as stale changes nothing.
    """
    now = _now()
    job = await db.next_inbound_job(
        now=_iso(now), stale_before=_iso(now - timedelta(minutes=STALE_PROCESSING_MIN))
    )
    if job is None:
        return None
    pid, jid = job["patient_id"], job["id"]
    pp = pid[:4]
    lease = {"value": _iso(now)}
    if not await db.claim_inbound_job(job, now=lease["value"]):
        return "lost_claim"
    attempts = job["attempts"] + 1

    def lease_lost() -> str:
        inbound_email._state.counters["jobs_lease_lost"] += 1
        logger.warning("inbound: job patient %s lease lost, left to its new owner", pp)
        return "lease_lost"

    async def finish(
        state: str, reason: str, *, retry_in: timedelta | None = None, refund: bool = False
    ) -> str:
        at = _now()
        ok = await db.finish_inbound_job(
            jid,
            patient_id=pid,
            state=state,
            reason=reason,
            now=_iso(at),
            lease=lease["value"],
            next_attempt_at=_iso(at + retry_in) if retry_in is not None else None,
            refund_attempt=refund,
        )
        if not ok:
            return lease_lost()
        inbound_email._state.counters[f"jobs_{state}"] += 1
        if state == "failed":
            inbound_email._state.counters[f"job_failed_{reason}"] += 1
        logger.info("inbound: job patient %s state=%s reason=%s", pp, state, reason)
        return f"{state}:{reason}"

    async def retry_or_fail(code: str) -> str:
        """A temporary failure: back to ``pending`` with the backoff, or
        ``failed:<code>`` once the attempts are used up."""
        if attempts >= MAX_ATTEMPTS:
            return await finish("failed", code)
        return await finish("pending", code, retry_in=_backoff(attempts))

    async def wait_for_quota(retry_at: float | None, basis: str) -> str:
        """``rate_limited``: back to ``pending`` until the quota frees up, the
        attempt given back; ``failed:rate_limited`` past the upper bound."""
        if retry_at is None:  # the core always sets it; next full hour otherwise
            retry_at, basis = float((int(time.time()) // 3600 + 1) * 3600), "next_hour"
        # the core's quota clock is ``time.time``; the queue's is ``_now``
        wait = timedelta(seconds=max(retry_at - time.time(), 0.0))
        created = _parse_iso(job["created_at"])
        bound = RATE_LIMIT_MAX_WAIT_S
        if payload.get("attachments"):
            bound = min(bound, BREVO_ATTACHMENT_RETENTION_S)
        if _now() + wait > created + timedelta(seconds=bound):
            return await finish("failed", "rate_limited")
        logger.info(
            "inbound: job patient %s waits for the filing quota basis=%s minutes=%d",
            pp,
            basis,
            int(wait.total_seconds() // 60),
        )
        return await finish("pending", "rate_limited", retry_in=wait, refund=True)

    if job["attempts"] >= MAX_ATTEMPTS:
        return await finish("failed", "max_attempts")
    try:
        payload = decrypt_payload(
            job["payload_enc"],
            patient_id=pid,
            token_hash=job["token_hash"],
            job_uuid=job["job_uuid"],
        )
    except UndecryptablePayloadError:
        return await finish("failed", "undecryptable")
    age = (now - _parse_iso(job["created_at"])).total_seconds()
    if payload.get("attachments") and age > BREVO_ATTACHMENT_RETENTION_S:
        return await finish("failed", "download_expired")

    prog = payload.get("progress") or {}
    progress = inbound_email.JobProgress(
        filed=set(prog.get("filed") or []),
        receipt_sent=bool(prog.get("receipt_sent")),
        admitted=bool(prog.get("admitted")),
        # claimed before (pending after an error, or a stale processing job):
        # a file may already be in Drive without its progress entry
        retry=job["attempts"] > 0,
        final=attempts >= MAX_ATTEMPTS,
    )

    async def save() -> None:
        payload["progress"] = {
            "filed": sorted(progress.filed),
            "receipt_sent": progress.receipt_sent,
            "admitted": progress.admitted,
        }
        new = _iso(_now())
        ok = await db.update_inbound_job_payload(
            jid,
            patient_id=pid,
            payload_enc=encrypt_payload(
                payload, patient_id=pid, token_hash=job["token_hash"], job_uuid=job["job_uuid"]
            ),
            lease=lease["value"],
            now=new,
        )
        if not ok:
            raise LeaseLostError()
        lease["value"] = new

    async def heartbeat() -> None:
        new = _iso(_now())
        if not await db.touch_inbound_job(jid, patient_id=pid, lease=lease["value"], now=new):
            raise LeaseLostError()
        lease["value"] = new

    progress.save = save
    progress.heartbeat = heartbeat
    try:
        result = await inbound_email._process_message(db, from_payload(payload), progress)
    except LeaseLostError:
        return lease_lost()
    except Exception as exc:  # noqa: BLE001 - one job never stops the queue
        if attempts >= MAX_ATTEMPTS:
            return await finish("failed", "max_attempts")
        reason = f"error:{type(exc).__name__}"[:64]
        return await finish("pending", reason, retry_in=_backoff(attempts))
    if result.temporary == "rate_limited":
        # #952 follow-up: nothing was filed or admitted (the quota check runs
        # before any download), so there is no progress to save.
        return await wait_for_quota(result.retry_at, result.retry_basis)
    if result.temporary:
        # #952 infra review: fetch_failed / store_failed / no_api_key never
        # end ``done``. The progress (incl. ``admitted``, so
        # the retry does not count against the filing quota again) is saved
        # first, under the lease.
        if attempts < MAX_ATTEMPTS:
            try:
                await save()
            except LeaseLostError:
                return lease_lost()
        return await retry_or_fail(result.temporary)
    return await finish("done", result.outcome)
