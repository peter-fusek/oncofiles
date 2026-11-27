"""The #868 access log retention as a lifespan loop, independent of APScheduler (#999).

Once per UTC day at ``SLOT_HOUR_UTC``:``SLOT_MINUTE_UTC`` (04:25 UTC, the one
definition of the slot, read by the admin job list through the job object)
one ``dashboard_audit.purge_expired_access_log`` pass under
``_sync_semaphore`` (the single DB connection), in the ``local_only_task``
zone ``dashboard_access_log_retention``: ``dashboard_access_log`` rows older
than ``DASHBOARD_ACCESS_LOG_RETENTION_DAYS`` (default 90, minimum 30) are
deleted in chunks of at most 500 rows (maintainer decision 2026-11-16).
Only that table; it holds access telemetry, no patient documents or facts.

It was an APScheduler job (cron 04:25 UTC), which never starts in production
(``SYNC_ENABLED=false``, #784), so the retention promised in #868 never ran
there. This loop is now the job's ONLY owner (the ``add_job`` registration is
gone, so nothing runs twice when ``SYNC_ENABLED=true``). Startup grace,
catch-up after a restart past the slot, at most one run per process and UTC
day (the purge is idempotent), failure handling and telemetry:
``oncofiles.interval_job.DailyJob``. ``last_deleted`` on operator
``/readiness.jobs.dashboard_access_log_retention`` is the last run's count.
"""

from __future__ import annotations

import asyncio
import logging

from oncofiles.interval_job import DailyJob

logger = logging.getLogger(__name__)

JOB_ID = "dashboard_access_log_retention"
# 04:25 UTC = 05:25 / 06:25 Bratislava, outside the 00:30-02:30 local night
# window (curated nightly runs), ten minutes before the erase copy retention.
SLOT_HOUR_UTC = 4
SLOT_MINUTE_UTC = 25


def summarize(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one pass (counts only)."""
    return {"deleted": c.get("deleted", 0), "retention_days": c.get("retention_days", 0)}


ACCESS_LOG = DailyJob(
    JOB_ID, SLOT_HOUR_UTC, SLOT_MINUTE_UTC, summarize=summarize, deleted_key="deleted"
)
JOBS = {JOB_ID: ACCESS_LOG}


async def access_log_iteration(db, semaphore: asyncio.Semaphore) -> dict[str, int]:
    """One purge under ``semaphore``, as the former job; logs the count only."""
    from oncofiles.access_log_sql import effective_days
    from oncofiles.dashboard_audit import purge_expired_access_log, retention_days

    days = effective_days(retention_days())
    async with semaphore:
        deleted = await purge_expired_access_log(db, days=days)
    logger.info("Dashboard access log retention: removed %d rows older than %d days", deleted, days)
    return {"deleted": deleted, "retention_days": days}


async def access_log_retention_loop(
    db, semaphore: asyncio.Semaphore, *, grace_s: float | None = None
) -> None:
    await ACCESS_LOG.loop(lambda: access_log_iteration(db, semaphore), grace_s=grace_s)


def _reset_for_tests() -> None:
    ACCESS_LOG.reset()
