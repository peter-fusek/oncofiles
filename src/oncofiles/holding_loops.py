"""The two #950 holding jobs as lifespan loops, independent of APScheduler (#999).

* ``holding_move`` (phase three): every ``holding_move.WORKER_INTERVAL_MIN``
  minutes one ``holding_move.run_pending`` pass under ``_sync_semaphore``
  (copies held files of trials that connected Drive, flags superseded rows,
  sends due "ready" mails).
* ``holding_extract`` (phase two): every ``holding_extract.WORKER_INTERVAL_MIN``
  minutes one ``holding_extract.run_pending`` pass (local reading of pending
  held documents). Like the former scheduler job it does NOT hold
  ``_sync_semaphore``: the reader takes minutes per scan on the local model and
  serializes itself (``holding_extract._run_lock``, one document at a time).

Both were APScheduler jobs, which never start in production
(``SYNC_ENABLED=false``, #784). The loops here are now their ONLY periodic
owner (the ``add_job`` registrations are gone, so nothing runs twice when
``SYNC_ENABLED=true``). The event-driven kicks stay as they were
(``holding_move.kick`` after a Drive connect, ``holding_extract.kick`` after a
stored file). Shape, grace, failure handling and telemetry:
``oncofiles.interval_job``.

A missing holding directory (no trial ever held a file) is normal: neither
job creates it (only ``holding_store.write_encrypted`` does, when it stores a
file); a pass then finds nothing and reports zero counts.
"""

from __future__ import annotations

import asyncio

from oncofiles import holding_extract, holding_move
from oncofiles.interval_job import IntervalJob

MOVE_JOB_ID = holding_move.JOB  # "holding_move"
EXTRACT_JOB_ID = holding_extract.JOB  # "holding_extract"

_MOVE_ERRORS = ("copy_failed", "held_unreadable", "sha_mismatch", "listing_failed", "lease_lost")
_EXTRACT_WAITS = (
    "curated_run_active",
    "lm_job_active",
    "curated_pending",
    "global_page_cap",
    "daily_page_budget",
)


def summarize_move(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one ``holding_move.run_pending`` pass (counts only)."""
    return {
        "moved": c.get("copied", 0) + c.get("present", 0),
        "copied": c.get("copied", 0),
        "present": c.get("present", 0),
        "errors": sum(c.get(k, 0) for k in _MOVE_ERRORS),
        "deferred": c.get("deferred", 0) + c.get("deferred_patients", 0),
        "converted": c.get("converted", 0),
        "superseded": c.get("superseded", 0),
        "ready_mails_sent": c.get("ready_mails", 0),
    }


def summarize_extract(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one ``holding_extract.run_pending`` pass (counts only)."""
    return {
        "docs_read": c.get("done", 0),
        "failed": c.get("failed", 0),
        "excluded": c.get("excluded", 0),
        "retry": c.get("retry", 0),
        "waits": sum(c.get(k, 0) for k in _EXTRACT_WAITS),
        "lease_lost": c.get("lease_lost", 0) + c.get("lost_claim", 0),
    }


MOVE = IntervalJob(MOVE_JOB_ID, holding_move.WORKER_INTERVAL_MIN * 60, summarize=summarize_move)
EXTRACT = IntervalJob(
    EXTRACT_JOB_ID, holding_extract.WORKER_INTERVAL_MIN * 60, summarize=summarize_extract
)
JOBS = {MOVE_JOB_ID: MOVE, EXTRACT_JOB_ID: EXTRACT}


async def move_iteration(db, semaphore: asyncio.Semaphore):
    """One mover pass under ``semaphore`` (single DB connection)."""
    async with semaphore:
        return await holding_move.run_pending(db)


async def extract_iteration(db):
    """One reader pass (serialized by the reader's own lock)."""
    return await holding_extract.run_pending(db)


async def holding_move_loop(db, semaphore: asyncio.Semaphore, *, grace_s: float | None = None):
    await MOVE.loop(lambda: move_iteration(db, semaphore), grace_s=grace_s)


async def holding_extract_loop(db, *, grace_s: float | None = None):
    await EXTRACT.loop(lambda: extract_iteration(db), grace_s=grace_s)


def _reset_for_tests() -> None:
    MOVE.reset()
    EXTRACT.reset()
