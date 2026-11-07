"""MCP tools for the curated-folder pipeline (#562).

``curated_folder_run`` starts a run in the background and returns at once
(local OCR can take many minutes — far beyond an MCP call timeout);
``curated_folder_status`` returns the latest report. Dry run is the default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastmcp import Context

from oncofiles.tools._helpers import _get_db, _resolve_patient_id

logger = logging.getLogger(__name__)

# patient_id -> {"state": "running"|"done"|"error", "started": ts, "report": dict|None}
_runs: dict[str, dict] = {}
_lock = asyncio.Lock()

DEFAULTS = {  # patient_type -> (max_docs, min_text_chars, max_doc_age_days)
    # Oncology (Cowork erika-daily-cleanup): a > 2-year-old date on a new scan is suspect.
    "oncology": (20, 100, 730),
    # General (peter-daily-cleanup): historical records back to birth are normal.
    "general": (8, 200, None),
}


async def _start(db, pid: str, *, dry_run: bool, max_docs: int | None, max_actions: int) -> dict:
    from oncofiles.curated import llm
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.runner import RunOptions, run_curated
    from oncofiles.curated_folder import curated_reason
    from oncofiles.patient_context import get_context
    from oncofiles.server import _create_patient_clients

    if not llm.configured():
        return {"error": "local LLM not configured (LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL)"}
    clients = await _create_patient_clients(db, pid)
    if not clients or clients[0] is None:
        return {"error": "Google Drive not connected for this patient (or integration disabled)"}
    gdrive, folder_id = clients[0], clients[3]
    reason = await asyncio.to_thread(curated_reason, gdrive, folder_id, use_cache=False)
    if reason != "is a curated folder":
        return {"error": f"patient folder is not a curated root ({reason or 'no README contract'})"}

    patient_type = (get_context(pid) or {}).get("patient_type", "oncology")
    default_docs, min_chars, max_age = DEFAULTS.get(patient_type, DEFAULTS["oncology"])
    opts = RunOptions(
        dry_run=dry_run,
        max_docs=max_docs or default_docs,
        max_actions=max_actions,
        min_text_chars=min_chars,
        max_doc_age_days=max_age,
    )

    def _blocking():
        if dry_run:  # a dry run never needs the write bypass
            return run_curated(DriveOps(gdrive), folder_id, opts)
        with gdrive.contract_writes():  # thread-scoped: other tools stay blocked
            return run_curated(DriveOps(gdrive), folder_id, opts)

    async def _task():
        sync_id = None
        t0 = time.monotonic()
        try:
            if not dry_run:
                sync_id = await db.insert_sync_history("curated", patient_id=pid)
            report = await asyncio.to_thread(_blocking)
            _runs[pid].update(state="done", report=report.to_dict())
            if sync_id is not None:
                filed = sum(1 for d in report.documents if d["result"] == "filed")
                await db.complete_sync_history(
                    sync_id,
                    status="completed" if report.status == "ok" else "failed",
                    duration_s=round(time.monotonic() - t0, 1),
                    to_organized=filed,
                    to_renamed=filed,
                    error_message=None if report.status == "ok" else report.message[:500],
                )
        except Exception as exc:
            logger.exception("curated run failed for %s", pid)
            _runs[pid].update(state="error", report={"error": type(exc).__name__})
            if sync_id is not None:
                await db.complete_sync_history(
                    sync_id, status="failed", error_message=type(exc).__name__
                )

    _runs[pid] = {"state": "running", "started": time.time(), "dry_run": dry_run, "report": None}
    asyncio.get_running_loop().create_task(_task())
    return {"status": "started", "dry_run": dry_run, "max_docs": opts.max_docs}


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
    curated_folder_status.

    Args:
        patient_slug: Patient to process (default: the caller's patient).
        dry_run: True (default) reads, OCRs and classifies but writes nothing.
        max_docs: Documents per run (default 20 oncology / 8 general).
        max_actions: File-action budget per run (default 40).
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    async with _lock:
        current = _runs.get(pid)
        if current and current["state"] == "running":
            return json.dumps({"error": "a run is already in progress for this patient"})
        result = await _start(
            _get_db(ctx), pid, dry_run=dry_run, max_docs=max_docs, max_actions=max(1, max_actions)
        )
    return json.dumps(result)


async def curated_folder_status(ctx: Context, patient_slug: str | None = None) -> str:
    """Status and report of the latest curated-folder run for a patient (#562)."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    run = _runs.get(pid)
    if not run:
        return json.dumps({"state": "none"})
    return json.dumps(run, ensure_ascii=False, default=str)


def register(mcp):
    mcp.tool()(curated_folder_run)
    mcp.tool()(curated_folder_status)
