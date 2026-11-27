"""#999 job 5: the periodic memory check runs without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so the ``memory_check`` job
(every five minutes, graceful restart above ``MEMORY_RESTART_THRESHOLD_MB``)
never ran there; only the ``/health`` hard ceiling was left. It is now a
lifespan loop (``oncofiles.memory_check_loop`` on ``IntervalJob``), its only
owner. Covered:

* first run seven minutes after start (the former ``start_date``, #366), then
  every five minutes; longer than the startup grace;
* the check itself is unchanged: over the threshold after a reclaim it sends
  SIGTERM through ``os.kill`` (never ``sys.exit``); under it, nothing;
* a failing check is recorded by class and, with a high RSS, still restarts
  (the old APScheduler error-listener fallback);
* the loop starts with ``SYNC_ENABLED=false``, is not an APScheduler job with
  true, runs in its local-only zone; telemetry on operator ``/readiness``
  and in the admin job list (counts only).
"""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from oncofiles import memory, memory_check_loop, scheduled_jobs, scheduler_singleton
from oncofiles.cloud_guard import local_only_job
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

JOB_ID = "memory_check"


@pytest.fixture(autouse=True)
def _clean():
    scheduler_singleton._reset_for_tests()
    memory_check_loop._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    memory_check_loop._reset_for_tests()


