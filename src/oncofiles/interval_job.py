"""A fixed-interval background job that runs without APScheduler (#999).

Production runs ``SYNC_ENABLED=false`` (#784), so APScheduler never starts
there and a job registered in it never runs. #998 moved the erase copy
retention into its own lifespan loop; #999 does the same for the jobs that
run on a plain interval (first ``holding_move`` + ``holding_extract``). This
module is the shared shape so every such job behaves the same:

* started from the lifespan through
  ``scheduler_singleton.ensure_background_loop`` (one per process, reference
  counted, an unexpected end logged by ``_report_loop_end``), whatever
  ``SYNC_ENABLED`` says, wrapped in ``cloud_guard.local_only_task`` named
  after the job; it is NOT an APScheduler job, so it can never run twice;
* the first run after ``tools.curated.STARTUP_GRACE_S`` (a rolling deploy runs
  two containers side by side for a few seconds), then every ``interval_s``;
* an iteration that raises is logged with the exception CLASS only (a message
  can carry ids or file names) and recorded; the loop goes on;
* telemetry (:meth:`IntervalJob.status`) is in memory, counts and codes only,
  for operator ``/readiness`` and the admin job list, never ``/health``.

The job function returns a mapping of counts (``collections.Counter``); only
keys that look like codes and integer values are kept (``last_counts``), and
an optional ``summarize`` turns them into the job's named totals
(``last_summary``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_CODE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,59}$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value is not None else None


def safe_counts(counts: Mapping | None) -> dict[str, int]:
    """Code-like keys with integer values only (never a name, id or text)."""
    out: dict[str, int] = {}
    for key, value in dict(counts or {}).items():
        if isinstance(key, str) and _CODE_RE.match(key) and isinstance(value, int):
            out[key] = int(value)
    return dict(sorted(out.items()))


class IntervalJob:
    """State + loop of one interval job (one instance per job id)."""

    def __init__(
        self,
        job_id: str,
        interval_s: float,
        *,
        summarize: Callable[[dict[str, int]], dict[str, int]] | None = None,
    ) -> None:
        self.job_id = job_id
        self.interval_s = float(interval_s)
        self.summarize = summarize
        self._state: dict[str, Any] = {}
        self.reset()

    # ── telemetry ───────────────────────────────────────────────────────

    def reset(self) -> None:
        """Fresh state (process start; tests)."""
        self._state.clear()
        self._state.update(
            {
                "running": False,
                "loop_started_at": None,
                "next_run_at": None,
                "last_run_at": None,
                "last_ok": None,
                "last_error": None,  # ISO time of the last failed iteration
                "last_error_type": None,  # its exception class, never the message
                "last_duration_s": None,
                "last_counts": None,
                "last_summary": None,
                "runs": 0,
                "errors": 0,
            }
        )

    @property
    def schedule_text(self) -> str:
        minutes = self.interval_s / 60
        if minutes >= 1 and minutes == int(minutes):
            return f"every {int(minutes)} min"
        return f"every {int(self.interval_s)} s"

    def trigger(self) -> dict:
        """The admin job list's trigger shape (``scheduled_jobs.trigger_info``)."""
        return {"kind": "interval", "seconds": int(self.interval_s), "text": self.schedule_text}

    def status(self) -> dict:
        out = dict(self._state)
        out["schedule"] = self.schedule_text
        return out

    # ── running ─────────────────────────────────────────────────────────

    async def run_once(self, fn: Callable[[], Awaitable[Mapping | None]]) -> dict | None:
        """One iteration. Returns the kept counts, None on failure; never
        raises an ``Exception`` (a cancel still propagates)."""
        st = self._state
        st["last_run_at"] = _iso(_utcnow())
        st["runs"] += 1
        st["running"] = True
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        try:
            counts = safe_counts(await fn())
        except Exception as exc:  # noqa: BLE001 - one failed iteration never stops the loop
            st["last_error"] = _iso(_utcnow())
            st["last_error_type"] = type(exc).__name__
            st["errors"] += 1
            logger.error("%s: iteration failed (%s)", self.job_id, type(exc).__name__)
            return None
        finally:
            st["running"] = False
            st["last_duration_s"] = round(loop.time() - t0, 2)
        st["last_ok"] = _iso(_utcnow())
        st["last_counts"] = counts
        st["last_summary"] = self.summarize(counts) if self.summarize else None
        return counts

    async def loop(
        self,
        fn: Callable[[], Awaitable[Mapping | None]],
        *,
        grace_s: float | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        now_fn: Callable[[], datetime] = _utcnow,
    ) -> None:
        """Wait the startup grace, then run ``fn`` every ``interval_s`` forever."""
        if grace_s is None:
            from oncofiles.tools.curated import STARTUP_GRACE_S

            grace_s = STARTUP_GRACE_S
        now = now_fn()
        self._state["loop_started_at"] = _iso(now)
        self._state["next_run_at"] = _iso(now + timedelta(seconds=grace_s))
        logger.info(
            "%s loop started (%s, first run after %ds)",
            self.job_id,
            self.schedule_text,
            int(grace_s),
        )
        await sleep(grace_s)
        while True:
            await self.run_once(fn)
            self._state["next_run_at"] = _iso(now_fn() + timedelta(seconds=self.interval_s))
            await sleep(self.interval_s)
