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
import hashlib
import json
import logging
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastmcp import Context

from oncofiles.tools._helpers import _get_db, _resolve_patient_id

logger = logging.getLogger(__name__)

# patient_id -> {"state": "running", "started": ts, "dry_run": bool, "trigger": str}
_runs: dict[str, dict] = {}
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


def run_active() -> bool:
    """True while any curated run executes (exposed on /health for CI deploys)."""
    return any(r.get("state") == "running" for r in _runs.values())


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


async def _execute(db, pid: str, gdrive, folder_id: str, opts, *, trigger: str) -> dict:
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
    db, pid: str, *, dry_run: bool, trigger: str, max_docs: int | None = None, max_actions: int = 40
) -> dict:
    """Run in the foreground (nightly loop). Returns the report or {"error": …}."""
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
    report = None
    try:
        report = await _execute(db, pid, *ctx, trigger=trigger)
        return report
    finally:
        _runs.pop(pid, None)
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
) -> list[dict]:
    """Preview first; if clean, real runs until the inbox is done or a run is not clean.

    ``reuse_preview`` (hourly mode) skips the preview while the last clean one is
    still inside the 24 h gate — the real runs keep their own preflight gates.
    """
    reports: list[dict] = []
    if not (reuse_preview and await preview_gate(db, pid) is None):
        reports.append(
            await execute_run(
                db, pid, dry_run=True, trigger=f"{trigger}-preview", max_docs=max_docs
            )
        )
        if reports[-1].get("status") != "ok":
            return reports
    for _ in range(max_real_runs):
        if _stopping.is_set():
            break
        rep = await execute_run(db, pid, dry_run=False, trigger=trigger, max_docs=max_docs)
        reports.append(rep)
        if rep.get("status") != "ok":
            break
        worked = any(d.get("result") != "skipped" for d in rep.get("documents", []))
        if not rep.get("not_processed") or not worked:
            break  # inbox done, or nothing left this run could do
    return reports


STARTUP_GRACE_S = 300  # let a rolling deploy retire the old container first (#581)


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
    }


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
    """(failed, progressed) for one sequence of runs."""
    failed = any(r.get("error") or r.get("status") != "ok" for r in reports)
    progressed = any(
        d.get("result") != "skipped"
        for r in reports
        if not r.get("dry_run") and not r.get("error")
        for d in r.get("documents", [])
    ) or any(r.get("actions") for r in reports if not r.get("dry_run"))
    return failed, progressed


async def continuous_tick(
    db, pid: str, *, settle_s: int, chunk: int, max_runs: int
) -> list[dict] | None:
    """Probe; run a short sequence when there is new, settled work. Never raises
    for an ordinary outcome; returns the reports when a sequence ran."""
    w = _watch.setdefault(pid, _Watch())
    if time.monotonic() < w.not_before:
        return None
    pr = await probe_patient(db, pid, w.idle_signature)
    if pr is None:
        return None
    work = set(pr.pending) - w.stuck
    if not work and not pr.canary:
        w.idle_signature = pr.signature
        return None
    if pr.newest_age_s is not None and pr.newest_age_s < settle_s:
        return None  # uploads still arriving — wait for the batch to settle
    logger.info(
        "curated auto %s: %d new item(s)%s — starting",
        pid,
        len(work),
        " + leftover canary" if pr.canary else "",
    )
    reports = await nightly_for_patient(
        db, pid, max_real_runs=max_runs, reuse_preview=True, trigger="auto", max_docs=chunk
    )
    failed, progressed = _sequence_outcome(reports)
    if failed:
        w.failures += 1
        delay = min(FAILURE_BACKOFF_S * 2 ** (w.failures - 1), FAILURE_BACKOFF_MAX_S)
        w.not_before = time.monotonic() + delay
    else:
        w.failures = 0
        # Nothing moved: what is pending now needs a human or the nightly sweep.
        w.stuck = frozenset() if progressed else frozenset(pr.pending)
    logger.info(
        "curated auto %s: %s%s",
        pid,
        [(r.get("dry_run"), r.get("status") or r.get("error")) for r in reports],
        f" — retry in {int(delay // 60)} min" if failed else "",
    )
    return reports