@pytest.fixture
def kills(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(memory_check_loop.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(memory.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def _rss(monkeypatch, value: float) -> None:
    monkeypatch.setattr(memory, "get_rss_mb", lambda: value)
    monkeypatch.setattr(memory, "reclaim_memory", lambda *_a, **_k: value)


# ── schedule ─────────────────────────────────────────────────────────────


def test_schedule_and_first_run_delay():
    from oncofiles.tools.curated import STARTUP_GRACE_S

    assert memory_check_loop.INTERVAL_S == 300
    assert memory_check_loop.FIRST_RUN_DELAY_S == 420  # the former start_date (#366)
    assert memory_check_loop.FIRST_RUN_DELAY_S >= STARTUP_GRACE_S
    assert memory_check_loop.MEMORY_CHECK.schedule_text == "every 5 min"


class _Clock:
    def __init__(self, stop_after: int) -> None:
        self.now = datetime(2027, 5, 10, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []
        self.stop_after = stop_after

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.stop_after:
            raise asyncio.CancelledError
        self.now += timedelta(seconds=seconds)


async def test_loop_waits_seven_minutes_then_every_five(monkeypatch, kills):
    _rss(monkeypatch, 150.0)
    clock = _Clock(stop_after=3)
    with pytest.raises(asyncio.CancelledError):
        await memory_check_loop.MEMORY_CHECK.loop(
            memory_check_loop.memory_check_iteration,
            grace_s=memory_check_loop.FIRST_RUN_DELAY_S,
            sleep=clock.sleep,
            now_fn=clock,
        )
    assert clock.sleeps == [420, 300, 300, 300]
    st = memory_check_loop.MEMORY_CHECK.status()
    assert st["runs"] == 3 and st["errors"] == 0
    assert st["last_summary"]["rss_mb"] == 150
    assert st["last_summary"]["threshold_mb"] == memory.MEMORY_RESTART_THRESHOLD_MB
    assert kills == []


async def test_memory_check_loop_defaults_to_the_first_run_delay(monkeypatch):
    seen: dict = {}

    async def fake_loop(fn, *, grace_s=None, **_k):
        seen["grace"] = grace_s

    monkeypatch.setattr(memory_check_loop.MEMORY_CHECK, "loop", fake_loop)
    await memory_check_loop.memory_check_loop()
    assert seen["grace"] == memory_check_loop.FIRST_RUN_DELAY_S


# ── the check: unchanged restart semantics ───────────────────────────────


async def test_over_threshold_sends_sigterm_never_exit(monkeypatch, kills):
    _rss(monkeypatch, memory.MEMORY_RESTART_THRESHOLD_MB + 50.0)
    out = await memory_check_loop.memory_check_iteration()
    assert kills and kills[0][1] == signal.SIGTERM
    assert out["rss_mb"] == memory.MEMORY_RESTART_THRESHOLD_MB + 50


async def test_reclaim_that_brings_rss_down_averts_the_restart(monkeypatch, kills):
    monkeypatch.setattr(memory, "get_rss_mb", lambda: memory.MEMORY_RESTART_THRESHOLD_MB + 50.0)
    monkeypatch.setattr(memory, "reclaim_memory", lambda *_a, **_k: 100.0)
    await memory_check_loop.memory_check_iteration()
    assert kills == []


async def test_failed_check_with_high_rss_still_restarts(monkeypatch, kills, caplog):
    async def boom():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(memory, "periodic_memory_check", boom)
    monkeypatch.setattr(memory, "get_rss_mb", lambda: memory.MEMORY_RESTART_THRESHOLD_MB + 1.0)
    caplog.set_level("ERROR")
    assert (
        await memory_check_loop.MEMORY_CHECK.run_once(memory_check_loop.memory_check_iteration)
        is None
    )
    assert kills and kills[0][1] == signal.SIGTERM
    st = memory_check_loop.MEMORY_CHECK.status()
    assert st["errors"] == 1 and st["last_error_type"] == "RuntimeError"


async def test_failed_check_with_low_rss_does_not_restart(monkeypatch, kills):
    async def boom():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(memory, "periodic_memory_check", boom)
    monkeypatch.setattr(memory, "get_rss_mb", lambda: 100.0)
    await memory_check_loop.MEMORY_CHECK.run_once(memory_check_loop.memory_check_iteration)
    assert kills == []


def test_no_sys_exit_in_the_loop_module():
    from pathlib import Path

    src = Path(memory_check_loop.__file__).read_text()
    assert "sys.exit" not in src.replace("never ``sys.exit``", "")


# ── lifespan ─────────────────────────────────────────────────────────────


async def test_loop_starts_with_sync_disabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    async with server.lifespan(MagicMock()):
        assert scheduler_singleton.current_scheduler() is None
        assert scheduler_singleton.background_loop_running(JOB_ID)
    await asyncio.sleep(0)
    assert not scheduler_singleton.background_loop_running(JOB_ID)


async def test_not_an_apscheduler_job_with_sync_enabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    async with server.lifespan(MagicMock()):
        sched, _ = scheduler_singleton.current_scheduler()
        assert JOB_ID not in {j.id for j in sched.get_jobs()}
        assert scheduler_singleton.background_loop_running(JOB_ID)
    await asyncio.sleep(0)
    assert not scheduler_singleton.background_loop_running(JOB_ID)


async def test_loop_runs_in_its_local_only_zone(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_loop():
        seen["zone"] = local_only_job()

    monkeypatch.setattr(memory_check_loop, "memory_check_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["zone"] == JOB_ID


# ── telemetry ────────────────────────────────────────────────────────────


async def test_readiness_carries_loop_telemetry(monkeypatch):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)

    async def one():
        return {"rss_mb": 210, "peak_mb": 260, "threshold_mb": 420}

    await memory_check_loop.MEMORY_CHECK.run_once(one)
    request, _ = _request(operator=True)
    job = json.loads((await server.readiness(request)).body)["jobs"][JOB_ID]
    assert job["schedule"] == "every 5 min" and job["runs"] == 1
    assert job["last_summary"] == {"rss_mb": 210, "peak_mb": 260, "threshold_mb": 420}
    assert "last_deleted" not in job


def test_admin_job_list_shows_the_loop():
    assert JOB_ID in scheduled_jobs.BACKGROUND_LOOP_JOBS
    entry = scheduled_jobs.background_loop_entry(
        JOB_ID, running=True, state=scheduled_jobs.background_loop_state(JOB_ID)
    )
    assert entry["registered"] is True and entry["known"] is True
    assert entry["trigger"]["kind"] == "interval" and entry["trigger"]["seconds"] == 300
