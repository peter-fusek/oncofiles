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
a random 96-bit nonce and the row's patient id + token hash as associated data
(a payload copied onto another row does not decrypt). The plaintext is the
minimum the core needs: the address token, the From address and the auth
verdict computed at enqueue, the loop-check header values, the Message-ID,
the adapter's parse skips, and per attachment the Brevo download token, name,
content type, declared size and inline flag; plus the progress below. Never a
subject or a body.

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
* Claimable: ``pending``, or ``processing`` not touched for
  ``STALE_PROCESSING_MIN`` (a crash or kill mid-way). The claim is a
  conditional UPDATE (state and updated_at unchanged), so two containers of a
  rolling deploy sharing the volume never take the same job.
* Each claim counts an attempt. A job that reaches ``MAX_ATTEMPTS`` claims
  without finishing ends ``failed`` / ``max_attempts``.
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
from collections import Counter
from datetime import UTC, datetime, timedelta

from oncofiles import inbound_email

logger = logging.getLogger(__name__)

STALE_PROCESSING_MIN = 10
MAX_ATTEMPTS = 5
WORKER_INTERVAL_MIN = 2
MAX_JOBS_PER_RUN = 50
# After a failed attempt: 2, 4, 8, 16 minutes before the next one, so a short
# Drive or Brevo outage does not use up MAX_ATTEMPTS in one run.
RETRY_BASE_MIN = 2
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

PAYLOAD_VERSION = 1
_ENC_PREFIX = "v1:"


class UndecryptablePayloadError(ValueError):
    """The payload does not decrypt with the current key (key rotated?)."""


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# ── encryption ───────────────────────────────────────────────────────────


def _aad(patient_id: str, token_hash: str) -> bytes:
    return f"oncofiles.inbound_jobs.v1|{patient_id}|{token_hash}".encode()


def encrypt_payload(payload: dict, *, patient_id: str, token_hash: str) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import inbound_jobs_key

    nonce = os.urandom(12)
    plain = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ct = AESGCM(inbound_jobs_key()).encrypt(nonce, plain, _aad(patient_id, token_hash))
    return _ENC_PREFIX + base64.b64encode(nonce + ct).decode("ascii")


def decrypt_payload(blob: str, *, patient_id: str, token_hash: str) -> dict:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from oncofiles.secrets_keys import inbound_jobs_key

    try:
        if not blob.startswith(_ENC_PREFIX):
            raise ValueError("prefix")
        raw = base64.b64decode(blob[len(_ENC_PREFIX) :], validate=True)
        plain = AESGCM(inbound_jobs_key()).decrypt(raw[:12], raw[12:], _aad(patient_id, token_hash))
        payload = json.loads(plain)
    except (InvalidTag, ValueError) as exc:
        raise UndecryptablePayloadError(type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("v") != PAYLOAD_VERSION:
        raise UndecryptablePayloadError("version")
    return payload


def message_id_hash(message_id: str) -> str | None:
    from oncofiles.secrets_keys import inbound_message_id_hash_key

    mid = (message_id or "").strip()
    if not mid:
        return None
    return hmac.new(inbound_message_id_hash_key(), mid.encode(), hashlib.sha256).hexdigest()


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
    """The minimal plaintext of a job (encrypted before it is stored)."""
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
        "parse_skips": dict(msg.parse_skips),
        "attachments": [
            {
                "ref": a.ref,
                "name": a.name,
                "content_type": a.content_type,
                "declared_size": a.declared_size,
                "inline": bool(a.inline),
            }
            for a in msg.attachments
            if a.ref
        ],
        "progress": {"filed": [], "receipt_sent": False},
    }


