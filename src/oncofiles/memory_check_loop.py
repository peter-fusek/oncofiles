"""The periodic memory check as a lifespan loop, independent of APScheduler (#999).

Every ``INTERVAL_S`` (five minutes) one ``memory.periodic_memory_check``: the
peak RSS is updated, the RSS is logged, and above
``MEMORY_RESTART_THRESHOLD_MB`` (default 420) memory is reclaimed first and,
when the RSS stays above it, the process asks for a graceful restart with
``os.kill(os.getpid(), SIGTERM)`` (never ``sys.exit``, #213/#244). No DB call,
so no ``_sync_semaphore``; zone ``local_only_task("memory_check", …)``.

It was an APScheduler interval job, which never starts in production
(``SYNC_ENABLED=false``, #784): there the only memory guard left was the 600 MB
hard ceiling in ``/health``. This loop is now the job's ONLY owner (the
``add_job`` registration is gone, so it never runs twice when
``SYNC_ENABLED=true``).

First run: ``FIRST_RUN_DELAY_S`` (seven minutes) after the start, as the
APScheduler job had (``start_date`` now + 7 min, #366: the startup import
spike must not trip the threshold). That is longer than the startup grace, so
the check never runs inside the rolling-deploy overlap either; during those
first minutes the scheduler-independent ``/health`` hard ceiling still
protects the process.

A failing check is logged by class (``interval_job``) and, like the old job
error listener, followed by a direct RSS read: above the threshold the
process is restarted anyway, because a broken safety net is worse than none.
Telemetry (in memory, counts only): operator ``/readiness.jobs.memory_check``
with ``last_summary`` = ``rss_mb``, ``peak_mb``, ``threshold_mb``.
"""

from __future__ import annotations

import logging
import os
import signal

from oncofiles.interval_job import IntervalJob

logger = logging.getLogger(__name__)

JOB_ID = "memory_check"
INTERVAL_S = 5 * 60
# The former APScheduler start_date offset (#366): the first check waits out the
# startup import spike. Longer than tools.curated.STARTUP_GRACE_S.
FIRST_RUN_DELAY_S = 7 * 60


def summarize(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one check (counts only)."""
    return {k: c.get(k, 0) for k in ("rss_mb", "peak_mb", "threshold_mb")}


MEMORY_CHECK = IntervalJob(JOB_ID, INTERVAL_S, summarize=summarize)
JOBS = {JOB_ID: MEMORY_CHECK}


def _restart_if_over_threshold_after_failure() -> None:
    """The old ``_job_error`` fallback: a failed check with a high RSS restarts."""
    from oncofiles.memory import MEMORY_RESTART_THRESHOLD_MB, get_rss_mb

    rss = get_rss_mb()
    if rss > MEMORY_RESTART_THRESHOLD_MB:
        logger.critical(
            "memory_check failed AND RSS %.1f MB > %d MB — forcing restart",
            rss,
            MEMORY_RESTART_THRESHOLD_MB,
        )
        os.kill(os.getpid(), signal.SIGTERM)


async def memory_check_iteration() -> dict[str, int]:
    """One ``periodic_memory_check`` (which may itself SIGTERM); counts only."""
    from oncofiles import memory

    try:
        await memory.periodic_memory_check()
    except Exception:
        _restart_if_over_threshold_after_failure()
        raise
    trend = memory.get_rss_trend()
    return {
        "rss_mb": int(round(trend["current_mb"])),
        "peak_mb": int(round(trend["peak_mb"])),
        "threshold_mb": int(memory.MEMORY_RESTART_THRESHOLD_MB),
    }


async def memory_check_loop(*, grace_s: float | None = None) -> None:
    await MEMORY_CHECK.loop(
        memory_check_iteration, grace_s=FIRST_RUN_DELAY_S if grace_s is None else grace_s
    )


def _reset_for_tests() -> None:
    MEMORY_CHECK.reset()
