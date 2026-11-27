"""#856: the pre-store erase copy retention runs without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so a job registered in
APScheduler never ran and erase copies outlived /privacy's 30 days. The job is
now a lifespan loop (``oncofiles.erase_retention``), its only owner:

* it starts with ``SYNC_ENABLED=false`` and is not registered twice with true;
* the 04:35 UTC slot is defined once and read by the admin job list;
* catch-up: before the slot → wait; after it, not run → run now; already run → next day;
* a run records ``last_deleted`` and a failure never ends the loop.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import erase_retention, scheduled_jobs, scheduler_singleton
from oncofiles.cloud_guard import LOCAL_ONLY_TASK_ATTR, local_only_job

DAY = date(2027, 4, 12)


@pytest.fixture(autouse=True)
def _clean():
    scheduler_singleton._reset_for_tests()
    erase_retention._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    erase_retention._reset_for_tests()


def _at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


# ── slot and catch-up decision ────────────────────────────────────────────


def test_slot_is_defined_once_at_0435_utc():
    assert (erase_retention.SLOT_HOUR_UTC, erase_retention.SLOT_MINUTE_UTC) == (4, 35)
    assert erase_retention.slot_on(DAY) == _at(4, 35)
    assert erase_retention.SCHEDULE_TEXT == "daily 04:35 UTC"


def test_before_the_slot_waits_for_todays_slot():
    early = _at(hour=1)
    assert erase_retention.next_run_at(early, None) == erase_retention.slot_on(DAY)
    # even when it ran yesterday
    assert erase_retention.next_run_at(_at(4, 34), DAY - timedelta(days=1)) == _at(4, 35)


def test_after_the_slot_and_not_run_today_runs_now():
    now = _at(9, 12)  # restart late in the morning
    assert erase_retention.next_run_at(now, None) == now
    assert erase_retention.next_run_at(now, DAY - timedelta(days=1)) == now
    assert erase_retention.next_run_at(_at(4, 35), None) == _at(4, 35)  # exactly at the slot


def test_after_the_slot_and_already_run_today_waits_for_tomorrow():
    tomorrow = DAY + timedelta(days=1)
    assert erase_retention.next_run_at(_at(9, 12), DAY) == _at(4, 35, tomorrow)
    assert erase_retention.next_run_at(_at(23, 59), DAY) == _at(4, 35, tomorrow)


def test_next_run_handles_month_end_and_other_zones():
    month_end = date(2027, 4, 30)
    first_of_next = month_end + timedelta(days=1)
    after = _at(hour=5, day=month_end)
    assert erase_retention.next_run_at(after, month_end) == erase_retention.slot_on(first_of_next)
    from zoneinfo import ZoneInfo

    bratislava = ZoneInfo("Europe/Bratislava")
    local = _at(hour=4).astimezone(bratislava)  # 04:00 UTC seen in local time
    assert local.utcoffset() != timedelta(0)
    assert erase_retention.next_run_at(local, None) == erase_retention.slot_on(DAY)


# ── the loop ──────────────────────────────────────────────────────────────


class _Clock:
    """Fake time: ``sleep`` advances it; stops the loop after ``max_sleeps``."""

    def __init__(self, start: datetime, max_sleeps: int = 20):
        self.now = start
        self.sleeps: list[float] = []
        self.max_sleeps = max_sleeps

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.max_sleeps:
            raise asyncio.CancelledError
        self.now += timedelta(seconds=seconds)


async def _run_loop(clock: _Clock, grace_s: float = 300) -> None:
    with pytest.raises(asyncio.CancelledError):
        await erase_retention.erase_retention_loop(
            asyncio.Semaphore(1), grace_s=grace_s, now_fn=clock, sleep=clock.sleep
        )


async def test_catch_up_runs_once_right_after_the_grace(monkeypatch):
    calls: list[datetime] = []
    monkeypatch.setattr(
        "oncofiles.pre_store.prune_expired_erase_copies", lambda: calls.append(1) or 3
    )
    clock = _Clock(_at(10, 0), max_sleeps=3)
    await _run_loop(clock)
    assert clock.sleeps[0] == 300  # the startup grace first
    assert len(calls) == 1  # catch-up after the grace, not tomorrow
    status = erase_retention.status()
    assert status["last_deleted"] == 3
    assert status["last_ok"] and status["last_error"] is None
    assert status["next_run_at"] == _at(4, 35, DAY + timedelta(days=1)).isoformat()
    # No second run the same day, however often it wakes up.
    assert all(s <= erase_retention.MAX_SLEEP_S for s in clock.sleeps[1:])


async def test_before_the_slot_waits_then_runs_daily(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        "oncofiles.pre_store.prune_expired_erase_copies", lambda: calls.append(1) or 0
    )
    clock = _Clock(_at(2, 0), max_sleeps=60)  # ~2.5 days of hourly wake-ups
    await _run_loop(clock, grace_s=60)
    assert len(calls) == 3  # one per UTC day
    assert erase_retention.status()["runs"] == 3
    assert erase_retention._state["last_run_day"] == DAY + timedelta(days=2)


async def test_a_failed_run_is_recorded_and_the_loop_goes_on(monkeypatch):
    attempts: list[int] = []

    def boom():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("synthetic disk error")
        return 2

    monkeypatch.setattr("oncofiles.pre_store.prune_expired_erase_copies", boom)
    clock = _Clock(_at(5, 0), max_sleeps=40)
    await _run_loop(clock, grace_s=1)
    assert len(attempts) == 2  # failed today, ran again tomorrow
    status = erase_retention.status()
    assert status["last_error_type"] == "OSError"
    assert status["last_deleted"] == 2
    assert status["running"] is False


async def test_run_once_holds_the_semaphore_and_uses_a_worker_thread(monkeypatch):
    sem = asyncio.Semaphore(1)
    seen: dict = {}

    def prune():
        seen["locked"] = sem.locked()
        seen["thread"] = __import__("threading").current_thread().name
        return 1

    monkeypatch.setattr("oncofiles.pre_store.prune_expired_erase_copies", prune)
    assert await erase_retention.run_once(sem) == 1
    assert seen["locked"] is True
    assert seen["thread"] != "MainThread"
    assert not sem.locked()


async def test_run_logs_the_count_only(monkeypatch, caplog):
    monkeypatch.setattr("oncofiles.pre_store.prune_expired_erase_copies", lambda: 4)
    with caplog.at_level("INFO", logger="oncofiles.erase_retention"):
        await erase_retention.run_once(asyncio.Semaphore(1))
    assert "Pre-store erase retention: removed 4 copies" in caplog.text


# ── lifespan: one owner, independent of SYNC_ENABLED ─────────────────────


@pytest.fixture
def lifespan_env(monkeypatch, tmp_path):
    import oncofiles.server as server
    from oncofiles import patient_context
    from oncofiles.curated import indexer
    from oncofiles.database import Database
    from oncofiles.tools import curated

    monkeypatch.setattr(server, "DATABASE_PATH", str(tmp_path / "t856.db"))
    for name in [k for k in os.environ if k.startswith("TURSO_")]:
        monkeypatch.delenv(name)
    monkeypatch.setattr(server, "GOOGLE_DRIVE_FOLDER_ID", "")
    monkeypatch.setattr(server, "_setup_logging", lambda: None)
    monkeypatch.setattr(server, "auth", SimpleNamespace())
    monkeypatch.setattr(server, "create_gdrive_client", MagicMock(return_value=None))
    monkeypatch.setattr(patient_context, "initialize", AsyncMock())
    monkeypatch.setattr(patient_context, "load_from_db", AsyncMock())
    monkeypatch.setattr(indexer, "register_curated_ids", MagicMock())
    monkeypatch.setattr(indexer, "mark_guard_ready", MagicMock())
    monkeypatch.setattr(indexer, "mark_guard_failed", MagicMock())
    monkeypatch.setattr(curated, "request_stop", MagicMock())
    monkeypatch.setenv("CURATED_NIGHTLY_ENABLED", "false")
    monkeypatch.setattr(
        Database,
        "get_patient_by_slug",
        AsyncMock(return_value=SimpleNamespace(patient_id="00000000-0000-4000-8000-000000000856")),
    )
    monkeypatch.setattr(Database, "get_oauth_token", AsyncMock(return_value=None))
    return server


async def test_loop_starts_with_sync_disabled(monkeypatch, lifespan_env):
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    async with server.lifespan(MagicMock()):
        assert scheduler_singleton.current_scheduler() is None  # no APScheduler
        assert scheduler_singleton.background_loop_running(erase_retention.JOB_ID)
        task = scheduler_singleton._loops[erase_retention.JOB_ID]
        status = scheduler_singleton.status()
        assert status["running"] is False
        assert erase_retention.JOB_ID in status["background_loops"]
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert not scheduler_singleton.background_loop_running(erase_retention.JOB_ID)


async def test_not_double_registered_with_sync_enabled(monkeypatch, lifespan_env):
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    async with server.lifespan(MagicMock()):
        sched, _ = scheduler_singleton.current_scheduler()
        assert erase_retention.JOB_ID not in {j.id for j in sched.get_jobs()}
        assert scheduler_singleton.background_loop_running(erase_retention.JOB_ID)
        # a second lifespan in the same process reuses the loop
        async with server.lifespan(MagicMock()):
            assert len(scheduler_singleton._loops) == 1
            assert scheduler_singleton._loop_refs[erase_retention.JOB_ID] == 2
        assert scheduler_singleton.background_loop_running(erase_retention.JOB_ID)
    await asyncio.sleep(0)
    assert not scheduler_singleton.background_loop_running(erase_retention.JOB_ID)


async def test_loop_runs_in_a_local_only_zone(monkeypatch, lifespan_env):
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_loop(semaphore):
        seen["zone"] = local_only_job()
        seen["semaphore"] = semaphore

    monkeypatch.setattr(erase_retention, "erase_retention_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["zone"] == erase_retention.JOB_ID
    assert seen["semaphore"] is server._sync_semaphore
    assert LOCAL_ONLY_TASK_ATTR  # imported for the lock's attribute name


def test_ensure_background_loop_is_reference_counted():
    made = []

    def factory():
        task = MagicMock()
        task.done.return_value = False
        made.append(task)
        return task

    first, started_first = scheduler_singleton.ensure_background_loop("x", factory)
    again, started_again = scheduler_singleton.ensure_background_loop("x", factory)
    assert started_first is True and started_again is False
    assert again is first and made == [first]
    assert scheduler_singleton.release_background_loop("x", first) is False
    assert scheduler_singleton.release_background_loop("x", first) is True
    assert scheduler_singleton.release_background_loop("x", first) is False  # stale holder


async def test_a_dying_background_loop_is_logged_and_a_cancel_is_not(caplog):
    """Infra review of #998: a loop that dies must not be silent."""

    async def boom():
        raise RuntimeError("synthetic")

    async def forever():
        await asyncio.sleep(3600)

    caplog.set_level("ERROR", logger="oncofiles.scheduler_singleton")
    dead, _ = scheduler_singleton.ensure_background_loop(
        "dies", lambda: asyncio.create_task(boom())
    )
    with pytest.raises(RuntimeError):
        await dead
    await asyncio.sleep(0)  # let the done callback run
    assert "dies loop died: RuntimeError" in caplog.text

    caplog.clear()
    alive, _ = scheduler_singleton.ensure_background_loop(
        "cancelled", lambda: asyncio.create_task(forever())
    )
    alive.cancel()
    with pytest.raises(asyncio.CancelledError):
        await alive
    await asyncio.sleep(0)
    assert caplog.text == ""
    scheduler_singleton.release_background_loop("dies", dead)
    scheduler_singleton.release_background_loop("cancelled", alive)


# ── the #734 lock still sees the job, within the existing entry ──────────


def test_the_734_scan_discovers_the_loop():
    from tests.test_no_automatic_deletion import _job_registrations, _load_modules

    roots, _ = _job_registrations(_load_modules()["oncofiles.server"])
    assert "erase_retention_loop" in roots


# ── admin job list + readiness read the same slot ────────────────────────


def test_admin_entry_uses_the_shared_slot():
    running = scheduled_jobs.background_loop_entry(
        erase_retention.JOB_ID, running=True, state=erase_retention.status()
    )
    assert running["registered"] is True
    assert running["trigger"]["text"] == erase_retention.SCHEDULE_TEXT
    assert running["trigger"]["cadence"] == "daily"
    off = scheduled_jobs.background_loop_entry(
        erase_retention.JOB_ID, running=False, state=erase_retention.status()
    )
    assert off["reason"] == "loop_not_running"
    assert erase_retention.status()["schedule"] == erase_retention.SCHEDULE_TEXT
