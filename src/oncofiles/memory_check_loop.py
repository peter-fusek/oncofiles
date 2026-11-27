"""The periodic memory check as a lifespan loop, independent of APScheduler (#999).

Every ``INTERVAL_S`` (five minutes) one check: the peak RSS is updated and the
RSS logged; above the restart threshold memory is reclaimed first, and when
the RSS stays above it the check reports ``would_restart``. No DB call, so no
``_sync_semaphore`` is taken (it is only READ as a busy signal); zone
``local_only_task("memory_check", …)``.

It was an APScheduler interval job, which never starts in production
(``SYNC_ENABLED=false``, #784), so it never ran there. Infra review of #1011:

* **Report-only by default.** The runtime env ``MEMORY_CHECK_RESTART``
  (default false) gates the restart. While it is off the loop only measures and
  reports on operator ``/readiness.jobs.memory_check`` (``memory``: RSS, peak,
  threshold and its source, ``would_restart``, the deferral code) and never
  sends a signal.
* **Never restart during active work.** With the switch on, a restart that is
  due is deferred while anything is busy (:func:`busy_reason`: a curated run,
  any curated job (re-OCR, re-read, restore, clinical, compare…), a held
  patient lock (curated Drive writes), the inbound processing lock (filing to
  Drive) or ``_sync_semaphore``); one line ``memory_check: restart_deferred
  reason=<code>`` per tick, codes only, and the next tick tries again. A signal
  that cannot be read counts as busy (``state_unknown``): fail safe, no restart.
  Then ``os.kill(os.getpid(), SIGTERM)`` (graceful, never ``sys.exit``).
* **Threshold relative to the container** (:func:`resolve_threshold`, read
  once at loop start in a thread): an explicit ``MEMORY_RESTART_THRESHOLD_MB``
  wins (``env``); else ``CGROUP_SHARE`` of the cgroup memory limit (v2
  ``memory.max``, v1 ``memory.limit_in_bytes``; ``cgroup``); else the old
  ``DEFAULT_THRESHOLD_MB`` (``default``).

The ``/health`` hard ceiling (``HARD_RSS_CEILING_MB``) is unchanged and stays
the scheduler-independent last resort. First run ``FIRST_RUN_DELAY_S`` (seven
minutes) after the start, as the APScheduler job had (#366: the startup import
spike must not count), which is past the startup grace.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path
from typing import Any

from oncofiles.interval_job import IntervalJob

logger = logging.getLogger(__name__)

JOB_ID = "memory_check"
INTERVAL_S = 5 * 60
# The former APScheduler start_date offset (#366). Longer than STARTUP_GRACE_S.
FIRST_RUN_DELAY_S = 7 * 60

RESTART_ENV = "MEMORY_CHECK_RESTART"
THRESHOLD_ENV = "MEMORY_RESTART_THRESHOLD_MB"
DEFAULT_THRESHOLD_MB = 420  # the Railway-era value, kept when no limit is known
CGROUP_SHARE = 0.60
CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
# cgroup v1 reports "no limit" as a huge page-aligned number
_V1_UNLIMITED_BYTES = 1 << 60
_MB = 1024 * 1024


# ── threshold ────────────────────────────────────────────────────────────


def parse_cgroup_v2(text: str | None) -> int | None:
    """Bytes of a v2 ``memory.max``; None for ``max``, empty or garbage."""
    raw = (text or "").strip()
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def parse_cgroup_v1(text: str | None) -> int | None:
    """Bytes of a v1 ``memory.limit_in_bytes``; None for unlimited or garbage."""
    try:
        value = int((text or "").strip())
    except ValueError:
        return None
    return value if 0 < value < _V1_UNLIMITED_BYTES else None


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def cgroup_limit_bytes(unified: Path = CGROUP_V2_MAX, legacy: Path = CGROUP_V1_LIMIT) -> int | None:
    """The container memory limit: v2 first, then v1; None when none is set."""
    if unified.exists():
        return parse_cgroup_v2(_read(unified))
    return parse_cgroup_v1(_read(legacy))


def resolve_threshold(
    environ: dict[str, str] | None = None,
    *,
    unified: Path = CGROUP_V2_MAX,
    legacy: Path = CGROUP_V1_LIMIT,
) -> tuple[int, str]:
    """``(threshold_mb, source)``: ``env`` | ``cgroup`` | ``default``."""
    env = os.environ if environ is None else environ
    raw = (env.get(THRESHOLD_ENV) or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value, "env"
        except ValueError:
            logger.warning("memory_check: %s is not a positive integer, ignored", THRESHOLD_ENV)
    limit = cgroup_limit_bytes(unified, legacy)
    if limit:
        return max(1, int(limit * CGROUP_SHARE / _MB)), "cgroup"
    return DEFAULT_THRESHOLD_MB, "default"


def restart_enabled(environ: dict[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(RESTART_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


# ── busy ─────────────────────────────────────────────────────────────────


def busy_reason(semaphore: Any = None) -> str | None:
    """Why a restart must wait now (a code), or None when idle.

    Any error reading a signal is ``state_unknown``: busy, never a restart."""
    try:
        from oncofiles import inbound_email
        from oncofiles.tools import curated

        if curated.run_active():
            return "curated_work"
        if any(j.get("state") == "running" for j in curated._reocr_jobs.values()):
            return "curated_job"
        if any(lock.locked() for lock in list(curated._patient_locks.values())):
            return "patient_lock"
        if inbound_email.PROCESS_LOCK.locked():
            return "inbound_processing"
        if semaphore is None:
            return "state_unknown"
        if semaphore.locked():
            return "sync_semaphore"
        return None
    except Exception:  # noqa: BLE001 - fail safe: unknown = busy
        return "state_unknown"


# ── the job ──────────────────────────────────────────────────────────────


class MemoryCheckJob(IntervalJob):
    """:class:`IntervalJob` plus the last check's memory report (``memory``)."""

    def reset(self) -> None:
        super().reset()
        self.threshold_mb: int = DEFAULT_THRESHOLD_MB
        self.threshold_source: str = "default"
        self._memory: dict[str, Any] | None = None

    def status(self) -> dict:
        out = super().status()
        out["memory"] = dict(self._memory) if self._memory is not None else None
        return out


