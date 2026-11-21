"""Holding store -> the patient's Drive when a trial patient connects Drive (#950 phase 3).

A welcome@ trial patient's documents wait in the encrypted holding store
(``holding_store``). Once the person connects a Drive folder in the New
Patient wizard (``drive_folder_link.connect_folder``, which bootstraps a
curated folder, #853), this module copies every held file into that folder's
curated INBOX, and the curated pipeline files it from there like any upload.

Rules
-----
* **One write path.** Every copy goes through the #935 inbox upload,
  ``curated.inbox_upload.upload_to_inbox`` (``drive_ops.upload_original``
  inside ``contract_writes()``: create-only, read back by size + md5, never
  retried). Only a curated root is a target (``dashboard_upload._resolve_target``
  path ``curated_inbox``); no Drive, a Drive-only (legacy layout) folder or a
  folder overlapping a curated tree is a wait code, nothing is written.
* **One run per trial (infra review of PR #962).** Before any Drive write a
  run claims the trial with ONE conditional UPDATE of
  ``trial_accounts.move_claimed_at`` (free, or older than ``STALE_CLAIM_MIN``:
  a run that died is taken over), the ``inbound_jobs`` pattern. The lease is
  refreshed before every file copy (conditional on the current lease), every
  per-row record (``moved_to_drive``, ``move_error``) and the conversion are
  written only while it holds (``EXISTS … move_claimed_at = lease`` in the
  UPDATE), a lost lease stops the run (``lease_lost``) with no further write,
  and the claim is released at the end. Both entry points (the connect-time
  :func:`kick` and the scheduler job) go through :func:`move_patient`, so a
  second container's run on the same trial is refused (``claim_refused``)
  while the lease is fresh.
* **One file at a time**, at most ``MAX_FILES_PER_RUN`` per run; the first
  failed copy stops that patient's run (retried on the next run, the held copy
  untouched: ``holding.move_error`` = code, ``move_attempts`` + 1).
* **Idempotent.** Before the first copy of a patient's run, the md5 of the
  files already in the inbox (one read-only listing,
  ``dashboard_upload.stored_inbox_md5_ids``) and of the filed tree (the
  curated mirror rows' ``source_md5``, ``db.curated_index_state``, the same
  sources the #952 queue checks) are collected; a held file whose md5 is
  there is not copied again, only recorded with that Drive id (``via =
  present``). A failed listing fails the run for that patient (fail closed:
  never risk a second copy). The held bytes are checked against the row's
  sha256 before a copy.
* **Record.** After a verified copy ``holding.moved_to_drive = {drive_id, at,
  via}`` (``db.mark_holding_moved``). From then on the row is served from the
  Drive copy (``tools._helpers._try_download``, the #948 previews), the
  holding reader skips it, and once the curated mirror row of the copy exists
  the held row is hidden from the document lists (``holding.superseded_by``,
  ``database._documents.HOLDING_VISIBLE_SQL``), so the dashboard never shows
  the document twice.
* **Conversion.** When no active held row is left without a copy, the trial is
  converted (``trial_accounts.converted_at``): ``holding_store.holding_mode``
  is false for good, the phase-1 caps apply, and welcome@ treats the sender
  as an ordinary caregiver.
* **Nothing is deleted (#734).** The encrypted held copy stays on the volume.
  TODO(#950): removing it after a verified move needs a RUNBOOK lifecycle
  entry and a reviewed ``tests/test_no_automatic_deletion.py`` allowlist
  entry (maintainer decision); only then may a later change remove it.

Runs: kicked right after a successful connect (:func:`kick`) and every
``WORKER_INTERVAL_MIN`` minutes by the ``holding_move`` scheduler job; both
under the scheduler's ``_sync_semaphore`` (single DB connection) and inside
``cloud_guard.local_only`` (nothing here may call a cloud model). DB calls stay
on the event loop; decryption and Drive calls run in worker threads. Logs:
document ids, counts, codes and ``patient_id[:4]`` only, never a file name.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from collections import Counter
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

JOB = "holding_move"
WORKER_INTERVAL_MIN = 10
MAX_FILES_PER_RUN = 20
STALE_CLAIM_MIN = 30  # a trial claim this old belongs to a run that died

_EXT_BY_MIME = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/tiff": ".tif",
    "image/webp": ".webp",
    "image/heic": ".heic",
    "image/heif": ".heif",
    "text/plain": ".txt",
    "text/markdown": ".md",
}

_drain_task: asyncio.Task | None = None
_rekick = False


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def inbox_name(row: dict) -> str:
    """The name of the copy in the inbox: the original file name (the holding
    state's ``original_filename`` if recorded, else the row's), sanitised
    (``files_api.sanitize_filename``, no leading ``_``), with an extension
    that fits the stored type when it has none."""
    from oncofiles.files_api import sanitize_filename
    from oncofiles.holding_store import holding_meta

    held = holding_meta(row.get("structured_metadata"))
    raw = held.get("original_filename") or row.get("original_filename") or row.get("filename")
    name = sanitize_filename(str(raw or "")).lstrip("_").strip()
    if not name or name == "file":
        name = f"document-{int(row['id'])}"
    if "." not in name.lstrip("."):
        name += _EXT_BY_MIME.get((row.get("mime_type") or "").lower(), "")
    return name


def kick(db) -> None:
    """Copy held files in the background now (one drain at a time)."""
    global _drain_task, _rekick
    if (
        _drain_task is not None
        and not _drain_task.done()
        and _drain_task.get_loop() is asyncio.get_running_loop()
    ):
        _rekick = True
        return
    from oncofiles import inbound_email

    _drain_task = asyncio.create_task(_drain(db))
    inbound_email._BACKGROUND.add(_drain_task)
    _drain_task.add_done_callback(inbound_email._BACKGROUND.discard)


async def _drain(db) -> None:
    global _rekick
    from oncofiles.cloud_guard import local_only
    from oncofiles.server import _sync_semaphore

    try:
        with local_only(JOB):
            while True:
                _rekick = False
                async with _sync_semaphore:
                    await run_pending(db)
                if not _rekick:
                    return
    except Exception as exc:  # noqa: BLE001 - a background task never raises
        logger.error("holding: move drain failed (%s)", type(exc).__name__)


async def _present_md5s(db, patient_id: str):
    """``(target, {md5: Drive id})``: the inbox listing plus the filed tree."""
    from oncofiles.dashboard_upload import stored_inbox_md5_ids

    target, present = await stored_inbox_md5_ids(db, patient_id, log_pid=patient_id[:4])
    state = await db.curated_index_state(patient_id=patient_id)
    for drive_id, s in state.items():
        md5 = s.get("source_md5")
        if md5 and not s.get("deleted") and not s.get("foreign"):
            present.setdefault(md5, drive_id)
    return target, present


def _read_held(file_id: str, patient_id: str) -> bytes:
    from oncofiles import holding_store

    return holding_store.read(file_id, patient_id)


async def _copy_one(target, name: str, data: bytes, mime: str) -> dict:
    from oncofiles.curated import inbox_upload

    files = [inbox_upload.InboxFile(name, data, mime)]
    results = await asyncio.to_thread(
        inbox_upload.upload_to_inbox, target.gdrive, target.folder_id, files
    )
    return results[0]


class LeaseLostError(RuntimeError):
    """The trial's claim was taken over (stale) by another run: stop, write nothing."""


