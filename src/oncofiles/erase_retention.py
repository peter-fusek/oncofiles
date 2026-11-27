"""Daily removal of expired pre-store erase copies, independent of APScheduler (#856).

An erase copy (``oncofiles-<ts>Z-erase.db``, made before an erasure on request)
holds the erased patient's rows, so it must not outlive the 30 days /privacy
promises (maintainer decision 2026-11-26, RUNBOOK lifecycle). The removal
itself is ``pre_store.prune_expired_erase_copies`` (inside the existing
``_prune`` allowlist entry of the #734 lock).

Production runs with ``SYNC_ENABLED=false`` (a maintainer decision, #784), so
APScheduler is not started there and a job registered in it never ran. This
module is therefore the ONLY owner of the job: a small asyncio loop started
from the lifespan through ``scheduler_singleton.ensure_background_loop`` (one
per process, reference counted), whatever ``SYNC_ENABLED`` says. It is not an
APScheduler job, so it can never be registered twice.

Schedule: daily at ``SLOT_HOUR_UTC``:``SLOT_MINUTE_UTC`` UTC (04:35), defined
once here and read by the admin job list (``scheduled_jobs``). The loop first
waits ``tools.curated.STARTUP_GRACE_S`` (a rolling deploy runs two containers
side by side for a few seconds). Catch-up: a process that comes up after
today's slot runs once right after the grace, then daily. At most one run per
process and UTC day; the marker is in memory, which is enough because pruning
is idempotent.

Each run holds ``_sync_semaphore`` (passed in by the lifespan), does the file
I/O in ``asyncio.to_thread``, logs the removed count only, and never raises:
a failure is logged and recorded, and the loop waits for the next day.
Telemetry (``status()``) is in memory, for operator ``/readiness`` and the
admin job list only, never ``/health``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime
from typing import Any

from oncofiles.interval_job import DAILY_MAX_SLEEP_S, daily_next_run, daily_slot

logger = logging.getLogger(__name__)

JOB_ID = "pre_store_erase_retention"
# The one definition of the daily slot (04:35 UTC = 05:35 / 06:35 Bratislava,
# outside the 00:30-02:30 local night window of the curated runs).
SLOT_HOUR_UTC = 4
SLOT_MINUTE_UTC = 35
SCHEDULE_TEXT = f"daily {SLOT_HOUR_UTC:02d}:{SLOT_MINUTE_UTC:02d} UTC"
# Never sleep longer than this in one go, so a clock jump or a suspended VM
# cannot push the run far past its slot.
MAX_SLEEP_S = DAILY_MAX_SLEEP_S

_state: dict[str, Any] = {}


def _fresh_state() -> dict[str, Any]:
    return {
        "running": False,
        "loop_started_at": None,
        "next_run_at": None,
        "last_run_at": None,
        "last_ok": None,
        "last_error": None,
        "last_error_type": None,
        "last_deleted": None,
        "last_duration_s": None,
        "runs": 0,
        "last_run_day": None,  # date of the last attempt (UTC), the once-per-day marker
    }


_state.update(_fresh_state())


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def slot_on(day: date) -> datetime:
    """The run slot of one UTC day."""
    return daily_slot(day, SLOT_HOUR_UTC, SLOT_MINUTE_UTC)


def next_run_at(now: datetime, last_run_day: date | None) -> datetime:
    """When the next run is due (``now`` itself = run now); the shared daily
    rule ``interval_job.daily_next_run`` (catch-up after a restart past the
    slot, at most once per process and UTC day)."""
    return daily_next_run(now, last_run_day, SLOT_HOUR_UTC, SLOT_MINUTE_UTC)


async def run_once(semaphore: asyncio.Semaphore, *, now: datetime | None = None) -> int | None:
    """One prune under ``semaphore``. Returns the removed count, None on failure.

    Never raises an ``Exception``: the failure is logged (class name in the
    state, the traceback in the server log) and the loop goes on.
    """
    from oncofiles.pre_store import prune_expired_erase_copies

    started = now or _utcnow()
    _state["last_run_day"] = started.astimezone(UTC).date()
    _state["last_run_at"] = _iso(started)
    _state["runs"] += 1
    _state["running"] = True
    t0 = asyncio.get_running_loop().time()
    try:
        async with semaphore:
            removed = await asyncio.to_thread(prune_expired_erase_copies)
    except Exception as exc:  # noqa: BLE001 - one failed day never stops the loop
        _state["last_error"] = _iso(_utcnow())
        _state["last_error_type"] = type(exc).__name__
        logger.exception("Pre-store erase retention failed")
        return None
    finally:
        _state["running"] = False
        _state["last_duration_s"] = round(asyncio.get_running_loop().time() - t0, 2)
    _state["last_ok"] = _iso(_utcnow())
    _state["last_deleted"] = removed
    logger.info("Pre-store erase retention: removed %d copies", removed)
    return removed


async def erase_retention_loop(
    semaphore: asyncio.Semaphore,
    *,
    grace_s: float | None = None,
    now_fn: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> None:
    """Wait the startup grace, then prune daily at the slot (catch-up included)."""
    if grace_s is None:
        from oncofiles.tools.curated import STARTUP_GRACE_S

        grace_s = STARTUP_GRACE_S
    _state["loop_started_at"] = _iso(now_fn())
    logger.info(
        "Pre-store erase retention loop started (%s, first check after %ds)",
        SCHEDULE_TEXT,
        int(grace_s),
    )
    await sleep(grace_s)
    while True:
        now = now_fn()
        due = next_run_at(now, _state["last_run_day"])
        _state["next_run_at"] = _iso(due)
        wait = (due - now).total_seconds()
        if wait > 0:
            await sleep(min(wait, MAX_SLEEP_S))
            continue
        await run_once(semaphore, now=now)


def status() -> dict:
    """In-memory telemetry for operator /readiness and the admin job list."""
    out = {k: v for k, v in _state.items() if k != "last_run_day"}
    out["schedule"] = SCHEDULE_TEXT
    return out


def _reset_for_tests() -> None:
    _state.clear()
    _state.update(_fresh_state())