def summarize(c: dict[str, int]) -> dict[str, int]:
    """Named totals of one check (counts only)."""
    return {k: c.get(k, 0) for k in ("rss_mb", "peak_mb", "threshold_mb", "would_restart")}


MEMORY_CHECK = MemoryCheckJob(JOB_ID, INTERVAL_S, summarize=summarize)
JOBS = {JOB_ID: MEMORY_CHECK}


def _restart_or_defer(semaphore: Any) -> str:
    """With the switch on: SIGTERM when idle, else defer. Returns the outcome code."""
    reason = busy_reason(semaphore)
    if reason is not None:
        logger.warning("memory_check: restart_deferred reason=%s", reason)
        return reason
    logger.critical("memory_check: initiating graceful restart via SIGTERM")
    os.kill(os.getpid(), signal.SIGTERM)
    return "restart_sent"


async def memory_check_iteration(semaphore: Any = None) -> dict[str, int]:
    """One check; reports, and restarts only when enabled and idle."""
    from oncofiles import memory

    job = MEMORY_CHECK
    threshold = job.threshold_mb
    enabled = restart_enabled()
    report: dict[str, Any] = {
        "threshold_mb": threshold,
        "threshold_source": job.threshold_source,
        "restart_enabled": enabled,
        "would_restart": False,
        "outcome": None,
    }
    job._memory = report
    try:
        rss = memory.update_peak_rss()
        report["rss_mb"] = int(round(rss))
        logger.info("Memory check: RSS=%.1f MB (threshold=%d, %s)", rss, threshold,
                    job.threshold_source)  # fmt: skip
        if rss > threshold:
            rss = memory.reclaim_memory("memory_check")
            report["rss_after_reclaim_mb"] = int(round(rss))
            report["would_restart"] = rss > threshold
        report["peak_mb"] = int(round(memory.get_rss_trend()["peak_mb"]))
    except Exception:
        # the old error-listener fallback, under the same switch and busy rule
        if enabled:
            try:
                over = memory.get_rss_mb() > threshold
            except Exception:  # noqa: BLE001
                over = False
            if over:
                report["would_restart"] = True
                report["outcome"] = _restart_or_defer(semaphore)
        raise
    if report["would_restart"]:
        report["outcome"] = _restart_or_defer(semaphore) if enabled else "report_only"
    return {
        "rss_mb": report["rss_mb"],
        "peak_mb": report["peak_mb"],
        "threshold_mb": threshold,
        "would_restart": int(report["would_restart"]),
    }


async def memory_check_loop(semaphore: Any = None, *, grace_s: float | None = None) -> None:
    threshold, source = await asyncio.to_thread(resolve_threshold)
    MEMORY_CHECK.threshold_mb, MEMORY_CHECK.threshold_source = threshold, source
    logger.info(
        "memory_check: threshold %d MB (%s), restart %s",
        threshold,
        source,
        "on" if restart_enabled() else "off (report only)",
    )
    await MEMORY_CHECK.loop(
        lambda: memory_check_iteration(semaphore),
        grace_s=FIRST_RUN_DELAY_S if grace_s is None else grace_s,
    )


def _reset_for_tests() -> None:
    MEMORY_CHECK.reset()
