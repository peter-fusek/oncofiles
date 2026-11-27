"""The MCP OAuth token cleanup as a lifespan loop, independent of APScheduler (#999).

Once per UTC day at ``SLOT_HOUR_UTC``:``SLOT_MINUTE_UTC`` (04:00 UTC, the one
definition of the slot, read by the admin job list through the job object)
one ``persistent_oauth.delete_expired_oauth_tokens`` pass under
``_sync_semaphore`` (the single DB connection), in the ``local_only_task``
zone ``oauth_token_cleanup``: ``mcp_oauth_tokens`` rows whose INTEGER
``expires_at`` lies more than 30 days in the past are deleted (credential
cleanup, kept on purpose by #734). Rows without an expiry stay.

It was an APScheduler job (cron 04:00), which never starts in production
(``SYNC_ENABLED=false``, #784), so expired token rows were never removed
there. This loop is now the job's ONLY owner (the ``add_job`` registration is
gone, so nothing runs twice when ``SYNC_ENABLED=true``). Startup grace,
catch-up after a restart past the slot, at most one run per process and UTC
day (the delete is idempotent), failure handling and telemetry:
``oncofiles.interval_job.DailyJob``. ``last_deleted`` on operator
``/readiness.jobs.oauth_token_cleanup`` is the last run's count.
"""

from __future__ import annotations

import asyncio
import logging

from oncofiles.interval_job import DailyJob

logger = logging.getLogger(__name__)

JOB_ID = "oauth_token_cleanup"
# 04:00 UTC = 05:00 / 06:00 Bratislava (the former cron slot), outside the
# 00:30-02:30 local night window, before the access log (04:25) and the erase
# copy (04:35) retention loops.
SLOT_HOUR_UTC = 4
SLOT_MINUTE_UTC = 0


def summarize(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one pass (counts only)."""
    return {"deleted": c.get("deleted", 0)}


OAUTH_CLEANUP = DailyJob(
    JOB_ID, SLOT_HOUR_UTC, SLOT_MINUTE_UTC, summarize=summarize, deleted_key="deleted"
)
JOBS = {JOB_ID: OAUTH_CLEANUP}


async def oauth_cleanup_iteration(db, semaphore: asyncio.Semaphore) -> dict[str, int]:
    """One delete under ``semaphore``, as the former job; logs the count only."""
    from oncofiles.persistent_oauth import delete_expired_oauth_tokens

    async with semaphore:
        deleted = await delete_expired_oauth_tokens(db)
    logger.info("OAuth cleanup: removed %d expired tokens", deleted)
    return {"deleted": deleted}


async def oauth_token_cleanup_loop(
    db, semaphore: asyncio.Semaphore, *, grace_s: float | None = None
) -> None:
    await OAUTH_CLEANUP.loop(lambda: oauth_cleanup_iteration(db, semaphore), grace_s=grace_s)


def _reset_for_tests() -> None:
    OAUTH_CLEANUP.reset()
