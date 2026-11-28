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
import dataclasses
import functools
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

from oncofiles.curated.llm_extract_call import DATE_NULL_KEYS
from oncofiles.network_errors import NETWORK_UNAVAILABLE
from oncofiles.secrets_keys import caller_email_hash
from oncofiles.tools._helpers import _caller_email, _get_db, _resolve_patient_id

logger = logging.getLogger(__name__)

# patient_id -> {"state": "running", "started": ts, "dry_run": bool, "trigger": str}
_runs: dict[str, dict] = {}
# patient_id -> the patient's latest sidecar restore (#806): kind, state, ids,
# pre_store; only a "running" one blocks or counts in run_active()
_restoring: dict[str, dict] = {}
# patient_id -> birth dates (ISO) from the README contract, refreshed by every index
_readme_births: dict[str, set[str]] = {}
_lock = asyncio.Lock()

DEFAULTS = {  # patient_type -> (max_docs, min_text_chars, max_doc_age_days)
    # Oncology (the first patient's Cowork clean-up job): a date more than two
    # years old on a new scan is suspect.
    "oncology": (20, 100, 730),
    # General (the second patient's Cowork clean-up job): historical records
    # back to birth are normal.
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


def cached_extract(name, content, mime_type="", *, min_text_chars=100, on_model_pages=None):
    """``on_model_pages(n)`` is called after a fresh (not cached) vision
    extraction with the number of pages the local model read (#855 budget)."""
    from oncofiles.curated import extract as extract_mod

    ext = name.rsplit(".", 1)[-1]
    key = f"{hashlib.sha256(content).hexdigest()}:{mime_type}:{min_text_chars}:{ext}"
    if content and key in _extract_cache:
        hit = _extract_cache[key]
        if getattr(hit, "orientation", None):  # #882: a cache hit asked the model nothing
            hit = dataclasses.replace(hit, orientation={**hit.orientation, "model_calls": 0})
        if getattr(hit, "retried_pages", None):  # #931: nor did it retry a page
            hit = dataclasses.replace(hit, retried_pages=[])
        return hit
    ex = extract_mod.extract(name, content, mime_type, min_text_chars=min_text_chars)
    if on_model_pages is not None and ex.method == "vision" and ex.pages:
        on_model_pages(ex.pages)
    if content and ex.ok:  # never cache failures (the model may be back next time)
        _remember(_extract_cache, key, ex)
    return ex


def propose_cache_key(contract, text) -> str:
    """The proposal depends on the whole README the model reads (vocabulary, folder
    table, rules), not only on its version: a README edit or a vocabulary block the
    run itself added (#854, the runner then classifies with the re-parsed contract)
    must miss, and so must a change of the classify prompt. Under the default
    ``CURATED_CLASSIFY_PROMPT=v1`` the key is the pre-#926 one byte for byte
    (``classify.cache_version()`` = ``PROMPT_VERSION``); v2 adds its own prompt
    hash and the sampling mode."""
    from oncofiles.curated import classify

    readme_sha = hashlib.sha256(contract.readme_text.encode("utf-8")).hexdigest()
    return hashlib.sha256(
        f"{classify.cache_version()}\n{contract.version}\n{contract.patient_token}\n"
        f"{readme_sha}\n{text}".encode()
    ).hexdigest()


def cached_propose(contract, text):
    from oncofiles.curated import classify

    key = propose_cache_key(contract, text)
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
_NO_WRITE_KINDS = frozenset(
    {
        "llm_shadow",
        "clinical_preview",
        "extraction_scorecard",
        "lab_preview",
        "reocr_compare",
        "classify_scorecard",
        "chat_scorecard",
    }  # fmt: skip
)


def run_active() -> bool:
    """True while any curated run or re-OCR executes (on /health, CI deploys wait).

    An LLM-extract shadow job (#714), a clinical extraction preview (#726) or
    an extraction scorecard (#773) writes no facts, so a deploy may cut it
    (the scorecard's own summary row is then marked interrupted)."""
    return (
        any(r.get("state") == "running" for r in _runs.values())
        or any(
            j.get("state") == "running" and j.get("kind") not in _NO_WRITE_KINDS
            for j in _reocr_jobs.values()
        )
        # a sidecar restore (#806) writes Drive and re-indexes
        or any(j.get("state") == "running" for j in _restoring.values())
    )


def _confirm_blocked(pid: str) -> bool:
    """Does write-capable work for THIS patient block a caregiver's "Confirm
    as is" (#919)? A curated run, a restore/confirm slot, a held patient lock
    or a job that can write. A dry job (``_NO_WRITE_KINDS``: scorecards,
    previews, a re-OCR compare) writes nothing and never takes the lock, so it
    does not block one sidecar line (a classification scorecard runs for
    hours)."""
    if (_runs.get(pid) or {}).get("state") == "running":
        return True
    if (_restoring.get(pid) or {}).get("state") == "running":
        return True
    job = _reocr_jobs.get(pid) or {}
    if job.get("state") == "running" and job.get("kind") not in _NO_WRITE_KINDS:
        return True
    return patient_lock(pid).locked()


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
    # neither does a re-OCR compare (#883): it writes nothing, never takes the lock
    if job.get("state") == "running" and job.get("kind") not in ("llm_shadow", "reocr_compare"):
        return f"{job.get('kind') or 'reocr'}_in_progress"
    if patient_lock(pid).locked():
        return "patient_lock_held"
    return None


def patient_work_reason(pid: str, *, lock_held_by_caller: bool = False) -> str | None:
    """Why a Drive disconnect or an erasure of THIS patient must wait (#856).

    Stricter than :func:`run_block_reason`: ANY curated work of the patient
    counts, a dry job included (it holds the patient's Drive token and an LM
    Studio slot), and so does a restore / confirm slot. Another patient's work
    never does. Codes only. ``lock_held_by_caller``: the re-check made while
    the caller itself holds ``patient_lock(pid)`` (the lock is then not a reason).
    """
    if (_runs.get(pid) or {}).get("state") == "running":
        return "run_in_progress"
    if (_restoring.get(pid) or {}).get("state") == "running":
        return "restore_in_progress"
    job = _reocr_jobs.get(pid) or {}
    if job.get("state") == "running":
        return f"{job.get('kind') or 'reocr'}_in_progress"
    if not lock_held_by_caller and patient_lock(pid).locked():
        return "patient_lock_held"
    return None


def forget_patient(pid: str) -> None:
    """Drop the in-memory curated state of ONE patient (#856).

    After a Drive disconnect or an erasure: the inbox snapshot (file names
    shown on the dashboard), probe counters, watch / onboarding state and the
    last run summaries. Never touches running work (callers refuse while
    :func:`patient_work_reason` is set) nor the patient lock object.
    """
    for registry in (
        _inbox_snapshots,
        _probe_stats,
        _watch,
        _empty_streak,
        _onboarding,
        _readme_births,
        _text_reviews,
        _backfill_failures,
        _inflight_pages,
    ):
        registry.pop(pid, None)
    for registry in (_runs, _restoring, _reocr_jobs):
        if (registry.get(pid) or {}).get("state") != "running":
            registry.pop(pid, None)


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


# ── filed documents whose sidecar backfill got bad model answers ──────

# patient_id -> {drive file id: {"attempts": n, "code": reason}}. A filed
# document without a sidecar has no place on Drive for a counter, so the run
# reports carry it (``RunReport.documents``, stage backfill) and this map is
# their merge, seeded from the DB once per process like ``_text_reviews``.
_backfill_failures: dict[str, dict[str, dict]] = {}


def _remember_backfill_failures(pid: str, report: dict | None) -> None:
    from oncofiles.curated.runner import backfill_attempts_from

    seen = _backfill_failures.setdefault(pid, {})
    for file_id, entry in backfill_attempts_from(report).items():
        if entry["attempts"] >= seen.get(file_id, {}).get("attempts", 0):
            seen[file_id] = entry


async def backfill_failures(db, pid: str) -> dict[str, dict]:
    """Backfill model answer failures of earlier real runs (attempt counters)."""
    if pid not in _backfill_failures:
        _backfill_failures[pid] = {}
        for row in await db.list_curated_runs(patient_id=pid, limit=TEXT_REVIEW_SEED_RUNS):
            if row.get("dry_run"):
                continue
            full = await db.get_curated_run(row["id"], patient_id=pid)
            _remember_backfill_failures(pid, (full or {}).get("report"))
    return {k: dict(v) for k, v in _backfill_failures[pid].items()}


def _backfill_given_up(failures: dict[str, dict]) -> frozenset:
    from oncofiles.curated.runner import MAX_TECHNICAL_ATTEMPTS

    return frozenset(k for k, v in failures.items() if v["attempts"] >= MAX_TECHNICAL_ATTEMPTS)


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


# ── capacity (#855): daily model-page budget per patient ──────────────

# patient_id -> pages the model OCR'd in the run executing now (not yet in the
# DB). Written from the run's worker thread, so behind a lock.
_inflight_pages: dict[str, int] = {}
_pages_lock = threading.Lock()
HOLD_PAGE_BUDGET = "daily_page_budget"


def scheduling_settings() -> tuple[int, int]:
    """(daily page budget per patient, onboarding threshold) from runtime env.

    ``CURATED_DAILY_PAGE_BUDGET`` (default 300, 0 = off): pages the local model
    may OCR for one patient per UTC day, previews included, cache hits not.
    ``CURATED_ONBOARDING_MIN_PENDING`` (default 10): a patient whose first
    import is unfinished counts as onboarding once this many items are pending.
    """
    budget = max(0, int(os.environ.get("CURATED_DAILY_PAGE_BUDGET", "300")))
    min_pending = max(1, int(os.environ.get("CURATED_ONBOARDING_MIN_PENDING", "10")))
    return budget, min_pending


def _utc_day_start(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%dT00:00:00Z")


async def pages_used_today(db, pid: str, *, now: datetime | None = None) -> int:
    """Model pages of this patient's runs started this UTC day (persisted rows)
    plus the run executing now. Survives restarts: the rows carry the count."""
    done = await db.curated_model_pages_since(patient_id=pid, since=_utc_day_start(now))
    with _pages_lock:
        return done + _inflight_pages.get(pid, 0)


def _budget_gate(pid: str, pages_base: int):
    """Per-document check inside a run: stop starting new OCR once the day's
    budget is used (the document in progress finishes; the rest stays)."""
    budget, _ = scheduling_settings()
    if budget <= 0:
        return None

    def gate() -> str | None:
        with _pages_lock:
            used = pages_base + _inflight_pages.get(pid, 0)
        return HOLD_PAGE_BUDGET if used >= budget else None

    return gate


# ── core ──────────────────────────────────────────────────────────────


def _network_unavailable_error() -> dict:
    """#979: Google could not be reached (WAN/DNS outage). Transient: the
    connection is fine, so the wording never says "not connected"."""
    return {
        "error": "Google could not be reached (network unavailable); will retry",
        "code": NETWORK_UNAVAILABLE,
        "deferred": True,
    }


def _is_network_deferred(report: dict) -> bool:
    return report.get("code") == NETWORK_UNAVAILABLE


async def _prepare(db, pid: str, *, dry_run: bool, max_docs: int | None, max_actions: int):
    """Validate and build what a run needs. Returns (error_dict | None, ctx)."""
    from oncofiles.curated import llm
    from oncofiles.curated.runner import RunOptions
    from oncofiles.curated_folder import curated_reason
    from oncofiles.patient_context import get_context
    from oncofiles.server import _create_patient_clients

    if not llm.configured():
        return {"error": "local LLM not configured (LOCAL_LLM_BASE_URL / LOCAL_LLM_MODEL)"}, None
    if host_refused := llm.base_url_refusal():  # #925: say so before a run row exists
        return {"error": f"local LLM refused: {host_refused}"}, None
    if not dry_run and (reason := await preview_gate(db, pid)):
        return {"error": f"real run refused: {reason}"}, None
    from oncofiles.curated_folder import detection_error
    from oncofiles.server import patient_client_unavailable_reason

    clients = await _create_patient_clients(db, pid)
    if not clients or clients[0] is None:
        if patient_client_unavailable_reason(pid) == NETWORK_UNAVAILABLE:
            return _network_unavailable_error(), None  # #979: transient, not a disconnect
        return {
            "error": "Google Drive not connected for this patient (or integration disabled)"
        }, None
    gdrive, folder_id = clients[0], clients[3]
    reason = await asyncio.to_thread(curated_reason, gdrive, folder_id, use_cache=False)
    if reason != "is a curated folder" and detection_error(folder_id) == NETWORK_UNAVAILABLE:
        return _network_unavailable_error(), None
    if reason != "is a curated folder":
        return {
            "error": f"patient folder is not a curated root ({reason or 'no README contract'})"
        }, None

    patient_type = (get_context(pid) or {}).get("patient_type", "oncology")
    default_docs, min_chars, max_age = DEFAULTS.get(patient_type, DEFAULTS["oncology"])
    from oncofiles import config

    # #855: the cap and the page budget are checked before every OCR, for every
    # trigger (loop, nightly, MCP, admin). count_documents is a COUNT (no LIMIT).
    pages_base = await pages_used_today(db, pid)
    opts = RunOptions(
        dry_run=dry_run,
        max_docs=max_docs or default_docs,
        max_actions=max(1, max_actions),
        min_text_chars=min_chars,
        max_doc_age_days=max_age,
        should_stop=_stopping.is_set,
        reviewed_text=await reviewed_text_sources(db, pid),
        backfill_attempts=await backfill_failures(db, pid),
        doc_cap=max(0, config.MAX_DOCUMENTS_PER_PATIENT),
        docs_existing=await db.count_documents(patient_id=pid),
        ocr_gate=_budget_gate(pid, pages_base),
        # #919 item 4: filed documents held in review with no recognised reason
        stale_review_ids=await db.curated_stale_review_ids(patient_id=pid),
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
    """Run to completion and persist. Never raises; returns the report dict.

    The whole run is a local-only zone (#925): OCR and classification use the
    local model, and any cloud path inside (``enhance._get_client``, the Files
    API) raises ``CloudForbiddenError`` — also in the ``asyncio.to_thread``
    worker, which inherits the ContextVar. Covers every trigger: the MCP and
    dashboard runs, continuous mode and the nightly sweep."""
    from oncofiles.cloud_guard import local_only

    with local_only("curated run"):
        return await _execute_locked_zone(db, pid, gdrive, folder_id, opts, trigger=trigger)


async def _execute_locked_zone(db, pid: str, gdrive, folder_id: str, opts, *, trigger: str) -> dict:
    """Body of ``_execute_locked``; always called inside ``local_only``."""
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.runner import run_curated

    def _count(n: int) -> None:
        with _pages_lock:
            _inflight_pages[pid] = _inflight_pages.get(pid, 0) + n

    def _blocking():
        extract = functools.partial(cached_extract, on_model_pages=_count)
        kwargs = {"extract_fn": extract, "propose_fn": cached_propose}
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
    with _pages_lock:
        _inflight_pages[pid] = 0
    try:
        report = (await asyncio.to_thread(_blocking)).to_dict()
    except Exception as exc:  # runner catches everything; this is belt and braces
        logger.exception("curated run crashed for %s", pid)
        report = {"status": "aborted", "message": f"crashed ({type(exc).__name__})"}
    with _pages_lock:
        report["model_pages"] = _inflight_pages.get(pid, 0)
    if not opts.dry_run:
        _remember_text_reviews(pid, report)
        _remember_backfill_failures(pid, report)
        _note_run_outcome(pid, report)
    status, message = report.get("status", "aborted"), report.get("message", "")
    await db.finish_curated_run(
        row_id, patient_id=pid, status=status, message=message, report=report
    )
    with _pages_lock:  # now counted by the persisted row (pages_used_today)
        _inflight_pages[pid] = 0
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
    """Running state, else the last finished run (with its full report).

    #606: the report's numbers are a snapshot of that run, so a finished run
    carries ``as_of`` (when it ended), and ``live`` holds what the continuous
    probe last saw for THIS patient (see ``live_probe``).
    """
    live = live_probe(pid)
    extra = {"live": live} if live else {}
    running = _runs.get(pid)
    if running:
        return {
            "state": "running",
            **{k: v for k, v in running.items() if k != "state"},
            **extra,
        }
    last = await db.get_last_curated_run(patient_id=pid)
    if last is None:
        return {"state": "none", **extra}
    return {
        "state": "done",
        "as_of": last.get("finished_at") or last.get("started_at"),
        "as_of_note": AS_OF_NOTE,
        "run": last,
        **extra,
    }


AS_OF_NOTE = (
    "The numbers in `run` describe the folder at `as_of` (the end of that run), "
    "not now. Current state: `live`, when present."
)


def live_probe(pid: str) -> dict | None:
    """What the continuous probe last saw for ONE patient (#606), or None when
    this process has not probed the patient yet. In-memory only: no Drive call,
    no DB call, and never another patient's counters."""
    st = _probe_stats.get(pid)
    if st is None or st.last_probe_at is None or st.pending_inbox_count is None:
        return None
    return {
        "as_of": st.last_probe_at,
        "pending_inbox_count": st.pending_inbox_count,
        "note": "Inbox files the next automatic run would process, as seen by the last probe.",
    }


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
# deploys new work is at least seen (and counted) right away. The APScheduler
# uses the same grace: no job's first slot falls inside it (#784,
# server._defer_jobs_past_startup_grace).
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

# ── heartbeat and loop detection (in-memory; operator /readiness, #1027) ──
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
    """An age and a boolean (no counts, ids or names); operator /readiness since #1027."""
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
        "scheduling": scheduling_detail(),  # #855
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
    # #855 fair scheduling: "active" | "queued" | "done" | None (ordinary)
    onboarding: str | None = None
    onboarding_queue_position: int | None = None  # 0 = active, 1.. = waiting
    pages_today: int | None = None  # model pages this UTC day (when a budget is set)
    page_budget: int | None = None
    last_held: dict[str, int] = field(default_factory=dict)  # reason -> files, last turn
    held_total: dict[str, int] = field(default_factory=dict)  # reason -> files, all turns


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


# ── inbox snapshot for the dashboard (#921 follow-up) ────────────────
#
# The curated read model indexes filed folders only, so a file a caregiver
# just put into the inbox would appear in the dashboard table only once it is
# filed. The probe already lists the inbox every poll; it keeps here, per
# patient and in memory only, what it saw: Drive id, name, size, mime,
# modifiedTime and whether a sidecar holds the file for review. The dashboard
# route reads it (no Drive call, no DB write); the upload route adds its file
# right away. Never on /health, never logged (names are patient data), gone
# on restart. A snapshot no probe confirmed for INBOX_SNAPSHOT_MAX_AGE_S
# (continuous mode off, Drive unreachable for long) is treated as unknown.

INBOX_SNAPSHOT_MAX_AGE_S = 30 * 60


@dataclass
class _InboxSnapshot:
    signature: str | None  # tree fingerprint of the probe that built it
    taken_at: float  # time.monotonic() of the last probe that confirmed it
    entries: dict[str, dict] = field(default_factory=dict)  # drive id -> entry


_inbox_snapshots: dict[str, _InboxSnapshot] = {}


def _inbox_entry(e) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "size": e.size,
        "mime": e.mime,
        "modified": e.modified,
        "review": bool(e.review),
    }


def record_inbox_snapshot(pid: str, pr) -> None:
    """Keep what a probe saw of the inbox. An idle repeat (``pr.inbox is None``)
    of the same tree only confirms the snapshot it already has."""
    inbox = getattr(pr, "inbox", None)
    now = time.monotonic()
    if inbox is None:
        snap = _inbox_snapshots.get(pid)
        if snap is not None and snap.signature == getattr(pr, "signature", None):
            snap.taken_at = now
        return
    _inbox_snapshots[pid] = _InboxSnapshot(
        pr.signature, now, {e.id: _inbox_entry(e) for e in inbox}
    )


def note_inbox_upload(pid: str, *, drive_id: str, name: str, size: int, mime: str) -> None:
    """The dashboard upload put a file into this patient's inbox: show it now,
    before the next probe (which replaces the whole snapshot anyway)."""
    if not pid or not drive_id:
        return
    snap = _inbox_snapshots.get(pid)
    if snap is None or time.monotonic() - snap.taken_at > INBOX_SNAPSHOT_MAX_AGE_S:
        snap = _inbox_snapshots[pid] = _InboxSnapshot(None, time.monotonic())
    snap.entries[drive_id] = {
        "id": drive_id,
        "name": name,
        "size": int(size or 0),
        "mime": mime,
        "modified": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "review": False,
    }


def inbox_snapshot(pid: str | None) -> list[dict]:
    """Copies of this patient's inbox entries, [] when unknown or stale."""
    snap = _inbox_snapshots.get(pid) if pid else None
    if snap is None or time.monotonic() - snap.taken_at > INBOX_SNAPSHOT_MAX_AGE_S:
        return []
    return [dict(e) for e in snap.entries.values()]


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
    given_up = _backfill_given_up(await backfill_failures(db, pid))

    def _blocking():
        own = gdrive.dedicated_copy()
        try:
            return probe(
                DriveOps(own),
                folder_id,
                idle_signature=idle_signature,
                reviewed_text=reviewed,
                backfill_given_up=given_up,
            )
        finally:
            own.close()

    result = await asyncio.to_thread(_blocking)
    global _last_probe_at
    _last_probe_at = time.monotonic()
    record_inbox_snapshot(pid, result)
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


# ── fair scheduling (#855): one local model, many patients ────────────
#
# A ROUND probes every enabled patient (read-only), then gives each patient
# with settled work at most ONE bounded turn: a preview when needed plus one
# real run of CURATED_CHUNK_DOCS documents. Patients already past their first
# import go first (rotating start), the one active onboarding patient last, so
# an existing patient's new file waits at most one round plus one chunk.
#
# ONBOARDING = the first import is unfinished: no real run of this patient has
# ever ended ok with nothing left for a later run (``inbox_drained``; older
# rows: an empty ``not_processed``), AND at least
# CURATED_ONBOARDING_MIN_PENDING items are runnable. A smaller first import is
# an ordinary patient. At most one onboarding patient gets turns; the others
# wait in a FIFO queue — a patient that already started its import first,
# then by enrolment time (curated_settings.updated_at), then id. The active
# one keeps its slot until its import finishes or it has nothing runnable.

_import_done: set[str] = set()  # first import finished (sticky for the process)
_onboarding: dict[str, tuple] = {}  # pid -> FIFO key, active one included
_onboarding_active: str | None = None
_round_no = 0
ROUND_PAUSE_S = 5  # between rounds while work is flowing (a turn takes minutes)


def onboarding_queue() -> list[str]:
    """Waiting onboarding patients, FIFO order (the active one excluded)."""
    return sorted((p for p in _onboarding if p != _onboarding_active), key=_onboarding.get)


def _release_onboarding(pid: str) -> None:
    global _onboarding_active
    _onboarding.pop(pid, None)
    if _onboarding_active == pid:
        _onboarding_active = None


def _pick_onboarding() -> str | None:
    global _onboarding_active
    if _onboarding_active not in _onboarding:
        _onboarding_active = min(_onboarding, key=_onboarding.get) if _onboarding else None
    _refresh_onboarding_stats()
    return _onboarding_active


def _refresh_onboarding_stats() -> None:
    queue = onboarding_queue()
    for pid, st in _probe_stats.items():
        if pid == _onboarding_active:
            st.onboarding, st.onboarding_queue_position = "active", 0
        elif pid in _onboarding:
            st.onboarding, st.onboarding_queue_position = "queued", queue.index(pid) + 1
        else:
            st.onboarding = "done" if pid in _import_done else None
            st.onboarding_queue_position = None


async def _note_onboarding(db, pid: str, n_work: int, *, canary: bool = False) -> None:
    """Classify a probed patient (see the block comment above)."""
    if pid in _import_done:
        return
    if n_work == 0 and not canary:
        _release_onboarding(pid)  # nothing runnable: never hold the slot idle
        return
    if pid in _onboarding:
        return
    _, min_pending = scheduling_settings()
    if n_work < min_pending:
        return  # a small first import is an ordinary patient
    state = await db.curated_import_state(patient_id=pid)
    if state["done"]:
        _import_done.add(pid)
        return
    _onboarding[pid] = (0 if state["started"] else 1, state["enrolled_at"] or "", pid)


def _note_import_progress(pid: str, reports: list[dict]) -> None:
    if any(
        not r.get("dry_run") and r.get("status") == "ok" and r.get("inbox_drained") for r in reports
    ):
        _import_done.add(pid)
        _release_onboarding(pid)


async def _scheduling_reason(db, pid: str) -> str | None:
    """Why the fair scheduler gives this patient no run now, or None."""
    st = _stats(pid)
    if pid in _onboarding and _pick_onboarding() != pid:
        return "onboarding_queue"
    budget, _ = scheduling_settings()
    st.page_budget = budget
    if budget > 0:
        st.pages_today = await pages_used_today(db, pid)
        if st.pages_today >= budget:
            return HOLD_PAGE_BUDGET  # until the next UTC day
    return None


def _note_held(pid: str, reports: list[dict]) -> None:
    st = _stats(pid)
    last: dict[str, int] = {}
    for r in reports:
        for h in r.get("held") or []:
            _bump(last, h.get("reason") or "unknown")
    for reason, n in last.items():
        st.held_total[reason] = st.held_total.get(reason, 0) + n
    st.last_held = last


@dataclass
class _Found:
    """A probe with settled work, waiting for its turn this round."""

    probe: object
    work: set


async def _probe_for_work(db, pid: str, *, settle_s: int) -> tuple[object | None, set]:
    """Probe and update counters. Returns (probe, runnable work); the probe is
    None when there is nothing to run now (idle, unreachable or unsettled)."""
    w = _watch.setdefault(pid, _Watch())
    st = _stats(pid)
    try:
        pr = await probe_patient(db, pid, w.idle_signature)
    except Exception:
        _bump(st.probes_skipped, "error")
        raise
    if pr is None:
        _bump(st.probes_skipped, "drive_unavailable")
        return None, set()
    st.probes_total += 1
    st.last_probe_at = _now_iso()
    if w.idle_signature is None or pr.signature != w.idle_signature:
        st.pending_inbox_count = len(pr.pending)  # an idle repeat reads no sidecars
    work = set(pr.pending) - w.stuck
    if not work and not pr.canary:
        w.idle_signature = pr.signature
        return None, work
    if pr.newest_age_s is not None and pr.newest_age_s < settle_s:
        return None, work  # uploads still arriving — wait for the batch to settle
    return pr, work


async def _run_turn(
    db, pid: str, pr, work: set, *, chunk: int, max_runs: int, grace_until: float
) -> list[dict] | None:
    """One bounded turn for a patient with settled work: the gates, then a short
    sequence. A blocked turn is logged once and retried next round."""
    w = _watch.setdefault(pid, _Watch())
    st = _stats(pid)
    now = time.monotonic()
    if now < grace_until:
        reason = "startup_grace"
    elif now < w.not_before:
        reason = "failure_backoff"
    else:
        reason = run_block_reason(pid) or await _scheduling_reason(db, pid)
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
    _note_held(pid, reports)
    _note_import_progress(pid, reports)
    if st.page_budget:
        st.pages_today = await pages_used_today(db, pid)
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


async def continuous_tick(
    db, pid: str, *, settle_s: int, chunk: int, max_runs: int, grace_until: float = 0.0
) -> list[dict] | None:
    """One patient on its own: probe, then a turn when there is new, settled
    work. Never raises for an ordinary outcome; returns the reports when a
    sequence ran. The loop uses ``continuous_round`` (all patients, fair).

    The probe always happens (it only reads Drive). What may wait is the RUN:
    startup grace, failure backoff, this patient's own write-capable work
    (``run_block_reason``), the onboarding queue or the daily page budget.
    """
    pr, work = await _probe_for_work(db, pid, settle_s=settle_s)
    await _note_onboarding(db, pid, len(work), canary=bool(pr and pr.canary))
    if pr is None:
        return None
    return await _run_turn(
        db, pid, pr, work, chunk=chunk, max_runs=max_runs, grace_until=grace_until
    )


async def continuous_round(
    db, pids: list[str], *, settle_s: int, chunk: int, grace_until: float = 0.0
) -> bool:
    """Probe every patient, then one bounded turn (one real run of ``chunk``
    documents) per patient with settled work. True when a turn moved
    something, so the loop starts the next round right away."""
    global _round_no
    found: dict[str, _Found] = {}
    for pid in pids:  # phase 1: read-only probes, never blocked
        if _stopping.is_set():
            return False
        try:
            pr, work = await _probe_for_work(db, pid, settle_s=settle_s)
            await _note_onboarding(db, pid, len(work), canary=bool(pr and pr.canary))
        except Exception:
            logger.exception("curated auto %s: probe failed", pid)
            continue
        if pr is not None:
            found[pid] = _Found(pr, work)
    for pid in [p for p in _onboarding if p not in pids]:  # no longer enabled
        _release_onboarding(pid)
    active = _pick_onboarding()
    steady = [p for p in pids if p in found and p not in _onboarding]
    if steady:
        k = _round_no % len(steady)
        steady = steady[k:] + steady[:k]
    _round_no += 1
    order = steady + [p for p in pids if p in found and p in _onboarding]
    progressed_any = False
    for pid in order:  # phase 2: turns (queued onboarding patients log "onboarding_queue")
        if _stopping.is_set():
            break
        f = found[pid]
        try:
            reports = await _run_turn(
                db, pid, f.probe, f.work, chunk=chunk, max_runs=1, grace_until=grace_until
            )
        except Exception:
            logger.exception("curated auto %s: turn failed", pid)
            continue
        if reports and _sequence_outcome(reports)[1]:
            progressed_any = True
    _refresh_onboarding_stats()
    if active != _onboarding_active:
        logger.info("curated onboarding: active patient changed to %s", _onboarding_active)
    return progressed_any


def scheduling_detail() -> dict:
    """Operator/admin view of the fair scheduler (ids and counts only)."""
    budget, min_pending = scheduling_settings()
    from oncofiles import config

    return {
        "onboarding_active": _onboarding_active,
        "onboarding_queue": onboarding_queue(),
        "onboarding_min_pending": min_pending,
        "daily_page_budget": budget,
        "document_cap": config.MAX_DOCUMENTS_PER_PATIENT,
    }


# #880: startup index telemetry for operator /readiness (counts only).
_startup_index_stats: dict = {}


def startup_index_stats() -> dict:
    """Counts of the startup index (#880): who was due and how many were done."""
    return dict(_startup_index_stats)


async def _startup_index_targets(db) -> set[str]:
    """Patients to index once per start: every enrolled patient, plus every
    patient whose curated mirror rows were indexed by an older INDEX_VERSION,
    enrolled or not (#880). The read model must not depend on enrolment."""
    from oncofiles.curated.indexer import INDEX_VERSION

    enrolled = set(await db.list_curated_nightly_patients())
    stale = set(await db.list_stale_curated_index_patients(INDEX_VERSION))
    _startup_index_stats.clear()
    _startup_index_stats.update(
        {
            "index_version": INDEX_VERSION,
            "enrolled": len(enrolled),
            "stale_index": len(stale),
            "stale_not_enrolled": len(stale - enrolled),
            "due": len(enrolled | stale),
            "indexed": 0,
            "failed": 0,
            "pending": len(enrolled | stale),
        }
    )
    return enrolled | stale


async def _startup_index(db, pending: set[str]) -> None:
    """Read model once per start (#603), after the grace. A patient whose lock
    is held (a job started meanwhile) is retried next iteration instead of
    stalling the loop behind it."""
    for pid in sorted(pending):
        if _stopping.is_set():
            break
        if patient_lock(pid).locked():
            continue
        ok = await _index_quietly(db, pid)
        pending.discard(pid)
        key = "indexed" if ok else "failed"
        _startup_index_stats[key] = _startup_index_stats.get(key, 0) + 1
    _startup_index_stats["pending"] = len(pending)


# ── nightly sweep + same-night retry on a network outage (#979) ───────
#
# A WAN/DNS outage during the sweep makes token refresh / Drive detection fail
# with ``network_unavailable`` (``_network_unavailable_error``). Such a patient
# is DEFERRED, not "not connected": the sweep is retried for the deferred
# patients only, first after NIGHTLY_RETRY_FIRST_S, then every
# NIGHTLY_RETRY_MAX_INTERVAL_S, at most NIGHTLY_RETRY_MAX_ATTEMPTS times, and
# an attempt STARTS only before NIGHTLY_RETRY_CUTOFF local time (the 06:00
# backup window must stay free). Then the next night / continuous rounds take
# over. Only ``network_unavailable`` defers; every other outcome is unchanged.
# Daily mode only: with CURATED_NIGHTLY_HOUR=every the next hourly slot is the
# retry. Counts on operator /readiness only (``nightly_retry_stats``).
NIGHTLY_RETRY_FIRST_S = 15 * 60
NIGHTLY_RETRY_MAX_INTERVAL_S = 30 * 60
NIGHTLY_RETRY_MAX_ATTEMPTS = 8
NIGHTLY_RETRY_CUTOFF = (5, 45)  # local (Europe/Bratislava) hour, minute

_nightly_retry: dict | None = None  # the pending retry of this night, or None
_nightly_retry_outcome: str | None = None  # deferred | recovered | gave_up
_nightly_retry_counts: dict[str, int] = {
    "deferred_total": 0,  # patient deferrals (network_unavailable) since start
    "retries_total": 0,  # retry sweeps run
    "recovered_total": 0,  # deferred patients a retry got through
    "gave_up_total": 0,  # deferred patients left for the next night
}


def nightly_retry_stats() -> dict:
    """Operator /readiness view (#979): codes, counts and times only."""
    r = _nightly_retry
    pending = None
    if r is not None:
        pending = {
            "code": NETWORK_UNAVAILABLE,
            "deferred": len(r["pids"]),
            "attempts": r["attempts"],
            "next_at": r["next_at"].isoformat(timespec="seconds"),
            "deadline": r["deadline"].isoformat(timespec="seconds"),
        }
    return {**_nightly_retry_counts, "last_outcome": _nightly_retry_outcome, "pending": pending}


def _end_nightly_retry(outcome: str | None) -> None:
    global _nightly_retry, _nightly_retry_outcome
    _nightly_retry = None
    if outcome is not None:
        _nightly_retry_outcome = outcome


def _retry_deadline(now: datetime) -> datetime:
    """The first NIGHTLY_RETRY_CUTOFF (local) strictly after ``now``."""
    h, m = NIGHTLY_RETRY_CUTOFF
    cut = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return cut if cut > now else cut + timedelta(days=1)


def _schedule_nightly_retry(slot: str, deferred: set[str], now: datetime, *, attempted: bool):
    """Book (or drop) the next retry after a sweep (``attempted`` = it was one)."""
    global _nightly_retry, _nightly_retry_outcome
    c = _nightly_retry_counts
    r = _nightly_retry if attempted else None
    if r is not None:
        r["attempts"] += 1
        c["retries_total"] += 1
        c["recovered_total"] += len(set(r["pids"]) - deferred)
    if not deferred:
        if r is not None:
            logger.info("curated nightly retry: all deferred patients done")
        _end_nightly_retry("recovered" if r is not None else None)
        return
    c["deferred_total"] += len(deferred)
    if r is None:
        r = {"slot": slot, "pids": [], "attempts": 0, "deadline": _retry_deadline(now)}
    delay = min(NIGHTLY_RETRY_FIRST_S * 2 ** r["attempts"], NIGHTLY_RETRY_MAX_INTERVAL_S)
    next_at = now + timedelta(seconds=delay)
    if r["attempts"] >= NIGHTLY_RETRY_MAX_ATTEMPTS or next_at >= r["deadline"]:
        c["gave_up_total"] += len(deferred)
        _end_nightly_retry("gave_up")
        logger.warning(
            "curated nightly: %d patient(s) still %s after %d retr(ies); "
            "left for continuous rounds / the next night",
            len(deferred),
            NETWORK_UNAVAILABLE,
            r["attempts"],
        )
        return
    r.update(pids=sorted(deferred), next_at=next_at)
    _nightly_retry, _nightly_retry_outcome = r, "deferred"
    logger.info(
        "curated nightly: %d patient(s) deferred (%s), retry %d in %d min",
        len(deferred),
        NETWORK_UNAVAILABLE,
        r["attempts"] + 1,
        delay // 60,
    )


def _due_nightly_retry(now: datetime) -> list[str] | None:
    """Patients of a retry that is due now, or None."""
    r = _nightly_retry
    if r is None or now < r["next_at"]:
        return None
    if now >= r["deadline"]:  # the loop was late (a long round): never start past the cutoff
        _nightly_retry_counts["gave_up_total"] += len(r["pids"])
        _end_nightly_retry("gave_up")
        logger.warning(
            "curated nightly: retry for %d patient(s) dropped at the cutoff (%s)",
            len(r["pids"]),
            NETWORK_UNAVAILABLE,
        )
        return None
    return list(r["pids"])


async def _nightly_sweep(db, pids: list[str], *, max_runs: int, reuse_preview: bool) -> set[str]:
    """The full nightly sequence for ``pids``; returns the patients deferred
    because Google could not be reached (``network_unavailable``)."""
    deferred: set[str] = set()
    for pid in pids:
        if _stopping.is_set():
            break
        if reason := run_block_reason(pid) or await _scheduling_reason(db, pid):
            logger.info("curated nightly %s: skipped (%s)", pid, reason)
            continue
        reports = await nightly_for_patient(
            db,
            pid,
            max_real_runs=max_runs,
            reuse_preview=reuse_preview,
            skip_if_busy=True,
        )
        if any(_is_network_deferred(r) for r in reports):
            deferred.add(pid)  # nothing was done: not "ok", not "disconnected"
            logger.info("curated nightly %s: deferred (%s)", pid, NETWORK_UNAVAILABLE)
            continue
        _note_held(pid, reports)
        _note_import_progress(pid, reports)
        if not any(r.get("blocked") for r in reports):
            _watch.pop(pid, None)  # the sweep retried everything
        logger.info(
            "curated nightly %s: %s",
            pid,
            [(r.get("dry_run"), r.get("status") or r.get("error")) for r in reports],
        )
    return deferred


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
      makes the sweep hourly. A patient Google could not be reached for
      (``network_unavailable``, #979) is retried later the same night.

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
        cont, cont_poll, settle, chunk, flowing = False, 300, 0, 5, False
        try:
            enabled, hour, max_runs = nightly_settings()
            cont, cont_poll, settle, chunk = continuous_settings()
            in_grace = time.monotonic() < grace_until
            if not in_grace:
                if to_index is None:
                    to_index = await _startup_index_targets(db)
                if to_index:
                    await _startup_index(db, to_index)
            now = datetime.now(tz)
            slot = now.strftime("%Y-%m-%d") if hour is not None else now.strftime("%Y-%m-%dT%H")
            due = hour is None or now.hour == hour
            if enabled and due and last_night != slot and not in_grace:
                last_night = slot
                _end_nightly_retry(None)  # a new night starts clean
                deferred = await _nightly_sweep(
                    db,
                    await db.list_curated_nightly_patients(),
                    max_runs=max_runs,
                    reuse_preview=hour is None,
                )
                if hour is not None:  # hourly mode: the next slot is the retry
                    _schedule_nightly_retry(slot, deferred, now, attempted=False)
            elif enabled and (pids := _due_nightly_retry(now)) is not None and not in_grace:
                enrolled = set(await db.list_curated_nightly_patients())
                deferred = await _nightly_sweep(
                    db, [p for p in pids if p in enrolled], max_runs=max_runs, reuse_preview=False
                )
                _schedule_nightly_retry(slot, deferred, now, attempted=True)
            elif enabled and cont:
                # #855: one fair round; while turns move work, the next round
                # starts after a short pause instead of a full poll interval.
                flowing = await continuous_round(
                    db,
                    await db.list_curated_nightly_patients(),
                    settle_s=settle,
                    chunk=chunk,
                    grace_until=grace_until,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("curated loop iteration failed")
        pause = poll_s or (cont_poll if cont else 300)
        await asyncio.sleep(min(pause, ROUND_PAUSE_S) if flowing else pause)


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


async def _index_patient_lock_held(db, pid: str, *, force_ids: set[int] | None = None) -> dict:
    """``index_patient`` for a caller that already holds ``patient_lock(pid)``
    (the sidecar restore, #806): the same context load and local-only zone."""
    from oncofiles.cloud_guard import local_only

    try:
        from oncofiles import patient_context

        await patient_context.load_from_db(db.db, pid)
    except Exception:
        logger.warning("curated index: patient context not loaded for %s", pid)
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
                        # #919: codes and a boolean only (document_stage reads them)
                        "review_reasons": rec.review_reasons,
                        "review_override": rec.review_override,
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
        # never a Drive-only row the legacy sync/upload created (#777)
        if gdrive_id not in seen and not row["deleted"] and not row.get("foreign"):
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


async def _index_quietly(db, pid: str) -> bool:
    """Index a patient; False (logged) when it raised. Never propagates."""
    try:
        await index_patient(db, pid)
    except Exception:
        logger.exception("curated index failed for %s", pid)
        return False
    return True


_reocr_jobs: dict[str, dict] = {}


_NOT_OCR = ("application/vnd.google-apps.", "application/vnd.openxmlformats")


async def _reocr_targets(
    db,
    pid: str,
    document_ids: list[int] | None,
    *,
    only_with_facts: bool = True,
    excluded: list[dict] | None = None,
    log_names: list[str] | None = None,
    excluded_counts: dict[str, int] | None = None,
    exclude_ids: set[int] | frozenset[int] = frozenset(),
) -> list[dict]:
    """Documents worth reading again: lab sheets first, then most unverified
    facts first (#674). By default only those with unverified facts (the
    benchmark, #658). Listed ``document_ids`` are marked explicit: only they
    may be read again after an earlier re-OCR or a deliberate restore.

    Raw genotype data is never a candidate (``llm_input.reocr_exclusion``,
    #815): it is never downloaded nor sent to a vision pass; its skipped
    entry (reason ``raw_genotype_excluded``) goes to ``excluded``. Any other
    text source (``extract.TEXT_EXTS``: md / txt / csv / json) is its own text,
    not a scan, and is never a candidate either.

    ``log_names`` (the logs folder listing, #894): a document the runner
    would skip as handled before (``reocr.auto_selection_skip``, the runner's
    own rule) is left out here, so the caller's ``limit`` is filled with
    documents that will be read. Every document left out for one of these
    reasons is counted by reason in ``excluded_counts`` (counts only).

    ``exclude_ids`` (default selection only): the caller's own list of
    documents to leave out, dropped before the limit and counted as
    ``excluded_by_caller``. Only this patient's candidates are compared, so
    an id of another patient simply never matches (no existence oracle)."""
    from oncofiles.curated import extract as extract_mod
    from oncofiles.curated import llm_input
    from oncofiles.curated.reocr import auto_selection_skip, needs_reocr

    def drop(reason: str) -> None:
        if excluded_counts is not None:
            excluded_counts[reason] = excluded_counts.get(reason, 0) + 1

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
        if not document_ids and r["id"] in exclude_ids:
            drop("excluded_by_caller")
            continue  # the caller asked to leave it out of the default selection
        if reason := await llm_input.reocr_exclusion(db, pid, r):
            if excluded is not None:
                excluded.append(llm_input.skipped_entry(r, reason))
            drop(reason)
            continue  # raw genotype data: never re-OCR'd, never to a vision pass
        if (r.get("mime_type") or "").startswith(_NOT_OCR):
            drop("not_a_scan")
            continue  # exported or spreadsheet text, not a scan
        if extract_mod._ext(r.get("filename") or "") in extract_mod.TEXT_EXTS:
            drop("text_source")
            continue  # a text source is its own text, not a scan
        reasons = needs_reocr(meta, ["[?]"] if r["uncertain"] else [])
        if not reasons or not cur.get("sidecar_id") or not r.get("gdrive_id"):
            continue
        if log_names is not None and (
            skip := auto_selection_skip(
                {"filename": r["filename"], "explicit": bool(document_ids)}, log_names
            )
        ):
            drop(skip)
            continue  # #894: the runner would skip it; it must not use up the limit
        out.append({
            "document_id": r["id"], "filename": r["filename"], "gdrive_id": r["gdrive_id"],
            "sidecar_id": cur["sidecar_id"], "mime": r.get("mime_type") or "",
            "engine": str(meta.get("ocr_engine") or meta.get("extraction") or ""),
            "reasons": reasons, "unverified_facts": r.get("unverified_facts") or 0,
            "category": r.get("category"), "explicit": bool(document_ids),
        })  # fmt: skip
    return out


async def _with_trusted_codes(db, pid: str, docs: list[dict]) -> list[dict]:
    """Each document with ``trusted_codes``: the codes of its active stored
    facts that are machine-verified, verified or corrected (#831). Under the
    Tesseract profile losing such a code still stops a rewrite. A failed
    lookup leaves ``None`` (unknown), which fails closed in ``reocr``."""
    try:
        found = await db.trusted_fact_codes(
            patient_id=pid, document_ids=[d["document_id"] for d in docs]
        )
    except Exception as exc:
        logger.warning("trusted fact lookup failed for %s: %s", pid[:8], type(exc).__name__)
        return [{**d, "trusted_codes": None} for d in docs]
    return [{**d, "trusted_codes": sorted(found.get(d["document_id"]) or ())} for d in docs]


async def _reocr_job(
    db, pid: str, docs: list[dict], *, dry_run: bool, passes: int,
    excluded: list[dict] | None = None,
) -> None:  # fmt: skip
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
            docs = await _with_trusted_codes(db, pid, docs)  # under the lock: no index now
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
        # raw genotype data (#815): never read, listed as skipped in the report
        report["documents"] = [*(excluded or []), *report["documents"]]
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
    untrusted = Counter(r for d in docs for r in d.get("baseline_untrusted") or [])
    if untrusted:  # #831: reason codes muted by the Tesseract baseline profile
        counts["baseline_untrusted"] = dict(sorted(untrusted.items()))
        counts["profile_documents"] = sum(1 for d in docs if d.get("profile"))
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


async def _reocr_log_names(db, pid: str) -> list[str] | None:
    """The names in the patient's curated logs folder, listed once for a
    re-OCR plan (#894; a constant number of Drive reads, never per document).
    ``None`` when Drive is not connected or the listing fails (logged; the
    plan then falls back to the runner's own skip)."""
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.reocr import logs_listing
    from oncofiles.server import _create_patient_clients

    try:
        clients = await _create_patient_clients(db, pid)
        if not clients or clients[0] is None:
            return None
        gdrive, folder_id = clients[0], clients[3]

        def _blocking():
            own = gdrive.dedicated_copy()
            try:
                return logs_listing(DriveOps(own), folder_id)[1]
            finally:
                own.close()

        return await asyncio.to_thread(_blocking)
    except Exception as exc:
        logger.warning("re-OCR plan: logs listing failed for %s: %s", pid[:8], type(exc).__name__)
        return None


REOCR_DEFAULT_LIMIT = 3  # documents picked when no document_ids are given
REOCR_MAX_DOCS = 10  # explicit document_ids per call, as the other explicit-id tools
REOCR_MAX_EXCLUDE = 50  # exclude_ids per call (default selection only)


async def start_reocr(
    db,
    pid: str,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    document_ids: list[int] | None = None,
    passes: int = 2,
    only_with_facts: bool = True,
    actor: str = "mcp",
    exclude_ids: list[int] | None = None,
) -> dict:
    """Start a re-OCR job in the background (#660). Shared by the MCP tool and
    the admin dashboard API (#573); the caller has already resolved ``pid``.
    A real run first makes a verified safety copy of the DB (``pre_store``).

    ``limit`` defaults to ``REOCR_DEFAULT_LIMIT`` without ids and to
    ``len(document_ids)`` with them; explicit ids are never truncated
    silently: more than ``REOCR_MAX_DOCS`` ids, or a ``limit`` below their
    count, is refused with an error.

    ``exclude_ids`` (at most ``REOCR_MAX_EXCLUDE``) applies only to the
    default selection: those documents are dropped before the limit and
    counted as ``excluded_by_caller``; combined with ``document_ids`` it is
    refused. Ids that are not this patient's candidates are ignored."""
    if exclude_ids:
        if document_ids:
            return {"error": "exclude_ids only applies to the default selection; "
                             "pass either document_ids or exclude_ids, not both"}  # fmt: skip
        if len(exclude_ids) > REOCR_MAX_EXCLUDE:
            return {"error": f"at most {REOCR_MAX_EXCLUDE} exclude_ids per call, "
                             f"got {len(exclude_ids)}"}  # fmt: skip
    if document_ids:
        if len(document_ids) > REOCR_MAX_DOCS:
            return {"error": f"at most {REOCR_MAX_DOCS} document_ids per call, "
                             f"got {len(document_ids)}"}  # fmt: skip
        if limit is None:
            limit = len(document_ids)
        elif limit < len(document_ids):
            return {"error": f"limit {limit} is smaller than the {len(document_ids)} "
                             "document_ids given; pass fewer ids or omit limit"}  # fmt: skip
    elif limit is None:
        limit = REOCR_DEFAULT_LIMIT
    busy = "busy: a run or re-OCR is in progress for this patient"
    if _busy(pid):
        return {"error": busy}
    excluded: list[dict] = []
    counts: dict[str, int] = {}
    # #894: the default selection drops what the runner would skip BEFORE the
    # limit; that needs the logs folder, listed once here (explicit ids: the
    # runner reads them again anyway, so no listing and no change)
    log_names = None if document_ids else await _reocr_log_names(db, pid)
    targets = await _reocr_targets(
        db, pid, document_ids, only_with_facts=only_with_facts, excluded=excluded,
        log_names=log_names, excluded_counts=counts, exclude_ids=set(exclude_ids or ()),
    )  # fmt: skip
    plan: dict = {"excluded_before_limit": dict(sorted(counts.items()))}
    if not document_ids and log_names is None:
        plan["logs_listing"] = "unavailable"  # the runner still skips handled documents
    docs = targets[: max(0, min(limit, 25))]
    if not docs:
        out: dict = {"status": "nothing to do", "candidates": len(targets), **plan}
        if excluded:
            out["skipped"] = excluded
        return out
    job = {
        "state": "running", "kind": "reocr", "dry_run": dry_run, "passes": passes,
        "started_at": datetime.now(UTC).isoformat(), "candidates": len(targets),
        "actor": actor, "skipped": excluded, **plan,
        "documents": [
            {k: d[k] for k in ("document_id", "filename", "category", "reasons",
                               "unverified_facts")}
            for d in docs
        ],
    }  # fmt: skip
    return await _launch(
        db, pid, job,
        lambda: _reocr_job(db, pid, docs, dry_run=dry_run, passes=passes,
                           **({"excluded": excluded} if excluded else {})),
        dry_run=dry_run, busy=busy,
    )  # fmt: skip


async def curated_reocr(
    ctx: Context,
    patient_slug: str | None = None,
    dry_run: bool = True,
    limit: int | None = None,
    document_ids: list[int] | None = None,
    passes: int = 2,
    only_with_facts: bool = True,
    exclude_ids: list[int] | None = None,
) -> str:
    """Read uncertain documents again with the LOCAL model (#660): Cowork-era
    transcriptions and pages with [?]. Two vision passes with high reasoning at
    two resolutions (a PDF text layer is used as-is); digits are accepted only
    where both passes agree. Dry run (default) writes nothing and previews how
    many values would become machine-verified. A real run backs the old sidecar
    up to the logs folder, then updates it in place, then re-indexes. When the
    new text would lose something (result ``review``) the sidecar is left as it
    is and the new text is kept next to it in the logs folder
    (``_reocr-candidate-…``, its Drive id in ``candidate``); an admin can
    promote it with curated_restore_sidecar. Runs in the background; poll
    curated_reocr_status. Slow: minutes per page.
    When the OLD sidecar was written by Tesseract (``ocr_engine: tesseract…``)
    the entry has ``profile: tesseract_baseline``: numbers, lab values and words
    only the old text had are listed as ``baseline_untrusted`` and do not stop
    the rewrite; a lost lab value that has a machine-verified, verified or
    corrected stored fact, lost dates, a shrunk text, a near-empty page that has ink on
    the scan (a blank back side is fine) or too many uncertain numbers still
    do. curated_promote_candidates applies the same rules to candidates kept
    earlier.
    By default only documents with unverified facts are chosen, most first
    (only_with_facts=false: every Cowork-era or uncertain document).
    ``limit``: without ``document_ids`` the number of documents picked
    (default 3). With ``document_ids`` every id is processed (``limit``
    defaults to their count); at most 10 ids per call, and a ``limit``
    smaller than the number of ids is refused, never silently truncated.
    ``exclude_ids`` (at most 50): only without ``document_ids`` (both together
    are refused). Those documents are left out of the default selection
    before ``limit`` is applied, so the limit is filled with other documents;
    the plan counts them in ``excluded_before_limit.excluded_by_caller``.
    Ids that are not this patient's candidates are ignored.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    out = await start_reocr(
        _get_db(ctx), pid, dry_run=dry_run, limit=limit, document_ids=document_ids,
        passes=passes, only_with_facts=only_with_facts, exclude_ids=exclude_ids,
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

    from oncofiles.curated import reocr
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
    # #800: per-pass LLM usage (counts only), also for a failed read
    summary, _ = reocr.report_usage(d["id"], getattr(rr, "llm_passes", None) or [])
    res.update(summary)  # never raises; job-level warnings come from truncated_passes
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
    db, pid: str, docs: list[dict], *, dry_run: bool, fingerprint: bool = False,
    excluded: list[dict] | None = None,
) -> None:  # fmt: skip
    from oncofiles import config
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.facts_rules import Vocabulary
    from oncofiles.curated.reread_verify import loads
    from oncofiles.memory import get_rss_mb
    from oncofiles.server import _create_patient_clients

    job = _reocr_jobs[pid]
    row_id = await db.insert_curated_run(patient_id=pid, dry_run=dry_run, trigger="reread")
    report: list[dict] = list(excluded or [])  # raw genotype data (#815): skipped, never read
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
        from oncofiles.curated.reocr import truncation_warnings

        # #800: a pass cut off at its budget is a warning, never a silent pass
        try:
            warnings = [
                w
                for r in report
                for w in truncation_warnings(r["document_id"], r.get("truncated_passes") or [])
            ]
        except Exception as exc:  # reporting never fails the job
            logger.warning("re-read usage warnings failed: %s", type(exc).__name__)
            warnings = []
        job.update(
            state="done", report={"dry_run": dry_run, "documents": report, "warnings": warnings}
        )
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
    from oncofiles.curated import llm_input

    docs, excluded = [], []
    for r in await db.curated_reocr_candidates(patient_id=pid):
        if r["id"] not in ids:
            continue
        if reason := await llm_input.reocr_exclusion(db, pid, r):  # #815: never re-read
            excluded.append(llm_input.skipped_entry(r, reason))
        else:
            docs.append(r)
    if not docs:
        return {"status": "nothing to do", **({"skipped": excluded} if excluded else {})}
    job = {
        "state": "running", "kind": "reread", "dry_run": dry_run,
        "started_at": datetime.now(UTC).isoformat(), "actor": actor, "skipped": excluded,
        "documents": [{"document_id": d["id"], "filename": d["filename"],
                       "category": d.get("category")} for d in docs],
    }  # fmt: skip
    return await _launch(
        db, pid, job,
        lambda: _reread_job(db, pid, docs, dry_run=dry_run, fingerprint=fingerprint and dry_run,
                            **({"excluded": excluded} if excluded else {})),
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


# ── dry re-OCR compare (#883) ───────────────────────────────────────────

COMPARE_KIND = "reocr_compare"


async def _compare_targets(db, pid: str, ids: list[int]) -> tuple[list[dict], list[dict]]:
    """The listed curated documents of this patient that have a
    vision-readable source and a sidecar, and a skipped entry for each other
    id (ids and reason codes only). No target filter otherwise: unlike
    ``_reocr_targets`` a document need not be Cowork-era nor hold ``[?]``.
    An id of another patient is ``not_found``, like an id that does not exist."""
    from oncofiles.curated import extract as extract_mod
    from oncofiles.curated import llm_input

    rows = {r["id"]: r for r in await db.curated_reocr_candidates(patient_id=pid)}
    docs, skipped = [], []
    for i in ids:
        r = rows.get(i)
        if r is None:
            skipped.append({"document_id": i, "result": "skipped", "error": "not_found"})
            continue
        cur = (r.get("structured_metadata") or {}).get("curated") or {}
        if reason := await llm_input.reocr_exclusion(db, pid, r):  # #815: never read
            skipped.append(llm_input.skipped_entry(r, reason))
        elif (r.get("mime_type") or "").startswith(_NOT_OCR) or (
            extract_mod._ext(r.get("filename") or "") in extract_mod.TEXT_EXTS
        ):
            skipped.append(llm_input.skipped_entry(r, "not_a_scan"))
        elif not cur.get("sidecar_id") or not r.get("gdrive_id"):
            skipped.append(llm_input.skipped_entry(r, "no_sidecar"))
        else:
            docs.append(r)
    return docs, skipped


async def _reocr_compare_job(
    db, pid: str, docs: list[dict], counts: dict[int, dict[str, int]], skipped: list[dict],
    *, model: str | None = None,
) -> None:  # fmt: skip
    """Background part of ``curated_reocr_compare``. Writes NOTHING: no Drive
    (two downloads per document), no DB row (not even a run row), no index.
    One document and one vision call at a time: one LM Studio slot.
    ``model``: the validated per-job override, passed explicitly per call."""
    from oncofiles import config
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated import reocr_compare
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.facts_rules import Vocabulary
    from oncofiles.memory import get_rss_mb
    from oncofiles.server import _create_patient_clients

    job = _reocr_jobs[pid]
    report: list[dict] = []
    warnings: list[dict] = []
    try:
        clients = await _create_patient_clients(db, pid)
        if not clients or clients[0] is None:
            raise RuntimeError("Drive not connected")
        gdrive = clients[0]
        vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
        for d in docs:
            stored = counts.get(d["id"]) or {}
            base = {"document_id": d["id"], "category": d.get("category"),
                    "machine_facts": stored.get("machine", 0),
                    "unverified_facts": stored.get("unverified", 0)}  # fmt: skip
            if _stopping.is_set():
                report.append({**base, "result": "skipped", "error": "stopping"})
                continue
            if (rss := get_rss_mb()) > config.REOCR_RSS_LIMIT_MB:
                report.append({**base, "result": "skipped", "error": f"rss {rss:.0f} MB"})
                continue

            def _blocking(d=d):
                own = gdrive.dedicated_copy()
                try:
                    with local_only("curated re-OCR compare"):
                        return reocr_compare.compare_one(DriveOps(own), d, vocab, parallel=1,
                                                         model=model)  # fmt: skip
                finally:
                    own.close()

            t0 = time.monotonic()
            try:
                res = await asyncio.to_thread(_blocking)
            except Exception as exc:
                logger.exception("re-OCR compare of doc %s failed", d["id"])
                res = {"document_id": d["id"], "result": "failed", "error": type(exc).__name__}
            warnings += res.pop("warnings", None) or []
            report.append({**base, **res, "job_seconds": round(time.monotonic() - t0, 1)})
        done = [r for r in report if r.get("result") == "compared"]
        totals = {k: sum(r["totals"][k] for r in done) for k in reocr_compare.PAGE_KEYS}
        for k in ("agree", "value_differs", "missing_in_new", "missing_in_today"):
            totals[f"facts_{k}"] = sum(r["facts"][k] for r in done)
        job.update(state="done", report={
            "dry_run": True, "settings": _compare_settings(model), "compared": len(done),
            "totals": totals, "warnings": warnings, "documents": [*report, *skipped],
        })  # fmt: skip
    except Exception as exc:
        logger.exception("re-OCR compare failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__,
                   report={"dry_run": True, "settings": _compare_settings(model),
                           "documents": [*report, *skipped]})  # fmt: skip
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()


def _compare_settings(model: str | None) -> dict:
    """The compare's effective model and its per-job override (ids only)."""
    from oncofiles.curated import llm

    return {"model": model or llm.model_id(),
            "overrides": {"model": model} if model is not None else {}}  # fmt: skip


async def start_reocr_compare(
    db, pid: str, document_ids: list[int], *, actor: str = "mcp", model: str | None = None
) -> dict:
    """Start a dry re-OCR compare (#883) in the background; ``pid`` is resolved.
    Explicit ids only, at most ``COMPARE_MAX_DOCS``; more is refused, not cut.
    ``model``: a per-job LM Studio model for every call of the read (benching
    a separate OCR model), validated and checked ``loaded`` exactly like the
    scorecards' override before any job, slot or Drive call."""
    from oncofiles.curated import llm
    from oncofiles.curated.reocr_compare import COMPARE_MAX_DOCS

    if err := llm.model_override_error(model, None):
        return {"error": err}
    ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))
    if not ids:
        return {"error": "document_ids is required"}
    if len(ids) > COMPARE_MAX_DOCS:
        return {"error": f"too many documents: at most {COMPARE_MAX_DOCS} per job"}
    if not llm.configured():
        return {"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"}
    if refused := await _model_override_refusal(model):
        return refused
    busy = "busy: a run, re-OCR, re-read or other job is in progress for this patient"
    if _busy(pid) or run_block_reason(pid):
        return {"error": busy}
    docs, skipped = await _compare_targets(db, pid, ids)
    if not docs:
        return {"status": "nothing to do", **({"skipped": skipped} if skipped else {})}
    counts = await db.active_fact_counts(patient_id=pid, document_ids=[d["id"] for d in docs])
    # most machine facts first: the order of the audit (#883)
    docs.sort(key=lambda d: (-(counts.get(d["id"]) or {}).get("machine", 0), d["id"]))
    job = {
        "state": "running", "kind": COMPARE_KIND, "dry_run": True, "passes": 2,
        "started_at": datetime.now(UTC).isoformat(), "actor": actor, "skipped": skipped,
        **_compare_settings(model),
        "documents": [{"document_id": d["id"], "category": d.get("category"),
                       "machine_facts": (counts.get(d["id"]) or {}).get("machine", 0)}
                      for d in docs],
    }  # fmt: skip
    return await _launch(
        db, pid, job, lambda: _reocr_compare_job(db, pid, docs, counts, skipped, model=model),
        dry_run=True, busy=busy,
    )  # fmt: skip


async def curated_reocr_compare(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    model: str | None = None,
) -> str:
    """Dry audit of re-OCR'd sidecars (#883): read again, compare, write nothing.

    For each listed curated document (any with a vision-readable source and a
    sidecar; it need not be Cowork-era nor hold [?]) the LOCAL model reads the
    original source again exactly like a re-OCR (pass 1 on the whole page,
    pass 2 as two independent halves, reconciled), and the result is compared
    with TODAY's sidecar page by page: numbers of today's sidecar confirmed
    by the new reading / not found / found only on a [?] line, numbers only in
    the new reading, dates the same, and lab facts per code and day (agree,
    value differs, missing on either side; codes and counts, never values).
    Each document also carries its per-pass usage records and its active
    machine/unverified fact counts; documents run in that order (most machine
    facts first). Writes nothing: no Drive, no DB, no backup, no candidate,
    no index. Explicit ``document_ids`` only (max 10, more is refused).
    Refused while another run or job is active for the patient. Runs in the
    background (one LM Studio slot); poll curated_reocr_status. Slow:
    minutes per page.
    Admin-only (it spends the shared local model on an audit), and
    ``patient_slug`` is required: there is no implicit patient.

    Args:
        document_ids: curated documents (explicit ids, at most 10).
        patient_slug: the patient (required).
        model: LM Studio model id for this job only, to bench a separate OCR
            model through the same path (1-200 of ``A-Za-z0-9._:/@-``); null
            = LOCAL_LLM_MODEL. Every call of the read uses it (orientation
            question, both passes, the second-pass halves, the near-empty
            retry). Refused (``model_not_loaded``) unless the configured LM
            Studio reports it ``loaded`` at its native ``/api/v0/models``
            (``model_state_unknown`` when that list is unavailable); nothing
            is loaded or downloaded. Echoed in the report's ``settings``.
    """
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("curated_reocr_compare")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if not (patient_slug or "").strip():
        return json.dumps({"error": "patient_slug is required"})
    pid = await _resolve_patient_id(patient_slug, ctx)
    out = await start_reocr_compare(_get_db(ctx), pid, document_ids, model=model)
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
    ``lost_codes`` (re-OCR, #805; legacy ``lost`` strings) becomes ``lost_count``."""
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
        if isinstance(d.get("lost_codes"), dict):  # #805: codes only
            r["lost_count"] = sum(int(n) for n in d["lost_codes"].values())
        elif "lost" in d:  # an in-memory legacy shape; never shown, only counted
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


def _overrides(
    thinking, sampling, timeout_s, reasoning_marker, parallel_docs=None, model=None,
    reasoning_effort=None,
) -> dict:  # fmt: skip
    """The per-job A/B overrides as echoed in job and report (#767, #770);
    ``parallel_docs`` (#773), ``model`` and ``reasoning_effort`` (scorecards
    only) only when given (the effective values are in ``settings``)."""
    out = {"thinking": thinking, "sampling": sampling, "timeout_s": timeout_s,
           "reasoning_marker": reasoning_marker}  # fmt: skip
    for key, value in (("parallel_docs", parallel_docs), ("model", model),
                       ("reasoning_effort", reasoning_effort)):  # fmt: skip
        if value is not None:
            out[key] = value
    return out


_SHADOW_DOC_KEYS = (
    "method", "pages", "pages_used", "pages_not_assessed", "pages_truncated", "page_errors",
    "finish_reasons", "page_diagnostics", "warnings", "dob_known", "birth_date_guard",
    "dates_unchecked_no_dob", "facts_from_content", "facts_from_reasoning", "dropped", "seconds",
    "date_shapes_unverified", *DATE_NULL_KEYS,
)  # fmt: skip


# ── bounded document-level parallelism of the LLM extractors (#773) ──────
#
# A job runs in three phases: (1) on the event loop, one DB call after the
# other, everything the model calls need is fetched (birth dates, vocabulary,
# stored facts, then each document's page text); (2) up to ``parallel_docs``
# documents run their model calls in worker threads, each document's pages
# one after another, with NO DB call and no Drive client; (3) on the event
# loop, results are assembled in input order. A real write STREAMS phase 3:
# document i is stored as soon as its model work is done and 0..i-1 are
# stored, while later documents may still be extracting, so a kill midway
# keeps every document already stored. The app holds ONE aiosqlite
# connection: never move a DB call into a worker thread.


@dataclass
class _DocWork:
    """One document of an LLM extraction job. ``blocking`` is the model work
    for a worker thread (``None`` when ``final`` decided the entry without a
    model call); ``out`` / ``error`` / ``stopped`` are what phase 2 left."""

    d: dict
    entry: dict
    pages: list = field(default_factory=list)
    blocking: object = None
    final: dict | None = None
    out: dict | None = None
    error: str | None = None
    stopped: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)  # model work ended
    consumed: asyncio.Event = field(default_factory=asyncio.Event)  # caller handled it


def _stopping_entry(d: dict) -> dict:
    return {"document_id": d["id"], "category": d.get("category"),
            "result": "skipped", "error": "stopping"}  # fmt: skip


async def _doc_work(db, pid: str, d: dict, make_blocking) -> _DocWork:
    """Phase 1 for one document (event loop, DB): its stored page text,
    loaded ONLY through ``llm_input.load_for_llm`` — the one chokepoint that
    keeps raw genotype data away from the model (#815): an excluded document
    gets no model work and the entry ``skipped`` / ``raw_genotype_excluded``.
    ``make_blocking(pages, meta, day)`` returns the model work, which must
    not touch the DB."""
    from oncofiles.curated import llm_input

    w = _DocWork(d=d, entry={"document_id": d["id"], "category": d.get("category")})
    if _stopping.is_set():
        w.final = _stopping_entry(d)
        return w
    loaded = await llm_input.load_for_llm(db, pid, d)
    if isinstance(loaded, llm_input.Excluded):
        w.final = llm_input.skipped_entry(d, loaded.reason)
        return w
    w.pages = loaded
    if not any(t.strip() for _, t in w.pages):
        w.final = {**w.entry, "result": "skipped", "error": "no page text"}
        return w
    meta = d.get("structured_metadata") or {}
    day = str(d.get("document_date") or "")[:10] or None
    w.blocking = make_blocking(w.pages, meta, day)
    return w


def _start_extractions(
    works: list[_DocWork], *, parallel: int, hold_until_consumed: bool = False
) -> list[asyncio.Task]:
    """Phase 2: start the model work of up to ``parallel`` documents at once,
    in input order (the semaphore is FIFO). Each work's ``done`` event is set
    when its model work ended (or was never started). NO DB access in here:
    the tasks only wait on worker threads. ``local_only`` follows into every
    worker (``asyncio.to_thread`` copies the context, and the work re-enters
    it). A shutdown (``request_stop``) lets in-flight documents finish their
    current page (the extractors check ``stop`` before each page) and starts
    no further document.

    ``hold_until_consumed`` (a real write): a document keeps its slot until
    the caller has handled it (``consumed``), so with one slot the order is
    exactly extract → store → next document, and the store of document i
    runs (on the event loop) while later documents may still be extracting."""
    gate = asyncio.Semaphore(max(1, int(parallel)))

    async def one(w: _DocWork) -> None:
        async with gate:
            try:
                if _stopping.is_set():
                    w.stopped = True
                    return
                try:
                    w.out = await asyncio.to_thread(w.blocking)
                except Exception as exc:
                    logger.exception("LLM extraction of doc %s failed", w.d["id"])
                    w.error = type(exc).__name__
            finally:
                w.done.set()
            if hold_until_consumed:
                await w.consumed.wait()

    return [asyncio.create_task(one(w)) for w in works if w.blocking is not None]


async def _wait_done(w: _DocWork) -> None:
    """Until document ``w``'s model work has ended (no-op without model work)."""
    if w.blocking is not None:
        await w.done.wait()


async def _extract_documents(works: list[_DocWork], *, parallel: int) -> None:
    """Phase 2 for a dry run: all model work, then return (see
    ``_start_extractions``)."""
    tasks = _start_extractions(works, parallel=parallel)
    try:
        for t in tasks:
            await t
    finally:
        for t in tasks:  # a no-op unless this job itself was cancelled
            t.cancel()


def _timing(started: float, entries: list[dict], parallel: int) -> dict:
    """Wall-clock seconds of the whole job next to the summed model seconds of
    its documents (= the sum of per-page seconds); their ratio is the speed-up."""
    wall = round(time.monotonic() - started, 1)
    pages = round(sum(float(e.get("seconds") or 0) for e in entries), 1)
    return {"parallel_docs": parallel, "wall_seconds": wall, "page_seconds_total": pages,
            "speedup": round(pages / wall, 2) if wall > 0 else None}  # fmt: skip


async def _lab_shadow_work(
    db, pid: str, d: dict, *, vocab, births, cfg, job_name: str = "curated LLM extract shadow"
) -> _DocWork:
    """Phase 1 of ONE document of the LLM-extract shadow (#714 phase 2a) and of
    the LLM lab write (``curated_lab_extract``, same model work)."""

    def make(pages, meta, day):
        def _blocking():
            from oncofiles.cloud_guard import local_only
            from oncofiles.curated import llm_extract

            with local_only(job_name):
                return llm_extract.extract_document(
                    pages, vocab=vocab, report_date=day, birth_dates=births,
                    source_reason=_source_reason(meta), stop=_stopping.is_set, cfg=cfg,
                    document_category=d.get("category"),
                )  # fmt: skip

        return _blocking

    return await _doc_work(db, pid, d, make)


def _lab_shadow_entry(w: _DocWork, stored) -> dict:
    """Phase 3 of ONE lab shadow document: extractor + generic verifier output
    compared with the stored rule facts (``stored`` = the patient's
    ``fact_verification_rows``, fetched before dispatch). Pure, no DB. Shared
    by the shadow preview and the extraction scorecard (#773)."""
    from oncofiles.curated import llm_extract, verifier

    if w.final is not None:
        return w.final
    if w.stopped:
        return _stopping_entry(w.d)
    if w.error is not None or w.out is None:
        return {**w.entry, "result": "failed", "error": w.error or "no_result"}
    out, d = w.out, w.d
    meta = d.get("structured_metadata") or {}
    rows = [r for r in stored if r["source_document_id"] == d["id"]]
    # text-only extraction never upgrades a transcription (#714 rule)
    verifier.inherit_transcription_reasons(out["facts"], rows)
    cross = verifier.transcription_pages(rows, _source_reason(meta), [n for n, _ in w.pages])
    return {
        **w.entry, "result": "compared", **{k: out[k] for k in _SHADOW_DOC_KEYS},
        **llm_extract.shadow_compare(
            rows, out["facts"], not_assessed_pages=out["pages_not_assessed"],
            crosscheck_pages=cross,
        ),
    }  # fmt: skip


def _add_label_review(entry: dict, w: _DocWork) -> None:
    """Attach the label review list (#814) to a compared lab entry, in place.
    Only the in-memory reports of the shadow preview and the scorecard call
    this; the lab write job, whose report is persisted, never does."""
    from oncofiles.curated import llm_extract

    if entry.get("result") != "compared" or w.out is None:
        return
    labels, omitted = llm_extract.unmapped_labels(w.out["facts"])
    entry["unmapped_labels"] = labels
    entry["unmapped_labels_omitted"] = omitted


async def _llm_shadow_job(
    db,
    pid: str,
    docs: list[dict],
    *,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
) -> None:
    """LLM extractor + generic verifier on stored page text, compared with the
    stored rule facts (#714 phase 2a). Writes NOTHING: no facts, no Drive, no
    run row. One model call at a time per document, up to ``parallel_docs``
    documents at once (#773; default ``LLM_EXTRACT_PARALLEL_DOCS`` = 1, i.e.
    one LM Studio slot); local only. ``thinking`` / ``sampling`` (#767),
    ``timeout_s`` (#770) and ``parallel_docs`` override the env for this job only."""
    from oncofiles.curated import llm_extract, llm_extract_call, verifier
    from oncofiles.curated.facts_rules import Vocabulary

    started = time.monotonic()
    job = _reocr_jobs[pid]
    cfg = llm_extract_call.settings(thinking, sampling, timeout_s, reasoning_marker)
    parallel = llm_extract_call.parallel_docs(parallel_docs)
    overrides = _overrides(thinking, sampling, timeout_s, reasoning_marker, parallel_docs)
    report: list[dict] = []
    try:
        vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
        # same DOB resolution as the clinical path: context + README (#778)
        births = await _all_birth_dates(db, pid)
        stored = await db.fact_verification_rows(patient_id=pid)
        works = [await _lab_shadow_work(db, pid, d, vocab=vocab, births=births, cfg=cfg)
                 for d in docs]  # fmt: skip
        await _extract_documents(works, parallel=parallel)
        report = [_lab_shadow_entry(w, stored) for w in works]
        for w, entry in zip(works, report, strict=True):
            _add_label_review(entry, w)
        done = [r for r in report if r.get("result") == "compared"]
        totals = {k: sum(r[k] for r in done) for k in
                  ("rule_facts", "llm_facts", "llm_machine", "agree", "agree_machine",
                   "parser_crosscheck", "value_mismatch", "date_mismatch", "rule_only",
                   "llm_only", "llm_no_value", "llm_uncoded", "pages_used", "pages_truncated",
                   "dates_unchecked_no_dob")}  # fmt: skip
        totals["pages_not_assessed"] = sum(len(r["pages_not_assessed"]) for r in done)
        job.update(state="done", report={
            "dry_run": True, "extractor": llm_extract.EXTRACTOR_VERSION,
            "verifier": verifier.VERIFIER_VERSION,
            "settings": {**cfg.report(), "parallel_docs": parallel, "overrides": overrides},
            "timing": _timing(started, report, parallel),
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
    parallel_docs: int | None = None,
) -> str:
    """SHADOW mode of LLM-first fact extraction (#714): compare, never store.

    For each listed curated document, the LOCAL model reads the stored page
    text and lists the printed results; a generic verifier (no layout rules)
    accepts each as machine or unverified with reason codes. The result is
    compared with the facts currently stored for that document and reported
    as keys and counts only (agreement on code + date + value hash, codes
    found by only one side, unverified reasons, seconds) — never values.
    Each document also lists ``unmapped_labels`` (#814): the labels that map
    to no ``fact_codes`` alias (or only fuzzily, with ``near_code``), digits
    masked as 9, at most 60 characters, with counts — the review list for new
    aliases; in this report only, never stored or logged.
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
        reasoning_marker: "on" / "off" overrides LLM_EXTRACT_REASONING_MARKER for this
            job only (#770): with thinking off, the system message ends with
            the reasoning-off marker (for the LM Studio Prompt Template rule)
            and no /no_think is sent; with thinking on, no marker.
        parallel_docs: documents extracted at once (int, 1-3) for this job
            only (#773); null keeps LLM_EXTRACT_PARALLEL_DOCS (default 1).
            Each running document holds one LM Studio slot.
        All five are echoed in the report (``settings``) and refused (error)
        with dry_run=false. The report's ``timing`` has the job's wall-clock
        seconds next to the summed per-page seconds.
    """
    from oncofiles import config
    from oncofiles.curated import llm, llm_extract_call

    llm_extract_call.refuse_overrides_on_write(
        write=not dry_run, thinking=thinking, sampling=sampling, timeout_s=timeout_s,
        reasoning_marker=reasoning_marker, parallel_docs=parallel_docs,
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
        "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker, parallel_docs),
        "parallel_docs": llm_extract_call.parallel_docs(parallel_docs),
        "documents": [{"document_id": d["id"], "category": d.get("category")} for d in docs],
    }  # fmt: skip
    _reocr_jobs[pid]["task"] = asyncio.create_task(
        _llm_shadow_job(db, pid, docs, thinking=thinking, sampling=sampling,
                        timeout_s=timeout_s, reasoning_marker=reasoning_marker,
                        parallel_docs=parallel_docs)
    )  # fmt: skip
    return json.dumps(
        {k: v for k, v in _reocr_jobs[pid].items() if k != "task"}, ensure_ascii=False
    )


# ── LLM-first extraction of the non-lab kinds (#726 item 4, #711 phase 2b) ──

CLINICAL_MAX_DOCS = 10


async def _all_birth_dates(db, pid: str) -> set[str]:
    """Every birth date known for the patient (ISO): patient context plus the
    README contract (cached by the last index; read from Drive, read-only,
    when not cached yet). Never used as a fact date. The one DOB resolution
    of every LLM extractor: lab shadow and clinical alike (#778)."""
    try:
        from oncofiles import patient_context

        await patient_context.load_from_db(db.db, pid)
    except Exception:
        logger.warning("llm extract: patient context not loaded for %s", pid)
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
            logger.warning("llm extract: README birth dates not read for %s", pid)
    return set(_birth_dates(pid)) | _readme_births.get(pid, set())


_CLINICAL_DOC_KEYS = (
    "method", "profile", "pages", "pages_used", "pages_not_assessed", "pages_truncated",
    "page_errors", "error_kinds", "answer_sources", "page_diagnostics", "warnings", "dob_known",
    "birth_date_guard", "counts", "unverified_reasons", "dropped", "seconds",
    *DATE_NULL_KEYS,
)  # fmt: skip


def _clinical_totals(report: list[dict]) -> dict:
    """Sums over the documents: counts per kind, reasons, stored rows — no values."""
    counts: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    stored: dict[str, dict[str, int]] = {}
    rerun: dict[str, dict[str, int]] = {}
    for r in report:
        for target, c in (r.get("rerun") or {}).items():
            tot = rerun.setdefault(target, {"inserted": 0, "removed": 0, "kept_missing": 0})
            for k, n in c.items():
                tot[k] = tot.get(k, 0) + n
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
        "rerun": rerun,
    }


async def _patient_type(db, pid: str) -> str:
    """``oncology`` or ``general`` from the patient context (hydrated from the
    DB when not cached); ``oncology`` when unknown."""
    from oncofiles import patient_context

    try:
        await patient_context.ensure_loaded(db.db, pid)
    except Exception:
        logger.warning("clinical extraction: patient context not loaded for %s", pid)
    try:
        return str((patient_context.get_context(pid) or {}).get("patient_type") or "oncology")
    except Exception:
        return "oncology"


async def _clinical_work(db, pid: str, d: dict, *, vocab, births, cfg) -> _DocWork:
    """Phase 1 of ONE document of the clinical extraction (#726): its page
    text (DB, event loop) and the model work for a worker thread (local-only,
    no DB). The request profile (#815) follows the patient type and the
    document category (``llm_extract_clinical.profile_for``). Raw genotype
    data is excluded in ``_doc_work`` (``llm_input.load_for_llm``)."""
    from oncofiles.curated import llm_extract_clinical as lx

    profile = lx.profile_for(await _patient_type(db, pid), d.get("category"))

    def make(pages, meta, day):
        def _blocking():
            from oncofiles.cloud_guard import local_only

            with local_only("curated clinical extraction"):
                return lx.extract_document(
                    pages, vocab=vocab, report_date=day, birth_dates=births,
                    source_reason=_source_reason(meta), stop=_stopping.is_set, cfg=cfg,
                    profile=profile,
                )  # fmt: skip

        return _blocking

    w = await _doc_work(db, pid, d, make)
    if w.final is None:
        w.entry["profile"] = profile
    return w


def _clinical_entry(w: _DocWork) -> tuple[dict, dict | None]:
    """Phase 3 of ONE clinical document: the report entry and the extractor
    output (``None`` when the document was skipped, stopped or failed). Pure,
    stores nothing; shared by the preview / write job and the extraction
    scorecard (#773)."""
    if w.final is not None:
        return w.final, None
    if w.stopped:
        return _stopping_entry(w.d), None
    if w.error is not None or w.out is None:
        return {**w.entry, "result": "failed", "error": w.error or "no_result"}, None
    entry = {**w.entry, **{k: w.out[k] for k in _CLINICAL_DOC_KEYS}, "result": "previewed"}
    return entry, w.out


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
    parallel_docs: int | None = None,
) -> None:
    """Extract the non-lab kinds of each document with the LOCAL model and
    verify them (#726 item 4). Preview (``write=False``) writes NOTHING; a
    write holds the patient lock, refuses without a known birth date, and
    stores nothing for a document with a page that was not assessed. One
    model call at a time per document, up to ``parallel_docs`` documents at
    once (#773; default ``LLM_EXTRACT_PARALLEL_DOCS`` = 1, one LM Studio
    slot); all DB reads before the model phase, every store on the event loop
    in input order as soon as that document is done (never in a worker
    thread); local only. ``thinking`` / ``sampling`` (#767), ``timeout_s``
    (#770) and ``parallel_docs`` override the env for a PREVIEW only."""
    from oncofiles import config
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
        parallel_docs=parallel_docs,
    )
    started = time.monotonic()
    cfg = llm_extract_call.settings(thinking, sampling, timeout_s, reasoning_marker)
    parallel = llm_extract_call.parallel_docs(parallel_docs)
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
            works = [await _clinical_work(db, pid, d, vocab=vocab, births=births, cfg=cfg)
                     for d in docs]  # fmt: skip
            # a write streams: document i is stored (event loop, in input order)
            # as soon as its model work is done, while later documents may still
            # be extracting in worker threads; with one slot: extract → store → next
            tasks = _start_extractions(works, parallel=parallel, hold_until_consumed=write)
            try:
                for w in works:
                    await _wait_done(w)
                    d = w.d
                    entry, out = _clinical_entry(w)
                    if out is None:
                        report.append(entry)
                        w.consumed.set()
                        continue
                    day = str(d.get("document_date") or "")[:10] or None
                    if write and out["pages_not_assessed"]:
                        entry.update(
                            result="skipped", error="a page was not assessed: nothing stored"
                        )
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
                                result="stored",
                                stored=res["stored"],
                                store_dropped=res["dropped"],
                                rerun=res["rerun"],  # #819 inserted/removed/kept_missing
                            )
                            if res.get("warnings"):  # #819 rerun_lost_many (counts + doc id)
                                entry["warnings"] = [*(entry.get("warnings") or []),
                                                     *res["warnings"]]  # fmt: skip
                    report.append(entry)
                    w.consumed.set()
            finally:
                for w in works:
                    w.consumed.set()
                for t in tasks:  # a no-op unless the job failed or was cancelled
                    t.cancel()
        job.update(state="done", report={
            "dry_run": not write, "extractor": lx.EXTRACTOR_VERSION,
            "verifier": verifier_clinical.CLINICAL_VERIFIER_VERSION, "dob_known": bool(births),
            "settings": {**cfg.report(), "parallel_docs": parallel,
                         "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker,
                                                 parallel_docs)},
            "timing": _timing(started, report, parallel),
            "warnings": llm_extract_call.merge_warnings(report) + [
                w for r in report for w in r.get("warnings") or []
                if w.get("warning") == "rerun_lost_many"
            ],
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
    parallel_docs: int | None = None,
) -> str:
    from oncofiles import config
    from oncofiles.curated import llm, llm_extract_call
    from oncofiles.curated.llm_extract_clinical import EXTRACTOR_VERSION

    # A/B overrides are for a dry run only: a write never takes them (#767, #770, #773).
    llm_extract_call.refuse_overrides_on_write(
        write=write,
        thinking=thinking,
        sampling=sampling,
        timeout_s=timeout_s,
        reasoning_marker=reasoning_marker,
        parallel_docs=parallel_docs,
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
        "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker, parallel_docs),
        "parallel_docs": llm_extract_call.parallel_docs(parallel_docs),
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
                parallel_docs=parallel_docs,
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
    parallel_docs: int | None = None,
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
        reasoning_marker: "on" / "off" overrides LLM_EXTRACT_REASONING_MARKER for this
            preview only (#770): with thinking off, the system message ends
            with the reasoning-off marker (for the LM Studio Prompt Template rule)
            and no /no_think is sent; with thinking on, no marker.
        parallel_docs: documents extracted at once (int, 1-3) for this preview
            only (#773); null keeps LLM_EXTRACT_PARALLEL_DOCS (default 1).
            Each running document holds one LM Studio slot.
        All five are echoed in the report (``settings``), with per-page token
        counts, warnings and ``timing`` (wall-clock seconds of the job next to
        the summed per-page seconds).
    """
    return await _start_clinical(
        ctx, document_ids, patient_slug, write=False, thinking=thinking, sampling=sampling,
        timeout_s=timeout_s, reasoning_marker=reasoning_marker, parallel_docs=parallel_docs,
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
    parallel_docs: int | None = None,
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

    ``thinking`` / ``sampling`` / ``timeout_s`` / ``reasoning_marker`` /
    ``parallel_docs`` overrides (see curated_clinical_extract_preview) are
    accepted with dry_run=true only; with dry_run=false they are refused
    (error): a write always uses the configured env settings.
    """
    return await _start_clinical(
        ctx, document_ids, patient_slug, write=not dry_run, thinking=thinking, sampling=sampling,
        timeout_s=timeout_s, reasoning_marker=reasoning_marker, parallel_docs=parallel_docs,
    )  # fmt: skip


# ── rollback of the clinical extractor's rows (#773 / #726) ───────────────

RETRACT_MAX_DOCS = 50
RETRACT_REASON_MAX = 200
_RETRACT_ANNOTATIONS = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": False}


def _sum_stored(report: list[dict]) -> dict[str, dict[str, int]]:
    totals: dict[str, dict[str, int]] = {}
    for d in report:
        for target, c in (d.get("stored") or {}).items():
            tot = totals.setdefault(target, {})
            for k, n in c.items():
                tot[k] = tot.get(k, 0) + n
    return totals


async def _audit_retract(db, pid: str, job: dict, report: list[dict], *, family: str) -> None:
    """One activity_log row per REAL retract: caller (keyed hash), ids, the
    pre-store copy and counts per target. Never a value, an email or the
    free-text reason (only its length)."""
    from oncofiles.models import ActivityLogEntry

    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id=f"curated-{family}",
                agent_id="mcp",
                tool_name=f"curated_{family}_retract",
                input_summary=json.dumps(
                    {
                        "by": "mcp",
                        # keyed hash, never the email itself (#590/#604, migration 085)
                        "caller_email_hash": job.get("caller_email_hash"),
                        "dry_run": False,
                        "pre_store": job.get("pre_store"),
                        "document_ids": job.get("document_ids"),
                        "reason_chars": job.get("reason_chars"),
                        "started_at": job.get("started_at"),
                    }
                ),
                output_summary=json.dumps(
                    {
                        "results": {
                            r: sum(1 for d in report if d.get("result") == r)
                            for r in {str(d.get("result")) for d in report}
                        },
                        "stored": _sum_stored(report),
                    }
                ),
                status="ok" if all(d.get("result") == "retracted" for d in report) else "error",
                tags=json.dumps(["curated", family, "retract", "real", "pre-store"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for a %s retract", family)


async def _retract(
    ctx: Context,
    document_ids,
    patient_slug: str | None,
    dry_run: bool,
    reason: str,
    *,
    family: str,
    retract,
) -> str:
    """Shared body of the user-invoked rollbacks (``curated_clinical_retract``,
    ``curated_lab_retract``): ``retract`` is the store's ``retract_document``,
    ``family`` names the job kind, the pre-store copy and the audit row."""
    from oncofiles.tools._helpers import _is_admin_caller

    if _is_admin_caller() and not patient_slug:
        return json.dumps({"error": "Admin callers must pass patient_slug=<slug>; there is "
                           "no implicit admin patient scope."})  # fmt: skip
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    try:
        ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))
    except (TypeError, ValueError):
        return json.dumps({"error": "document_ids must be integers"})
    if not ids:
        return json.dumps({"error": "document_ids is required"})
    if len(ids) > RETRACT_MAX_DOCS:
        return json.dumps({"error": f"too many documents: at most {RETRACT_MAX_DOCS} explicit "
                           f"document_ids per call, got {len(ids)}"})  # fmt: skip
    reason = (reason or "").strip()
    if not dry_run:
        if not reason:
            return json.dumps({"error": "reason is required with dry_run=false"})
        if len(reason) > RETRACT_REASON_MAX:
            return json.dumps({"error": f"reason is too long: at most {RETRACT_REASON_MAX} "
                               "characters"})  # fmt: skip
    owned = await db.get_documents_by_ids(set(ids), patient_id=pid)
    missing = [i for i in ids if i not in owned]
    if missing:  # another patient's id looks exactly like an unknown one
        return json.dumps({"error": "not_found", "document_ids": missing,
                           "message": "nothing was changed"})  # fmt: skip

    if dry_run:
        report = []
        for i in ids:
            res = await retract(db, patient_id=pid, document_id=i, reason="", dry_run=True)
            report.append({"document_id": i, "result": "would_retract", **res})
        return json.dumps(
            {"dry_run": True, "totals": _sum_stored(report), "documents": report},
            ensure_ascii=False,
        )

    if (
        _reocr_jobs.get(pid, {}).get("state") == "running"
        or (pid in _runs and _runs[pid].get("state") == "running")
        or patient_lock(pid).locked()
    ):
        return json.dumps({"error": "busy: a curated run, index, re-OCR, re-read or "
                           "extraction is in progress for this patient"})  # fmt: skip
    # Reserve the patient's job slot before any await (CI deploys wait on it,
    # other jobs see busy); the previous job's record is restored afterwards.
    previous = _reocr_jobs.get(pid)
    job = _reocr_jobs[pid] = {
        "state": "running", "kind": f"{family}_retract", "dry_run": False,
        "started_at": datetime.now(UTC).isoformat(), "actor": "mcp",
        "caller_email_hash": caller_email_hash(_caller_email()),
        "document_ids": ids, "reason_chars": len(reason),
    }  # fmt: skip
    try:
        async with patient_lock(pid):
            from oncofiles.pre_store import PreStoreError, make_copy

            try:
                job["pre_store"] = await asyncio.to_thread(make_copy, db, family)
            except Exception as exc:
                why = str(exc) if isinstance(exc, PreStoreError) else type(exc).__name__
                logger.error("pre-store copy failed for %s (%s): retract refused", pid[:8], why)
                return json.dumps(
                    {"error": f"refused: pre-store copy failed ({why}); nothing was written"}
                )
            who = (job["caller_email_hash"] or "mcp")[:12]
            audit_reason = f"retract by {who}: {reason}"
            report = []
            for i in ids:
                try:
                    res = await retract(
                        db, patient_id=pid, document_id=i, reason=audit_reason, dry_run=False
                    )
                except Exception as exc:  # idempotent: calling again retracts the rest
                    logger.exception("%s retract of doc %s failed", family, i)
                    report.append({"document_id": i, "result": "failed",
                                   "error": type(exc).__name__})  # fmt: skip
                else:
                    report.append({"document_id": i, "result": "retracted", **res})
            logger.info(
                "%s retract for %s: %d documents (reason %d chars)",
                family, pid[:8], len(ids), len(reason),
            )  # fmt: skip
            await _audit_retract(db, pid, job, report, family=family)
            return json.dumps(
                {"dry_run": False, "pre_store": job["pre_store"],
                 "totals": _sum_stored(report), "documents": report},
                ensure_ascii=False,
            )  # fmt: skip
    finally:
        if previous is not None:
            _reocr_jobs[pid] = previous
        else:
            _reocr_jobs.pop(pid, None)


async def curated_clinical_retract(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    dry_run: bool = True,
    reason: str = "",
) -> str:
    """Roll back what curated_clinical_extract stored for documents (#773 / #726).

    Soft-deletes exactly the rows a re-extraction of each document would
    remove: every condition, stage, treatment course, cycle, medication
    event, procedure, biomarker / lesion fact and planned encounter written
    by the clinical LLM extractor (``llm-clinical*``) for that document.
    Verified and corrected rows stay, and so does every row they point to
    (a verified cycle keeps its course). Lab rule facts and anything another
    extractor or a person wrote are never touched. Nothing is hard-deleted;
    running curated_clinical_extract again stores the document anew.

    dry_run=true (default) writes nothing and returns, per document and per
    target, how many rows would be ``removed`` and how many are ``kept``.
    dry_run=false needs a short ``reason`` (max 200 characters), holds the
    patient lock, is refused while a curated run, re-OCR, re-read or
    extraction is active for the patient, makes a verified safety copy of the
    database first (no copy, no change) and leaves one activity_log row
    (ids and counts only). Returns counts only, never values. Works also
    while LLM_CLINICAL_EXTRACT_DISABLED is set and without the local model.

    Args:
        document_ids: 1-50 document ids of this patient. An id that is not
            this patient's is reported as not_found and nothing is changed.
        patient_slug: required for admin callers.
        dry_run: false to actually retract.
        reason: why (required with dry_run=false); kept in the rows' deletion
            audit, never logged.
    """
    from oncofiles.curated import clinical_store

    return await _retract(
        ctx, document_ids, patient_slug, dry_run, reason, family="clinical",
        retract=clinical_store.retract_document,
    )  # fmt: skip


# ── WRITE path for LLM lab facts (#714 phase 2b for labs), OFF by default ──
#
# ``curated_lab_extract`` runs the same model work as the lab shadow
# (``_lab_shadow_work``) and, on a real run, stores the verified facts through
# ``curated.lab_store`` — the ONLY caller of ``lab_store.store_document``
# (lock test in tests/test_llm_lab_write.py). A real run is refused unless
# ``LLM_LAB_WRITE_ENABLED=true``; enabling it is a maintainer decision after
# the dry-run agreement scorecard (``gate``) is good on the rule-parser formats.

LAB_MAX_DOCS = 10
# one bucket per fact (``llm_extract.bucket_facts``, #826)
_LAB_GATE_KEYS = ("agree", "value_mismatch", "date_mismatch", "rule_only", "llm_only",
                  "parser_crosscheck")  # fmt: skip
_LAB_SUM_KEYS = (
    "rule_facts", "llm_facts", "llm_machine", "llm_unverified", "agree_machine",
    "pages_used", "pages_truncated", "dates_unchecked_no_dob", *_LAB_GATE_KEYS,
    # the LLM-only buckets: not comparable, but every bucket is summed (#933)
    "llm_no_value", "llm_uncoded",
)  # fmt: skip


def _add_counts(into: dict, counts: dict | None) -> None:
    for k, n in (counts or {}).items():
        into[k] = into.get(k, 0) + int(n or 0)


def _lab_totals(report: list[dict]) -> dict:
    """Sums over the documents: agreement with the rule facts (the write gate),
    stored / would-store / skipped counts per document. Counts and codes only."""
    compared = [r for r in report if "agree" in r]
    totals: dict = {k: sum(int(r.get(k) or 0) for r in compared) for k in _LAB_SUM_KEYS}
    totals["pages_not_assessed"] = sum(len(r.get("pages_not_assessed") or []) for r in compared)
    # the gate: only documents that hold rule facts can show agreement
    gated = [r for r in compared if r.get("rule_facts")]
    gate: dict = {k: sum(int(r.get(k) or 0) for r in gated) for k in _LAB_GATE_KEYS}
    rule_facts = sum(int(r.get("rule_facts") or 0) for r in gated)
    by_code: dict[str, dict] = {}
    for r in gated:
        for code, c in (r.get("agreement_by_code") or {}).items():
            _add_counts(by_code.setdefault(code, {}), c)
    gate.update(
        documents_with_rule_facts=len(gated), rule_facts=rule_facts,
        agreement_rate=round(gate["agree"] / rule_facts, 3) if rule_facts else None,
        value_mismatch_rate=round(gate["value_mismatch"] / rule_facts, 3) if rule_facts else None,
        by_code=dict(sorted(by_code.items())),
    )  # fmt: skip
    stored: dict = {}
    would: dict = {}
    skipped: dict = {}
    for r in report:
        _add_counts(stored, r.get("stored"))
        _add_counts(would, r.get("would_store"))
        _add_counts(skipped, r.get("skipped"))
    return {
        "documents": len(report),
        "stored_documents": sum(1 for r in report if r.get("result") == "stored"),
        **totals, "gate": gate, "would_store": would, "skipped": skipped, "stored": stored,
    }  # fmt: skip


async def _lab_job(
    db,
    pid: str,
    docs: list[dict],
    *,
    write: bool,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
) -> None:
    """LLM lab extraction of each document + the generic verifier, compared with
    the stored rule facts (the shadow metrics), and on a write stored through
    ``lab_store``. A dry run writes NOTHING. A write holds the patient lock,
    refuses without a known birth date or with ``LLM_LAB_WRITE_ENABLED`` off,
    and stores nothing for a document with a page that was not assessed. All
    DB reads before the model phase; every store on the event loop in input
    order (never in a worker thread); local only."""
    from oncofiles import config
    from oncofiles.curated import lab_store, llm_extract, llm_extract_call, verifier
    from oncofiles.curated.facts_rules import Vocabulary

    llm_extract_call.refuse_overrides_on_write(
        write=write, thinking=thinking, sampling=sampling, timeout_s=timeout_s,
        reasoning_marker=reasoning_marker, parallel_docs=parallel_docs,
    )  # fmt: skip
    started = time.monotonic()
    cfg = llm_extract_call.settings(thinking, sampling, timeout_s, reasoning_marker)
    parallel = llm_extract_call.parallel_docs(parallel_docs)
    job = _reocr_jobs[pid]
    if write and not config.LLM_LAB_WRITE_ENABLED:  # refused at the tool already
        job.update(state="failed", error="write_disabled",
                   finished_at=datetime.now(UTC).isoformat())  # fmt: skip
        return
    row_id = (
        await db.insert_curated_run(patient_id=pid, dry_run=False, trigger="lab") if write else None
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
            # rule facts only: the LLM lab rows are not part of the comparison
            stored = await db.fact_verification_rows(patient_id=pid)
            name = "curated LLM lab write" if write else "curated LLM lab preview"
            works = [await _lab_shadow_work(db, pid, d, vocab=vocab, births=births, cfg=cfg,
                                            job_name=name) for d in docs]  # fmt: skip
            tasks = _start_extractions(works, parallel=parallel, hold_until_consumed=write)
            try:
                for w in works:
                    await _wait_done(w)
                    d = w.d
                    entry = _lab_shadow_entry(w, stored)  # adds transcription reasons in place
                    if entry.get("result") != "compared":
                        report.append(entry)
                        w.consumed.set()
                        continue
                    rows = [r for r in stored if r["source_document_id"] == d["id"]]
                    cross = verifier.transcription_pages(
                        rows, _source_reason(d.get("structured_metadata") or {}),
                        [n for n, _ in w.pages],
                    )  # fmt: skip
                    entry["agreement_by_code"] = llm_extract.agreement_by_code(
                        rows, w.out["facts"], not_assessed_pages=w.out["pages_not_assessed"],
                        crosscheck_pages=cross,
                    )  # fmt: skip
                    plan = await lab_store.plan_document(
                        db, patient_id=pid, document_id=d["id"], facts=w.out["facts"]
                    )
                    entry.update(result="previewed", would_store=plan["would_store"],
                                 skipped=plan["skipped"])  # fmt: skip
                    if write and w.out["pages_not_assessed"]:
                        entry.update(
                            result="skipped", error="a page was not assessed: nothing stored"
                        )
                    elif write:
                        verifier.require_dob_for_write(births)
                        try:
                            res = await lab_store.store_document(
                                db, patient_id=pid, document_id=d["id"], facts=w.out["facts"],
                                birth_dates=births, model=model,
                            )  # fmt: skip
                        except Exception as exc:  # re-running the document replaces it again
                            logger.exception("storing LLM lab facts of doc %s failed", d["id"])
                            entry.update(result="failed", error=type(exc).__name__)
                        else:
                            entry.update(result="stored", stored=res["stored"],
                                         skipped=res["skipped"])  # fmt: skip
                    report.append(entry)
                    w.consumed.set()
            finally:
                for w in works:
                    w.consumed.set()
                for t in tasks:  # a no-op unless the job failed or was cancelled
                    t.cancel()
        job.update(state="done", report={
            "dry_run": not write, "extractor": llm_extract.EXTRACTOR_VERSION,
            "writer": lab_store.WRITER_VERSION, "verifier": verifier.VERIFIER_VERSION,
            "dob_known": bool(births), "write_enabled": config.LLM_LAB_WRITE_ENABLED,
            "settings": {**cfg.report(), "parallel_docs": parallel,
                         "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker,
                                                 parallel_docs)},
            "timing": _timing(started, report, parallel),
            "warnings": llm_extract_call.merge_warnings(
                [r for r in report if r.get("result") != "failed"]
            ),
            "totals": _lab_totals(report), "documents": report,
        })  # fmt: skip
    except verifier.NoBirthDateError:
        job.update(state="failed", error="no_birth_date", report={
            "dry_run": not write, "documents": report,
            "message": "no birth date known (README contract or patient context): nothing stored",
        })  # fmt: skip
    except Exception as exc:
        logger.exception("LLM lab job failed for %s", pid)
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
                logger.exception("could not persist the LLM lab job for %s", pid)
        if write:
            await _audit_lab_write(db, pid, job)


async def _audit_lab_write(db, pid: str, job: dict) -> None:
    """One activity_log row per REAL lab write: caller (keyed hash), document
    ids, the pre-store copy, results and stored / skipped counts. Counts only —
    never a value, a label or evidence text."""
    from oncofiles.models import ActivityLogEntry

    report = job.get("report") or {}
    totals = report.get("totals") or {}
    docs = [d for d in report.get("documents") or [] if isinstance(d, dict)]
    actor = str(job.get("actor") or "unknown")[:120]
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id="curated-lab",
                agent_id=actor,
                tool_name="curated_lab_extract",
                input_summary=json.dumps(
                    {
                        "by": actor,
                        "caller_email_hash": job.get("caller_email_hash"),
                        "dry_run": False,
                        "pre_store": job.get("pre_store"),
                        "document_ids": [d.get("document_id") for d in job.get("documents") or []],
                        "started_at": job.get("started_at"),
                        "extractor": job.get("extractor"),
                        "writer": job.get("writer"),
                    }
                ),  # fmt: skip
                output_summary=json.dumps(
                    {
                        "state": job.get("state"),
                        "results": {
                            r: sum(1 for d in docs if d.get("result") == r)
                            for r in {str(d.get("result")) for d in docs}
                        },
                        "stored": totals.get("stored") or {},
                        "skipped": totals.get("skipped") or {},
                    }
                ),  # fmt: skip
                status="ok" if job.get("state") == "done" else "error",
                error_message=job.get("error"),
                tags=json.dumps(["curated", "lab", "real", "pre-store"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for an LLM lab write")


async def curated_lab_extract(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    dry_run: bool = True,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
) -> str:
    """LLM lab facts for curated documents whose lab layout has no rule parser (#714).

    The LOCAL model reads each document's stored page text and lists the printed
    results; the generic verifier marks each machine or unverified with reasons.

    dry_run=true (default) writes NOTHING and reports, per document, the agreement
    with the rule facts already stored (agree / value_mismatch / rule_only /
    llm_only by code, day and value hash, also per code), what a write would store
    and what it would skip. ``totals.gate`` sums the documents that hold rule
    facts: that is the scorecard a maintainer reads before enabling writes.

    dry_run=false is OFF unless the server has LLM_LAB_WRITE_ENABLED=true (a
    maintainer decision). It stores the verified facts in the facts model with
    ``method = llm-lab`` (machine or unverified, never verified), page, evidence
    and a provenance row; a fact whose code and day the document already holds
    from a rule parser is never stored (``skipped_rule_exists``), nor an uncoded
    or non-lab one. Re-running a document replaces only this writer's rows and
    never verified or corrected ones; rule re-extraction never touches them.
    Before writing, a verified safety copy of the database is made (no copy, no
    write); every real run leaves an activity_log row with counts only. Refused
    while another curated run, re-OCR or extraction is active, or when no birth
    date is known; holds the patient lock; a document with a page the model did
    not assess is not stored. Explicit ``document_ids`` (max 10). Runs in the
    background; poll curated_reocr_status. Reports counts and codes only, never
    values. Roll back with curated_lab_retract.

    ``thinking`` / ``sampling`` / ``timeout_s`` / ``reasoning_marker`` /
    ``parallel_docs`` (see curated_llm_extract_preview) are accepted with
    dry_run=true only; with dry_run=false they are refused (error).
    """
    from oncofiles import config
    from oncofiles.curated import lab_store, llm, llm_extract_call
    from oncofiles.curated.llm_extract import EXTRACTOR_VERSION

    write = not dry_run
    llm_extract_call.refuse_overrides_on_write(
        write=write, thinking=thinking, sampling=sampling, timeout_s=timeout_s,
        reasoning_marker=reasoning_marker, parallel_docs=parallel_docs,
    )  # fmt: skip
    if write and not config.LLM_LAB_WRITE_ENABLED:
        return json.dumps({"error": "writes are disabled: LLM_LAB_WRITE_ENABLED is not true "
                           "(a maintainer decision after the dry-run agreement gate); "
                           "nothing was written"})  # fmt: skip
    if not llm.configured():
        return json.dumps({"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"})
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    try:
        ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))
    except (TypeError, ValueError):
        return json.dumps({"error": "document_ids must be integers"})
    if not ids:
        return json.dumps({"error": "document_ids is required"})
    if len(ids) > LAB_MAX_DOCS:
        return json.dumps({"error": f"too many documents: at most {LAB_MAX_DOCS} explicit "
                           f"document_ids per call, got {len(ids)}"})  # fmt: skip
    owned = await db.get_documents_by_ids(set(ids), patient_id=pid)
    missing = [i for i in ids if i not in owned]
    if missing:  # another patient's id looks exactly like an unknown one
        return json.dumps({"error": "not_found", "document_ids": missing,
                           "message": "nothing was changed"})  # fmt: skip
    docs = [r for r in await db.curated_reocr_candidates(patient_id=pid) if r["id"] in ids]
    if not docs:
        return json.dumps({"status": "nothing to do"})
    if (
        _reocr_jobs.get(pid, {}).get("state") == "running"
        or (pid in _runs and _runs[pid].get("state") == "running")
        or (write and patient_lock(pid).locked())
    ):
        return json.dumps({"error": "busy: a curated run, index, re-OCR, re-read or "
                           "extraction is in progress for this patient"})  # fmt: skip
    # Reserve the patient's job slot before any further await; released on refusal.
    previous = _reocr_jobs.get(pid)
    _reocr_jobs[pid] = job = {
        "state": "running", "kind": "lab_extract" if write else "lab_preview",
        "dry_run": not write, "extractor": EXTRACTOR_VERSION,
        "writer": lab_store.WRITER_VERSION, "started_at": datetime.now(UTC).isoformat(),
        "actor": "mcp", "caller_email_hash": caller_email_hash(_caller_email()),
        "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker, parallel_docs),
        "parallel_docs": llm_extract_call.parallel_docs(parallel_docs),
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
        from oncofiles.pre_store import PreStoreError, make_copy

        try:
            job["pre_store"] = await asyncio.to_thread(make_copy, db, "lab")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, PreStoreError) else type(exc).__name__
            logger.error("pre-store copy failed for %s (%s): lab write refused", pid[:8], reason)
            return release(f"refused: pre-store copy failed ({reason}); nothing was written")
    from oncofiles.cloud_guard import local_only

    # the WHOLE job (DB writes included, not only the model calls) runs local-only
    with local_only("curated LLM lab write" if write else "curated LLM lab preview"):
        job["task"] = asyncio.create_task(
            _lab_job(
                db, pid, docs, write=write, thinking=thinking, sampling=sampling,
                timeout_s=timeout_s, reasoning_marker=reasoning_marker,
                parallel_docs=parallel_docs,
            )
        )  # fmt: skip
    return json.dumps({k: v for k, v in job.items() if k != "task"}, ensure_ascii=False)


async def curated_lab_retract(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    dry_run: bool = True,
    reason: str = "",
) -> str:
    """Roll back what curated_lab_extract stored for documents (#714).

    Soft-deletes exactly the rows a re-extraction of each document would remove:
    every lab fact written by the LLM lab writer (``llm-lab*``) for that
    document that is not verified or corrected. Rule-parser lab facts, the
    clinical extractor's rows and anything a person wrote are never touched.
    Nothing is hard-deleted; running curated_lab_extract again stores the
    document anew.

    dry_run=true (default) writes nothing and returns, per document, how many
    rows would be ``removed`` and how many are ``kept``. dry_run=false needs a
    short ``reason`` (max 200 characters), holds the patient lock, is refused
    while a curated run, re-OCR, re-read or extraction is active for the
    patient, makes a verified safety copy of the database first (no copy, no
    change) and leaves one activity_log row (ids and counts only). Returns
    counts only, never values. Works also while LLM_LAB_WRITE_ENABLED is off
    and without the local model.

    Args:
        document_ids: 1-50 document ids of this patient. An id that is not
            this patient's is reported as not_found and nothing is changed.
        patient_slug: required for admin callers.
        dry_run: false to actually retract.
        reason: why (required with dry_run=false); kept in the rows' deletion
            audit, never logged.
    """
    from oncofiles.curated import lab_store

    return await _retract(
        ctx, document_ids, patient_slug, dry_run, reason, family="lab",
        retract=lab_store.retract_document,
    )  # fmt: skip


# ── extraction scorecard (#773): fixed page set, counts only ──────────────


async def _model_override_refusal(model: str | None) -> dict | None:
    """A scorecard's per-job ``model`` must already be LOADED in the
    configured LM Studio (native ``GET /api/v0/models``, ``state ==
    "loaded"``; same host and key as every call) before anything starts: no
    slot, no run row. An override never triggers a JIT load next to the
    production model; an unreadable native list refuses (fail closed)."""
    if model is None:
        return None
    from oncofiles.curated import llm

    code = await asyncio.to_thread(llm.model_refusal, model)
    if code is None:
        return None
    message = (
        "the model is not loaded in the local LM Studio (/api/v0/models state != loaded)"
        if code == "model_not_loaded"
        else "the local LM Studio model states (/api/v0/models) are unavailable"
    )
    return {"error": code, "model": model, "message": message}


SCORECARD_MAX_DOCS = 25
_SCORECARD_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,59}")


async def _scorecard_job(
    db,
    pid: str,
    clinical_docs: list[dict],
    lab_docs: list[dict],
    *,
    row_id: int,
    label: str,
    compare_to_label: str | None,
    requested: int,
    missing: int,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Run the existing extractors + verifiers over a fixed document set and
    score them (#773). DRY RUN: no facts, no Drive; the only write is the
    job's own ``curated_runs`` row (trigger ``scorecard``) with the
    aggregated counts. Up to ``parallel_docs`` documents at once (default
    ``LLM_EXTRACT_PARALLEL_DOCS`` = 1, one LM Studio slot), each document's
    pages one after another; every DB read happens before the model phase;
    the task runs inside ``local_only()``. ``totals.speed.wall_seconds`` sits
    next to ``seconds_total`` (summed per-page seconds). ``model`` /
    ``reasoning_effort`` (validated by the tool) ride in this job's
    ``CallSettings`` to every call of this job only."""
    from oncofiles.curated import (
        llm,
        llm_extract,
        llm_extract_call,
        scorecard,
        verifier,
        verifier_clinical,
    )
    from oncofiles.curated import llm_extract_clinical as lx
    from oncofiles.curated.facts_rules import Vocabulary

    started = time.monotonic()
    job = _reocr_jobs[pid]
    cfg = llm_extract_call.settings(thinking, sampling, timeout_s, reasoning_marker,
                                    model=model, reasoning_effort=reasoning_effort)  # fmt: skip
    parallel = llm_extract_call.parallel_docs(parallel_docs)
    cards: list[dict] = []
    stopped = False
    try:
        births = await _all_birth_dates(db, pid)
        vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
        stored = await db.fact_verification_rows(patient_id=pid) if lab_docs else []
        plan = [("clinical", d) for d in clinical_docs] + [("lab", d) for d in lab_docs]
        # phase 1 (event loop, DB one call at a time): every document's page text
        works = []
        for extractor, d in plan:
            prepare = _clinical_work if extractor == "clinical" else _lab_shadow_work
            works.append(await prepare(db, pid, d, vocab=vocab, births=births, cfg=cfg))
        # phase 2: model calls only, up to ``parallel`` documents at once
        await _extract_documents(works, parallel=parallel)
        # phase 3: cards in input order
        for (extractor, _d), w in zip(plan, works, strict=True):
            if extractor == "clinical":
                cards.append(scorecard.clinical_card(_clinical_entry(w)[0]))
            else:
                entry = _lab_shadow_entry(w, stored)
                _add_label_review(entry, w)  # in-memory card only; never persisted
                cards.append(scorecard.lab_card(entry))
        stopped = any(w.stopped or (w.final or {}).get("error") == "stopping" for w in works)
        stopped = stopped or _stopping.is_set()  # a stop inside a document: pages unread
        totals = scorecard.totals(cards, requested=requested, missing=missing)
        totals["speed"]["wall_seconds"] = round(time.monotonic() - started, 1)
        prev = await db.get_last_scorecard(
            patient_id=pid, label_prefix=compare_to_label, before_id=row_id
        )
        prev_report = (prev or {}).get("report") or {}
        versions = scorecard.versions(
            extractor=llm_extract.EXTRACTOR_VERSION, verifier=verifier.VERIFIER_VERSION,
            clinical_extractor=lx.EXTRACTOR_VERSION,
            clinical_verifier=verifier_clinical.CLINICAL_VERIFIER_VERSION,
            model=model, reasoning_effort=reasoning_effort,
        )  # fmt: skip
        job.update(state="done", report={
            "dry_run": True, "scorecard": scorecard.SCORECARD_VERSION, "label": label,
            "partial": stopped, "versions": versions,
            "extractors": {"clinical": lx.EXTRACTOR_VERSION,
                           "clinical_verifier": verifier_clinical.CLINICAL_VERIFIER_VERSION,
                           "lab": llm_extract.EXTRACTOR_VERSION,
                           "lab_verifier": verifier.VERIFIER_VERSION},
            "settings": {**cfg.report(), "parallel_docs": parallel,
                         "model": model or llm.model_id(),
                         "reasoning_effort": reasoning_effort or llm.DEFAULT_REASONING_EFFORT,
                         "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker,
                                                 parallel_docs, model, reasoning_effort)},
            "totals": totals,
            "compared_to": {"row_id": prev["id"], "label": prev_report.get("label"),
                            "finished_at": prev.get("finished_at")} if prev else None,
            # never a delta or a regression flag across versions (#826)
            **scorecard.compare(prev_report if prev else None, versions, totals),
            "documents": cards,
        })  # fmt: skip
    except Exception as exc:
        logger.exception("extraction scorecard failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__,
                   report={"dry_run": True, "label": label, "documents": cards})  # fmt: skip
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
        status = "failed"
        if job.get("state") == "done":
            status = "partial" if stopped else "ok"  # only "ok" is a delta baseline
        try:
            from oncofiles.curated import scorecard

            await db.finish_curated_run(
                row_id, patient_id=pid, status=status, message=job.get("error") or "",
                report=scorecard.persisted(job.get("report") or {}),
            )  # fmt: skip
        except Exception:
            logger.exception("could not persist the extraction scorecard for %s", pid)


async def curated_extraction_scorecard(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    lab_document_ids: list[int] | None = None,
    label: str = "scorecard",
    compare_to_label: str | None = None,
    thinking: str | None = None,
    sampling: str | None = None,
    timeout_s: int | None = None,
    reasoning_marker: str | None = None,
    parallel_docs: int | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """DRY RUN extraction scorecard (#773): measure the LOCAL-model extractors
    on a fixed set of curated documents, as counts and codes only.

    ``document_ids`` run through the clinical extractor (diagnoses, biomarkers,
    treatments, procedures, lesions, planned dates); ``lab_document_ids``
    through the lab extractor, compared with the stored rule facts. Both use
    the same extractor + verifier code as curated_clinical_extract_preview and
    curated_llm_extract_preview. Writes NO facts and nothing to Drive; only a
    summary row (trigger ``scorecard``: label, settings, aggregated totals, no
    values) that never counts as a curated run or preview.

    The report (poll curated_reocr_status, kind ``extraction_scorecard``) has,
    per document: pages; per page finish / seconds / completion and reasoning
    tokens; items per kind as machine / unverified / dropped; unverified and
    drop reason codes; for lab documents agree / value_mismatch /
    date_mismatch / rule_only / llm_only / llm_no_value / llm_uncoded.
    Totals: stability (% finish=stop, timeouts, truncations, % pages with
    reasoning_tokens > 50), speed (p50/p95 seconds and completion tokens per page),
    trust (% machine), lab agreement (% agree of comparable =
    agree + value_mismatch), the settings used, and ``delta`` metric by metric
    against the previous ok scorecard of this patient (or the newest one whose
    label starts with ``compare_to_label``), with a ``regressions`` list.
    ``versions`` holds the scorecard, extractor and verifier versions (lab:
    ``extractor_version`` / ``verifier_version``, plus the clinical pair). A
    baseline of other or unrecorded versions is not compared:
    ``baseline_comparable`` is false, ``definition_changed`` lists
    ``{field: [baseline, current]}`` and there is no ``delta`` and no
    regression flag.
    Never values, labels or evidence text.

    Refused while a curated run, re-OCR, re-read, extraction or other preview
    is active for the patient. Max 25 documents in total; one model call at a
    time per document, ``parallel_docs`` documents at once; about a minute per
    page. ``totals.speed.wall_seconds`` (the whole job) sits next to
    ``seconds_total`` (summed per-page seconds).

    Args:
        document_ids: clinical documents (explicit ids).
        lab_document_ids: lab documents (explicit ids), optional.
        label: short name of this scorecard, e.g. "v1-baseline"
            (letters, digits, ``. _ : -``; max 60).
        compare_to_label: compare with the newest scorecard whose label starts
            with this; null = the previous scorecard.
        thinking / sampling / timeout_s / reasoning_marker: per-job overrides,
            as in curated_clinical_extract_preview; echoed in ``settings``.
        parallel_docs: documents scored at once (int, 1-3, #773); null keeps
            LLM_EXTRACT_PARALLEL_DOCS (default 1). Each running document holds
            one LM Studio slot; echoed in ``settings.parallel_docs``.
        model: LM Studio model id for this job only (dry run; 1-200 of
            ``A-Za-z0-9._:/@-``); null = LOCAL_LLM_MODEL. Refused
            (``model_not_loaded``) unless the configured LM Studio reports it
            ``loaded`` at its native ``/api/v0/models`` (``model_state_unknown``
            when that list is unavailable); nothing is loaded or downloaded.
        reasoning_effort: "none" | "low" | "medium" | "high", sent for this
            job only; null = the default request. Both are echoed in
            ``settings`` and, when given, recorded in ``versions``: a baseline
            with another (or no) override is not comparable.
    """
    from oncofiles import config
    from oncofiles.curated import llm, llm_extract_call

    llm_extract_call.validate_overrides(
        thinking, sampling, timeout_s, reasoning_marker, parallel_docs, model, reasoning_effort
    )
    for name, value in (("label", label), ("compare_to_label", compare_to_label)):
        if value is not None and not _SCORECARD_LABEL.fullmatch(str(value)):
            return json.dumps({"error": f"{name} must be 1-60 letters, digits or . _ : - "
                               "(starting with a letter or digit)"})  # fmt: skip
    clinical_ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))
    lab_ids = list(dict.fromkeys(int(i) for i in (lab_document_ids or [])))
    requested = len(clinical_ids) + len(lab_ids)
    if not requested:
        return json.dumps({"error": "document_ids (or lab_document_ids) is required"})
    if requested > SCORECARD_MAX_DOCS:
        return json.dumps({"error": f"too many documents: at most {SCORECARD_MAX_DOCS} in total, "
                           f"got {requested}"})  # fmt: skip
    if clinical_ids and config.LLM_CLINICAL_EXTRACT_DISABLED:
        return json.dumps({"error": "disabled by LLM_CLINICAL_EXTRACT_DISABLED=true"})
    if lab_ids and config.LLM_EXTRACT_SHADOW_DISABLED:
        return json.dumps({"error": "disabled by LLM_EXTRACT_SHADOW_DISABLED=true"})
    if not llm.configured():
        return json.dumps({"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"})
    if refused := await _model_override_refusal(model):
        return json.dumps(refused)
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    known = {r["id"]: r for r in await db.curated_reocr_candidates(patient_id=pid)}
    clinical_docs = [known[i] for i in clinical_ids if i in known]
    lab_docs = [known[i] for i in lab_ids if i in known]
    missing = sorted({i for i in clinical_ids + lab_ids if i not in known})
    if not clinical_docs and not lab_docs:
        return json.dumps({"status": "nothing to do", "missing_document_ids": missing})
    if _busy(pid):
        return json.dumps({"error": "busy: a curated run, re-OCR, re-read, extraction or "
                           "preview is in progress for this patient"})  # fmt: skip
    # Reserve the patient's job slot before any further await (one job per patient).
    previous = _reocr_jobs.get(pid)
    _reocr_jobs[pid] = job = {
        "state": "running", "kind": "extraction_scorecard", "dry_run": True, "label": label,
        "compare_to_label": compare_to_label, "started_at": datetime.now(UTC).isoformat(),
        "overrides": _overrides(thinking, sampling, timeout_s, reasoning_marker, parallel_docs,
                                model, reasoning_effort),
        "parallel_docs": llm_extract_call.parallel_docs(parallel_docs),
        "documents": [{"document_id": d["id"], "category": d.get("category"),
                       "extractor": "clinical"} for d in clinical_docs]
        + [{"document_id": d["id"], "category": d.get("category"), "extractor": "lab"}
           for d in lab_docs],
        "missing_document_ids": missing,
    }  # fmt: skip
    try:
        row_id = await db.insert_curated_run(patient_id=pid, dry_run=True, trigger="scorecard")
    except Exception as exc:
        if previous is not None:
            _reocr_jobs[pid] = previous
        else:
            _reocr_jobs.pop(pid, None)
        logger.exception("could not start the extraction scorecard for %s", pid)
        return json.dumps({"error": f"could not record the scorecard ({type(exc).__name__})"})
    job["run_row_id"] = row_id
    from oncofiles.cloud_guard import local_only

    # The task copies the current context: the WHOLE job runs local-only.
    with local_only("curated extraction scorecard"):
        job["task"] = asyncio.create_task(
            _scorecard_job(
                db, pid, clinical_docs, lab_docs, row_id=row_id, label=label,
                compare_to_label=compare_to_label, requested=requested, missing=len(missing),
                thinking=thinking, sampling=sampling, timeout_s=timeout_s,
                reasoning_marker=reasoning_marker, parallel_docs=parallel_docs,
                model=model, reasoning_effort=reasoning_effort,
            )
        )  # fmt: skip
    return json.dumps(_public_job(pid), ensure_ascii=False)


# ── classification scorecard (#926): stored sidecar texts, counts only ────

CLASSIFY_SCORECARD_MAX_DOCS = 25
CLASSIFY_SCORECARD_MAX_REPEAT = 3


async def _patient_contract(db, pid: str):
    """The patient's README contract, read from Drive (read-only, own
    connection in a worker thread). Raises when there is none."""
    from oncofiles.curated import indexer
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.server import _create_patient_clients

    clients = await _create_patient_clients(db, pid)
    if not clients or clients[0] is None:
        raise RuntimeError("no Drive client for the patient")
    gdrive, folder_id = clients[0], clients[3]

    def _blocking():
        own = gdrive.dedicated_copy()
        try:
            return indexer.read_contract(DriveOps(own), folder_id)
        finally:
            own.close()

    return await asyncio.to_thread(_blocking)


async def _classify_scorecard_job(
    db,
    pid: str,
    docs: list[dict],
    *,
    row_id: int,
    label: str,
    compare_to_label: str | None,
    requested: int,
    missing: int,
    repeat: int,
    sampling: str | None = None,
    prompt_variant: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> None:
    """Re-run ``classify.propose`` + ``validate`` on each document's stored
    sidecar text (#926). DRY RUN: no Drive write, no filing, no sidecar, no
    fact; the only write is the job's own ``curated_runs`` row (trigger
    ``scorecard``, report ``kind: classification``). DB reads first, then the
    model calls one at a time in a worker thread (one LM Studio slot); the
    task runs inside ``local_only()``. ``model`` / ``reasoning_effort``
    (validated by the tool) are passed explicitly to every call of this job
    only; never config or module state."""
    from oncofiles.curated import classify_scorecard as csc
    from oncofiles.curated import llm, llm_input
    from oncofiles.curated import vocabulary as vocab_mod

    started = time.monotonic()
    job = _reocr_jobs[pid]
    request = csc.request_settings(sampling, prompt_variant)
    variant = request["prompt_variant"]
    auto_vocab = vocab_mod.enabled()
    cards: list[dict] = []
    stopped = False
    try:
        contract = await _patient_contract(db, pid)
        # phase 1 (event loop, DB one call at a time): every document's text
        work: list[tuple[dict, str | None, str | None]] = []
        for d in docs:
            loaded = await llm_input.load_for_llm(db, pid, d)
            if isinstance(loaded, llm_input.Excluded):
                work.append((d, None, loaded.reason))
                continue
            text = "\n\n".join(t for _, t in loaded if t and t.strip())
            work.append((d, text, None if text.strip() else "no_page_text"))
        # phase 2: model calls only, one document and one run at a time.
        # Progress (#942 follow-up): counts in the job (curated_reocr_status)
        # and one INFO line per document — ids, counts and seconds only.
        runnable = sum(1 for _, _, skip in work if not skip)
        progress = job["progress"] = {
            "docs_total": len(work), "docs_done": 0, "docs_skipped": 0,
            "calls_total": runnable * repeat, "calls_done": 0,
        }  # fmt: skip
        for d, text, skip in work:
            if skip:
                cards.append(csc.skipped_card(d["id"], d.get("category"), skip))
                progress["docs_skipped"] += 1
                progress["docs_done"] += 1
                continue
            if _stopping.is_set():
                stopped = True
                break
            runs = []
            doc_started = time.monotonic()
            for _ in range(repeat):
                runs.append(await asyncio.to_thread(
                    csc.run_once, contract, text, d.get("filename") or "",
                    prompt_variant=variant,
                    sampling=request["sampling"] if variant == "v2" else None,
                    auto_vocab=auto_vocab, model=model, reasoning_effort=reasoning_effort,
                ))  # fmt: skip
                progress["calls_done"] += 1
            cards.append(csc.card(d["id"], d.get("category"), runs))
            progress["docs_done"] += 1
            logger.info(
                "classification scorecard row=%s doc=%s %d/%d calls=%d/%d errors=%d seconds=%.0f",
                row_id, d["id"], progress["docs_done"], progress["docs_total"],
                progress["calls_done"], progress["calls_total"],
                sum(1 for r in runs if r.get("error_kind")), time.monotonic() - doc_started,
            )  # fmt: skip
        totals = csc.totals(cards, requested=requested, missing=missing)
        totals["speed"]["wall_seconds"] = round(time.monotonic() - started, 1)
        prev = await db.get_last_scorecard(
            patient_id=pid, label_prefix=compare_to_label, before_id=row_id, kind=csc.KIND
        )
        prev_report = (prev or {}).get("report") or {}
        versions = csc.versions(sampling=request["sampling"], prompt_variant=variant,
                                model=model, reasoning_effort=reasoning_effort)  # fmt: skip
        job.update(state="done", report={
            "kind": csc.KIND, "dry_run": True, "scorecard": csc.SCORECARD_VERSION,
            "label": label, "partial": stopped, "versions": versions,
            "settings": {**request, "repeat": repeat, "auto_vocab": auto_vocab,
                         "max_age_days": None, "model": model or llm.model_id(),
                         "reasoning_effort": reasoning_effort or llm.DEFAULT_REASONING_EFFORT,
                         "overrides": {k: v for k, v in (("prompt_variant", prompt_variant),
                                                         ("sampling", sampling),
                                                         ("model", model),
                                                         ("reasoning_effort",
                                                          reasoning_effort)) if v}},
            "totals": totals,
            "compared_to": {"row_id": prev["id"], "label": prev_report.get("label"),
                            "finished_at": prev.get("finished_at")} if prev else None,
            **csc.compare(prev_report if prev else None, versions, totals,
                          csc.per_document(cards)),
            "documents": cards,
        })  # fmt: skip
    except Exception as exc:
        logger.exception("classification scorecard failed for %s", pid)
        job.update(state="failed", error=type(exc).__name__,
                   report={"kind": csc.KIND, "dry_run": True, "label": label,
                           "documents": cards})  # fmt: skip
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
        status = "failed"
        if job.get("state") == "done":
            status = "partial" if stopped else "ok"  # only "ok" is a delta baseline
        try:
            await db.finish_curated_run(
                row_id, patient_id=pid, status=status, message=job.get("error") or "",
                report=csc.persisted(job.get("report") or {}),
            )  # fmt: skip
        except Exception:
            logger.exception("could not persist the classification scorecard for %s", pid)


async def curated_classify_scorecard(
    ctx: Context,
    document_ids: list[int],
    patient_slug: str | None = None,
    label: str = "classify",
    compare_to_label: str | None = None,
    repeat: int = 1,
    sampling: str | None = None,
    prompt_variant: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """DRY RUN classification scorecard (#926): re-run the curated
    classification (LOCAL model, ``classify.propose`` + ``validate``) on the
    stored sidecar texts of filed curated documents, as counts and codes only.

    Nothing is filed, renamed, written to Drive or stored as a fact; the
    README is only read. The only write is a summary row (trigger
    ``scorecard``, report ``kind: classification``: label, settings, versions,
    totals and ``per_document`` rows of ids and codes only) that never counts
    as a curated run or preview. The report (poll
    curated_reocr_status, kind ``classify_scorecard``) has per document its
    runs: review yes/no, review reason codes, agreement of doc type / date /
    institution with the currently filed name, truncation, retry, seconds,
    tokens, the ``metadata_unverified`` codes; totals: review share, reason
    histogram, agreement %, truncations, p50/p95 seconds, and with
    ``repeat`` ≥ 2 the share of documents whose runs all agreed. ``delta``
    against the previous ok classification scorecard (or the newest whose
    label starts with ``compare_to_label``), with ``regressions``, and
    ``per_document_delta``: documents whose review state or doc type
    agreement flipped (counts and ids). Never a
    filename, value or text. The age rule of new scans is not applied
    (filed documents are old by nature).

    Refused while a curated run, re-OCR, re-read, extraction or preview is
    active for the patient. Max 25 documents; each run holds one LM Studio
    slot, one at a time (about a minute per run with reasoning on).

    Args:
        document_ids: filed curated documents (explicit ids, at most 25).
        patient_slug: the patient (admins must pass it).
        label: short name, e.g. "before-926" (letters, digits, ``. _ : -``; max 60).
        compare_to_label: compare with the newest classification scorecard
            whose label starts with this; null = the previous one.
        repeat: runs per document (1-3); 2 measures stability.
        sampling: "thinking" | "greedy" for this job (#926 step 2); null =
            CURATED_CLASSIFY_SAMPLING. Only with the v2 request (refused
            under v1). Echoed in ``settings``.
        prompt_variant: "v1" | "v2" for this job (dry run only); null =
            CURATED_CLASSIFY_PROMPT. v1 = the request before #926, v2 =
            reasoning + sampling + worked examples. Echoed in ``settings``
            and ``versions`` (with its ``prompt_version``), so a before (v1)
            and an after (v2) run on the same deployed code.
        model: LM Studio model id for this job only (dry run; 1-200 of
            ``A-Za-z0-9._:/@-``); null = LOCAL_LLM_MODEL. Refused
            (``model_not_loaded``) unless the configured LM Studio reports it
            ``loaded`` at its native ``/api/v0/models`` (``model_state_unknown``
            when that list is unavailable); nothing is loaded or downloaded. In ``settings``
            and ``versions``: a baseline of another model is not comparable
            (no aggregate ``delta``), ``per_document_delta`` is still given
            with ``across_definitions: true``.
        reasoning_effort: "none" | "low" | "medium" | "high", sent for this
            job only; null = the default request. Same versions rule.
    """
    from oncofiles.curated import classify, llm
    from oncofiles.curated import classify_scorecard as csc

    for name, value in (("label", label), ("compare_to_label", compare_to_label)):
        if value is not None and not _SCORECARD_LABEL.fullmatch(str(value)):
            return json.dumps({"error": f"{name} must be 1-60 letters, digits or . _ : - "
                               "(starting with a letter or digit)"})  # fmt: skip
    if prompt_variant is not None and prompt_variant not in classify.PROMPT_VARIANTS:
        return json.dumps({"error": "prompt_variant must be one of "
                           f"{list(classify.PROMPT_VARIANTS)} or null"})  # fmt: skip
    if sampling is not None and sampling not in classify.SAMPLING_MODES:
        return json.dumps({"error": f"sampling must be one of {sorted(classify.SAMPLING_MODES)} "
                           "or null"})  # fmt: skip
    if sampling is not None and classify.prompt_variant(prompt_variant) != "v2":
        return json.dumps({"error": "sampling applies only to prompt_variant v2"})
    if (
        isinstance(repeat, bool)
        or not isinstance(repeat, int)
        or not 1 <= repeat <= CLASSIFY_SCORECARD_MAX_REPEAT
    ):
        return json.dumps({"error": f"repeat must be an int, 1-{CLASSIFY_SCORECARD_MAX_REPEAT}"})
    if err := llm.model_override_error(model, reasoning_effort):
        return json.dumps({"error": err})
    ids = list(dict.fromkeys(int(i) for i in (document_ids or [])))
    if not ids:
        return json.dumps({"error": "document_ids is required"})
    if len(ids) > CLASSIFY_SCORECARD_MAX_DOCS:
        return json.dumps({"error": f"too many documents: at most {CLASSIFY_SCORECARD_MAX_DOCS}, "
                           f"got {len(ids)}"})  # fmt: skip
    if not llm.configured():
        return json.dumps({"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"})
    if refused := await _model_override_refusal(model):
        return json.dumps(refused)
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    known = {r["id"]: r for r in await db.curated_reocr_candidates(patient_id=pid)}
    docs = [known[i] for i in ids if i in known]
    missing = sorted(i for i in ids if i not in known)
    if not docs:
        return json.dumps({"status": "nothing to do", "missing_document_ids": missing})
    if _busy(pid):
        return json.dumps({"error": "busy: a curated run, re-OCR, re-read, extraction or "
                           "preview is in progress for this patient"})  # fmt: skip
    previous = _reocr_jobs.get(pid)
    _reocr_jobs[pid] = job = {
        "state": "running", "kind": "classify_scorecard", "dry_run": True, "label": label,
        "compare_to_label": compare_to_label, "started_at": datetime.now(UTC).isoformat(),
        "repeat": repeat, **csc.request_settings(sampling, prompt_variant),
        "overrides": {k: v for k, v in (("model", model), ("reasoning_effort", reasoning_effort))
                      if v is not None},
        "documents": [{"document_id": d["id"], "category": d.get("category")} for d in docs],
        "missing_document_ids": missing,
    }  # fmt: skip
    try:
        row_id = await db.insert_curated_run(patient_id=pid, dry_run=True, trigger="scorecard")
    except Exception as exc:
        if previous is not None:
            _reocr_jobs[pid] = previous
        else:
            _reocr_jobs.pop(pid, None)
        logger.exception("could not start the classification scorecard for %s", pid)
        return json.dumps({"error": f"could not record the scorecard ({type(exc).__name__})"})
    job["run_row_id"] = row_id
    from oncofiles.cloud_guard import local_only

    with local_only("curated classification scorecard"):
        job["task"] = asyncio.create_task(
            _classify_scorecard_job(
                db, pid, docs, row_id=row_id, label=label, compare_to_label=compare_to_label,
                requested=len(ids), missing=len(missing), repeat=repeat, sampling=sampling,
                prompt_variant=prompt_variant, model=model, reasoning_effort=reasoning_effort,
            )
        )  # fmt: skip
    return json.dumps(_public_job(pid), ensure_ascii=False)


# ── chat scorecard (#1025): the chat's quality gate, counts only ──────────

CHAT_SCORECARD_PRINCIPAL = "chat-scorecard"


async def _chat_citation_checks(db, pid: str, shown: str, citations: list[dict]) -> dict[int, str]:
    """Per cited document: does its detail (re-read through the chat's own
    ``document_detail`` tool, in the chat scope) hold a number or date of the
    answer? ``found`` | ``missing`` | ``unavailable``."""
    from oncofiles.chat import grounding
    from oncofiles.chat import scorecard as chs
    from oncofiles.chat import tools as chat_tools
    from oncofiles.chat.scope import chat_scope

    answer_tokens = set(grounding.tokens(shown))
    out: dict[int, str] = {}
    for doc_id in sorted({c["document_id"] for c in citations
                          if isinstance(c.get("document_id"), int)}):  # fmt: skip
        with chat_scope(pid):
            result = await chat_tools.run_tool(db, "document_detail", {"document_id": doc_id})
        detail = json.dumps(result.data, ensure_ascii=False, default=str) if result.ok else None
        out[doc_id] = chs.citation_value_found(answer_tokens, detail)
    return out


async def _chat_scorecard_job(
    db,
    pid: str,
    qset,
    *,
    row_id: int,
    label: str,
    compare_to_label: str | None,
    repeat: int,
    model: str | None,
) -> None:
    """Run every question of ``qset`` through the REAL chat core
    (``chat.core.answer_text``, chat scope, local model) ``repeat`` times.
    DRY RUN: the chat tools are read-only and nothing is filed or stored as a
    fact; the writes are this job's ``curated_runs`` row and the core's own
    per-turn ``activity_log`` row (counts only). Every model call of the job
    goes to the BACKGROUND lane (``llm.scorecard_call_overrides``), so a
    scorecard never takes the slot reserved for a person in a chat, and gets
    ``model`` (validated by the tool) when given. Question and answer texts
    live in memory for one turn only (the follow-up needs the previous
    answer); the job report and the row hold codes and counts."""
    from oncofiles.chat import core as chat_core
    from oncofiles.chat import scorecard as chs
    from oncofiles.chat import tools as chat_tools
    from oncofiles.curated import llm

    started = time.monotonic()
    job = _reocr_jobs[pid]
    cards: list[dict] = [chs.skipped_card(cat, reason) for cat, reason in qset.skipped]
    stopped = False
    lang = qset.lang
    principal = f"{CHAT_SCORECARD_PRINCIPAL}:{row_id}"
    try:
        progress = job["progress"] = {"turns_total": len(qset.questions) * repeat,
                                      "turns_done": 0}  # fmt: skip
        for run in range(1, repeat + 1):
            answers: dict[str, tuple[str, str]] = {}  # id → (question, shown answer), in memory
            for q in qset.questions:
                if _stopping.is_set():
                    stopped = True
                    break
                history: list[dict] = []
                if q.follow_up_of:
                    prior = answers.get(q.follow_up_of)
                    if prior is None:
                        cards.append(chs.skipped_card(q.category, "follow_up_base_missing", run))
                        progress["turns_done"] += 1
                        continue
                    history = [{"role": "user", "content": prior[0]},
                               {"role": "assistant", "content": prior[1]}]  # fmt: skip
                messages = [*history, {"role": "user", "content": q.text}]
                t0 = time.monotonic()
                with (
                    chat_tools.record_results() as recorded,
                    llm.scorecard_call_overrides(model=model),
                ):
                    out = await chat_core.answer_text(
                        db, patient_id=pid, principal=principal, messages=messages, lang=lang
                    )
                seconds = time.monotonic() - t0
                shown = chs.strip_fixed(out.get("text") or "", lang)
                if shown and not out.get("error"):
                    cut = shown.encode()[: chat_core.MAX_MESSAGE_BYTES - 500]
                    answers[q.id] = (q.text, cut.decode("utf-8", "ignore"))
                checks = await _chat_citation_checks(db, pid, shown, out.get("citations") or [])
                c = chs.card(
                    q, out, recorded_texts=[r.text for r in recorded],
                    today=chat_core._today(), lang=lang, citation_checks=checks,
                    seconds=seconds, run=run,
                )  # fmt: skip
                cards.append(c)
                progress["turns_done"] += 1
                logger.info(
                    "chat scorecard row=%s %d/%d category=%s result=%s route=%s model_calls=%d "
                    "seconds=%.1f",
                    row_id, progress["turns_done"], progress["turns_total"], c["category"],
                    c["result"], c["route"], c["model_calls"], c["seconds"],
                )  # fmt: skip
            if stopped:
                break
        totals = chs.totals(cards, repeat=repeat)
        totals["speed"]["wall_seconds"] = round(time.monotonic() - started, 1)
        versions = chs.versions(question_set=qset.name, model=model or llm.model_id())
        prev = await db.get_last_scorecard(
            patient_id=pid, label_prefix=compare_to_label, before_id=row_id, kind=chs.KIND
        )
        prev_report = (prev or {}).get("report") or {}
        job.update(state="done", report={
            "kind": chs.KIND, "dry_run": True, "scorecard": chs.SCORECARD_VERSION,
            "label": label, "partial": stopped, "versions": versions,
            "settings": {"question_set": qset.name, "lang": lang, "repeat": repeat,
                         "model": model or llm.model_id(), "lane": "background",
                         "overrides": {"model": model} if model else {}},
            "totals": totals, "gate": chs.gate(totals),
            "compared_to": {"row_id": prev["id"], "label": prev_report.get("label"),
                            "finished_at": prev.get("finished_at")} if prev else None,
            **chs.compare(prev_report if prev else None, versions, totals),
            "questions": cards,
        })  # fmt: skip
    except Exception as exc:
        logger.exception("chat scorecard failed for %s", pid[:8])
        job.update(state="failed", error=type(exc).__name__,
                   report={"kind": chs.KIND, "dry_run": True, "label": label,
                           "questions": cards})  # fmt: skip
    finally:
        job["finished_at"] = datetime.now(UTC).isoformat()
        status = "failed"
        if job.get("state") == "done":
            status = "partial" if stopped else "ok"  # only "ok" is a delta baseline
        try:
            await db.finish_curated_run(
                row_id, patient_id=pid, status=status, message=job.get("error") or "",
                report=chs.persisted(job.get("report") or {}),
            )  # fmt: skip
        except Exception:
            logger.exception("could not persist the chat scorecard for %s", pid[:8])


async def _chat_question_set(db, pid: str, question_set: str):
    """The default set built from this patient's own data, or a question file."""
    from oncofiles.chat import core as chat_core
    from oncofiles.chat import scorecard as chs

    if question_set == "default":
        frame = await chat_core._frame(db, pid)
        catalog = await db.fact_catalog_rows(patient_id=pid)
        institutions = await db.document_institution_counts(patient_id=pid)
        return chs.build_default(catalog, institutions, frame.vocab.allowed_names)
    return await asyncio.to_thread(chs.load_file, question_set)


async def curated_chat_scorecard(
    ctx: Context,
    patient_slug: str,
    question_set: str = "default",
    label: str | None = None,
    compare_to_label: str | None = None,
    model: str | None = None,
    repeat: int = 1,
) -> str:
    """DRY RUN chat scorecard (#1025): the quality gate of the chat before
    caregivers get it, and for every later model or prompt change.

    Runs a question set through the REAL chat core for ONE patient (chat
    scope as in production, read-only tools, LOCAL model on the background
    lane, so it never takes the slot reserved for a person in a chat) and
    reports counts and codes only: answered / refused / not found, citations
    present and whether each cited document holds a number or date of the
    answer (re-read through the chat's ``document_detail``), grounding
    regenerations and replacements plus an independent re-check of every
    shown answer, wrong-patient questions refused without a model call,
    empty-answer guard hits, p50/p95 seconds and tokens, route mix (router vs
    JSON loop), and a ``gate`` (0 ungrounded answers, 100 % other-person
    refusals, citations on ≥ 95 % of answered questions, p95 ≤ 30 s; pass /
    fail per item; thresholds to be confirmed by the maintainer).

    Nothing is filed or stored as a fact. The writes are one summary row
    (trigger ``scorecard``, report ``kind: chat``: label, settings, versions,
    totals, gate) and the chat core's own per-turn activity row (counts
    only); no question, answer, name or value is kept. ``delta`` against the
    previous ok chat scorecard (or the newest whose label starts with
    ``compare_to_label``), withheld when the scorecard version, model,
    question set or chat prompt version differs. Poll curated_reocr_status
    (kind ``chat_scorecard``). Admin only; refused while any curated run or a
    job of this patient is active.

    Args:
        patient_slug: the patient (required).
        question_set: "default" (generic questions built from this patient's
            own lab codes, institutions, report and planned dates at run time)
            or an absolute path to a JSON question file on the server (e.g.
            the production questions of the private RUNBOOK; its content is
            never stored or echoed, only a digest).
        label: short name, e.g. "before-1025" (letters, digits, ``. _ : -``;
            max 60); null = "chat".
        compare_to_label: compare with the newest chat scorecard whose label
            starts with this; null = the previous one.
        model: LM Studio model id for this job only (dry run); null =
            LOCAL_LLM_MODEL. Refused unless LM Studio reports it loaded.
            Another model is another definition (no delta).
        repeat: runs of the whole set (1-3); 2 measures stability.
    """
    from oncofiles.chat import scorecard as chs
    from oncofiles.curated import llm
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("curated_chat_scorecard")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if not (patient_slug or "").strip():
        return json.dumps({"error": "patient_slug is required"})
    label = "chat" if label is None else label
    for name, value in (("label", label), ("compare_to_label", compare_to_label)):
        if value is not None and not _SCORECARD_LABEL.fullmatch(str(value)):
            return json.dumps({"error": f"{name} must be 1-60 letters, digits or . _ : - "
                               "(starting with a letter or digit)"})  # fmt: skip
    if isinstance(repeat, bool) or not isinstance(repeat, int) or not 1 <= repeat <= chs.MAX_REPEAT:
        return json.dumps({"error": f"repeat must be an int, 1-{chs.MAX_REPEAT}"})
    if not isinstance(question_set, str) or not question_set:
        return json.dumps({"error": "question_set must be 'default' or a JSON file path"})
    if err := llm.model_override_error(model, None):
        return json.dumps({"error": err})
    if not llm.configured():
        return json.dumps({"error": "local model not configured (LOCAL_LLM_BASE_URL/MODEL)"})
    if run_active():
        return json.dumps({"error": "busy: a curated run or a writing job is in progress"})
    if refused := await _model_override_refusal(model):
        return json.dumps(refused)
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    if _busy(pid):
        return json.dumps({"error": "busy: a curated run, re-OCR, re-read, extraction, "
                           "scorecard or preview is in progress for this patient"})  # fmt: skip
    try:
        qset = await _chat_question_set(db, pid, question_set)
    except chs.QuestionFileError as exc:
        return json.dumps({"error": "question_file_invalid", "reason": exc.code})
    if not qset.questions:
        return json.dumps({"status": "nothing to do", "skipped": len(qset.skipped)})
    if run_active() or _busy(pid):  # re-checked after the awaits above
        return json.dumps({"error": "busy: a curated run or job started meanwhile"})
    previous = _reocr_jobs.get(pid)
    _reocr_jobs[pid] = job = {
        "state": "running", "kind": "chat_scorecard", "dry_run": True, "label": label,
        "compare_to_label": compare_to_label, "started_at": datetime.now(UTC).isoformat(),
        "repeat": repeat, "question_set": qset.name, "questions": len(qset.questions),
        "skipped": len(qset.skipped), "lane": "background",
        "overrides": {"model": model} if model else {},
    }  # fmt: skip
    try:
        row_id = await db.insert_curated_run(patient_id=pid, dry_run=True, trigger="scorecard")
    except Exception as exc:
        if previous is not None:
            _reocr_jobs[pid] = previous
        else:
            _reocr_jobs.pop(pid, None)
        logger.exception("could not start the chat scorecard for %s", pid[:8])
        return json.dumps({"error": f"could not record the scorecard ({type(exc).__name__})"})
    job["run_row_id"] = row_id
    from oncofiles.cloud_guard import local_only

    with local_only("curated chat scorecard"):
        job["task"] = asyncio.create_task(
            _chat_scorecard_job(
                db, pid, qset, row_id=row_id, label=label, compare_to_label=compare_to_label,
                repeat=repeat, model=model,
            )
        )  # fmt: skip
    return json.dumps(_public_job(pid), ensure_ascii=False)


async def curated_reocr_status(ctx: Context, patient_slug: str | None = None) -> str:
    """State and report of the last re-OCR job for this patient (#660). Read-only."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    out = await reocr_status(_get_db(ctx), pid)
    return json.dumps(out, ensure_ascii=False, default=str)


RESTORE_REASON_MAX = 200


async def _audit_restore(db, pid: str, out: dict, *, caller_hash: str | None) -> None:
    """One activity_log row per REAL restore (#806): ids and counts only,
    the caller as a keyed hash; never the reason, a value or the text."""
    from oncofiles.models import ActivityLogEntry

    counts = {k: v for k, v in out.items() if isinstance(v, int) and not isinstance(v, bool)}
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id="curated-restore",
                agent_id="mcp",
                tool_name="curated_restore_sidecar",
                input_summary=json.dumps(
                    {
                        "caller_email_hash": caller_hash,
                        "document_id": out.get("document_id"),
                        "backup_id": out.get("backup_id"),
                        "backup_kind": out.get("backup_kind"),
                        "dry_run": False,
                        "pre_store": out.get("pre_store"),
                    }
                ),
                output_summary=json.dumps(
                    {
                        "result": out.get("result"),
                        "restore_backup_id": out.get("restore_backup_id"),
                        "index": (out.get("index") or {}).get("status"),
                        **counts,
                    }
                ),
                status="ok" if out.get("result") == "restored" else "error",
                error_message=out.get("error"),
                tags=json.dumps(["curated", "restore", "real", "pre-store"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for a sidecar restore")


async def restore_sidecar_for(
    db,
    pid: str,
    document_id: int,
    backup_id: str,
    *,
    dry_run: bool = True,
    caller_hash: str | None = None,
) -> dict:
    """Restore one document's sidecar from its re-OCR backup (#806). The
    caller has resolved ``pid`` and checked admin scope and the reason.

    Holds ``patient_lock(pid)`` for the whole operation and refuses while a
    run, re-OCR/re-read/extraction job, index or another restore is active
    for the patient. A real restore writes only inside ``contract_writes()``
    through ``drive_ops`` (backup of the current sidecar first, then the
    verified update), then re-indexes the patient. Never deletes anything."""
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated.reocr import RestoreRefusedError, restore_sidecar
    from oncofiles.server import _create_patient_clients

    row = next(
        (r for r in await db.curated_reocr_candidates(patient_id=pid) if r["id"] == document_id),
        None,
    )  # SQL-scoped to the patient: a foreign id is simply not found
    sidecar_id = ((row or {}).get("structured_metadata") or {}).get("curated", {}).get("sidecar_id")
    if row is None or not sidecar_id:
        return {"error": "not_found"}
    if (
        _busy(pid)
        or patient_lock(pid).locked()
        or (_restoring.get(pid) or {}).get("state") == "running"
    ):
        return {"error": "busy: a curated run, index, re-OCR, re-read, extraction or restore "
                "is in progress for this patient"}  # fmt: skip
    # the slot is reserved before any await: two calls can never both start
    _restoring[pid] = job = {
        "kind": "restore", "state": "running", "dry_run": dry_run,
        "document_id": document_id, "backup_id": backup_id,
        "started_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    doc = {"document_id": document_id, "filename": row["filename"], "sidecar_id": sidecar_id}
    out: dict = {"document_id": document_id, "backup_id": backup_id, "dry_run": dry_run}
    try:
        if not dry_run:
            # Same safety bar as every other write job (#573): a verified copy of
            # the DB before any Drive write or re-index, or nothing at all.
            from oncofiles.pre_store import PreStoreError, make_copy

            try:
                job["pre_store"] = out["pre_store"] = await asyncio.to_thread(
                    make_copy, db, "restore"
                )
            except Exception as exc:
                reason = str(exc) if isinstance(exc, PreStoreError) else type(exc).__name__
                logger.error("pre-store copy failed for %s (%s): restore refused", pid[:8], reason)
                out["error"] = f"refused: pre-store copy failed ({reason}); nothing was written"
                return out
        async with patient_lock(pid):
            clients = await _create_patient_clients(db, pid)
            if not clients or clients[0] is None:
                out["error"] = "Drive not connected"
                return out
            gdrive, folder_id = clients[0], clients[3]

            def _blocking():
                own = gdrive.dedicated_copy()
                try:
                    with local_only("curated sidecar restore"):
                        from oncofiles.curated.drive_ops import DriveOps

                        kwargs = dict(dry_run=dry_run, now=datetime.now(UTC))
                        if dry_run:
                            return restore_sidecar(DriveOps(own), folder_id, doc, backup_id,
                                                   **kwargs)  # fmt: skip
                        with own.contract_writes():
                            return restore_sidecar(DriveOps(own), folder_id, doc, backup_id,
                                                   **kwargs)  # fmt: skip
                finally:
                    own.close()

            try:
                out.update(await asyncio.to_thread(_blocking))
            except RestoreRefusedError as exc:
                out["error"] = f"refused: {exc}"  # nothing was written
                return out
            except Exception as exc:
                logger.exception("curated sidecar restore failed for doc %s", document_id)
                out.update(result="failed", error=type(exc).__name__)
            # one INFO line per attempt: ids and the read-back result, never content
            logger.info(
                "curated restore: document_id=%s backup_id=%s dry_run=%s verify=%s result=%s",
                document_id, backup_id, dry_run,
                "skipped" if dry_run else ("ok" if out.get("result") == "restored" else "failed"),
                out.get("result"),
            )  # fmt: skip
            if out.get("result") == "restored":
                out["index"] = await _index_patient_lock_held(db, pid, force_ids={document_id})
    finally:
        job.update(state="failed" if out.get("error") else "done", result=out.get("result"),
                   finished_at=datetime.now(UTC).isoformat())  # fmt: skip
        if not dry_run and out.get("result") in ("restored", "failed"):
            await _audit_restore(db, pid, out, caller_hash=caller_hash)
    return out


# ── #919: a caregiver confirms a reviewed filed document "as is" (dashboard) ──


async def _audit_confirm_review(
    db, pid: str, document_id: int, codes: list[str], result: str, caller_hash: str | None
) -> None:
    """One activity_log row per confirm attempt that reached Drive: the
    document id, the codes set aside, the keyed caller hash and the result.
    Never a filename, the sidecar text or an e-mail."""
    from oncofiles.models import ActivityLogEntry

    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id="curated-confirm-review",
                agent_id="dashboard",
                tool_name="curated_confirm_review",
                input_summary=json.dumps(
                    {"document_id": document_id, "caller_email_hash": caller_hash}
                ),
                output_summary=json.dumps({"result": result, "codes_set_aside": codes}),
                status="ok" if result == "confirmed" else "error",
                tags=json.dumps(["curated", "confirm_review", "dashboard"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for a review confirm")


async def confirm_review_for(
    db, pid: str, document_id: int, *, caller_hash: str | None = None
) -> tuple[int, dict]:
    """Confirm a filed curated document in review "as is" (#919): ``(status, body)``.

    The caller has passed the dashboard ACL (caregiver of ``pid``). Checks the
    row (curated mirror row, ``review_needed``, a sidecar, only
    ``confirm_review.CONFIRMABLE_CODES``), refuses while a run, job, index or
    restore is active for the patient, then holds ``patient_lock`` and writes
    the #900 override line into the sidecar inside ``contract_writes()``
    through ``drive_ops`` (``confirm_review.confirm``: the sidecar's current
    text is checked again first), and re-indexes that one document. A second
    confirm is idempotent (200 ``already_resolved``, nothing written). Any
    Drive error → 502, nothing else changed.
    """
    from oncofiles import document_stage
    from oncofiles.curated import confirm_review
    from oncofiles.server import _create_patient_clients

    doc = await db.get_document(document_id, patient_id=pid)  # SQL-scoped: foreign = None
    if doc is None or doc.deleted_at is not None:
        return 404, {"error": "not_found"}
    meta = document_stage._metadata(doc.structured_metadata)
    if not doc.gdrive_id or not document_stage.is_curated_row(doc.file_id, meta) or not meta:
        return 409, {"error": "not_curated"}
    curated = document_stage._curated(meta)
    if curated.get("review_override") is True:
        return 200, {"result": "already_resolved", "already_resolved": True}
    if not document_stage._review_needed(meta):
        return 409, {"error": "not_in_review"}
    sidecar_id = curated.get("sidecar_id")
    if not sidecar_id or meta.get("text_source"):
        return 409, {"error": "no_sidecar"}
    codes = document_stage.review_reason_codes(meta)
    if confirm_review.hard_codes(codes):
        return 409, {"error": "hard_check", "codes": confirm_review.hard_codes(codes)}
    if _confirm_blocked(pid):
        return 409, {"error": "busy"}
    # the slot is reserved before any await: two calls can never both start,
    # and run_active() counts it (CI deploys wait for the write + index)
    _restoring[pid] = job = {
        "kind": "confirm_review", "state": "running", "document_id": document_id,
        "started_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    status, out = 500, {"error": "internal"}
    try:
        async with patient_lock(pid):
            try:
                clients = await _create_patient_clients(db, pid)
            except Exception as exc:
                logger.warning("curated confirm-review: Drive client failed (%s)",
                               type(exc).__name__)  # fmt: skip
                status, out = 502, {"error": "drive_failed"}
                return status, out
            if not clients or clients[0] is None:
                status, out = 409, {"error": "drive_not_connected"}
                return status, out
            gdrive = clients[0]
            today = datetime.now(UTC).date()

            def _blocking():
                from oncofiles.curated.drive_ops import DriveOps

                own = gdrive.dedicated_copy()
                try:
                    with own.contract_writes():
                        return confirm_review.confirm(DriveOps(own), sidecar_id, today)
                finally:
                    own.close()

            try:
                done = await asyncio.to_thread(_blocking)
            except confirm_review.AlreadyResolvedError:
                status, out = 200, {"result": "already_resolved", "already_resolved": True}
            except confirm_review.ConfirmRefusedError as exc:
                status, out = 409, {"error": exc.code}
            except Exception as exc:
                logger.warning(
                    "curated confirm-review: document_id=%s drive write failed (%s)",
                    document_id, type(exc).__name__,
                )  # fmt: skip
                status, out = 502, {"error": "drive_failed"}
                await _audit_confirm_review(db, pid, document_id, codes, "drive_failed",
                                            caller_hash)  # fmt: skip
            else:
                # the sidecar is written; a failed re-index only delays the
                # dashboard row until the next index (startup, run, nightly)
                try:
                    index = await _index_patient_lock_held(db, pid, force_ids={document_id})
                    index_state = index.get("status") or ("errors" if index.get("errors") else "ok")
                except Exception as exc:
                    logger.warning(
                        "curated confirm-review: document_id=%s re-index failed (%s)",
                        document_id, type(exc).__name__,
                    )  # fmt: skip
                    index_state = "failed"
                status, out = (
                    200,
                    {
                        "result": "confirmed",
                        "codes": done["codes"],
                        "index": index_state,
                    },
                )
                await _audit_confirm_review(db, pid, document_id, done["codes"], "confirmed",
                                            caller_hash)  # fmt: skip
            logger.info(
                "curated confirm-review: document_id=%s codes=%s status=%s result=%s",
                document_id, ",".join(codes), status, out.get("result") or out.get("error"),
            )  # fmt: skip
            return status, out
    finally:
        job.update(state="done" if status == 200 else "failed",
                   finished_at=datetime.now(UTC).isoformat())  # fmt: skip


async def curated_restore_sidecar(
    ctx: Context,
    document_id: int,
    backup_id: str,
    patient_slug: str | None = None,
    dry_run: bool = True,
    reason: str = "",
) -> str:
    """ADMIN: put a re-OCR backup back as a curated document's sidecar (#806),
    or promote a kept re-OCR candidate to it.

    For a document a re-OCR rewrote with less text, or one whose re-OCR ended
    in ``review`` and whose new transcription a person checked and prefers.
    ``backup_id`` must be the Drive id of a file in the patient's curated logs
    folder named ``_reocr-backup-*-<that document's filename>.md`` or
    ``_reocr-candidate-*-<that document's filename>.md`` (the ``candidate`` id
    of a ``review`` entry in a re-OCR report); anything else is refused. The
    result's ``backup_kind`` is ``reocr_backup`` or ``reocr_candidate``. Dry
    run (default) reports only counts (characters, numbers, dates of the
    current and the backup or candidate text). A real run (dry_run=false)
    first copies the current sidecar to the logs folder
    (``_restore-backup-<ts>-…``), then writes the backup's or candidate's
    text into the sidecar with read-back verification, then re-indexes the
    patient. Nothing is deleted. ``reason`` (required, at most 200 characters)
    is asked for accountability and never stored or logged. Refused while a
    run or job is active for the patient.
    """
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("curated_restore_sidecar")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    reason = (reason or "").strip()
    if not reason:
        return json.dumps({"error": "reason is required"})
    if len(reason) > RESTORE_REASON_MAX:
        return json.dumps({"error": f"reason is longer than {RESTORE_REASON_MAX} characters"})
    pid = await _resolve_patient_id(patient_slug, ctx)
    try:
        doc_id = int(document_id)
    except (TypeError, ValueError):
        return json.dumps({"error": "not_found"})
    out = await restore_sidecar_for(
        _get_db(ctx), pid, doc_id, str(backup_id or ""), dry_run=bool(dry_run),
        caller_hash=caller_email_hash(_caller_email()),
    )  # fmt: skip
    return json.dumps(out, ensure_ascii=False, default=str)


# ── promote kept candidates of Tesseract-era sidecars (#831) ────────────


async def _audit_promote(db, pid: str, out: dict, *, caller_hash: str | None) -> None:
    """One activity_log row per REAL promotion call (#831): ids, result counts
    and reason-code counts, the caller as a keyed hash; never a value."""
    from collections import Counter

    from oncofiles.models import ActivityLogEntry

    docs = out.get("documents") or []
    try:
        await db.insert_activity_log(
            ActivityLogEntry(
                patient_id=pid,
                session_id="curated-promote",
                agent_id="mcp",
                tool_name="curated_promote_candidates",
                input_summary=json.dumps(
                    {
                        "caller_email_hash": caller_hash,
                        "document_ids": [d.get("document_id") for d in docs],
                        "dry_run": False,
                        "profile": out.get("profile"),
                        "pre_store": out.get("pre_store"),
                    }
                ),
                output_summary=json.dumps(
                    {
                        "results": out.get("results"),
                        "promoted": [
                            d.get("document_id") for d in docs if d.get("result") == "promoted"
                        ],
                        "baseline_untrusted": dict(
                            sorted(
                                Counter(
                                    r for d in docs for r in d.get("baseline_untrusted") or []
                                ).items()
                            )
                        ),
                        "review_reasons": dict(
                            sorted(
                                Counter(
                                    r for d in docs for r in d.get("review_reasons") or []
                                ).items()
                            )
                        ),
                        "index": (out.get("index") or {}).get("status"),
                    }
                ),  # fmt: skip
                status="error" if out.get("error") else "ok",
                error_message=out.get("error"),
                tags=json.dumps(["curated", "promote", "real", "pre-store"]),
            )
        )
    except Exception:
        logger.exception("could not write the activity_log row for a candidate promotion")


async def promote_candidates_for(
    db,
    pid: str,
    document_ids: list[int] | None = None,
    *,
    dry_run: bool = True,
    caller_hash: str | None = None,
) -> dict:
    """Promote kept re-OCR candidates of Tesseract-era sidecars (#831). The
    caller has resolved ``pid`` and checked admin scope.

    Same safety bar as ``restore_sidecar_for``: refuses while a run, job,
    index or restore is active for the patient, holds ``patient_lock(pid)``
    for the whole call, counts in ``run_active()`` (the ``_restoring`` slot),
    a real call first makes the verified pre-store copy, writes only inside
    ``contract_writes()`` through ``drive_ops`` and re-indexes the promoted
    documents. The model is never called; originals are never read."""
    from collections import Counter

    from oncofiles import config
    from oncofiles.cloud_guard import local_only
    from oncofiles.curated import reocr
    from oncofiles.curated.facts_rules import Vocabulary
    from oncofiles.server import _create_patient_clients

    if not config.REOCR_TESSERACT_PROFILE:
        return {"error": "refused: tesseract_profile_disabled"}
    ids = list(dict.fromkeys(document_ids or []))
    if len(ids) > reocr.PROMOTE_MAX_DOCS:
        return {"error": f"refused: at most {reocr.PROMOTE_MAX_DOCS} documents per call"}
    if (
        _busy(pid)
        or patient_lock(pid).locked()
        or (_restoring.get(pid) or {}).get("state") == "running"
    ):
        return {"error": "busy: a curated run, index, re-OCR, re-read, extraction or restore "
                "is in progress for this patient"}  # fmt: skip
    # the slot is reserved before any await: two calls can never both start
    _restoring[pid] = job = {
        "kind": "promote", "state": "running", "dry_run": dry_run,
        "started_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    out: dict = {"dry_run": dry_run, "profile": reocr.TESSERACT_PROFILE}
    try:
        rows = {
            r["id"]: r for r in await db.curated_reocr_candidates(patient_id=pid)
        }  # SQL-scoped to the patient: a foreign id is simply not found
        docs, missing = [], []
        for doc_id in ids or rows:
            row = rows.get(doc_id)
            sidecar_id = (
                ((row or {}).get("structured_metadata") or {}).get("curated", {}).get("sidecar_id")
            )
            if row is None or not sidecar_id:
                if ids:
                    missing.append({"document_id": doc_id, "result": "refused",
                                    "error": "not_found"})  # fmt: skip
                continue
            docs.append({"document_id": doc_id, "filename": row["filename"],
                         "sidecar_id": sidecar_id})  # fmt: skip
        if not dry_run:
            from oncofiles.pre_store import PreStoreError, make_copy

            try:
                job["pre_store"] = out["pre_store"] = await asyncio.to_thread(
                    make_copy, db, "restore"
                )
            except Exception as exc:
                reason = str(exc) if isinstance(exc, PreStoreError) else type(exc).__name__
                logger.error("pre-store copy failed for %s (%s): promotion refused",
                             pid[:8], reason)  # fmt: skip
                out["error"] = f"refused: pre-store copy failed ({reason}); nothing was written"
                return out
        async with patient_lock(pid):
            clients = await _create_patient_clients(db, pid)
            if not clients or clients[0] is None:
                out["error"] = "Drive not connected"
                return out
            gdrive, folder_id = clients[0], clients[3]
            vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
            docs = await _with_trusted_codes(db, pid, docs)  # under the lock: no index now

            def _blocking():
                own = gdrive.dedicated_copy()
                try:
                    with local_only("curated candidate promotion"):
                        from oncofiles.curated.drive_ops import DriveOps

                        kwargs = dict(dry_run=dry_run, now=datetime.now(UTC),
                                      explicit=bool(ids), stop=_stopping.is_set)  # fmt: skip
                        if dry_run:
                            return reocr.promote_candidates(DriveOps(own), folder_id, docs,
                                                            vocab, **kwargs)  # fmt: skip
                        with own.contract_writes():
                            return reocr.promote_candidates(DriveOps(own), folder_id, docs,
                                                            vocab, **kwargs)  # fmt: skip
                finally:
                    own.close()

            try:
                out.update(await asyncio.to_thread(_blocking))
            except Exception as exc:
                logger.exception("curated candidate promotion failed for %s", pid[:8])
                out["error"] = type(exc).__name__
            out["documents"] = [*missing, *(out.get("documents") or [])]
            out["results"] = dict(Counter(str(d.get("result")) for d in out["documents"]))
            promoted = {d["document_id"] for d in out["documents"] if d.get("result") == "promoted"}
            logger.info(
                "curated promote: patient=%s dry_run=%s documents=%d results=%s",
                pid[:8], dry_run, len(out["documents"]), out["results"],
            )  # fmt: skip
            if promoted:
                out["index"] = await _index_patient_lock_held(db, pid, force_ids=promoted)
    finally:
        job.update(state="failed" if out.get("error") else "done", results=out.get("results"),
                   finished_at=datetime.now(UTC).isoformat())  # fmt: skip
        if not dry_run and out.get("documents"):
            await _audit_promote(db, pid, out, caller_hash=caller_hash)
    return out


async def curated_promote_candidates(
    ctx: Context,
    patient_slug: str | None = None,
    document_ids: list[int] | None = None,
    dry_run: bool = True,
) -> str:
    """ADMIN: promote kept re-OCR candidates of documents whose current
    sidecar was written by Tesseract (#831).

    For each such document that has a ``_reocr-candidate-…`` file in the
    curated logs folder, the NEWEST candidate is checked again against the
    current sidecar under the ``tesseract_baseline`` rules, without reading
    the scan or calling the model: numbers, lab values and words only the old
    Tesseract text had are listed as ``baseline_untrusted`` and do not block;
    a lost lab value that has a machine-verified, verified or corrected stored
    fact, a lost date, a shrunk text, a near-empty page that has ink on the scan or
    too many uncertain numbers keep the candidate a candidate (``result:
    review`` with ``review_reasons``). A candidate written before the page ink
    was recorded in its header cannot show that a near-empty page is a blank
    back side: it stays a candidate with ``near_empty_unknown_ink``; read such
    a document again with curated_reocr and explicit ``document_ids``.
    Dry run (default) writes nothing and reports the
    decision per document. A real run (dry_run=false) promotes each passing
    candidate exactly like curated_restore_sidecar: the current sidecar is
    copied to the logs folder first, the new text is written with read-back
    verification, then the patient is re-indexed. Candidate files stay in the
    logs folder; nothing is deleted and no original is touched.

    Args:
        document_ids: At most 20 documents; each gets an entry (``refused``
            with a reason code when it has no candidate or its sidecar is not
            Tesseract-era). Omitted: up to 20 documents with a candidate;
            ``remaining`` says how many more there are.

    The report carries ids, counts and reason codes only. Refused while a run
    or job is active for the patient.
    """
    from oncofiles.tools._helpers import _require_admin_or_raise

    try:
        _require_admin_or_raise("curated_promote_candidates")
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    pid = await _resolve_patient_id(patient_slug, ctx)
    try:
        ids = [int(i) for i in document_ids] if document_ids else None
    except (TypeError, ValueError):
        return json.dumps({"error": "document_ids must be integers"})
    out = await promote_candidates_for(
        _get_db(ctx), pid, ids, dry_run=bool(dry_run),
        caller_hash=caller_email_hash(_caller_email()),
    )  # fmt: skip
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
    """Status and report of the latest curated-folder run for a patient (#562).

    The numbers inside ``run`` (files waiting, filed, held for review) are a
    snapshot taken when that run ended: ``as_of`` is that time (UTC), so say
    "as of ..." when quoting them. ``live``, when present, is newer: the number
    of inbox files the next automatic run would process (``pending_inbox_count``)
    as seen by the server's last folder probe at ``live.as_of`` (UTC). It is
    absent until the server has probed the folder since its last restart.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    return json.dumps(await run_status(_get_db(ctx), pid), ensure_ascii=False, default=str)


def register(mcp):
    mcp.tool()(curated_folder_run)
    mcp.tool()(curated_folder_status)
    mcp.tool()(curated_index)
    mcp.tool()(curated_reocr)
    mcp.tool()(curated_reocr_status)
    mcp.tool()(curated_reread_verify)
    # writes nothing (#883): read-only for the client; admin-only in the tool
    mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})(curated_reocr_compare)
    mcp.tool()(curated_llm_extract_preview)
    mcp.tool()(curated_clinical_extract_preview)
    mcp.tool()(curated_clinical_extract)
    # user-invoked rollback (soft delete only, #734); never scheduled
    mcp.tool(annotations=_RETRACT_ANNOTATIONS)(curated_clinical_retract)
    # LLM lab facts (#714): writes OFF unless LLM_LAB_WRITE_ENABLED=true
    mcp.tool()(curated_lab_extract)
    mcp.tool(annotations=_RETRACT_ANNOTATIONS)(curated_lab_retract)
    mcp.tool()(curated_extraction_scorecard)
    # dry run (#926): re-classifies stored sidecar texts; only its summary row
    mcp.tool()(curated_classify_scorecard)
    mcp.tool()(curated_chat_scorecard)
    # admin, user-invoked (#806): replaces a sidecar's text, its current text
    # is backed up first; never scheduled
    mcp.tool(annotations=_RETRACT_ANNOTATIONS)(curated_restore_sidecar)
    # admin, user-invoked (#831): promotes kept candidates of Tesseract-era
    # sidecars through the restore path; dry run by default, never scheduled
    mcp.tool(annotations=_RETRACT_ANNOTATIONS)(curated_promote_candidates)
