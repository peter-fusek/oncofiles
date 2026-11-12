"""One APScheduler and one curated loop per process (#784).

The app relies on a single scheduler: ``_sync_semaphore`` and the single DB
connection serialize jobs only inside one scheduler, and ``patient_lock()`` is
in-process. A second scheduler (or a second curated loop) in the same process
would fire every job twice. This module is the process-wide registry that
makes starting them idempotent:

* ``existing_scheduler()`` / ``register_scheduler()`` — ``_start_sync_scheduler``
  returns the running instance instead of building a second one;
* ``ensure_curated_loop()`` — the lifespan gets the running loop task back
  instead of creating another;
* ``release_*()`` — reference counted, so only the last holder shuts down.

Across processes (a rolling deploy runs the old and new container side by side
for a few seconds) nothing here helps; that overlap is covered by the startup
grace (``tools.curated.STARTUP_GRACE_S``): the curated loop waits it out and
``server._defer_jobs_past_startup_grace`` moves every first job slot past it.

``status()`` feeds the operator-only ``/readiness.scheduler`` block. It is
in-memory only — never a DB call.
"""

from __future__ import annotations

import logging
import os
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Random per process start: after a rolling deploy, every /readiness answer
# must carry the same id, or two instances are serving.
INSTANCE_ID: str = uuid.uuid4().hex[:12]
PROCESS_STARTED_AT: str = datetime.now(UTC).isoformat(timespec="seconds")

_lock = threading.Lock()
_scheduler: Any = None
_tracker: dict | None = None
_scheduler_refs = 0
_scheduler_started_at: str | None = None
_curated_task: Any = None
_curated_refs = 0
_curated_started_at: str | None = None
_duplicate_logged: set[str] = set()


def _log_duplicate_once(kind: str) -> None:
    if kind in _duplicate_logged:
        return
    _duplicate_logged.add(kind)
    logger.warning(
        "%s already running in this process (instance %s) — reusing it, not starting another",
        kind,
        INSTANCE_ID,
    )


def _scheduler_alive(sched: Any) -> bool:
    """Running, on an event loop that is still open.

    ``AsyncIOScheduler.shutdown()`` only queues the stop on its loop, so a
    scheduler whose loop has closed still reports ``running``; it is dead.
    """
    if sched is None or not getattr(sched, "running", False):
        return False
    loop = getattr(sched, "_eventloop", None)
    return not (loop is not None and hasattr(loop, "is_closed") and loop.is_closed())


def existing_scheduler() -> tuple[Any, dict] | None:
    """The running scheduler and its job tracker, or None.

    A hit counts as a new holder (``release_scheduler`` must be called once
    for it) and is logged once per process.
    """
    global _scheduler_refs
    with _lock:
        if not _scheduler_alive(_scheduler):
            return None
        _scheduler_refs += 1
        _log_duplicate_once("APScheduler")
        return _scheduler, _tracker if _tracker is not None else {}


def register_scheduler(sched: Any, tracker: dict) -> None:
    """Record a freshly started scheduler as the process's only one."""
    global _scheduler, _tracker, _scheduler_refs, _scheduler_started_at
    with _lock:
        if _scheduler_alive(_scheduler) and _scheduler is not sched:
            # Caller bypassed existing_scheduler(); refuse to lose track of one.
            raise RuntimeError("a second APScheduler was started in this process")
        _scheduler = sched
        _tracker = tracker
        _scheduler_refs = 1
        _scheduler_started_at = datetime.now(UTC).isoformat(timespec="seconds")
        _duplicate_logged.discard("APScheduler")


def release_scheduler(sched: Any) -> bool:
    """Drop one holder. True when the caller was the last and should shut it down."""
    global _scheduler, _tracker, _scheduler_refs, _scheduler_started_at
    with _lock:
        if sched is not _scheduler:
            return True  # not registered (e.g. a test double): the caller owns it
        _scheduler_refs = max(0, _scheduler_refs - 1)
        if _scheduler_refs:
            return False
        _scheduler = None
        _tracker = None
        _scheduler_started_at = None
        return True


def ensure_curated_loop(factory: Callable[[], Any]) -> tuple[Any, bool]:
    """Return ``(task, started_now)``; ``factory`` runs only when no loop is alive."""
    global _curated_task, _curated_refs, _curated_started_at
    with _lock:
        if _curated_task is not None and not _curated_task.done():
            _curated_refs += 1
            _log_duplicate_once("curated loop")
            return _curated_task, False
        _curated_task = factory()
        _curated_refs = 1
        _curated_started_at = datetime.now(UTC).isoformat(timespec="seconds")
        _duplicate_logged.discard("curated loop")
        return _curated_task, True


def release_curated_loop(task: Any) -> bool:
    """Drop one holder. True when the caller was the last and should cancel it."""
    global _curated_task, _curated_refs, _curated_started_at
    with _lock:
        if task is not _curated_task:
            return True
        _curated_refs = max(0, _curated_refs - 1)
        if _curated_refs:
            return False
        _curated_task = None
        _curated_started_at = None
        return True


def curated_loop_running() -> bool:
    task = _curated_task
    return task is not None and not task.done()


def status() -> dict:
    """In-memory snapshot for operator /readiness. No DB, no patient data."""
    sched = _scheduler
    running = _scheduler_alive(sched)
    job_count = 0
    if running:
        try:
            job_count = len(sched.get_jobs())
        except Exception:  # never let telemetry break readiness
            job_count = -1
    return {
        "instance_id": INSTANCE_ID,
        "pid": os.getpid(),
        "process_started_at": PROCESS_STARTED_AT,
        "running": running,
        "started_at": _scheduler_started_at if running else None,
        "job_count": job_count,
        "curated_loop_running": curated_loop_running(),
        "curated_loop_started_at": _curated_started_at if curated_loop_running() else None,
    }


def _reset_for_tests() -> None:
    global _scheduler, _tracker, _scheduler_refs, _scheduler_started_at
    global _curated_task, _curated_refs, _curated_started_at
    with _lock:
        _scheduler = None
        _tracker = None
        _scheduler_refs = 0
        _scheduler_started_at = None
        _curated_task = None
        _curated_refs = 0
        _curated_started_at = None
        _duplicate_logged.clear()
