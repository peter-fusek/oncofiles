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
import time
from collections import OrderedDict
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
    try:
        return await _execute(db, pid, *ctx, trigger=trigger)
    finally:
        _runs.pop(pid, None)


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
        try:
            await _execute(db, pid, *ctx, trigger=trigger)
        finally:
            _runs.pop(pid, None)

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


def nightly_settings() -> tuple[bool, int, int]:
    """(enabled, local hour Europe/Bratislava, max real runs per night) from env."""
    enabled = os.environ.get("CURATED_NIGHTLY_ENABLED", "false").lower() in ("1", "true", "yes")
    hour = int(os.environ.get("CURATED_NIGHTLY_HOUR", "1"))
    max_runs = int(os.environ.get("CURATED_NIGHTLY_MAX_RUNS", "5"))
    return enabled, hour, max_runs


async def nightly_for_patient(db, pid: str, *, max_real_runs: int) -> list[dict]:
    """Preview first; if clean, real runs until the inbox is done or a run is not clean."""
    reports = [await execute_run(db, pid, dry_run=True, trigger="nightly-preview")]
    if reports[-1].get("status") != "ok":
        return reports
    for _ in range(max_real_runs):
        rep = await execute_run(db, pid, dry_run=False, trigger="nightly")
        reports.append(rep)
        if rep.get("status") != "ok":
            break
        worked = any(d.get("result") != "skipped" for d in rep.get("documents", []))
        if not rep.get("not_processed") or not worked:
            break  # inbox done, or nothing left this run could do
    return reports


async def curated_nightly_loop(db, *, poll_s: int = 300) -> None:
    """Background task: once per local night at CURATED_NIGHTLY_HOUR, per enabled patient.

    Independent of SYNC_ENABLED/APScheduler (which is off while the legacy sync
    is retired). A crash in one night never kills the loop.
    """
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Bratislava")
    last_night: str | None = None
    while True:
        try:
            enabled, hour, max_runs = nightly_settings()
            now = datetime.now(tz)
            tonight = now.date().isoformat()
            if enabled and now.hour == hour and last_night != tonight:
                last_night = tonight
                for pid in await db.list_curated_nightly_patients():
                    reports = await nightly_for_patient(db, pid, max_real_runs=max_runs)
                    logger.info(
                        "curated nightly %s: %s",
                        pid,
                        [(r.get("dry_run"), r.get("status") or r.get("error")) for r in reports],
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("curated nightly loop iteration failed")
        await asyncio.sleep(poll_s)


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


async def curated_folder_status(ctx: Context, patient_slug: str | None = None) -> str:
    """Status and report of the latest curated-folder run for a patient (#562)."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    return json.dumps(await run_status(_get_db(ctx), pid), ensure_ascii=False, default=str)


def register(mcp):
    mcp.tool()(curated_folder_run)
    mcp.tool()(curated_folder_status)