async def curated_nightly_loop(
    db, *, poll_s: int | None = None, startup_grace_s: int = STARTUP_GRACE_S
) -> None:
    """Background task for curated folders, per enabled patient.

    * Continuous (default): every ``CURATED_POLL_S`` a read-only probe; new
      files are processed minutes after they land (``continuous_tick``).
    * Nightly sweep once per local night at CURATED_NIGHTLY_HOUR (full budget,
      also retries what continuous mode parked). ``CURATED_NIGHTLY_HOUR=every``
      makes the sweep hourly.

    One run per patient at a time; patients take turns, so one big upload
    cannot starve the others. Independent of SYNC_ENABLED/APScheduler. A
    failing iteration never kills the loop.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Bratislava")
    last_night: str | None = None
    try:
        if n := await db.mark_interrupted_curated_runs():
            logger.warning("curated: marked %d run(s) interrupted by the last restart", n)
    except Exception:
        logger.exception("curated: could not mark interrupted runs")
    await asyncio.sleep(startup_grace_s)
    try:  # read model for the chat tools, once per start (#603)
        for pid in await db.list_curated_nightly_patients():
            if _stopping.is_set():
                break
            await _index_quietly(db, pid)
    except Exception:
        logger.exception("curated: startup index failed")
    while not _stopping.is_set():
        cont, cont_poll, settle, chunk = False, 300, 0, 5
        try:
            enabled, hour, max_runs = nightly_settings()
            cont, cont_poll, settle, chunk = continuous_settings()
            now = datetime.now(tz)
            slot = now.strftime("%Y-%m-%d") if hour is not None else now.strftime("%Y-%m-%dT%H")
            due = hour is None or now.hour == hour
            if enabled and due and last_night != slot:
                last_night = slot
                for pid in await db.list_curated_nightly_patients():
                    if _stopping.is_set():
                        break
                    if pid in _runs and _runs[pid].get("state") == "running":
                        continue  # a manual run is in progress
                    reports = await nightly_for_patient(
                        db, pid, max_real_runs=max_runs, reuse_preview=hour is None
                    )
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
                    if pid in _runs and _runs[pid].get("state") == "running":
                        continue
                    try:
                        await continuous_tick(
                            db, pid, settle_s=settle, chunk=chunk, max_runs=max_runs
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


async def index_patient(db, pid: str) -> dict:
    """Local-only wrapper: the index and its fact extraction never reach the cloud."""
    from oncofiles.cloud_guard import local_only

    with local_only("curated index + fact extraction"):
        return await _index_patient(db, pid)


async def _index_patient(db, pid: str) -> dict:
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
    known = {g: s["sidecar_modified"] for g, s in state.items() if not s["deleted"]}

    def _blocking():
        own = gdrive.dedicated_copy()
        try:
            return indexer.scan(DriveOps(own), folder_id, known=known)
        finally:
            own.close()

    from oncofiles.curated.facts_rules import Vocabulary

    t0 = time.monotonic()
    scanned = await asyncio.to_thread(_blocking)
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


async def _link_entities(db, pid: str, doc_id: int, rec) -> dict:
    """Organization, department, practitioner, encounter, title, Drive link (#603)."""
    fields = rec.filename.rsplit(".", 1)[0].split("-") if "." in rec.filename else []
    std = len(fields) >= 6 and fields[0].isdigit()
    org_code = _known(rec.institution) or (_known(fields[2]) if std else None)
    doctor = _known(fields[4]) if std else None
    dept_code = _known(fields[5]) if std else None
    org_id = await db.upsert_organization(org_code) if org_code else None
    dept_id = await db.upsert_department(org_id, dept_code) if org_id and dept_code else None
    prac_id = None
    if doctor and org_code != "PacientAdvokat":
        prac_id = await db.upsert_practitioner(
            patient_id=pid,
            surname_token=doctor,
            display_name=rec.metadata.get("doctor"),
            organization_id=org_id,
            department_id=dept_id,
        )
    enc_id = None
    day = rec.document_date.isoformat() if rec.document_date else None
    cat = rec.category.value
    if day and org_code and cat not in ("reference", "advocate", "other"):
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
    from oncofiles.curated.facts_rules import extract_lab_facts

    engine = str(rec.metadata.get("ocr_engine") or "")
    local = any(k in engine.lower() for k in _LOCAL_ENGINES)
    unreliable = rec.metadata.get("handwritten") is True or not local
    facts, extractor = extract_lab_facts(rec.pages, vocab, unreliable=unreliable)
    if not facts:
        return 0
    day = rec.document_date.isoformat() if rec.document_date else None
    system = "curated_sidecar" if local else "cowork_sidecar"
    for f in facts:
        f["occurred_at"] = f["occurred_at"] or day
        f.update(entities)
        f["metadata"] = {"source_system": system, "ocr_engine": engine or None}
    res = await db.replace_document_facts(
        patient_id=pid, document_id=doc_id, facts=facts, extracted_by=extractor
    )
    return res["added"]


async def _index_quietly(db, pid: str) -> None:
    try:
        await index_patient(db, pid)
    except Exception:
        logger.exception("curated index failed for %s", pid)


async def curated_index(ctx: Context, patient_slug: str | None = None) -> str:
    """Refresh the searchable copy of a patient's curated Drive folder (#603).

    Reads the curated folder (filed documents and their sidecars) and updates
    what search_documents, view_document and the other read tools see. Drive
    is only read; nothing is uploaded or sent to a cloud model. Runs after
    every curated run anyway — call it only to force a refresh.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    return json.dumps(await index_patient(_get_db(ctx), pid), ensure_ascii=False, default=str)


async def curated_folder_status(ctx: Context, patient_slug: str | None = None) -> str:
    """Status and report of the latest curated-folder run for a patient (#562)."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    return json.dumps(await run_status(_get_db(ctx), pid), ensure_ascii=False, default=str)


def register(mcp):
    mcp.tool()(curated_folder_run)
    mcp.tool()(curated_folder_status)
    mcp.tool()(curated_index)