def from_payload(payload: dict) -> inbound_email.InboundMessage:
    """Rebuild the message; download callables are made now, with the current key."""
    from oncofiles.dashboard_upload import MAX_FILE_BYTES

    api_key = os.environ.get(inbound_email.BREVO_API_KEY_ENV, "")
    skips = Counter({str(k): int(v) for k, v in (payload.get("parse_skips") or {}).items()})
    attachments = []
    for a in payload.get("attachments") or []:
        ref = str(a.get("ref") or "")
        if not api_key or not ref:
            skips["fetch_failed"] += 1
            continue
        attachments.append(
            inbound_email.InboundAttachment(
                name=str(a.get("name") or ""),
                data=b"",
                content_type=str(a.get("content_type") or "application/octet-stream"),
                inline=bool(a.get("inline")),
                fetch=inbound_email._brevo_fetcher(ref, api_key, MAX_FILE_BYTES + 1),
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
    )


# ── enqueue ──────────────────────────────────────────────────────────────


async def enqueue(db, messages: list[inbound_email.InboundMessage]) -> dict:
    """Queue every routable message in ONE transaction; raises on a DB error.

    Returns counts: ``queued``, ``duplicate`` (already queued, a provider
    retry), ``dropped`` (unknown / inactive token, silent as before).
    """
    rows = []
    dropped = 0
    for msg in messages:
        token = inbound_email._find_token(msg)
        th = inbound_email.token_hash(token) if token else None
        row = await db.find_inbound_address(th) if th else None
        if row is None or not row["active"]:
            reason = "unknown_token" if row is None else "inactive_token"
            inbound_email._state.counters[reason] += 1
            dropped += 1
            continue
        pid = row["patient_id"]
        rows.append(
            {
                "patient_id": pid,
                "token_hash": th,
                "message_id_hash": message_id_hash(msg.message_id),
                "sender_hash": sender_hash(msg.sender, msg.auth_pass),
                "payload_enc": encrypt_payload(
                    to_payload(msg, token), patient_id=pid, token_hash=th
                ),
            }
        )
    queued, duplicate = await db.insert_inbound_jobs(rows) if rows else (0, 0)
    inbound_email._state.counters["jobs_queued"] += queued
    inbound_email._state.counters["jobs_duplicate"] += duplicate
    logger.info(
        "inbound: queued=%d duplicate=%d dropped=%d patients=%s",
        queued,
        duplicate,
        dropped,
        ",".join(sorted({r["patient_id"][:4] for r in rows})) or "-",
    )
    return {"queued": queued, "duplicate": duplicate, "dropped": dropped}


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
    return counts


def _due(job: dict, now: datetime) -> bool:
    """A failed attempt waits ``RETRY_BASE_MIN * 2**(attempts-1)`` minutes."""
    if job["state"] != "pending" or job["attempts"] == 0:
        return True
    wait = timedelta(minutes=RETRY_BASE_MIN * 2 ** (job["attempts"] - 1))
    return now - _parse_iso(job["updated_at"]) >= wait


async def _run_one(db) -> str | None:
    """Claim and process the next job (caller holds PROCESS_LOCK). None = no job."""
    now = _now()
    candidates = await db.claimable_inbound_jobs(
        stale_before=_iso(now - timedelta(minutes=STALE_PROCESSING_MIN))
    )
    job = next((j for j in candidates if _due(j, now)), None)
    if job is None:
        return None
    pid, jid = job["patient_id"], job["id"]
    pp = pid[:4]

    async def finish(state: str, reason: str) -> str:
        await db.finish_inbound_job(
            jid, patient_id=pid, state=state, reason=reason, now=_iso(_now())
        )
        inbound_email._state.counters[f"jobs_{state}"] += 1
        if state == "failed":
            inbound_email._state.counters[f"job_failed_{reason}"] += 1
        logger.info("inbound: job patient %s state=%s reason=%s", pp, state, reason)
        return f"{state}:{reason}"

    if job["attempts"] >= MAX_ATTEMPTS:
        return await finish("failed", "max_attempts")
    try:
        payload = decrypt_payload(job["payload_enc"], patient_id=pid, token_hash=job["token_hash"])
    except UndecryptablePayloadError:
        return await finish("failed", "undecryptable")
    age = (now - _parse_iso(job["created_at"])).total_seconds()
    if payload.get("attachments") and age > BREVO_ATTACHMENT_RETENTION_S:
        return await finish("failed", "download_expired")
    if not await db.claim_inbound_job(job, now=_iso(now)):
        return "lost_claim"

    prog = payload.get("progress") or {}
    progress = inbound_email.JobProgress(
        filed=set(prog.get("filed") or []),
        receipt_sent=bool(prog.get("receipt_sent")),
        # claimed before (pending after an error, or a stale processing job):
        # a file may already be in Drive without its progress entry
        retry=job["attempts"] > 0,
    )

    async def save() -> None:
        payload["progress"] = {
            "filed": sorted(progress.filed),
            "receipt_sent": progress.receipt_sent,
        }
        await db.update_inbound_job_payload(
            jid,
            patient_id=pid,
            payload_enc=encrypt_payload(payload, patient_id=pid, token_hash=job["token_hash"]),
            now=_iso(_now()),
        )

    progress.save = save
    try:
        result = await inbound_email._process_message(db, from_payload(payload), progress)
    except Exception as exc:  # noqa: BLE001 - one job never stops the queue
        attempts = job["attempts"] + 1
        if attempts >= MAX_ATTEMPTS:
            return await finish("failed", "max_attempts")
        reason = f"error:{type(exc).__name__}"[:64]
        return await finish("pending", reason)
    return await finish("done", result.outcome)
