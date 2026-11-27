"""#999 job 5: the periodic memory check runs without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so the ``memory_check`` job
never ran there. It is now a lifespan loop (``oncofiles.memory_check_loop``),
its only owner. Infra review of #1011:

* report-only by default (``MEMORY_CHECK_RESTART`` false): measures and
  reports on ``/readiness.jobs.memory_check.memory``, never sends a signal;
* with the switch on, a due restart is deferred while anything is busy
  (curated run or job, patient lock, inbound lock, ``_sync_semaphore``), one
  ``restart_deferred reason=<code>`` line, and happens on a later idle tick;
  an unreadable signal counts as busy;
* the threshold: explicit env, else a share of the cgroup limit (new_file / old_file),
  else the old default;
* first run seven minutes after start (#366), then every five minutes; the
  loop starts with ``SYNC_ENABLED=false``, is not an APScheduler job, runs in
  its local-only zone.

Synthetic only: no patient data, fake cgroup files under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import signal
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from oncofiles import (
    inbound_email,
    memory,
    memory_check_loop,
    scheduled_jobs,
    scheduler_singleton,
)
from oncofiles.cloud_guard import local_only_job
from oncofiles.tools import curated
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

JOB_ID = "memory_check"
M = memory_check_loop
GIB = 1024**3
MIB = 1024**2
TWO_GIB = 2 * GIB


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(M.RESTART_ENV, raising=False)
    monkeypatch.delenv(M.THRESHOLD_ENV, raising=False)
    scheduler_singleton._reset_for_tests()
    M._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    M._reset_for_tests()


@pytest.fixture
def kills(monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(M.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(memory.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


@pytest.fixture
def idle(monkeypatch):
    """Nothing busy: no curated run/job, no held lock."""
    monkeypatch.setattr(curated, "run_active", lambda: False)
    monkeypatch.setattr(curated, "_reocr_jobs", {})
    monkeypatch.setattr(curated, "_patient_locks", {})
    monkeypatch.setattr(inbound_email, "PROCESS_LOCK", asyncio.Lock())


def _signals(kills):
    return [sig for _pid, sig in kills]


def _rss(monkeypatch, before: float, after: float | None = None) -> None:
    monkeypatch.setattr(memory, "get_rss_mb", lambda: before)
    monkeypatch.setattr(memory, "update_peak_rss", lambda: before)
    monkeypatch.setattr(
        memory, "reclaim_memory", lambda *_a, **_k: before if after is None else after
    )


def _threshold(mb: int, source: str = "cgroup") -> None:
    M.MEMORY_CHECK.threshold_mb, M.MEMORY_CHECK.threshold_source = mb, source


# ── threshold: cgroup new_file / old_file, env, default ──────────────────────────────


def test_parse_cgroup_v2_and_v1():
    assert M.parse_cgroup_v2("2147483648\n") == 2 * GIB
    assert M.parse_cgroup_v2("max\n") is None
    assert M.parse_cgroup_v2("") is None and M.parse_cgroup_v2("garbage") is None
    assert M.parse_cgroup_v1("1073741824\n") == GIB
    assert M.parse_cgroup_v1("9223372036854771712\n") is None  # old_file "unlimited"
    assert M.parse_cgroup_v1(None) is None


def test_resolve_threshold_from_cgroup_v2(tmp_path):
    new_file = tmp_path / "memory.max"
    new_file.write_text(f"{TWO_GIB}\n")
    mb, source = M.resolve_threshold({}, unified=new_file, legacy=tmp_path / "none")
    assert source == "cgroup"
    assert mb == int(TWO_GIB * M.CGROUP_SHARE / MIB)


def test_resolve_threshold_v2_max_means_no_limit(tmp_path):
    new_file = tmp_path / "memory.max"
    new_file.write_text("max\n")
    old_file = tmp_path / "limit_in_bytes"
    old_file.write_text(f"{GIB}\n")  # the unified file wins: max means no limit
    assert M.resolve_threshold({}, unified=new_file, legacy=old_file) == (
        M.DEFAULT_THRESHOLD_MB,
        "default",
    )


def test_resolve_threshold_from_cgroup_v1(tmp_path):
    old_file = tmp_path / "limit_in_bytes"
    old_file.write_text(f"{GIB}\n")
    expected = (int(GIB * M.CGROUP_SHARE / MIB), "cgroup")
    assert M.resolve_threshold({}, unified=tmp_path / "none", legacy=old_file) == expected


def test_resolve_threshold_missing_files_keeps_the_default(tmp_path):
    assert M.resolve_threshold({}, unified=tmp_path / "a", legacy=tmp_path / "b") == (
        420,
        "default",
    )


def test_env_overrides_the_cgroup(tmp_path):
    new_file = tmp_path / "memory.max"
    new_file.write_text(f"{TWO_GIB}\n")
    env = {M.THRESHOLD_ENV: "900"}
    got = M.resolve_threshold(env, unified=new_file, legacy=tmp_path / "x")
    assert got == (int(env[M.THRESHOLD_ENV]), "env")
    bad = {M.THRESHOLD_ENV: "lots"}
    _mb, src = M.resolve_threshold(bad, unified=new_file, legacy=tmp_path / "x")
    assert src == "cgroup"


async def test_loop_reads_the_threshold_once_at_start_in_a_thread(monkeypatch):
    calls: list[str] = []

    def fake_resolve():
        calls.append("resolve")
        return 1228, "cgroup"

    seen: dict = {}

    async def fake_loop(fn, *, grace_s=None, **_k):
        seen["grace"] = grace_s

    real_to_thread = asyncio.to_thread
    threaded: list = []

    async def spy_to_thread(fn, *a, **k):
        threaded.append(fn)
        return await real_to_thread(fn, *a, **k)

    from oncofiles import memory_ceiling

    def fake_ceiling(soft):
        calls.append(f"ceiling:{soft}")

    monkeypatch.setattr(M, "resolve_threshold", fake_resolve)
    monkeypatch.setattr(memory_ceiling, "resolve", fake_ceiling)
    monkeypatch.setattr(M.asyncio, "to_thread", spy_to_thread)
    monkeypatch.setattr(M.MEMORY_CHECK, "loop", fake_loop)
    await M.memory_check_loop(asyncio.Semaphore(1))
    # #1012: the /health levels are resolved right after, in a thread, against it
    assert calls == ["resolve", "ceiling:1228"] and threaded == [fake_resolve, fake_ceiling]
    assert (M.MEMORY_CHECK.threshold_mb, M.MEMORY_CHECK.threshold_source) == (1228, "cgroup")
    assert seen["grace"] == M.FIRST_RUN_DELAY_S


# ── report only (default) ────────────────────────────────────────────────


async def test_report_only_never_kills(monkeypatch, kills, idle):
    _threshold(500)
    _rss(monkeypatch, 800.0)  # over, and reclaim does not help
    out = await M.memory_check_iteration(asyncio.Semaphore(1))
    assert kills == []
    assert out["would_restart"] == 1 and out["threshold_mb"] == 500
    rep = M.MEMORY_CHECK.status()["memory"]
    assert rep["restart_enabled"] is False and rep["outcome"] == "report_only"
    assert rep["threshold_source"] == "cgroup" and rep["rss_mb"] == 800


async def test_under_threshold_reports_no_restart(monkeypatch, kills, idle):
    monkeypatch.setenv(M.RESTART_ENV, "true")
    _threshold(500)
    _rss(monkeypatch, 200.0)
    out = await M.memory_check_iteration(asyncio.Semaphore(1))
    assert kills == [] and out["would_restart"] == 0


async def test_reclaim_that_brings_rss_down_averts_the_restart(monkeypatch, kills, idle):
    monkeypatch.setenv(M.RESTART_ENV, "true")
    _threshold(500)
    _rss(monkeypatch, 800.0, after=300.0)
    await M.memory_check_iteration(asyncio.Semaphore(1))
    assert kills == []
    assert M.MEMORY_CHECK.status()["memory"]["would_restart"] is False


# ── restart enabled: deferred while busy, then once idle ────────────────


async def test_restart_when_enabled_and_idle_is_sigterm(monkeypatch, kills, idle):
    monkeypatch.setenv(M.RESTART_ENV, "true")
    _threshold(500)
    _rss(monkeypatch, 800.0)
    await M.memory_check_iteration(asyncio.Semaphore(1))
    assert _signals(kills) == [signal.SIGTERM]
    assert M.MEMORY_CHECK.status()["memory"]["outcome"] == "restart_sent"


async def test_restart_deferred_while_busy_then_happens_once_idle(monkeypatch, kills, idle, caplog):
    monkeypatch.setenv(M.RESTART_ENV, "true")
    _threshold(500)
    _rss(monkeypatch, 800.0)
    sem = asyncio.Semaphore(1)
    caplog.set_level("WARNING", logger="oncofiles.memory_check_loop")

    await sem.acquire()  # a job holds _sync_semaphore
    await M.memory_check_iteration(sem)
    assert kills == []
    assert "memory_check: restart_deferred reason=sync_semaphore" in caplog.text
    assert M.MEMORY_CHECK.status()["memory"]["outcome"] == "sync_semaphore"

    sem.release()  # idle on the next tick
    await M.memory_check_iteration(sem)
    assert _signals(kills) == [signal.SIGTERM]


@pytest.mark.parametrize(
    ("make_busy", "code"),
    [
        (lambda mp: mp.setattr(curated, "run_active", lambda: True), "curated_work"),
        (
            lambda mp: mp.setattr(
                curated, "_reocr_jobs", {"p": {"state": "running", "kind": "reocr_compare"}}
            ),
            "curated_job",
        ),
    ],
)
def test_busy_reasons_for_curated_work(monkeypatch, idle, make_busy, code):
    make_busy(monkeypatch)
    assert M.busy_reason(asyncio.Semaphore(1)) == code


async def test_busy_reasons_for_held_locks(monkeypatch, idle):
    lock = asyncio.Lock()
    await lock.acquire()
    monkeypatch.setattr(curated, "_patient_locks", {"p": lock})
    assert M.busy_reason(asyncio.Semaphore(1)) == "patient_lock"
    monkeypatch.setattr(curated, "_patient_locks", {})
    monkeypatch.setattr(inbound_email, "PROCESS_LOCK", lock)
    assert M.busy_reason(asyncio.Semaphore(1)) == "inbound_processing"
    lock.release()
    assert M.busy_reason(asyncio.Semaphore(1)) is None


def test_unknown_state_counts_as_busy(monkeypatch, idle):
    def broken():
        raise RuntimeError("synthetic")

    monkeypatch.setattr(curated, "run_active", broken)
    assert M.busy_reason(asyncio.Semaphore(1)) == "state_unknown"
    monkeypatch.setattr(curated, "run_active", lambda: False)
    assert M.busy_reason(None) == "state_unknown"  # no semaphore to read


async def test_unknown_state_blocks_the_restart(monkeypatch, kills, idle):
    monkeypatch.setenv(M.RESTART_ENV, "true")
    _threshold(500)
    _rss(monkeypatch, 800.0)
    monkeypatch.setattr(curated, "run_active", lambda: 1 / 0)
    await M.memory_check_iteration(asyncio.Semaphore(1))
    assert kills == []
    assert M.MEMORY_CHECK.status()["memory"]["outcome"] == "state_unknown"


# ── a failing check ──────────────────────────────────────────────────────


async def test_failed_check_report_only_never_kills(monkeypatch, kills, idle):
    _threshold(500)

    def boom():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(memory, "update_peak_rss", boom)
    monkeypatch.setattr(memory, "get_rss_mb", lambda: 900.0)
    assert (
        await M.MEMORY_CHECK.run_once(lambda: M.memory_check_iteration(asyncio.Semaphore(1)))
        is None
    )
    assert kills == []
    st = M.MEMORY_CHECK.status()
    assert st["errors"] == 1 and st["last_error_type"] == "RuntimeError"


async def test_failed_check_with_high_rss_restarts_only_when_enabled_and_idle(
    monkeypatch, kills, idle
):
    monkeypatch.setenv(M.RESTART_ENV, "true")
    _threshold(500)

    def boom():
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(memory, "update_peak_rss", boom)
    monkeypatch.setattr(memory, "get_rss_mb", lambda: 900.0)
    sem = asyncio.Semaphore(1)
    await sem.acquire()
    await M.MEMORY_CHECK.run_once(lambda: M.memory_check_iteration(sem))
    assert kills == []  # busy: deferred
    sem.release()
    await M.MEMORY_CHECK.run_once(lambda: M.memory_check_iteration(sem))
    assert _signals(kills) == [signal.SIGTERM]


def test_no_sys_exit_in_the_loop_module():
    from pathlib import Path

    src = Path(M.__file__).read_text()
    assert "sys.exit(" not in src


# ── schedule ─────────────────────────────────────────────────────────────


def test_schedule_and_first_run_delay():
    from oncofiles.tools.curated import STARTUP_GRACE_S

    assert M.INTERVAL_S == 300
    assert M.FIRST_RUN_DELAY_S == 420  # the former start_date (#366)
    assert M.FIRST_RUN_DELAY_S >= STARTUP_GRACE_S
    assert M.MEMORY_CHECK.schedule_text == "every 5 min"


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


async def test_loop_waits_seven_minutes_then_every_five(monkeypatch, kills, idle):
    _threshold(500)
    _rss(monkeypatch, 150.0)
    clock = _Clock(stop_after=3)
    sem = asyncio.Semaphore(1)
    with pytest.raises(asyncio.CancelledError):
        await M.MEMORY_CHECK.loop(
            lambda: M.memory_check_iteration(sem),
            grace_s=M.FIRST_RUN_DELAY_S,
            sleep=clock.sleep,
            now_fn=clock,
        )
    assert clock.sleeps == [420, 300, 300, 300]
    st = M.MEMORY_CHECK.status()
    assert st["runs"] == 3 and st["errors"] == 0
    assert st["last_summary"]["rss_mb"] == 150 and st["last_summary"]["would_restart"] == 0
    assert kills == []


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


async def test_loop_runs_in_its_local_only_zone_with_the_sync_semaphore(
    monkeypatch,
    lifespan_env,  # noqa: F811
):
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_loop(semaphore):
        seen["zone"] = (local_only_job(), semaphore)

    monkeypatch.setattr(M, "memory_check_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["zone"] == (JOB_ID, server._sync_semaphore)


# ── telemetry ────────────────────────────────────────────────────────────


async def test_readiness_carries_the_memory_report(monkeypatch, kills, idle):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    _threshold(1228, "cgroup")
    _rss(monkeypatch, 210.0)
    await M.MEMORY_CHECK.run_once(lambda: M.memory_check_iteration(asyncio.Semaphore(1)))
    request, _ = _request(operator=True)
    job = json.loads((await server.readiness(request)).body)["jobs"][JOB_ID]
    assert job["schedule"] == "every 5 min" and job["runs"] == 1
    mem = job["memory"]
    assert mem["rss_mb"] == 210 and mem["threshold_mb"] == 1228
    assert mem["threshold_source"] == "cgroup" and mem["restart_enabled"] is False
    assert mem["would_restart"] is False and mem["outcome"] is None
    assert "last_deleted" not in job


def test_admin_job_list_shows_the_loop():
    assert JOB_ID in scheduled_jobs.BACKGROUND_LOOP_JOBS
    entry = scheduled_jobs.background_loop_entry(
        JOB_ID, running=True, state=scheduled_jobs.background_loop_state(JOB_ID)
    )
    assert entry["registered"] is True and entry["known"] is True
    assert entry["trigger"]["kind"] == "interval" and entry["trigger"]["seconds"] == 300