class _Lease:
    """This run's claim on one trial (``trial_accounts.move_claimed_at``)."""

    def __init__(self, db, patient_id: str):
        self.db = db
        self.patient_id = patient_id
        self.value = _new_lease()

    async def claim(self) -> bool:
        stale = (datetime.now(UTC) - timedelta(minutes=STALE_CLAIM_MIN)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return await self.db.claim_trial_move(
            patient_id=self.patient_id, lease=self.value, stale_before=stale
        )

    async def refresh(self) -> None:
        """Before each file copy: move the lease forward, or stop the run."""
        new = _new_lease()
        if not await self.db.refresh_trial_move(
            patient_id=self.patient_id, lease=self.value, new_lease=new
        ):
            raise LeaseLostError
        self.value = new

    def held(self, written: bool) -> None:
        """A lease-conditional write that changed nothing: the lease is gone."""
        if not written:
            raise LeaseLostError

    async def release(self) -> None:
        await self.db.release_trial_move(patient_id=self.patient_id, lease=self.value)


def _new_lease() -> str:
    # ISO time first (the stale check compares the text), then a random part so
    # two runs never hold the same value.
    return f"{_now_iso()}|{secrets.token_hex(6)}"


async def move_patient(db, patient_id: str, counts: Counter, budget: int) -> int:
    """Copy one trial patient's held files under the trial's claim; returns
    how many files it wrote. A trial claimed by a live run (another container)
    is refused (``claim_refused``); a claim older than ``STALE_CLAIM_MIN`` is
    taken over. A lost lease stops the run (``lease_lost``), no further write."""
    pp = patient_id[:4]
    lease = _Lease(db, patient_id)
    if not await lease.claim():
        counts["claim_refused"] += 1
        logger.info("holding: patient %s is being moved by another run", pp)
        return 0
    written = [0]
    try:
        await _move_claimed(db, patient_id, counts, budget, lease, written)
    except LeaseLostError:
        counts["lease_lost"] += 1
        logger.warning("holding: patient %s lease lost, left to its new owner", pp)
    finally:
        await lease.release()  # conditional: never frees another run's claim
    return written[0]


async def _move_claimed(
    db, patient_id: str, counts: Counter, budget: int, lease: _Lease, written_box: list
) -> None:
    from oncofiles.curated.inbox_upload import InboxUnavailableError
    from oncofiles.dashboard_upload import Outcome
    from oncofiles.holding_store import holding_meta

    pp = patient_id[:4]
    rows = await db.holding_rows_to_move(patient_id=patient_id)
    if not rows:
        if await db.convert_trial(patient_id=patient_id, lease=lease.value, at=_now_iso()):
            counts["converted"] += 1
            logger.info("holding: trial of patient %s converted", pp)
        return
    try:
        target, present = await _present_md5s(db, patient_id)
    except Exception as exc:  # noqa: BLE001 - fail closed: no copy this run
        counts["listing_failed"] += 1
        logger.warning("holding: patient %s inbox listing failed (%s)", pp, type(exc).__name__)
        return
    if isinstance(target, Outcome):
        code = str(target.payload.get("error") or "no_target")
        counts[code] += 1
        logger.info("holding: patient %s waits reason=%s", pp, code)
        return
    if target.path != "curated_inbox":
        counts["not_curated"] += 1
        logger.info("holding: patient %s waits reason=not_curated", pp)
        return
    for row in rows:
        doc_id = int(row["id"])
        held = holding_meta(row.get("structured_metadata"))
        md5 = str(held.get("md5") or "")
        if md5 and md5 in present:
            lease.held(
                await db.mark_holding_moved(
                    doc_id,
                    patient_id=patient_id,
                    drive_id=present[md5],
                    at=_now_iso(),
                    via="present",
                    lease=lease.value,
                )
            )
            counts["present"] += 1
            continue
        if written_box[0] >= budget:
            counts["deferred"] += 1
            return
        await lease.refresh()  # before every copy: still ours, or stop here
        try:
            data = await asyncio.to_thread(_read_held, row["file_id"], patient_id)
        except Exception as exc:  # noqa: BLE001 - the held copy stays; next run retries
            code = "held_unreadable"
            lease.held(
                await db.note_holding_move_error(
                    doc_id, patient_id=patient_id, code=code, lease=lease.value
                )
            )
            counts[code] += 1
            logger.warning("holding: doc=%d unreadable (%s)", doc_id, type(exc).__name__)
            return
        if held.get("sha256") and hashlib.sha256(data).hexdigest() != held["sha256"]:
            lease.held(
                await db.note_holding_move_error(
                    doc_id, patient_id=patient_id, code="sha_mismatch", lease=lease.value
                )
            )
            counts["sha_mismatch"] += 1
            logger.warning("holding: doc=%d content does not match its sha256", doc_id)
            return
        try:
            res = await _copy_one(target, inbox_name(row), data, row.get("mime_type") or "")
        except InboxUnavailableError as exc:
            res = {"status": "failed", "code": exc.code}
        except Exception as exc:  # noqa: BLE001 - reported as a code, retried later
            res = {"status": "failed", "code": type(exc).__name__}
        finally:
            data = b""
        if res.get("status") != "uploaded" or not res.get("id"):
            code = str(res.get("code") or "copy_failed")[:40]
            counts["copy_failed"] += 1
            logger.warning("holding: doc=%d patient %s copy failed (%s)", doc_id, pp, code)
            lease.held(
                await db.note_holding_move_error(
                    doc_id, patient_id=patient_id, code=code, lease=lease.value
                )
            )
            return  # first failure stops this patient; held copy untouched
        written_box[0] += 1
        present[md5] = res["id"]
        counts["copied"] += 1
        logger.info("holding: doc=%d patient %s copied into the inbox", doc_id, pp)
        # recorded only while the lease holds; a lost lease leaves the copy to
        # the next owner, whose md5 check finds it (``present``), never twice
        lease.held(
            await db.mark_holding_moved(
                doc_id,
                patient_id=patient_id,
                drive_id=res["id"],
                at=_now_iso(),
                via="copied",
                lease=lease.value,
            )
        )
    if not await db.holding_rows_to_move(patient_id=patient_id) and await db.convert_trial(
        patient_id=patient_id, lease=lease.value, at=_now_iso()
    ):
        counts["converted"] += 1
        logger.info("holding: trial of patient %s converted", pp)


async def run_pending(db, *, limit: int = MAX_FILES_PER_RUN) -> Counter:
    """One pass over every trial with a connected Drive that is not converted.

    The caller holds ``_sync_semaphore`` (the scheduler job and :func:`kick`
    both do). Also flags held rows whose curated mirror row now exists, and
    sends due "ready" e-mails (``trial_funnel.send_due_ready_mails``)."""
    from oncofiles import holding_store, trial_funnel

    counts: Counter = Counter()
    if not holding_store.configured():
        return counts
    budget = limit
    for pid in await db.list_trials_to_convert():
        if budget <= 0:
            counts["deferred_patients"] += 1
            break
        budget -= await move_patient(db, pid, counts, budget)
    superseded = await db.mark_superseded_holding_rows()
    if superseded:
        counts["superseded"] += superseded
    try:
        sent = await trial_funnel.send_due_ready_mails(db)
        if sent:
            counts["ready_mails"] += sent
    except Exception as exc:  # noqa: BLE001 - mail never stops the mover
        logger.warning("holding: ready mails failed (%s)", type(exc).__name__)
    if counts:
        logger.info("holding: move run %s", dict(sorted(counts.items())))
    return counts
