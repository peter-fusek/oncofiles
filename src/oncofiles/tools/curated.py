"""Curated-folder pipeline operations (#562, #573).

One core — ``execute_run`` — shared by three front doors:

* MCP tools ``curated_folder_run`` / ``curated_folder_status`` (claude.ai etc.);
* the admin dashboard API (``/api/admin/curated/*`` in server.py);
* the nightly loop ``curated_nightly_loop`` (server lifespan), which replaces
  the Cowork cleanup jobs — no Claude involved: OCR and classification run on
  the self-hosted local model.

Rules enforced here for every front door:

* one run per patient at a time (``_runs`` + ``_lock``);
* a **real** run requires a clean dry run (status ok) finished in the last
  24 h (``preview_gate``); the nightly loop always runs its own preview first;
* every run is persisted in ``curated_runs`` (in-memory state dies on deploy).

Extraction and classification are cached by content hash, so the real run
after a preview does not OCR the same pages twice on the slow local model.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

from fastmcp import Context

from oncofiles.secrets_keys import caller_email_hash
from oncofiles.tools._helpers import _caller_email, _get_db, _resolve_patient_id

logger = logging.getLogger(__name__)

# patient_id -> {"state": "running", "started": ts, "dry_run": bool, "trigger": str}
_runs: dict[str, dict] = {}
# patient_id -> birth dates (ISO) from the README contract, refreshed by every index
_readme_births: dict[str, set[str]] = {}
_lock = asyncio.Lock()

DEFAULTS = {  # patient_type -> (max_docs, min_text_chars, max_doc_age_days)
    # Oncology (Cowork erika-daily-cleanup): a > 2-year-old date on a new scan is suspect.
    "oncology": (20, 100, 730),
    # General (peter-daily-cleanup): historical records back to birth are normal.
    "general": (8, 200, None),
}
PREVIEW_MAX_AGE = timedelta(hours=24)

# ── content-hash cache (dry run → real run) ───────────────────────────

_CACHE_MAX = 400
_extract_cache: OrderedDict[str, object] = OrderedDict()
_propose_cache: OrderedDict[str, dict] = OrderedDict()


def _remember(cache: OrderedDict, key: str, value) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > _CACHE_MAX:
        cache.popitem(last=False)


def cached_extract(name, content, mime_type="", *, min_text_chars=100):
    from oncofiles.curated import extract as extract_mod

    ext = name.rsplit(".", 1)[-1]
    key = f"{hashlib.sha256(content).hexdigest()}:{mime_type}:{min_text_chars}:{ext}"
    if content and key in _extract_cache:
        return _extract_cache[key]
    ex = extract_mod.extract(name, content, mime_type, min_text_chars=min_text_chars)
    if content and ex.ok:  # never cache failures (the model may be back next time)
        _remember(_extract_cache, key, ex)
    return ex


def cached_propose(contract, text):
    from oncofiles.curated import classify

    key = hashlib.sha256(
        f"{contract.version}\n{contract.patient_token}\n{text}".encode()
    ).hexdigest()
    if key in _propose_cache:
        return dict(_propose_cache[key])
    raw = classify.propose(contract, text)
    _remember(_propose_cache, key, dict(raw))
    return raw


# ── shutdown drain ────────────────────────────────────────────────────

_stopping = threading.Event()


def request_stop() -> None:
    """Server shutdown: running runs finish the current document and log cleanly."""
    _stopping.set()


# background jobs that write nothing: a deploy may cut them
_NO_WRITE_KINDS = frozenset({"llm_shadow", "clinical_preview"})


def run_active() -> bool:
    """True while any curated run or re-OCR executes (on /health, CI deploys wait).

    An LLM-extract shadow job (#714) or a clinical extraction preview (#726)
    writes nothing, so a deploy may cut it."""
    return any(r.get("state") == "running" for r in _runs.values()) or any(
        j.get("state") == "running" and j.get("kind") not in _NO_WRITE_KINDS
        for j in _reocr_jobs.values()
    )


def run_block_reason(pid: str) -> str | None:
    """Why a new run for THIS patient must wait, or None (#665 starvation fix).

    Only the same patient's write-capable work blocks a run: a curated run, a
    re-OCR or re-read job (both hold ``patient_lock``), or an index holding the
    lock. Another patient's job never does, and neither does an LLM shadow job
    (#714: it writes nothing and never takes the lock). A probe is never
    blocked by any of these — it is a read-only Drive listing.
    """
    if (_runs.get(pid) or {}).get("state") == "running":
        return "run_in_progress"
    job = _reocr_jobs.get(pid) or {}
    if job.get("state") == "running" and job.get("kind") != "llm_shadow":
        return f"{job.get('kind') or 'reocr'}_in_progress"
    if patient_lock(pid).locked():
        return "patient_lock_held"
    return None


# ── text sources awaiting review (no sidecar to mark them) ─────────────

_text_reviews: dict[str, set] = {}
TEXT_REVIEW_SEED_RUNS = 30


def _remember_text_reviews(pid: str, report: dict | None) -> None:
    for d in (report or {}).get("documents", []):
        ts = d.get("text_source")
        if d.get("result") == "review_needed" and ts:
            _text_reviews.setdefault(pid, set()).add((ts.get("id"), ts.get("modified")))


async def reviewed_text_sources(db, pid: str) -> frozenset:
    """(id, modified) of text sources earlier runs sent to review; seeded from
    the DB once per process, so a restart does not re-classify them."""
    if pid not in _text_reviews:
        _text_reviews[pid] = set()
        for row in await db.list_curated_runs(patient_id=pid, limit=TEXT_REVIEW_SEED_RUNS):
            if row.get("dry_run"):
                continue
            full = await db.get_curated_run(row["id"], patient_id=pid)
            _remember_text_reviews(pid, (full or {}).get("report"))
    return frozenset(_text_reviews[pid])


# ── gates ─────────────────────────────────────────────────────────────


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


async def preview_gate(db, pid: str, *, now: datetime | None = None) -> str | None:
    """None when a real run is allowed, else the reason it is not."""
    last = await db.get_last_curated_run(patient_id=pid, dry_run=True)
    if last is None:
        return "no preview (dry run) yet — run a preview first"
    if last.get("status") != "ok":
        return f"the last preview ended '{last.get('status')}' — fix it and preview again"
    finished = _parse_ts(last.get("finished_at"))
    if finished is None or (now or datetime.now(UTC)) - finished > PREVIEW_MAX_AGE:
        return "the last clean preview is older than 24 h — preview again"
    return None


# ── core ──────────────────────────────────────────────────────────────


async def _prepare(db, pid: str, *, dry_run: bool, max_docs: int | None, max_actions: int):
    """Validate and build what a run needs. Returns (error_dict | None, ctx)."""
    from oncofiles.curated import llm
    from oncofiles.curated.runner import RunOptions
    from oncofiles.curated_folder import curated_reason
    from oncofiles.patient_context import get_context
    from oncofiles.server import _create_patient_clients

    if not llm.configured():
        return {"error": "local LLM not configured (LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL)"}, None
    if not dry_run and (reason := await preview_gate(db, pid)):
        return {"error": f"real run refused: {reason}"}, None
    clients = await _create_patient_clients(db, pid)
    if not clients or clients[0] is None:
        return {
            "error": "Google Drive not connected for this patient (or integration disabled)"
        }, None
    gdrive, folder_id = clients[0], clients[3]
    reason = await asyncio.to_thread(curated_reason, gdrive, folder_id, use_cache=False)
    if reason != "is a curated folder":
        return {
            "error": f"patient folder is not a curated root ({reason or 'no README contract'})"
        }, None

    patient_type = (get_context(pid) or {}).get("patient_type", "oncology")
    default_docs, min_chars, max_age = DEFAULTS.get(patient_type, DEFAULTS["oncology"])
    opts = RunOptions(
        dry_run=dry_run,
        max_docs=max_docs or default_docs,
        max_actions=max(1, max_actions),
        min_text_chars=min_chars,
        max_doc_age_days=max_age,
        should_stop=_stopping.is_set,
        reviewed_text=await reviewed_text_sources(db, pid),
    )
    return None, (gdrive, folder_id, opts)


# One writer per patient (#612): a curated run, the read-model index and the
# data backfill (#619) never overlap for the same patient, so the index never
# reads a half-moved pair and facts don't churn in the audit trail.
# INVARIANT: in-process lock — valid only while Oncofiles runs as ONE process
# in ONE container (true today). Before adding workers or replicas, move this
# to the DB (a per-patient lock row with a lease), or two writers return.
_patient_locks: dict[str, asyncio.Lock] = {}


def patient_lock(pid: str) -> asyncio.Lock:
    lock = _patient_locks.get(pid)
    if lock is None:
        lock = _patient_locks[pid] = asyncio.Lock()
    return lock


async def _execute(db, pid: str, gdrive, folder_id: str, opts, *, trigger: str) -> dict:
    async with patient_lock(pid):
        return await _execute_locked(db, pid, gdrive, folder_id, opts, trigger=trigger)


async def _execute_locked(db, pid: str, gdrive, folder_id: str, opts, *, trigger: str) -> dict:
    """Run to completion and persist. Never raises; returns the report dict."""
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.runner import run_curated

    def _blocking():
        kwargs = {"extract_fn": cached_extract, "propose_fn": cached_propose}
        # Own HTTP connection: the shared patient client is used concurrently by
        # dashboard/API threads, and httplib2 is not thread-safe (2026-11-08 abort).
        own = gdrive.dedicated_copy()
        try:
            if opts.dry_run:  # a dry run never needs the write bypass
                return run_curated(DriveOps(own), folder_id, opts, **kwargs)
            with own.contract_writes():  # thread-scoped: other tools stay blocked
                return run_curated(DriveOps(own), folder_id, opts, **kwargs)
        finally:
            own.close()

    row_id = await db.insert_curated_run(patient_id=pid, dry_run=opts.dry_run, trigger=trigger)
    sync_id = None if opts.dry_run else await db.insert_sync_history("curated", patient_id=pid)
    t0 = time.monotonic()
    try:
        report = (await asyncio.to_thread(_blocking)).to_dict()
    except Exception as exc:  # runner catches everything; this is belt and braces
        logger.exception("curated run crashed for %s", pid)
        report = {"status": "aborted", "message": f"crashed ({type(exc).__name__})"}
    if not opts.dry_run:
        _remember_text_reviews(pid, report)
        _note_run_outcome(pid, report)
    status, message = report.get("status", "aborted"), report.get("message", "")
    await db.finish_curated_run(
        row_id, patient_id=pid, status=status, message=message, report=report
    )
    if sync_id is not None:
        filed = sum(1 for d in report.get("documents", []) if d.get("result") == "filed")
        await db.complete_sync_history(
            sync_id,
            status="completed" if status == "ok" else "failed",
            duration_s=round(time.monotonic() - t0, 1),
            to_organized=filed,
            to_renamed=filed,
            error_message=None if status == "ok" else (message or "")[:500],
        )
    report["_row_id"] = row_id
    return report


async def _index_after(db, pid: str, opts, report: dict | None) -> None:
    """Refresh the read model after a successful real run (#603).

    Called once the run is no longer "active": a deploy may interrupt the
    index at no cost (it is a read-only copy, rebuilt at the next start).
    """
    if report and not opts.dry_run and report.get("status") == "ok":
        await _index_quietly(db, pid)


async def execute_run(
    db,
    pid: str,
    *,
    dry_run: bool,
    trigger: str,
    max_docs: int | None = None,
    max_actions: int = 40,
    skip_if_busy: bool = False,
) -> dict:
    """Run in the foreground (nightly loop). Returns the report or {"error": …}.

    ``skip_if_busy`` (the background loop): return ``{"blocked": reason}`` at
    once instead of waiting on ``patient_lock`` behind a re-OCR or re-read job,
    which can hold it for hours and would stall the loop for every patient."""
    async with _lock:
        if (_runs.get(pid) or {}).get("state") == "running":
            return {"error": "a run is already in progress for this patient"}
        if skip_if_busy and (reason := run_block_reason(pid)):
            return {"error": f"busy: {reason}", "blocked": reason}
        err, ctx = await _prepare(
            db, pid, dry_run=dry_run, max_docs=max_docs, max_actions=max_actions
        )
        if err:
            return err
        _runs[pid] = {
            "state": "running",
            "started": time.time(),
            "dry_run": dry_run,
            "trigger": trigger,
        }
    report = None
    try:
        report = await _execute(db, pid, *ctx, trigger=trigger)
        return report
    finally:
        _runs.pop(pid, None)
        if skip_if_busy and patient_lock(pid).locked():
            # A job took the lock right after the run: never stall the loop
            # behind it; the next real run (or start) refreshes the index.
            logger.info("curated %s: index after run skipped (patient_lock_held)", pid)
        else:
            await _index_after(db, pid, ctx[2], report)


async def start_run(
    db, pid: str, *, dry_run: bool, trigger: str, max_docs: int | None = None, max_actions: int = 40
) -> dict:
    """Start in the background (MCP, dashboard). Returns at once."""
    async with _lock:
        if (_runs.get(pid) or {}).get("state") == "running":
            return {"error": "a run is already in progress for this patient"}
        err, ctx = await _prepare(
            db, pid, dry_run=dry_run, max_docs=max_docs, max_actions=max_actions
        )
        if err:
            return err
        _runs[pid] = {
            "state": "running",
            "started": time.time(),
            "dry_run": dry_run,
            "trigger": trigger,
        }

    async def _task():
        report = None
        try:
            report = await _execute(db, pid, *ctx, trigger=trigger)
        finally:
            _runs.pop(pid, None)
            await _index_after(db, pid, ctx[2], report)

    asyncio.get_running_loop().create_task(_task())
    return {"status": "started", "dry_run": dry_run, "max_docs": ctx[2].max_docs}


async def run_status(db, pid: str) -> dict:
    """Running state, else the last finished run (with its full report)."""
    running = _runs.get(pid)
    if running:
        return {"state": "running", **{k: v for k, v in running.items() if k != "state"}}
    last = await db.get_last_curated_run(patient_id=pid)
    if last is None:
        return {"state": "none"}
    return {"state": "done", "run": last}


# ── nightly loop (replaces the Cowork jobs) ───────────────────────────


def nightly_settings() -> tuple[bool, int | None, int]:
    """(enabled, local hour Europe/Bratislava, max real runs per sequence) from env.

    ``CURATED_NIGHTLY_HOUR=every`` (or ``hourly`` / ``*``) returns hour ``None``:
    a sequence starts every hour instead of once per night.
    """
    enabled = os.environ.get("CURATED_NIGHTLY_ENABLED", "false").lower() in ("1", "true", "yes")
    raw = os.environ.get("CURATED_NIGHTLY_HOUR", "1").strip().lower()
    hour = None if raw in ("every", "hourly", "*") else int(raw)
    max_runs = int(os.environ.get("CURATED_NIGHTLY_MAX_RUNS", "5"))
    return enabled, hour, max_runs


async def nightly_for_patient(
    db,
    pid: str,
    *,
    max_real_runs: int,
    reuse_preview: bool = False,
    trigger: str = "nightly",
    max_docs: int | None = None,
    skip_if_busy: bool = False,
) -> list[dict]:
    """Preview first; if clean, real runs until the inbox is done or a run is not clean.

    ``reuse_preview`` (hourly mode) skips the preview while the last clean one is
    still inside the 24 h gate — the real runs keep their own preflight gates.
    """
    reports: list[dict] = []
    if not (reuse_preview and await preview_gate(db, pid) is None):
        reports.append(
            await execute_run(
                db,
                pid,
                dry_run=True,
                trigger=f"{trigger}-preview",
                max_docs=max_docs,
                skip_if_busy=skip_if_busy,
            )
        )
        if reports[-1].get("status") != "ok":
            return reports
    for _ in range(max_real_runs):
        if _stopping.is_set():
            break
        rep = await execute_run(
            db, pid, dry_run=False, trigger=trigger, max_docs=max_docs, skip_if_busy=skip_if_busy
        )
        reports.append(rep)
        if rep.get("status") != "ok":
            break
        worked = any(d.get("result") != "skipped" for d in rep.get("documents", []))
        if not rep.get("not_processed") or not worked:
            break  # inbox done, or nothing left this run could do
    return reports


# During a rolling deploy the old and new containers run side by side for a
# few seconds. That is safe only because the new curated loop waits this long
# before its first RUN (and its startup index), and patient_lock() is
# in-process: it cannot serialize two containers. Never shorten this below the
# deploy overlap, and never run extra replicas or workers (RUNBOOK, Oncofiles
# section; #581, #612). The grace gates runs only: a probe is a read-only Drive
# listing and starts PROBE_STARTUP_DELAY_S after boot, so on a day with many
# deploys new work is at least seen (and counted) right away.
STARTUP_GRACE_S = 300
PROBE_STARTUP_DELAY_S = 15


# ── continuous mode: new files processed within minutes ───────────────


def continuous_settings() -> tuple[bool, int, int, int]:
    """(enabled, poll seconds, settle seconds, documents per run) from env.

    On by default: the inbox is probed every ``CURATED_POLL_S`` and a run starts
    as soon as new work has been quiet for ``CURATED_SETTLE_S`` (an upload of
    many scans lands as one batch). Runs are short (``CURATED_CHUNK_DOCS``
    documents) so deploys and other work interleave. The nightly sweep at
    CURATED_NIGHTLY_HOUR stays as the full catch-all.
    """
    enabled = os.environ.get("CURATED_CONTINUOUS", "true").lower() in ("1", "true", "yes")
    poll = max(30, int(os.environ.get("CURATED_POLL_S", "120")))
    settle = max(0, int(os.environ.get("CURATED_SETTLE_S", "120")))
    chunk = max(1, int(os.environ.get("CURATED_CHUNK_DOCS", "5")))
    return enabled, poll, settle, chunk


FAILURE_BACKOFF_S = 1800  # after a failed sequence; doubles, capped at 4 h
FAILURE_BACKOFF_MAX_S = 4 * 3600


@dataclass
class _Watch:
    """Per-patient continuous-mode memory (in-process; a restart re-probes)."""

    idle_signature: str | None = None  # tree fingerprint with nothing to do
    stuck: frozenset = frozenset()  # pending ids a sequence could not move
    failures: int = 0
    not_before: float = 0.0  # monotonic time of the next allowed attempt


_watch: dict[str, _Watch] = {}

# ── heartbeat and loop detection (in-memory; /health shows booleans and ages only) ──
LOOP_ALERT_RUNS = 5  # this many real runs in a row, moving nothing, over the same documents
_last_probe_at: float | None = None  # monotonic time of the last completed probe
_empty_streak: dict[str, tuple[tuple, int]] = {}  # pid -> (documents seen, consecutive runs)


def _note_run_outcome(pid: str, report: dict) -> None:
    """Count consecutive real runs that moved nothing over the same documents."""
    if report.get("actions"):
        _empty_streak.pop(pid, None)
        return
    seen = tuple(sorted(str(d.get("source")) for d in report.get("documents", [])))
    prev, n = _empty_streak.get(pid, ((), 0))
    n = n + 1 if (n and prev == seen) else 1
    _empty_streak[pid] = (seen, n)
    if n == LOOP_ALERT_RUNS:
        logger.warning(
            "curated loop suspected for %s: %d real runs in a row moved nothing (%d documents)",
            pid,
            n,
            len(seen),
        )


def loop_suspected_patients() -> list[str]:
    return [pid for pid, (_, n) in _empty_streak.items() if n >= LOOP_ALERT_RUNS]


def heartbeat() -> dict:
    """Public, non-sensitive: an age and a boolean (no counts, ids or names)."""
    age = None if _last_probe_at is None else int(time.monotonic() - _last_probe_at)
    return {
        "curated_last_probe_age_s": age,
        "curated_loop_suspected": bool(loop_suspected_patients()),
    }


def heartbeat_detail() -> dict:
    """Admin-only detail behind the public flags."""
    return {
        **heartbeat(),
        "empty_run_streaks": {pid: n for pid, (_, n) in _empty_streak.items()},
        "loop_alert_runs": LOOP_ALERT_RUNS,
        "pending": {pid: len(w.stuck) for pid, w in _watch.items()},
        "probes": probe_stats(),
    }


# ── continuous-mode counters (in-memory, operator surfaces only) ──────


@dataclass
class _ProbeStats:
    """Per patient, since process start. Counts, reasons and times only — never
    file names, ids or document content (operator /readiness + admin overview)."""

    last_probe_at: str | None = None  # UTC ISO of the last completed probe
    probes_total: int = 0
    probes_skipped: dict[str, int] = field(default_factory=dict)  # reason -> count
    runs_blocked: dict[str, int] = field(default_factory=dict)  # reason -> count
    last_blocked_reason: str | None = None
    last_auto_run_at: str | None = None  # UTC ISO of the last auto sequence start
    auto_runs_total: int = 0
    pending_inbox_count: int | None = None  # what the next run would process
    parked_count: int = 0  # pending items a sequence could not move


_probe_stats: dict[str, _ProbeStats] = {}


def _stats(pid: str) -> _ProbeStats:
    return _probe_stats.setdefault(pid, _ProbeStats())


def _bump(counter: dict[str, int], reason: str) -> None:
    counter[reason] = counter.get(reason, 0) + 1


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def probe_stats() -> dict:
    """{patient_id: counters}. Operator-only: keyed by patient id."""
    return {pid: asdict(st) for pid, st in _probe_stats.items()}


async def probe_patient(db, pid: str, idle_signature: str | None):
    """Read-only probe of the patient's curated folder, or None if unreachable."""
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.runner import probe
    from oncofiles.server import _create_patient_clients

    clients = await _create_patient_clients(db, pid)
    if not clients or clients[0] is None:
        return None
    gdrive, folder_id = clients[0], clients[3]
    reviewed = await reviewed_text_sources(db, pid)

    def _blocking():
        own = gdrive.dedicated_copy()
        try:
            return probe(
                DriveOps(own), folder_id, idle_signature=idle_signature, reviewed_text=reviewed
            )
        finally:
            own.close()

    result = await asyncio.to_thread(_blocking)
    global _last_probe_at
    _last_probe_at = time.monotonic()
    return result


def _sequence_outcome(reports: list[dict]) -> tuple[bool, bool]:
    """(failed, progressed) for one sequence of runs. A run that was not started
    because the patient was busy is neither a failure nor progress."""
    reports = [r for r in reports if not r.get("blocked")]
    failed = any(r.get("error") or r.get("status") != "ok" for r in reports)
    progressed = any(
        d.get("result") != "skipped"
        for r in reports
        if not r.get("dry_run") and not r.get("error")
        for d in r.get("documents", [])
    ) or any(r.get("actions") for r in reports if not r.get("dry_run"))
    return failed, progressed


def _log_blocked(pid: str, reason: str, n_work: int) -> None:
    st = _stats(pid)
    _bump(st.runs_blocked, reason)
    st.last_blocked_reason = reason
    # One line per poll per patient: a tick calls this at most once.
    logger.info(
        "curated auto %s: %d new item(s), run blocked (%s) — retry next poll", pid, n_work, reason
    )


async def continuous_tick(
    db, pid: str, *, settle_s: int, chunk: int, max_runs: int, grace_until: float = 0.0
) -> list[dict] | None:
    """Probe; run a short sequence when there is new, settled work. Never raises
    for an ordinary outcome; returns the reports when a sequence ran.

    The probe always happens (it only reads Drive). What may wait is the RUN:
    startup grace, failure backoff, or this patient's own write-capable work
    (``run_block_reason``). A blocked run is logged once and retried next poll.
    """
    w = _watch.setdefault(pid, _Watch())
    st = _stats(pid)
    try:
        pr = await probe_patient(db, pid, w.idle_signature)
    except Exception:
        _bump(st.probes_skipped, "error")
        raise
    if pr is None:
        _bump(st.probes_skipped, "drive_unavailable")
        return None
    st.probes_total += 1
    st.last_probe_at = _now_iso()
    if w.idle_signature is None or pr.signature != w.idle_signature:
        st.pending_inbox_count = len(pr.pending)  # an idle repeat reads no sidecars
    work = set(pr.pending) - w.stuck
    if not work and not pr.canary:
        w.idle_signature = pr.signature
        return None
    if pr.newest_age_s is not None and pr.newest_age_s < settle_s:
        return None  # uploads still arriving — wait for the batch to settle
    now = time.monotonic()
    if now < grace_until:
        reason = "startup_grace"
    elif now < w.not_before:
        reason = "failure_backoff"
    else:
        reason = run_block_reason(pid)
    if reason:
        _log_blocked(pid, reason, len(work))
        return None
    logger.info(
        "curated auto %s: %d new item(s)%s — starting",
        pid,
        len(work),
        " + leftover canary" if pr.canary else "",
    )
    st.last_auto_run_at = _now_iso()
    st.auto_runs_total += 1
    reports = await nightly_for_patient(
        db,
        pid,
        max_real_runs=max_runs,
        reuse_preview=True,
        trigger="auto",
        max_docs=chunk,
        skip_if_busy=True,
    )
    blocked = next((r["blocked"] for r in reports if r.get("blocked")), None)
    if blocked:
        _log_blocked(pid, blocked, len(work))  # lost a race with a manual job
    failed, progressed = _sequence_outcome(reports)
    if failed:
        w.failures += 1
        delay = min(FAILURE_BACKOFF_S * 2 ** (w.failures - 1), FAILURE_BACKOFF_MAX_S)
        w.not_before = time.monotonic() + delay
    elif not blocked or progressed:
        w.failures = 0
        # Nothing moved: what is pending now needs a human or the nightly sweep.
        w.stuck = frozenset() if progressed else frozenset(pr.pending)
    st.parked_count = len(w.stuck)
    logger.info(
        "curated auto %s: %s%s",
        pid,
        [(r.get("dry_run"), r.get("status") or r.get("error")) for r in reports],
        f" — retry in {int(delay // 60)} min" if failed else "",
    )
    return reports


async def _startup_index(db, pending: set[str]) -> None:
    """Read model once per start (#603), after the grace. A patient whose lock
    is held (a job started meanwhile) is retried next iteration instead of
    stalling the loop behind it."""
    for pid in sorted(pending):
        if _stopping.is_set():
            return
        if patient_lock(pid).locked():
            continue
        await _index_quietly(db, pid)
        pending.discard(pid)


async def curated_nightly_loop(
    db,
    *,
    poll_s: int | None = None,
    startup_grace_s: int = STARTUP_GRACE_S,
    probe_delay_s: int = PROBE_STARTUP_DELAY_S,
) -> None:
    """Background task for curated folders, per enabled patient.

    * Continuous (default): every ``CURATED_POLL_S`` a read-only probe; new
      files are processed minutes after they land (``continuous_tick``).
    * Nightly sweep once per local night at CURATED_NIGHTLY_HOUR (full budget,
      also retries what continuous mode parked). ``CURATED_NIGHTLY_HOUR=every``
      makes the sweep hourly.

    Probes start ``probe_delay_s`` after boot; runs, the nightly sweep and the
    startup index wait ``startup_grace_s`` (rolling deploy overlap). One run
    per patient at a time; patients take turns. The loop never WAITS on a
    patient's lock: a patient busy with its own re-OCR/re-read is skipped for
    this poll, so one long job cannot stall the others. Independent of
    SYNC_ENABLED/APScheduler. A failing iteration never kills the loop.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Bratislava")
    last_night: str | None = None
    try:
        if n := await db.mark_interrupted_curated_runs():
            logger.warning("curated: marked %d run(s) interrupted by the last restart", n)
    except Exception:
        logger.exception("curated: could not mark interrupted runs")
    grace_until = time.monotonic() + startup_grace_s
    await asyncio.sleep(min(probe_delay_s, startup_grace_s))
    to_index: set[str] | None = None  # filled once the grace is over
    while not _stopping.is_set():
        cont, cont_poll, settle, chunk = False, 300, 0, 5
        try:
            enabled, hour, max_runs = nightly_settings()
            cont, cont_poll, settle, chunk = continuous_settings()
            in_grace = time.monotonic() < grace_until
            if not in_grace:
                if to_index is None:
                    to_index = set(await db.list_curated_nightly_patients())
                if to_index:
                    await _startup_index(db, to_index)
            now = datetime.now(tz)
            slot = now.strftime("%Y-%m-%d") if hour is not None else now.strftime("%Y-%m-%dT%H")
            due = hour is None or now.hour == hour
            if enabled and due and last_night != slot and not in_grace:
                last_night = slot
                for pid in await db.list_curated_nightly_patients():
                    if _stopping.is_set():
                        break
                    if reason := run_block_reason(pid):
                        logger.info("curated nightly %s: skipped (%s)", pid, reason)
                        continue
                    reports = await nightly_for_patient(
                        db,
                        pid,
                        max_real_runs=max_runs,
                        reuse_preview=hour is None,
                        skip_if_busy=True,
                    )
                    if not any(r.get("blocked") for r in reports):
                        _watch.pop(pid, None)  # the sweep retried everything
                    logger.info(
                        "curated nightly %s: %s",
                        pid,
                        [(r.get("dry_run"), r.get("status") or r.get("error")) for r in reports],
                    )
            elif enabled and cont:
                for pid in await db.list_curated_nightly_patients():
                    if _stopping.is_set():
                        break
                    try:
                        await continuous_tick(
                            db,
                            pid,
                            settle_s=settle,
                            chunk=chunk,
                            max_runs=max_runs,
                            grace_until=grace_until,
                        )
                    except Exception:
                        logger.exception("curated auto %s: tick failed", pid)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("curated loop iteration failed")
        await asyncio.sleep(poll_s or (cont_poll if cont else 300))


# ── MCP tools ─────────────────────────────────────────────────────────


async def curated_folder_run(
    ctx: Context,
    patient_slug: str | None = None,
    dry_run: bool = True,
    max_docs: int | None = None,
    max_actions: int = 40,
) -> str:
    """Process a curated patient Drive folder under its README.md contract (#562).

    Files new documents from 00-inbox (and loose root files): OCR on the local
    model, classification from the text, rename to the README standard, file
    flat into the README's folders with a Markdown sidecar, quarantine exact
    duplicates. Never creates folders, never deletes, verifies every write and
    aborts on the first failure. Runs in the background — poll
    curated_folder_status. A real run (dry_run=false) requires a clean dry run
    from the last 24 hours.

    Args:
        patient_slug: Patient to process (default: the caller's patient).
        dry_run: True (default) reads, OCRs and classifies but writes nothing.
        max_docs: Documents per run (default 20 oncology / 8 general).
        max_actions: File-action budget per run (default 40).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    result = await start_run(
        _get_db(ctx),
        pid,
        dry_run=dry_run,
        trigger="mcp",
        max_docs=max_docs,
        max_actions=max_actions,
    )
    return json.dumps(result)


# ── read model for the MCP read tools (#603) ─────────────────────────


def _parse_drive_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
    except ValueError:
        return None


async def index_patient(db, pid: str, *, force_ids: set[int] | None = None) -> dict:
    """Local-only wrapper: the index and its fact extraction never reach the cloud."""
    from oncofiles.cloud_guard import local_only

    try:  # hydrate the context cache: facts need the patient's sex (HGB, #659)
        from oncofiles import patient_context

        await patient_context.load_from_db(db.db, pid)
    except Exception:
        logger.warning("curated index: patient context not loaded for %s", pid)
    async with patient_lock(pid):  # never alongside a run or backfill (#612)
        with local_only("curated index + fact extraction"):
            return await _index_patient(db, pid, force_ids=force_ids)


async def _index_patient(db, pid: str, *, force_ids: set[int] | None = None) -> dict:
    """Mirror a patient's curated folder into the documents table (Drive reads only).

    One row per filed source (``file_id = gdrive:<id>``), sidecar text as its
    pages, metadata from the sidecar front matter. Rows whose file left the
    filed folders are soft-deleted. Never uploads, never calls a cloud model.
    """
    from oncofiles.curated import indexer
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.models import Document
    from oncofiles.server import _create_patient_clients

    clients = await _create_patient_clients(db, pid)
    if not clients or clients[0] is None:
        return {"status": "skipped", "reason": "Drive not connected"}
    gdrive, folder_id = clients[0], clients[3]
    state = await db.curated_index_state(patient_id=pid)
    known = {
        g: ("" if force_ids and s["id"] in force_ids else s["sidecar_modified"])
        for g, s in state.items()
        if not s["deleted"]
    }  # a forced document counts as changed: re-parsed with the current parser

    def _blocking():
        own = gdrive.dedicated_copy()
        try:
            known_md5 = {g: s.get("source_md5", "") for g, s in state.items() if not s["deleted"]}
            return indexer.scan(DriveOps(own), folder_id, known=known, known_md5=known_md5)
        finally:
            own.close()

    from oncofiles.curated.facts_rules import Vocabulary

    t0 = time.monotonic()
    scanned = await asyncio.to_thread(_blocking)
    _readme_births[pid] = set(scanned.birth_dates)  # DOB guard of the LLM extractors (#726)
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    stats = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0, "pages": 0, "errors": 0,
             "facts": 0}  # fmt: skip
    seen: set[str] = set()
    ids: list[int] = []
    for rec in scanned.records:
        seen.add(rec.gdrive_id)
        existing = state.get(rec.gdrive_id)
        meta = None
        text_fields = None
        if not rec.unchanged:
            meta = json.dumps(
                {
                    "source": "curated",
                    "folder": rec.folder,
                    "curated": {
                        "sidecar_id": rec.sidecar_id,
                        "sidecar_modified": rec.sidecar_modified,
                        "contract_version": scanned.contract_version,
                        "index_version": indexer.INDEX_VERSION,
                        "sidecar_sha": rec.sidecar_sha,
                        "source_md5": rec.source_md5,
                    },
                    **rec.metadata,
                },
                ensure_ascii=False,
            )
            text_fields = {
                "original_filename": rec.original_filename,
                "document_date": rec.document_date.isoformat() if rec.document_date else None,
                "institution": rec.institution,
                "description": rec.description,
                "ai_summary": rec.summary,
                "ai_tags": json.dumps(rec.tags, ensure_ascii=False),
            }
        try:
            if existing:
                doc_id = existing["id"]
            else:
                doc = await db.insert_document(
                    Document(
                        file_id=rec.file_id,
                        filename=rec.filename,
                        original_filename=rec.original_filename,
                        document_date=rec.document_date,
                        institution=rec.institution,
                        category=rec.category,
                        description=rec.description,
                        mime_type=rec.mime_type,
                        size_bytes=rec.size_bytes,
                        gdrive_id=rec.gdrive_id,
                        gdrive_modified_time=_parse_drive_time(rec.modified),
                        sync_state="synced",
                    ),
                    patient_id=pid,
                )
                doc_id = doc.id
            indexer.register_curated_ids([doc_id])  # before any other job can see it
            # The row is keyed by gdrive_id (UNIQUE) and pages by (doc, page),
            # so a re-run never duplicates. The "sidecar seen" marker inside
            # structured_metadata is written LAST: if a deploy interrupts this
            # document, the next index sees no marker and redoes it whole.
            await db.update_curated_document(
                doc_id,
                patient_id=pid,
                filename=rec.filename,
                category=rec.category.value,
                mime_type=rec.mime_type,
                size_bytes=rec.size_bytes,
                gdrive_modified_time=rec.modified,
                structured_metadata=None,
                text_fields=text_fields,
            )
            if not rec.unchanged:
                await db.delete_ocr_pages(doc_id)
                for n, page in enumerate(rec.pages, start=1):
                    if page:
                        await db.save_ocr_page(doc_id, n, page, indexer.INDEX_ENGINE)
                        stats["pages"] += 1
                entities = await _link_entities(db, pid, doc_id, rec)
                stats["facts"] += await _extract_facts(db, pid, doc_id, rec, entities, vocab)
                await db.update_structured_metadata(doc_id, meta)  # commit marker, last
            ids.append(doc_id)
            key = "unchanged" if rec.unchanged else ("updated" if existing else "created")
            stats[key] += 1
        except Exception:
            stats["errors"] += 1
            logger.exception("curated index: %s failed for %s", rec.filename, pid)
    for gdrive_id, row in state.items():
        if gdrive_id not in seen and not row["deleted"]:
            await db.delete_document(row["id"], patient_id=pid)  # soft delete
            stats["removed"] += 1
    try:
        stats["verification"] = await _corroborate_and_measure(db, pid)
    except Exception:
        logger.exception("curated index: corroboration failed for %s", pid)
    stats.update(
        status="ok" if not stats["errors"] else "partial",
        documents=len(ids),
        seconds=round(time.monotonic() - t0, 1),
    )
    logger.info("curated index %s: %s", pid, stats)
    return stats


_CATEGORY_TITLE = {
    "labs": "Laboratórne výsledky", "report": "Lekárska správa", "imaging": "Zobrazovanie",
    "pathology": "Patológia", "genetics": "Genetika", "surgery": "Operácia / zákrok",
    "discharge": "Prepúšťacia správa", "referral": "Odporúčanie", "prescription": "Lieky",
    "chemo_sheet": "Liečba", "reference": "Referenčný materiál", "advocate": "Poznámka advokáta",
    "other": "Dokument",
}  # fmt: skip
_ENCOUNTER_KIND = {
    "labs": "lab_draw", "imaging": "imaging", "surgery": "procedure",
    "chemo_sheet": "chemo_cycle", "discharge": "stay", "pathology": "procedure",
}  # fmt: skip
_LOCAL_ENGINES = ("qwen", "local", "google-export", "utf-8", "pymupdf")


def _known(token: str | None) -> str | None:
    return token if token and token != "Unknown" else None


_TITLES = {"mudr", "mgr", "phd", "doc", "prof", "ing", "bc", "mph", "mha", "prim", "dr", "phdr",
           "rndr", "mba", "csc", "drsc", "mudrmgr"}  # fmt: skip
_NO_ENCOUNTER = ("reference", "advocate", "other", "prescription")


def _surname(token: str) -> str:
    """'MUDrMgrZuzanaMinarikova' → 'Minarikova'; 'Minarikova' stays (#635)."""
    words = re.findall(r"[A-ZÁČĎÉÍĽĹŇÓÔŔŠŤÚÝŽ][a-záäčďéíľĺňóôŕšťúýž]+|[A-Z]{2,}(?![a-z])", token)
    words = [w for w in words if w.lower() not in _TITLES]
    return words[-1] if words else token


def _person(display: str | None) -> str | None:
    """'prim. MUDr. X Y (primár OKB — podpisuje …)' → 'prim. MUDr. X Y'."""
    if not display or not isinstance(display, str):
        return None
    return (
        re.split(r"\s[(—–]|\s-\s|,\s(?=[a-záčďéíľňóôŕšťúýž])", display, maxsplit=1)[0].strip()
        or None
    )


def _dept_ok(token: str | None) -> str | None:
    return token if token and re.fullmatch(r"[A-Z][A-Za-z]{2,40}", token) else None


async def _link_entities(db, pid: str, doc_id: int, rec) -> dict:
    """Organization, department, practitioner, encounter, title, Drive link (#603)."""
    fields = rec.filename.rsplit(".", 1)[0].split("-") if "." in rec.filename else []
    std = len(fields) >= 6 and fields[0].isdigit()
    org_code = _known(rec.institution) or (_known(fields[2]) if std else None)
    doctor = _known(fields[4]) if std else None
    doctor = _surname(doctor) if doctor else None
    dept_code = _dept_ok(_known(fields[5])) if std else None
    # organizations are SHARED across patients: never fill their names from one
    # patient's README (cross-tenant text; security review of #637). Names come
    # only from an admin-curated list (not yet built).
    org_id = await db.upsert_organization(org_code) if org_code else None
    dept_id = await db.upsert_department(org_id, dept_code) if org_id and dept_code else None
    prac_id = None
    if doctor and org_code != "PacientAdvokat":
        prac_id = await db.upsert_practitioner(
            patient_id=pid,
            surname_token=doctor,
            display_name=_person(rec.metadata.get("doctor")),
            organization_id=org_id,
            department_id=dept_id,
        )
    enc_id = None
    day = rec.document_date.isoformat() if rec.document_date else None
    cat = rec.category.value
    if day and org_code and cat not in _NO_ENCOUNTER:
        kind = _ENCOUNTER_KIND.get(cat, "visit")
        enc_id = await db.upsert_encounter(
            patient_id=pid,
            natural_key=f"{day}|{org_code}|{dept_code or '-'}|{kind}",
            start_at=day,
            kind=kind,
            title=" · ".join(x for x in (org_code, dept_code, doctor) if x),
            organization_id=org_id,
            department_id=dept_id,
            practitioner_id=prac_id,
        )
    title = " · ".join(
        x for x in (_CATEGORY_TITLE.get(cat, "Dokument"), org_code, doctor or dept_code, day) if x
    )
    await db.set_document_entities(
        doc_id,
        patient_id=pid,
        title=title,
        source_url=f"https://drive.google.com/file/d/{rec.gdrive_id}/view",
        encounter_id=enc_id,
        practitioner_id=prac_id,
        organization_id=org_id,
        department_id=dept_id,
    )
    return {"encounter_id": enc_id, "practitioner_id": prac_id,
            "organization_id": org_id, "department_id": dept_id}  # fmt: skip


async def _extract_facts(db, pid: str, doc_id: int, rec, entities: dict, vocab) -> int:
    """Rule-based lab facts from the sidecar text (no model call)."""
    from oncofiles.curated.facts_rules import INLINE_CATEGORIES, extract_lab_facts

    engine = str(rec.metadata.get("ocr_engine") or "")
    local = any(k in engine.lower() for k in _LOCAL_ENGINES)
    unreliable = rec.metadata.get("handwritten") is True or not local
    day = rec.document_date.isoformat() if rec.document_date else None
    facts, extractor = extract_lab_facts(
        rec.pages,
        vocab,
        unreliable=unreliable,
        report_date=day,
        inline=rec.category.value in INLINE_CATEGORIES,
        sex=_patient_sex(pid),
    )
    source_reason = "handwritten" if rec.metadata.get("handwritten") is True else "cowork_ocr"
    # Always replace, even with nothing: a document whose new extraction finds
    # no facts must lose the machine rows an older parser version made (#633).
    extractor = extractor or "rule:none"
    day = rec.document_date.isoformat() if rec.document_date else None
    system = "curated_sidecar" if local else "cowork_sidecar"
    for f in facts:
        f["occurred_at"] = f["occurred_at"] or day
        f.update(entities)
        meta = {**(f.get("metadata") or {}), "source_system": system, "ocr_engine": engine or None}
        if "unreliable_source" in meta.get("unverified_reasons", []):
            meta["unverified_reasons"] = sorted(
                {source_reason} | set(meta["unverified_reasons"]) - {"unreliable_source"}
            )
        f["metadata"] = meta
    res = await db.replace_document_facts(
        patient_id=pid, document_id=doc_id, facts=facts, extracted_by=extractor
    )
    return res["added"]


def _patient_sex(pid: str) -> str | None:
    try:
        from oncofiles.patient_context import get_context

        return (get_context(pid) or {}).get("sex") or None
    except Exception:
        return None


async def _corroborate_and_measure(db, pid: str) -> dict:
    """Flag header/draw date conflicts, date report quotes by the draw they
    quote, then measure unverified (#659, #672). Runs at the end of every
    index (startup, curated_index, after a run or re-OCR), so flags that a
    re-extraction resets are re-applied in the same call."""
    from oncofiles.curated.facts_rules import (
        _fact_meta,
        corroborations,
        date_conflicts,
        range_window,
        unverified_metric,
    )

    confirmed = await _apply_rereads(db, pid)
    rows = await db.fact_verification_rows(patient_id=pid)
    by_id = {r["id"]: r for r in rows}
    conflicts = 0
    for fact_id, other_day in date_conflicts(rows):  # #672: two dates, neither wins
        try:
            meta = json.loads(by_id[fact_id].get("metadata_json") or "{}")
        except ValueError:
            meta = {}
        meta["unverified_reasons"] = sorted({*meta.get("unverified_reasons", []), "date_conflict"})
        meta["date_conflict_with"] = other_day
        if await db.flag_date_conflict(patient_id=pid, fact_id=fact_id, metadata=meta):
            conflicts += 1
    if conflicts:  # a conflicted block is no longer a draw anything may be dated by
        rows = await db.fact_verification_rows(patient_id=pid)
        by_id = {r["id"]: r for r in rows}
    done = 0
    for fact_id, draw_day, draw_fact in corroborations(rows):
        try:
            meta = json.loads(by_id[fact_id].get("metadata_json") or "{}")
        except ValueError:
            meta = {}
        meta.pop("unverified_reasons", None)
        meta.update(date_source="corroborated", corroborated_by=draw_fact,
                    quoted_on=(by_id[fact_id]["occurred_at"] or "")[:10])  # fmt: skip
        # the date now comes from the draw, and so does its precision (#726): a
        # draw under a printed range lends its range, an exact draw none
        for k in ("date_precision", "date_range_start", "date_range_end", "date_range_open"):
            meta.pop(k, None)
        if window := range_window(_fact_meta(by_id.get(draw_fact) or {})):
            meta.update(date_precision="range", date_range_start=window[0],
                        date_range_end=window[1])  # fmt: skip
        if await db.corroborate_fact(
            patient_id=pid, fact_id=fact_id, occurred_at=draw_day, metadata=meta
        ):
            done += 1
    metric = unverified_metric(await db.fact_verification_rows(patient_id=pid))
    return {"corroborated": done, "date_conflicts": conflicts, "reread_applied": confirmed,
            **metric}  # fmt: skip


async def _apply_rereads(db, pid: str) -> int:
    """Re-apply stored verify-only re-read results to the current facts (#678).
    Runs in every index, so a re-extraction never loses a confirmation, and a
    result whose sidecar or source changed is void (its reasons come back)."""
    from oncofiles.curated.reread_verify import apply_plan, loads

    rereads = await db.valid_fact_rereads(patient_id=pid)
    if not rereads:
        return 0
    rows = await db.fact_verification_rows(patient_id=pid)
    applied = 0
    for doc_id, result in rereads.items():
        facts = [{**r, "meta": loads(r.get("metadata_json"))} for r in rows
                 if r["source_document_id"] == doc_id]  # fmt: skip
        for fact_id, verification, meta in apply_plan(result, facts):
            meta["reread_id"] = result["id"]
            if await db.apply_reread_fact(patient_id=pid, fact_id=fact_id,
                                          verification=verification, metadata=meta):  # fmt: skip
                applied += 1
    return applied


async def _index_quietly(db, pid: str) -> None:
    try:
        await index_patient(db, pid)
    except Exception:
        logger.exception("curated index failed for %s", pid)


_reocr_jobs: dict[str, dict] = {}


_NOT_OCR = ("application/vnd.google-apps.", "application/vnd.openxmlformats")


async def _reocr_targets(
    db, pid: str, document_ids: list[int] | None, *, only_with_facts: bool = True
) -> list[dict]:
    """Documents worth reading again: lab sheets first, then most unverified
    facts first (#674). By default only those with unverified facts (the
    benchmark, #658). Listed ``document_ids`` are marked explicit: only they
    may be read again after an earlier re-OCR or a deliberate restore."""
    from oncofiles.curated.reocr import needs_reocr

    out = []
    rows = sorted(
        await db.curated_reocr_candidates(patient_id=pid),
        key=lambda r: (r.get("category") != "labs", -(r.get("unverified_facts") or 0)),
    )
    for r in rows:
        meta = r["structured_metadata"]
        cur = meta.get("curated") or {}
        if document_ids and r["id"] not in document_ids:
            continue
        if not document_ids and only_with_facts and not r.get("unverified_facts"):
            continue
        if (r.get("mime_type") or "").startswith(_NOT_OCR):
            continue  # exported or spreadsheet text, not a scan
        reasons = needs_reocr(meta, ["[?]"] if r["uncertain"] else [])
        if not reasons or not cur.get("sidecar_id") or not r.get("gdrive_id"):
            continue
        out.append({
            "document_id": r["id"], "filename": r["filename"], "gdrive_id": r["gdrive_id"],
            "sidecar_id": cur["sidecar_id"], "mime": r.get("mime_type") or "",
            "engine": str(meta.get("ocr_engine") or meta.get("extraction") or ""),
            "reasons": reasons, "unverified_facts": r.get("unverified_facts") or 0,
            "category": r.get("category"), "explicit": bool(document_ids),
        })  # fmt: skip
    return out


async def _reocr_job(db, pid: str, docs: list[dict], *, dry_run: bool, passes: int) -> None:
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated.facts_rules import Vocabulary
    from oncofiles.curated.reocr import run_reocr
    from oncofiles.server import _create_patient_clients

    job = _reocr_jobs[pid]
    # Persisted (#667): a deploy or restart turns this row 'interrupted' at
    # startup instead of the job silently vanishing with the process.
    row_id = await db.insert_curated_run(patient_id=pid, dry_run=dry_run, trigger="reocr")
    try:
        async with patient_lock(pid):  # never alongside a run or an index (#612)
            clients = await _create_patient_clients(db, pid)
            if not clients or clients[0] is None:
                raise RuntimeError("Drive not connected")
            gdrive, folder_id = clients[0], clients[3]
            vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
            from oncofiles import config
            from oncofiles.curated.drive_ops import DriveOps
            from oncofiles.curated.reocr import reread_isolated

            def _blocking():
                own = gdrive.dedicated_copy()
                try:
                    with local_only("curated re-OCR"):
                        kwargs = dict(dry_run=dry_run, passes=passes, now=datetime.now(UTC),
                                      stop=_stopping.is_set, reader=reread_isolated,
                                      rss_limit_mb=config.REOCR_RSS_LIMIT_MB)  # fmt: skip
                        if dry_run:
                            return run_reocr(DriveOps(own), folder_id, docs, vocab, **kwargs)
                        with own.contract_writes():
                            return run_reocr(DriveOps(own), folder_id, docs, vocab, **kwargs)
                finally:
                    own.close()

            report = (await asyncio.to_thread(_blocking)).to_dict()
        job.update(state="done", report=report)
        if not dry_run and any(d["result"] == "rewritten" for d in report["documents"]):
            job["index"] = await index_patient(db, pid)  # new sidecars → new facts
    except Exception as exc:
        logger.exception("curated re-OCR failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__)
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
        if job.get("pre_store"):  # the safety copy is part of the record (#573)
            job["report"] = {**(job.get("report") or {}), "pre_store": job["pre_store"]}
        try:
            await db.finish_curated_run(
                row_id, patient_id=pid,
                status="ok" if job.get("state") == "done" else "failed",
                message=job.get("error") or "", report=job.get("report") or {},
            )  # fmt: skip
        except Exception:
            logger.exception("could not persist the re-OCR job for %s", pid)
        await _audit_real_job(db, pid, job)


def _busy(pid: str) -> bool:
    return _reocr_jobs.get(pid, {}).get("state") == "running" or (
        pid in _runs and _runs[pid].get("state") == "running"
    )


def _public_job(pid: str) -> dict:
    return {k: v for k, v in _reocr_jobs[pid].items() if k != "task"}


# Dry runs leave no activity_log row (#573 infra review); they are only counted.
_dry_runs: dict[str, int] = {"reread": 0, "reocr": 0}


def _global_slot_refusal(kind: str) -> str | None:
    """Hook for the global re-read slot cap (#685, LM Studio slots).

    TODO(#685): once its helper is on main, refuse here when every re-read
    slot is taken (return the reason; the caller answers busy / 409).
    """
    return None


async def _launch(db, pid: str, job: dict, factory, *, dry_run: bool, busy: str) -> dict:
    """Reserve the patient's job slot, make the pre-store copy for a real
    run, then start ``factory()`` in the background.

    The slot is reserved before any await, so two requests can never both
    start a job for one patient. A real run without a verified safety copy
    of the DB is refused and writes nothing (the slot is released).
    """
    if _busy(pid):
        return {"error": busy}
    refusal = _global_slot_refusal(job["kind"])
    if refusal:
        return {"error": f"busy: {refusal}"}
    previous = _reocr_jobs.get(pid)
    job["state"] = "running"
    _reocr_jobs[pid] = job
    if dry_run:
        _dry_runs[job["kind"]] = _dry_runs.get(job["kind"], 0) + 1
    else:
        from oncofiles.pre_store import PreStoreError, make_copy

        try:
            job["pre_store"] = await asyncio.to_thread(make_copy, db, job["kind"])
        except Exception as exc:
            if previous is not None:
                _reocr_jobs[pid] = previous
            else:
                _reocr_jobs.pop(pid, None)
            reason = str(exc) if isinstance(exc, PreStoreError) else type(exc).__name__
            logger.error("pre-store copy failed for %s (%s): job refused", pid[:8], reason)
            return {"error": f"refused: pre-store copy failed ({reason}); nothing was written"}
    job["task"] = asyncio.create_task(factory())
    return _public_job(pid)


async def _audit_real_job(db, pid: str, job: dict) -> None:
    """One activity_log row per REAL job (#573): who, which patient (#663:
    the resolved pid), ids, the pre-store copy and result counts. Counts
    only — never a value or evidence text."""
    if job.get("dry_run") is not False:
        return
    from collections import Counter

    from oncofiles.models import ActivityLogEntry

    kind = job.get("kind") or "reocr"
    docs = [d for d in (job.get("report") or {}).get("documents") or [] if isinstance(d, dict)]
    counts: dict = {"results": dict(Counter(str(d.get("result")) for d in docs))}
    for key in ("confirmed", "date_unconfirmed", "unconfirmed", "candidates"):
        if any(key in d for d in docs):
            counts[key] = sum(int(d.get(key) or 0) for d in docs)
    actor = str(job.get("actor") or "unknown")[:120]
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id=f"curated-{kind}",
                agent_id=actor,
                tool_name="curated_reread_verify" if kind == "reread" else "curated_reocr",
                input_summary=json.dumps(
                    {
                        "by": actor,
                        "document_ids": [d.get("document_id") for d in job.get("documents") or []],
                        "dry_run": False,
                        "pre_store": job.get("pre_store"),
                        "started_at": job.get("started_at"),
                    }
                ),  # fmt: skip
                output_summary=json.dumps({"state": job.get("state"), **counts}),
                status="ok" if job.get("state") == "done" else "error",
                error_message=job.get("error"),
                tags=json.dumps(["curated", kind, "real", "pre-store"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for a real %s job", kind)


async def start_reocr(
    db,
    pid: str,
    *,
    dry_run: bool = True,
    limit: int = 3,
    document_ids: list[int] | None = None,
    passes: int = 2,
    only_with_facts: bool = True,
    actor: str = "mcp",
) -> dict:
    """Start a re-OCR job in the background (#660). Shared by the MCP tool and
    the admin dashboard API (#573); the caller has already resolved ``pid``.
    A real run first makes a verified safety copy of the DB (``pre_store``)."""
    busy = "busy: a run or re-OCR is in progress for this patient"
    if _busy(pid):
        return {"error": busy}
    targets = await _reocr_targets(db, pid, document_ids, only_with_facts=only_with_facts)
    docs = targets[: max(0, min(limit, 25))]
    if not docs:
        return {"status": "nothing to do", "candidates": len(targets)}
    job = {
        "state": "running", "kind": "reocr", "dry_run": dry_run, "passes": passes,
        "started_at": datetime.now(UTC).isoformat(), "candidates": len(targets),
        "actor": actor,
        "documents": [
            {k: d[k] for k in ("document_id", "filename", "category", "reasons",
                               "unverified_facts")}
            for d in docs
        ],
    }  # fmt: skip
    return await _launch(
        db, pid, job, lambda: _reocr_job(db, pid, docs, dry_run=dry_run, passes=passes),
        dry_run=dry_run, busy=busy,
    )  # fmt: skip


async def curated_reocr(
    ctx: Context,
    patient_slug: str | None = None,
    dry_run: bool = True,
    limit: int = 3,
    document_ids: list[int] | None = None,
    passes: int = 2,
    only_with_facts: bool = True,
) -> str:
    """Read uncertain documents again with the LOCAL model (#660): Cowork-era
    transcriptions and pages with [?]. Two vision passes with high reasoning at
    two resolutions (a PDF text layer is used as-is); digits are accepted only
    where both passes agree. Dry run (default) writes nothing and previews how
    many values would become machine-verified. A real run backs the old sidecar
    up to the logs folder, then updates it in place, then re-indexes. Runs in
    the background; poll curated_reocr_status. Slow: minutes per page.
    By default only documents with unverified facts are chosen, most first
    (only_with_facts=false: every Cowork-era or uncertain document).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    out = await start_reocr(
        _get_db(ctx), pid, dry_run=dry_run, limit=limit, document_ids=document_ids,
        passes=passes, only_with_facts=only_with_facts,
    )  # fmt: skip
    return json.dumps(out, ensure_ascii=False)


def _reread_parallel() -> int:
    """Concurrent vision passes for the next re-read (#593): at most
    REOCR_PARALLEL_PASSES, and only 1 while another patient's re-OCR or
    re-read job runs, so re-reads never hold more than 3 LM Studio slots
    (2 jobs x 1, or 1 job x 2); the 4th slot stays for curation and users."""
    from oncofiles import config

    running = sum(1 for j in _reocr_jobs.values() if j.get("state") == "running")
    return max(1, min(int(config.REOCR_PARALLEL_PASSES), 2 if running <= 1 else 1))


from oncofiles.curated.facts_rules import INLINE_CATEGORIES as _REPORT_LIKE  # noqa: E402


def _reread_one(drive, d: dict, old: list[dict], vocab, *, fingerprint: bool = False) -> dict:
    """Blocking. Re-read one document locally and assess its Cowork-era facts
    (#678). Reads Drive only; never writes. Returns counts and the result;
    with ``fingerprint`` also the masked shape of the re-read text (#712)."""
    import hashlib

    from oncofiles.curated import reread_verify as rv
    from oncofiles.curated.facts_rules import extract_lab_facts
    from oncofiles.curated.reocr import reread_isolated

    res = {"document_id": d["id"], "confirmed": 0, "date_unconfirmed": 0, "unconfirmed": 0,
           "candidates": 0, "result": "previewed", "error": None}  # fmt: skip
    cur = (d.get("structured_metadata") or {}).get("curated") or {}
    raw_side = drive.download(cur.get("sidecar_id") or "")
    source = drive.download(d["gdrive_id"])
    sha, md5 = hashlib.sha256(raw_side).hexdigest(), hashlib.md5(source).hexdigest()  # noqa: S324
    if not cur.get("sidecar_sha") or sha != cur.get("sidecar_sha"):
        return {**res, "result": "skipped", "error": "sidecar changed since the index; index first"}
    if cur.get("source_md5") and md5 != cur.get("source_md5"):
        return {**res, "result": "skipped", "error": "source changed since the index"}
    from oncofiles import config
    from oncofiles.curated.indexer import parse_sidecar

    _, old_pages = parse_sidecar(raw_side.decode("utf-8", errors="replace"))
    # #593 adaptive: a second pass only on pages a confirmable value can be on
    # (the same page rule ``assess`` applies below); never narrower
    wanted = (
        rv.pages_with_candidates(old, old_page_count=len(old_pages))
        if config.REOCR_PASSES_MODE == "adaptive"
        else None
    )
    rr = reread_isolated(d["filename"], source, d.get("mime_type") or "", passes=2,
                         doc_ref=d["id"], parallel=_reread_parallel(),
                         second_pass_pages=wanted, purpose="reread")  # fmt: skip
    if rr.error or not any(p.strip() for p in rr.pages):
        return {**res, "result": "failed", "error": rr.error or "no text"}
    new, _ = extract_lab_facts(rr.pages, vocab, report_date=str(d.get("document_date") or "")[:10]
                               or None, inline=d.get("category") in _REPORT_LIKE)  # fmt: skip
    assessment = rv.assess(old, new, rr.pages, old_page_count=len(old_pages))
    res.update({k: len(v) for k, v in assessment.items()}, seconds=rr.seconds,
               parallel=rr.parallel, pages=len(rr.pages), method=rr.method,
               dpi=rr.dpi, passes_per_page=rr.passes_per_page,
               skipped_second_pass_pages=rr.skipped_second_pass_pages,
               diagnostics=rv.diagnostics(old, new, old_page_count=len(old_pages)),
               candidates=sum(len(v) for v in assessment.values()),
               engine=rr.engine, sidecar_sha=sha, source_md5=cur.get("source_md5") or md5,
               stored=rv.result_keys(assessment, {f["id"]: f for f in old},
                                     method=rr.method))  # fmt: skip
    if fingerprint:
        res["fingerprint"] = rv.fingerprint(rr.pages, vocab)
    return res


async def _reread_job(
    db, pid: str, docs: list[dict], *, dry_run: bool, fingerprint: bool = False
) -> None:
    from oncofiles import config
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.facts_rules import Vocabulary
    from oncofiles.curated.reread_verify import loads
    from oncofiles.memory import get_rss_mb
    from oncofiles.server import _create_patient_clients

    job = _reocr_jobs[pid]
    row_id = await db.insert_curated_run(patient_id=pid, dry_run=dry_run, trigger="reread")
    report: list[dict] = []
    try:
        async with patient_lock(pid):
            clients = await _create_patient_clients(db, pid)
            if not clients or clients[0] is None:
                raise RuntimeError("Drive not connected")
            gdrive = clients[0]
            vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
            rows = await db.fact_verification_rows(patient_id=pid)
            for d in docs:
                if _stopping.is_set():
                    report.append(
                        {"document_id": d["id"], "result": "skipped", "error": "stopping"}
                    )
                    continue
                if (rss := get_rss_mb()) > config.REOCR_RSS_LIMIT_MB:
                    report.append({"document_id": d["id"], "result": "skipped",
                                   "error": f"rss {rss:.0f} MB"})  # fmt: skip
                    continue
                old = [{**r, "meta": loads(r.get("metadata_json"))} for r in rows
                       if r["source_document_id"] == d["id"]]  # fmt: skip

                def _blocking(d=d, old=old):
                    own = gdrive.dedicated_copy()
                    try:
                        with local_only("curated verify-only re-read"):
                            return _reread_one(
                                DriveOps(own), d, old, vocab, fingerprint=fingerprint and dry_run
                            )
                    finally:
                        own.close()

                t0 = time.monotonic()
                try:
                    res = await asyncio.to_thread(_blocking)
                except Exception as exc:
                    logger.exception("re-read of doc %s failed", d["id"])
                    res = {"document_id": d["id"], "result": "failed", "error": type(exc).__name__}
                res["seconds"] = round(time.monotonic() - t0, 1)
                stored = res.pop("stored", None)
                if not dry_run and stored and res["result"] == "previewed":
                    await db.insert_fact_reread(
                        patient_id=pid, document_id=d["id"], sidecar_sha=res.pop("sidecar_sha"),
                        source_md5=res.pop("source_md5"), engine=res.get("engine") or "",
                        result=stored,
                    )  # fmt: skip
                    res["result"] = "stored"
                res.pop("sidecar_sha", None), res.pop("source_md5", None)
                report.append(res)
            if not dry_run and any(r.get("result") == "stored" for r in report):
                job["verification"] = await _corroborate_and_measure(db, pid)
        job.update(state="done", report={"dry_run": dry_run, "documents": report})
    except Exception as exc:
        logger.exception("verify-only re-read failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__)
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
        if job.get("pre_store"):  # the safety copy is part of the record (#573)
            job["report"] = {**(job.get("report") or {}), "pre_store": job["pre_store"]}
        try:
            await db.finish_curated_run(
                row_id, patient_id=pid,
                status="ok" if job.get("state") == "done" else "failed",
                message=job.get("error") or "", report=_without_fingerprints(job.get("report")),
            )  # fmt: skip
        except Exception:
            logger.exception("could not persist the re-read job for %s", pid)
        await _audit_real_job(db, pid, job)


async def start_reread(
    db,
    pid: str,
    document_ids: list[int],
    *,
    dry_run: bool = True,
    actor: str = "mcp",
    fingerprint: bool = False,
) -> dict:
    """Start a verify-only re-read in the background (#678). Shared by the MCP
    tool and the admin dashboard API (#573); the caller has resolved ``pid``.
    Explicit ids only, max 10; ids of another patient are ignored. A real
    run first makes a verified safety copy of the DB (``pre_store``)."""
    ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))[:10]
    if not ids:
        return {"error": "document_ids is required"}
    busy = "busy: a run, re-OCR or re-read is in progress"
    if _busy(pid):
        return {"error": busy}
    docs = [r for r in await db.curated_reocr_candidates(patient_id=pid) if r["id"] in ids]
    if not docs:
        return {"status": "nothing to do"}
    job = {
        "state": "running", "kind": "reread", "dry_run": dry_run,
        "started_at": datetime.now(UTC).isoformat(), "actor": actor,
        "documents": [{"document_id": d["id"], "filename": d["filename"],
                       "category": d.get("category")} for d in docs],
    }  # fmt: skip
    return await _launch(
        db, pid, job,
        lambda: _reread_job(db, pid, docs, dry_run=dry_run, fingerprint=fingerprint and dry_run),
        dry_run=dry_run, busy=busy,
    )  # fmt: skip


def _without_fingerprints(report: dict | None) -> dict:
    """The persisted copy of a re-read report: fingerprints stay in memory only."""
    if not report:
        return {}
    docs = [{k: v for k, v in d.items() if k != "fingerprint"}
            for d in report.get("documents") or []]  # fmt: skip
    return {**report, "documents": docs}


async def curated_reread_verify(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    dry_run: bool = True,
    fingerprint: bool = False,
) -> str:
    """Confirm Cowork-era values by a LOCAL re-read, without rewriting anything (#678).

    Reads each listed document again with the local model (two passes) and
    parses it with the same rules. A Cowork-era value loses its ``cowork_ocr``
    reason only when the re-read shows the same code and value on the same
    page (as often as the old text) and its date is printed there too;
    otherwise it stays unverified (``date_unconfirmed`` when only the date is
    missing). Nothing is written to Drive. Dry run (default) only reports
    counts. Explicit ``document_ids`` only (max 10). Runs in the background;
    poll curated_reocr_status. Slow: minutes per page.

    Args:
        fingerprint: Dry run only. Each document's report also carries the
            masked shape of the NEW re-read text (digits → 9, words → a's
            except lab codes, aliases and units; max 80 lines, plus line and
            separator counts), to diagnose a layout the parser misses (#712).
            Kept in memory for curated_reocr_status, never stored.
    """
    if fingerprint and not dry_run:
        return json.dumps({"error": "fingerprint is only allowed with dry_run=true"})
    pid = await _resolve_patient_id(patient_slug, ctx)
    out = await start_reread(
        _get_db(ctx), pid, document_ids, dry_run=dry_run, fingerprint=fingerprint
    )
    return json.dumps(out, ensure_ascii=False)


async def reocr_status(db, pid: str) -> dict:
    """State and report of the last re-OCR / re-read job (#660, #667)."""
    job = _reocr_jobs.get(pid)
    if not job:
        last = await db.get_last_reocr_job(patient_id=pid)  # survives a restart (#667)
        targets = await _reocr_targets(db, pid, None)
        if last:
            return {"state": last["status"], "job": last, "candidates": len(targets)}
        return {"state": "none", "candidates": len(targets)}
    return {k: v for k, v in job.items() if k != "task"}


_VIEW_JOB = (
    "state", "kind", "dry_run", "passes", "started_at", "finished_at", "candidates", "pre_store",
)  # fmt: skip
_VIEW_PLANNED = ("document_id", "filename", "category", "reasons", "unverified_facts")
_VIEW_RESULT = (
    "document_id", "filename", "result", "confirmed", "date_unconfirmed", "unconfirmed",
    "candidates", "seconds", "engine", "method", "pages", "uncertain_before",
    "uncertain_after", "facts_machine", "facts_unverified",
)  # fmt: skip


def _counts(d) -> dict:
    """Only numbers (and a reason -> count map) survive: no text can leak."""
    if not isinstance(d, dict):
        return {}
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, int | float):  # bool is an int
            out[k] = v
        elif isinstance(v, dict) and all(isinstance(x, int) for x in v.values()):
            out[k] = {str(rk): rv for rk, rv in v.items()}
    return out


def admin_job_view(status: dict) -> dict:
    """``reocr_status`` reduced for the admin dashboard (#573): states, counts,
    filenames and reason names. Never a fact value, date or evidence text —
    ``lost`` (re-OCR "CODE value date" strings) becomes ``lost_count``."""
    job = status.get("job") if isinstance(status.get("job"), dict) else status
    if job is not status:  # a persisted row (#667) after a restart
        job = {"state": status.get("state"), "kind": job.get("trigger"),
               "dry_run": bool(job.get("dry_run")), "started_at": job.get("started_at"),
               "finished_at": job.get("finished_at"), "error": job.get("message") or None,
               "report": job.get("report") or {},
               "pre_store": (job.get("report") or {}).get("pre_store")}  # fmt: skip
    out = {k: job[k] for k in _VIEW_JOB if k in job}
    if "candidates" in status and "candidates" not in out:
        out["candidates"] = status["candidates"]
    if job.get("error"):
        out["error"] = str(job["error"])[:200]
    out["documents"] = [
        {k: d[k] for k in _VIEW_PLANNED if k in d}
        for d in job.get("documents") or [] if isinstance(d, dict)
    ]  # fmt: skip
    report = job.get("report") if isinstance(job.get("report"), dict) else {}
    results = []
    for d in report.get("documents") or []:
        if not isinstance(d, dict):
            continue
        r = {k: d[k] for k in _VIEW_RESULT if k in d}
        if d.get("error"):
            r["error"] = str(d["error"])[:200]
        if "lost" in d:
            r["lost_count"] = len(d.get("lost") or [])
        results.append(r)
    out["results"] = results
    if "verification" in job:
        out["verification"] = _counts(job["verification"])
    if isinstance(job.get("index"), dict):
        out["index"] = _counts(job["index"])
    return out


async def reread_candidates(db, pid: str) -> list[dict]:
    """Curated documents with Cowork-era unverified facts (#573 admin panel):
    id, filename, category and how many facts still carry ``cowork_ocr``.
    Counts only — never a value, a date or evidence text."""
    from oncofiles.curated.reread_verify import CONFIRMABLE

    counts: dict[int, int] = {}
    for r in await db.fact_verification_rows(patient_id=pid):
        if r.get("verification") != "unverified":
            continue
        try:
            reasons = json.loads(r.get("metadata_json") or "{}").get("unverified_reasons") or []
        except (ValueError, AttributeError):
            continue
        if CONFIRMABLE in reasons:
            doc = r["source_document_id"]
            counts[doc] = counts.get(doc, 0) + 1
    out = [
        {"document_id": d["id"], "filename": d["filename"], "category": d.get("category"),
         "cowork_unverified": counts[d["id"]]}
        for d in await db.curated_reocr_candidates(patient_id=pid) if counts.get(d["id"])
    ]  # fmt: skip
    out.sort(key=lambda d: (d["category"] != "labs", -d["cowork_unverified"], d["document_id"]))
    return out


def _birth_dates(pid: str) -> set[str]:
    """The patient's birth date (patient context), never a fact date (#714)."""
    try:
        from oncofiles.patient_context import get_context

        dob = str((get_context(pid) or {}).get("date_of_birth") or "")[:10]
    except Exception:
        return set()
    return {dob} if re.fullmatch(r"\d{4}-\d{2}-\d{2}", dob) else set()


def _source_reason(meta: dict) -> str | None:
    """Same source rule as ``_extract_facts``: a handwritten page or a
    non-local (Cowork-era) transcription is never trusted as machine."""
    engine = str(meta.get("ocr_engine") or "")
    if meta.get("handwritten") is True:
        return "handwritten"
    if not any(k in engine.lower() for k in _LOCAL_ENGINES):
        return "cowork_ocr"
    return None


def _overrides(thinking, sampling, timeout_s, reasoning_marker) -> dict:
    """The per-job A/B overrides as echoed in job and report (#767, #770)."""
    return {"thinking": thinking, "sampling": sampling, "timeout_s": timeout_s,
            "reasoning_marker": reasoning_marker}  # fmt: skip


async def _llm_shadow_job(
    db,
    pid: str,
    docs: list[dict],
    *,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> None:
    """LLM extractor + generic verifier on stored page text, compared with the
    stored rule facts (#714 phase 2a). Writes NOTHING: no facts, no Drive, no
    run row. One model call at a time (one LM Studio slot); local only.
    ``thinking`` / ``sampling`` (#767) and ``timeout_s`` (#770)
    override the env for this job only."""
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated import llm_extract, llm_extract_call, verifier
    from oncofiles.curated.facts_rules import Vocabulary

    job = _reocr_jobs[pid]
    cfg = llm_extract_call.settings(thinking, sampling, timeout_s, reasoning_marker)
    overrides = _overrides(thinking, sampling, timeout_s, reasoning_marker)
    report: list[dict] = []
    try:
        try:
            from oncofiles import patient_context

            await patient_context.load_from_db(db.db, pid)
        except Exception:
            logger.warning("llm shadow: patient context not loaded for %s", pid)
        vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
        births = _birth_dates(pid)
        stored = await db.fact_verification_rows(patient_id=pid)
        for d in docs:
            entry = {"document_id": d["id"], "category": d.get("category")}
            if _stopping.is_set():
                report.append({**entry, "result": "skipped", "error": "stopping"})
                continue
            pages = [(int(p["page_number"]), p.get("extracted_text") or "")
                     for p in await db.get_ocr_pages(d["id"], patient_id=pid)]  # fmt: skip
            if not any(t.strip() for _, t in pages):
                report.append({**entry, "result": "skipped", "error": "no page text"})
                continue
            meta = d.get("structured_metadata") or {}
            day = str(d.get("document_date") or "")[:10] or None

            def _blocking(pages=pages, meta=meta, day=day):
                with local_only("curated LLM extract shadow"):
                    return llm_extract.extract_document(
                        pages, vocab=vocab, report_date=day, birth_dates=births,
                        source_reason=_source_reason(meta), stop=_stopping.is_set, cfg=cfg,
                    )  # fmt: skip

            try:
                out = await asyncio.to_thread(_blocking)
            except Exception as exc:
                logger.exception("llm shadow extraction of doc %s failed", d["id"])
                report.append({**entry, "result": "failed", "error": type(exc).__name__})
                continue
            rows = [r for r in stored if r["source_document_id"] == d["id"]]
            # text-only extraction never upgrades a transcription (#714 rule)
            verifier.inherit_transcription_reasons(out["facts"], rows)
            cross = verifier.transcription_pages(rows, _source_reason(meta), [n for n, _ in pages])
            meta_keys = ("method", "pages", "pages_used", "pages_not_assessed", "pages_truncated",
                         "page_errors", "finish_reasons", "page_diagnostics", "warnings",
                         "dob_known",
                         "birth_date_guard",
                         "dates_unchecked_no_dob", "facts_from_content",
                         "facts_from_reasoning", "dropped", "seconds")  # fmt: skip
            report.append({
                **entry, "result": "compared", **{k: out[k] for k in meta_keys},
                **llm_extract.shadow_compare(
                    rows, out["facts"], not_assessed_pages=out["pages_not_assessed"],
                    crosscheck_pages=cross,
                ),
            })  # fmt: skip
        done = [r for r in report if r.get("result") == "compared"]
        totals = {k: sum(r[k] for r in done) for k in
                  ("rule_facts", "llm_facts", "llm_machine", "agree", "agree_machine",
                   "rule_only", "llm_only", "value_mismatch", "pages_used", "pages_truncated",
                   "dates_unchecked_no_dob")}  # fmt: skip
        totals["pages_not_assessed"] = sum(len(r["pages_not_assessed"]) for r in done)
        job.update(state="done", report={
            "dry_run": True, "extractor": llm_extract.EXTRACTOR_VERSION,
            "verifier": verifier.VERIFIER_VERSION,
            "settings": {**cfg.report(), "overrides": overrides},
            "warnings": llm_extract_call.merge_warnings(done),
            "totals": totals, "documents": report,
        })  # fmt: skip
    except Exception as exc:
        logger.exception("llm shadow job failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__, report={"documents": report})
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()


async def curated_llm_extract_preview(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    dry_run: bool = True,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> str:
    """SHADOW mode of LLM-first fact extraction (#714): compare, never store.

    For each listed curated document, the LOCAL model reads the stored page
    text and lists the printed results; a generic verifier (no layout rules)
    accepts each as machine or unverified with reason codes. The result is
    compared with the facts currently stored for that document and reported
    as keys and counts only (agreement on code + date + value hash, codes
    found by only one side, unverified reasons, seconds) — never values.
    Writes nothing: no facts, no Drive. Off-switch: LLM_EXTRACT_SHADOW_DISABLED=true.
    Explicit ``document_ids`` only (max 10). Runs in the background; poll
    curated_reocr_status. Slow: about a minute per page.

    Args:
        thinking: "on" / "off" overrides LLM_EXTRACT_THINKING for this job
            only (A/B, #767); null keeps the env setting.
        sampling: "greedy" / "qwen" overrides LLM_EXTRACT_SAMPLING for this
            job only; null keeps the env setting.
        timeout_s: per-call timeout in seconds (int, 30-900) for this job only;
            null keeps LLM_EXTRACT_TIMEOUT_S.
        reasoning_marker: "on" / "off" overrides LLM_REASONING_MARKER for this
            job only (#770): with thinking off, the system message ends with
            the reasoning-off marker (for the LM Studio Prompt Template rule)
            and no /no_think is sent; with thinking on, no marker.
        All four are echoed in the report (``settings``) and refused (error)
        with dry_run=false.
    """
    from oncofiles import config
    from oncofiles.curated import llm, llm_extract_call

    llm_extract_call.refuse_overrides_on_write(
        write=not dry_run, thinking=thinking, sampling=sampling, timeout_s=timeout_s,
        reasoning_marker=reasoning_marker,
    )  # fmt: skip
    if config.LLM_EXTRACT_SHADOW_DISABLED:
        return json.dumps({"error": "disabled by LLM_EXTRACT_SHADOW_DISABLED=true"})
    if not dry_run:
        return json.dumps({"error": "shadow mode only: this tool never writes facts"})
    if not llm.configured():
        return json.dumps({"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"})
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))[:10]
    if not ids:
        return json.dumps({"error": "document_ids is required"})
    if _reocr_jobs.get(pid, {}).get("state") == "running" or (
        pid in _runs and _runs[pid].get("state") == "running"
    ):
        return json.dumps({"error": "busy: a run, re-OCR, re-read or shadow job is in progress"})
    docs = [r for r in await db.curated_reocr_candidates(patient_id=pid) if r["id"] in ids]
    if not docs:
        return json.dumps({"status": "nothing to do"})
    from oncofiles.curated.llm_extract import EXTRACTOR_VERSION

    _reocr_jobs[pid] = {
        "state": "running", "kind": "llm_shadow", "dry_run": True,
        "extractor": EXTRACTOR_VERSION, "started_at": datetime.now(UTC).isoformat(),
        "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker),
        "documents": [{"document_id": d["id"], "category": d.get("category")} for d in docs],
    }  # fmt: skip
    _reocr_jobs[pid]["task"] = asyncio.create_task(
        _llm_shadow_job(db, pid, docs, thinking=thinking, sampling=sampling,
                        timeout_s=timeout_s, reasoning_marker=reasoning_marker)
    )  # fmt: skip
    return json.dumps(
        {k: v for k, v in _reocr_jobs[pid].items() if k != "task"}, ensure_ascii=False
    )


# ── LLM-first extraction of the non-lab kinds (#726 item 4, #711 phase 2b) ──

CLINICAL_MAX_DOCS = 10


async def _all_birth_dates(db, pid: str) -> set[str]:
    """Every birth date known for the patient (ISO): patient context plus the
    README contract (cached by the last index; read from Drive, read-only,
    when not cached yet). Never used as a fact date."""
    try:
        from oncofiles import patient_context

        await patient_context.load_from_db(db.db, pid)
    except Exception:
        logger.warning("clinical extract: patient context not loaded for %s", pid)
    if pid not in _readme_births:
        try:
            from oncofiles.curated import indexer
            from oncofiles.curated.drive_ops import DriveOps
            from oncofiles.server import _create_patient_clients

            clients = await _create_patient_clients(db, pid)
            if clients and clients[0] is not None:
                gdrive, folder_id = clients[0], clients[3]

                def _blocking():
                    own = gdrive.dedicated_copy()
                    try:
                        return indexer.readme_birth_dates(DriveOps(own), folder_id)
                    finally:
                        own.close()

                _readme_births[pid] = set(await asyncio.to_thread(_blocking))
        except Exception:
            logger.warning("clinical extract: README birth dates not read for %s", pid)
    return set(_birth_dates(pid)) | _readme_births.get(pid, set())


_CLINICAL_DOC_KEYS = (
    "method", "pages", "pages_used", "pages_not_assessed", "pages_truncated", "page_errors",
    "error_kinds", "answer_sources", "page_diagnostics", "warnings", "dob_known",
    "birth_date_guard", "counts", "unverified_reasons", "dropped", "seconds",
)  # fmt: skip


def _clinical_totals(report: list[dict]) -> dict:
    """Sums over the documents: counts per kind, reasons, stored rows — no values."""
    counts: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    stored: dict[str, dict[str, int]] = {}
    for r in report:
        for kind, c in (r.get("counts") or {}).items():
            tot = counts.setdefault(kind, {"machine": 0, "unverified": 0})
            for k, n in c.items():
                tot[k] = tot.get(k, 0) + n
        for reason, n in (r.get("unverified_reasons") or {}).items():
            reasons[reason] = reasons.get(reason, 0) + n
        for target, c in (r.get("stored") or {}).items():
            tot = stored.setdefault(target, {})
            for k, n in c.items():
                tot[k] = tot.get(k, 0) + n
    return {
        "documents": len(report),
        "stored_documents": sum(1 for r in report if r.get("result") == "stored"),
        "pages_not_assessed": sum(len(r.get("pages_not_assessed") or []) for r in report),
        "counts": counts,
        "unverified_reasons": dict(sorted(reasons.items())),
        "stored": stored,
    }


async def _clinical_job(
    db,
    pid: str,
    docs: list[dict],
    *,
    write: bool,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> None:
    """Extract the non-lab kinds of each document with the LOCAL model and
    verify them (#726 item 4). Preview (``write=False``) writes NOTHING; a
    write holds the patient lock, refuses without a known birth date, and
    stores nothing for a document with a page that was not assessed. One
    model call at a time (one LM Studio slot); local only. ``thinking`` /
    ``sampling`` (#767) and ``timeout_s`` (#770) override the
    env for a PREVIEW only."""
    from oncofiles import config
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated import clinical_store, llm_extract_call, verifier, verifier_clinical
    from oncofiles.curated import llm_extract_clinical as lx
    from oncofiles.curated.facts_rules import Vocabulary

    # refused at the tool already; the job itself stays safe too
    llm_extract_call.refuse_overrides_on_write(
        write=write,
        thinking=thinking,
        sampling=sampling,
        timeout_s=timeout_s,
        reasoning_marker=reasoning_marker,
    )
    cfg = llm_extract_call.settings(thinking, sampling, timeout_s, reasoning_marker)
    job = _reocr_jobs[pid]
    row_id = (
        await db.insert_curated_run(patient_id=pid, dry_run=False, trigger="clinical")
        if write
        else None
    )
    report: list[dict] = []
    births: set[str] = set()
    try:
        async with patient_lock(pid) if write else contextlib.nullcontext():
            births = await _all_birth_dates(db, pid)
            if write:
                verifier.require_dob_for_write(births)
            vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
            model = config.LOCAL_LLM_MODEL or None
            for d in docs:
                entry = {"document_id": d["id"], "category": d.get("category")}
                if _stopping.is_set():
                    report.append({**entry, "result": "skipped", "error": "stopping"})
                    continue
                pages = [(int(p["page_number"]), p.get("extracted_text") or "")
                         for p in await db.get_ocr_pages(d["id"], patient_id=pid)]  # fmt: skip
                if not any(t.strip() for _, t in pages):
                    report.append({**entry, "result": "skipped", "error": "no page text"})
                    continue
                meta = d.get("structured_metadata") or {}
                day = str(d.get("document_date") or "")[:10] or None

                def _blocking(pages=pages, meta=meta, day=day):
                    with local_only("curated clinical extraction"):
                        return lx.extract_document(
                            pages, vocab=vocab, report_date=day, birth_dates=births,
                            source_reason=_source_reason(meta), stop=_stopping.is_set, cfg=cfg,
                        )  # fmt: skip

                try:
                    out = await asyncio.to_thread(_blocking)
                except Exception as exc:
                    logger.exception("clinical extraction of doc %s failed", d["id"])
                    report.append({**entry, "result": "failed", "error": type(exc).__name__})
                    continue
                entry.update({k: out[k] for k in _CLINICAL_DOC_KEYS}, result="previewed")
                if write and out["pages_not_assessed"]:
                    entry.update(result="skipped", error="a page was not assessed: nothing stored")
                elif write:
                    verifier.require_dob_for_write(births)
                    try:
                        res = await clinical_store.store_document(
                            db, patient_id=pid, document_id=d["id"], items=out["items"],
                            document_date=day, birth_dates=births, model=model,
                        )  # fmt: skip
                    except Exception as exc:  # re-running the document replaces it again
                        logger.exception("storing clinical items of doc %s failed", d["id"])
                        entry.update(result="failed", error=type(exc).__name__)
                    else:
                        entry.update(
                            result="stored", stored=res["stored"], store_dropped=res["dropped"]
                        )
                report.append(entry)
        job.update(state="done", report={
            "dry_run": not write, "extractor": lx.EXTRACTOR_VERSION,
            "verifier": verifier_clinical.CLINICAL_VERIFIER_VERSION, "dob_known": bool(births),
            "settings": {**cfg.report(),
                         "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker)},
            "warnings": llm_extract_call.merge_warnings(report),
            "totals": _clinical_totals(report), "documents": report,
        })  # fmt: skip
    except verifier.NoBirthDateError:
        job.update(state="failed", error="no_birth_date", report={
            "dry_run": not write, "documents": report,
            "message": "no birth date known (README contract or patient context): nothing stored",
        })  # fmt: skip
    except Exception as exc:
        logger.exception("clinical extraction job failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__, report={"documents": report})
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
        if job.get("pre_store"):  # the safety copy is part of the record
            job["report"] = {**(job.get("report") or {}), "pre_store": job["pre_store"]}
        if row_id is not None:
            try:
                await db.finish_curated_run(
                    row_id, patient_id=pid,
                    status="ok" if job.get("state") == "done" else "failed",
                    message=job.get("error") or "", report=job.get("report") or {},
                )  # fmt: skip
            except Exception:
                logger.exception("could not persist the clinical extraction job for %s", pid)
        if write:
            await _audit_clinical_write(db, pid, job)


async def _audit_clinical_write(db, pid: str, job: dict) -> None:
    """One activity_log row per REAL clinical extraction (as for re-read stores,
    #573): caller, document ids, the pre-store copy, counts per kind and the
    stored rows per target. Counts only — never a value or evidence text."""
    from oncofiles.models import ActivityLogEntry

    report = job.get("report") or {}
    totals = report.get("totals") or {}
    docs = [d for d in report.get("documents") or [] if isinstance(d, dict)]
    actor = str(job.get("actor") or "unknown")[:120]
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id="curated-clinical",
                agent_id=actor,
                tool_name="curated_clinical_extract",
                input_summary=json.dumps(
                    {
                        "by": actor,
                        # keyed hash, never the email itself (#590/#604, migration 085)
                        "caller_email_hash": job.get("caller_email_hash"),
                        "dry_run": False,
                        "pre_store": job.get("pre_store"),
                        "document_ids": [d.get("document_id") for d in job.get("documents") or []],
                        "started_at": job.get("started_at"),
                        "extractor": job.get("extractor"),
                    }
                ),  # fmt: skip
                output_summary=json.dumps(
                    {
                        "state": job.get("state"),
                        "results": {
                            r: sum(1 for d in docs if d.get("result") == r)
                            for r in {str(d.get("result")) for d in docs}
                        },
                        "counts": totals.get("counts") or {},
                        "stored": totals.get("stored") or {},
                    }
                ),  # fmt: skip
                status="ok" if job.get("state") == "done" else "error",
                error_message=job.get("error"),
                tags=json.dumps(["curated", "clinical", "real", "pre-store"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for a clinical extraction")


async def _start_clinical(
    ctx: Context,
    document_ids,
    patient_slug,
    *,
    write: bool,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> str:
    from oncofiles import config
    from oncofiles.curated import llm, llm_extract_call
    from oncofiles.curated.llm_extract_clinical import EXTRACTOR_VERSION

    # A/B overrides are for a dry run only: a write never takes them (#767, #770).
    llm_extract_call.refuse_overrides_on_write(
        write=write,
        thinking=thinking,
        sampling=sampling,
        timeout_s=timeout_s,
        reasoning_marker=reasoning_marker,
    )
    if config.LLM_CLINICAL_EXTRACT_DISABLED:
        return json.dumps({"error": "disabled by LLM_CLINICAL_EXTRACT_DISABLED=true"})
    if not llm.configured():
        return json.dumps({"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"})
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))
    if not ids:
        return json.dumps({"error": "document_ids is required"})
    if len(ids) > CLINICAL_MAX_DOCS:
        return json.dumps({"error": f"too many documents: at most {CLINICAL_MAX_DOCS} explicit "
                           f"document_ids per call, got {len(ids)}"})  # fmt: skip
    docs = [r for r in await db.curated_reocr_candidates(patient_id=pid) if r["id"] in ids]
    if not docs:
        return json.dumps({"status": "nothing to do"})

    def busy() -> bool:
        return (
            _reocr_jobs.get(pid, {}).get("state") == "running"
            or (pid in _runs and _runs[pid].get("state") == "running")
            or (write and patient_lock(pid).locked())
        )

    busy_error = json.dumps({"error": "busy: a curated run, index, re-OCR, re-read or "
                             "extraction is in progress for this patient"})  # fmt: skip
    if busy():
        return busy_error
    # Reserve the patient's job slot before any further await, so two calls can
    # never both start a job; released again on every refusal below.
    previous = _reocr_jobs.get(pid)
    _reocr_jobs[pid] = job = {
        "state": "running", "kind": "clinical_extract" if write else "clinical_preview",
        "dry_run": not write, "extractor": EXTRACTOR_VERSION,
        "started_at": datetime.now(UTC).isoformat(), "actor": "mcp",
        "caller_email_hash": caller_email_hash(_caller_email()),
        "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker),
        "documents": [{"document_id": d["id"], "category": d.get("category")} for d in docs],
    }  # fmt: skip

    def release(error: str) -> str:
        if previous is not None:
            _reocr_jobs[pid] = previous
        else:
            _reocr_jobs.pop(pid, None)
        return json.dumps({"error": error})

    if write:
        if not await _all_birth_dates(db, pid):
            return release("no birth date known for this patient (README contract or patient "
                           "context): refusing to store facts")  # fmt: skip
        # Same safety bar as the re-read / re-OCR stores (#573): a verified copy
        # of the DB before anything is written, or no write at all.
        from oncofiles.pre_store import PreStoreError, make_copy

        try:
            job["pre_store"] = await asyncio.to_thread(make_copy, db, "clinical")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, PreStoreError) else type(exc).__name__
            logger.error("pre-store copy failed for %s (%s): extraction refused", pid[:8], reason)
            return release(f"refused: pre-store copy failed ({reason}); nothing was written")
    from oncofiles.cloud_guard import local_only

    # The task copies the current context: the WHOLE job (DB writes included,
    # not only the model calls) runs local-only.
    with local_only("curated clinical extraction"):
        _reocr_jobs[pid]["task"] = asyncio.create_task(
            _clinical_job(
                db,
                pid,
                docs,
                write=write,
                thinking=thinking,
                sampling=sampling,
                timeout_s=timeout_s,
                reasoning_marker=reasoning_marker,
            )
        )
    return json.dumps(
        {k: v for k, v in _reocr_jobs[pid].items() if k != "task"}, ensure_ascii=False
    )


async def curated_clinical_extract_preview(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> str:
    """DRY RUN: what the LOCAL model would extract from curated documents (#726).

    Kinds: diagnoses (ICD code, stage, primary/metastasis), biomarker and
    genomic results, treatment courses, cycles (incl. postponements), medication
    doses, procedures / imaging / radiotherapy, CT lesion sizes and planned dates.
    Each item is checked by a generic verifier (evidence verbatim on the page,
    key tokens inside it, dates printed and not a birth date): machine or
    unverified with reasons. The verifier checks only that the text is printed
    on the page: the interpretation (which line is the diagnosis, a current vs
    a historical regimen) is the model's and is NOT verified. Writes NOTHING.
    Reports per document only counts per kind and reason codes — never values.
    Explicit ``document_ids`` (max 10). Runs in the background; poll
    curated_reocr_status. About a minute per page.

    Args:
        thinking: "on" / "off" overrides LLM_EXTRACT_THINKING for this preview
            only (A/B, #767); null keeps the env setting.
        sampling: "greedy" / "qwen" overrides LLM_EXTRACT_SAMPLING for this
            preview only; null keeps the env setting.
        timeout_s: per-call timeout in seconds (int, 30-900) for this preview
            only; null keeps LLM_EXTRACT_TIMEOUT_S.
        reasoning_marker: "on" / "off" overrides LLM_REASONING_MARKER for this
            preview only (#770): with thinking off, the system message ends
            with the reasoning-off marker (for the LM Studio Prompt Template rule)
            and no /no_think is sent; with thinking on, no marker.
        All four are echoed in the report (``settings``), with per-page token
        counts and warnings.
    """
    return await _start_clinical(
        ctx, document_ids, patient_slug, write=False, thinking=thinking, sampling=sampling,
        timeout_s=timeout_s, reasoning_marker=reasoning_marker,
    )  # fmt: skip


async def curated_clinical_extract(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    dry_run: bool = True,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
) -> str:
    """Extract diagnoses, biomarkers, treatments, procedures, lesion sizes and
    planned dates from curated documents with the LOCAL model and store them (#726).

    dry_run=true (default) is the same as curated_clinical_extract_preview and
    writes nothing. dry_run=false stores the verified items as conditions,
    stages, biomarker / genomic observations, treatment courses, cycles,
    medication events, procedures, lesion sizes and planned encounters, each
    with its document, page, evidence, verification and a provenance row.
    Stored rows are shown everywhere with ``method = llm-clinical`` and their
    own verification label: the verifier checks only that the text is printed
    on the page, while the interpretation (which line is the diagnosis, a
    current vs a historical regimen) is the model's and is NOT verified.
    Re-running a document replaces only rows this extractor wrote and never
    touches verified or corrected rows. Before writing, a verified safety copy
    of the database is made (pre-store); no copy, no write. Every real run
    leaves an activity_log row (caller, ids, copy, counts). Refused while
    another curated run, re-OCR or extraction is active, or when no birth date
    is known; holds the patient lock; a document with a page the model did not
    assess is not stored. Explicit ``document_ids`` (max 10). Runs in the
    background; poll curated_reocr_status. About a minute per page.

    ``thinking`` / ``sampling`` / ``timeout_s`` / ``reasoning_marker`` overrides (see
    curated_clinical_extract_preview) are accepted with dry_run=true only; with
    dry_run=false they are refused (error): a write always uses the configured
    env settings.
    """
    return await _start_clinical(
        ctx, document_ids, patient_slug, write=not dry_run, thinking=thinking, sampling=sampling,
        timeout_s=timeout_s, reasoning_marker=reasoning_marker,
    )  # fmt: skip


async def curated_reocr_status(ctx: Context, patient_slug: str | None = None) -> str:
    """State and report of the last re-OCR job for this patient (#660). Read-only."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    out = await reocr_status(_get_db(ctx), pid)
    return json.dumps(out, ensure_ascii=False, default=str)


async def curated_index(
    ctx: Context, patient_slug: str | None = None, document_ids: list[int] | None = None
) -> str:
    """Refresh the searchable copy of a patient's curated Drive folder (#603).

    Reads the curated folder (filed documents and their sidecars) and updates
    what search_documents, view_document and the other read tools see. Drive
    is only read; nothing is uploaded or sent to a cloud model. Runs after
    every curated run anyway — call it only to force a refresh.

    Args:
        document_ids: Re-parse these documents even though their sidecar did
            not change (e.g. after a parser fix). Machine facts are replaced;
            verified/corrected facts are never touched. Ids of another
            patient are ignored.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    force = {int(i) for i in document_ids} if document_ids else None
    out = await index_patient(_get_db(ctx), pid, force_ids=force)
    return json.dumps(out, ensure_ascii=False, default=str)


async def curated_folder_status(ctx: Context, patient_slug: str | None = None) -> str:
    """Status and report of the latest curated-folder run for a patient (#562)."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    return json.dumps(await run_status(_get_db(ctx), pid), ensure_ascii=False, default=str)


def register(mcp):
    mcp.tool()(curated_folder_run)
    mcp.tool()(curated_folder_status)
    mcp.tool()(curated_index)
    mcp.tool()(curated_reocr)
    mcp.tool()(curated_reocr_status)
    mcp.tool()(curated_reread_verify)
    mcp.tool()(curated_llm_extract_preview)
    mcp.tool()(curated_clinical_extract_preview)
    mcp.tool()(curated_clinical_extract)
