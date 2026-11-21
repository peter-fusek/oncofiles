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
    # never checked), so the Gmail prefix is never stored.
    recipient = f"{payload['token']}@{inbound_email.INBOUND_DOMAIN}"
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
    )


# ── enqueue ──────────────────────────────────────────────────────────────


async def enqueue(db, messages: list[inbound_email.InboundMessage]) -> dict:
    """Queue every routable message in ONE transaction; raises on a DB error.

    Returns counts: ``queued``, ``duplicate`` (already queued, a provider
    retry), ``dropped`` (unknown / inactive token, silent as before),
    ``flood_dropped`` (untrusted mail over its own cap, counter
    ``jobs_untrusted_dropped``). Each job is marked ``trusted`` with the
    core's own sender rule (``inbound_email.sender_refusal``). Raises
    ``InboundQueueFullError`` (nothing written, counter ``jobs_queue_full``)
    when trusted mail would pass ``MAX_OPEN_JOBS_PER_TOKEN`` or
    ``MAX_OPEN_JOBS_TOTAL``; untrusted mail never causes it.
    """
    rows = []
    dropped = 0
    for msg in messages:
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
        "inbound: queued=%d duplicate=%d dropped=%d flood_dropped=%d patients=%s",
        queued,
        duplicate,
        dropped,
        flood_dropped,
        ",".join(sorted({r["patient_id"][:4] for r in rows})) or "-",
    )
    return {
        "queued": queued,
        "duplicate": duplicate,
        "dropped": dropped,
        "flood_dropped": flood_dropped,
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
