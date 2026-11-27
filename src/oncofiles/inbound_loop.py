"""The #952 inbound queue sweep as a lifespan loop, independent of APScheduler (#999).

Every ``inbound_queue.WORKER_INTERVAL_MIN`` minutes one
``inbound_queue.run_cycle`` pass under ``_sync_semaphore`` (the single DB
connection), in the ``local_only_task`` zone ``inbound_queue``:

1. the claimed bucket: deferred rows promoted / expired, then each claimable
   row DKIM-verified (EML download + DNS outside ``PROCESS_LOCK``) and
   promoted into ``inbound_jobs`` with its real verdict;
2. the durable queue (``inbound_jobs``): each claimable job processed under
   ``PROCESS_LOCK`` (files filed, receipt, quarantine of other senders);
3. ``inbound_quarantine.run_maintenance`` under ``PROCESS_LOCK``: expiry of
   old pending items, the purge (encrypted files after the 30-day trash;
   approved copies only with ``QUARANTINE_PURGE_APPROVED``) and the digests.

It was an APScheduler job, which never starts in production
(``SYNC_ENABLED=false``, #784), so none of the above ran there except through
``inbound_queue.kick`` after an enqueue. This loop is now the job's ONLY
periodic owner (the ``add_job`` registration is gone, so nothing runs twice
when ``SYNC_ENABLED=true``); the kick stays. Without ``INBOUND_JOBS_SECRET``
a pass touches nothing (as before). Shape, startup grace, failure handling
and telemetry: ``oncofiles.interval_job``.

A missing quarantine directory is normal: nothing here creates it (only
``inbound_quarantine._write_files`` does, when it stores a file).
"""

from __future__ import annotations

import asyncio

from oncofiles import inbound_queue
from oncofiles.interval_job import IntervalJob

JOB_ID = "inbound_queue"


def _sum(c: dict[str, int], prefix: str, *, skip: tuple[str, ...] = ()) -> int:
    return sum(v for k, v in c.items() if k.startswith(prefix) and k not in skip)


def summarize(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one ``inbound_queue.run_cycle`` pass (counts only)."""
    return {
        "jobs_processed": _sum(c, "job_"),
        "jobs_done": _sum(c, "job_done_"),
        "jobs_failed": _sum(c, "job_failed_"),
        "jobs_retried": _sum(c, "job_pending_"),
        "claimed_verified": c.get("claimed_done_verified", 0),
        "claimed_refused": _sum(c, "claimed_done_", skip=("claimed_done_verified",)),
        "claimed_failed": _sum(c, "claimed_failed_"),
        "claimed_retried": _sum(c, "claimed_pending_"),
        "quarantine_expired": c.get("quarantine_expired", 0),
        "notices_sent": c.get("quarantine_notices_sent", 0),
        "purged_items": c.get("quarantine_purged_items", 0)
        + c.get("quarantine_approved_purged_items", 0),
        "purged_bytes": c.get("quarantine_purged_bytes", 0),
    }


INBOUND = IntervalJob(JOB_ID, inbound_queue.WORKER_INTERVAL_MIN * 60, summarize=summarize)
JOBS = {JOB_ID: INBOUND}


async def inbound_iteration(db, semaphore: asyncio.Semaphore):
    """One sweep under ``semaphore`` (single DB connection), as the former job."""
    async with semaphore:
        return await inbound_queue.run_cycle(db)


async def inbound_queue_loop(db, semaphore: asyncio.Semaphore, *, grace_s: float | None = None):
    await INBOUND.loop(lambda: inbound_iteration(db, semaphore), grace_s=grace_s)


def _reset_for_tests() -> None:
    INBOUND.reset()
