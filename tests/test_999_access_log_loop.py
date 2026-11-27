"""#999 job 3: the #868 access log retention runs without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so the daily
``dashboard_access_log_retention`` job, registered in APScheduler, never ran
there. It is now a lifespan loop (``oncofiles.access_log_loop`` on
``oncofiles.interval_job.DailyJob``), its only owner. Covered:

* the daily slot rule (shared with the #856 erase retention): before the slot
  it waits, past the slot it catches up once, then tomorrow; at most one run
  per process and UTC day;
* the loop starts with ``SYNC_ENABLED=false``, is not an APScheduler job with
  true (never twice), runs in its local-only zone, the purge under
  ``_sync_semaphore``;
* a failing run is recorded (class only) and the loop goes on;
* operator ``/readiness.jobs.dashboard_access_log_retention`` (incl.
  ``last_deleted``) and the admin job list carry its telemetry;
* ``oncofiles.job_dryrun dashboard_access_log_retention`` counts what one
  real run deletes (the shared predicate), read-only, offline, no config.

Synthetic only: rows carry no patient, no e-mail; fixed times are in 2027.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import (
    access_log_loop,
    access_log_sql,
    dashboard_audit,
    erase_retention,
    interval_job,
    job_dryrun,
    scheduled_jobs,
    scheduler_singleton,
)
from oncofiles.cloud_guard import local_only_job
from oncofiles.database import Database
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

JOB_ID = "dashboard_access_log_retention"
FMT = access_log_sql.CUTOFF_FORMAT
NOW = "2027-05-10T12:00:00Z"
NOW_DT = datetime.strptime(NOW, FMT).replace(tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(dashboard_audit.RETENTION_ENV, raising=False)
    scheduler_singleton._reset_for_tests()
    access_log_loop._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    access_log_loop._reset_for_tests()


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(FMT)


async def _seed(db, ages_days: list[float], *, now: datetime) -> None:
    await db.db.executemany(
        "INSERT INTO dashboard_access_log (event, caller_hash, is_admin, route, created_at) "
        "VALUES ('sign_in', 'synthhash', 0, 'dashboard_verify', ?)",
        [(_ts(now - timedelta(days=a)),) for a in ages_days],
    )
    await db.db.commit()


async def _count(db) -> int:
    async with db.db.execute("SELECT COUNT(*) AS n FROM dashboard_access_log") as cur:
        return (await cur.fetchone())["n"]


async def _file_db(path: Path, ages_days: list[float], *, now: datetime) -> Path:
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    await _seed(db, ages_days, now=now)
    await db.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size == 0:
        wal.unlink()
    return path


# ── the daily slot ───────────────────────────────────────────────────────


DAY = datetime.strptime("2027-05-10", "%Y-%m-%d").replace(tzinfo=UTC)
SLOT = DAY.replace(hour=access_log_loop.SLOT_HOUR_UTC, minute=access_log_loop.SLOT_MINUTE_UTC)
TOMORROW_SLOT = SLOT + timedelta(days=1)
HOUR = timedelta(hours=1)
BEFORE = SLOT - 2 * HOUR  # early morning, before the slot
LATE = SLOT + 5 * HOUR  # after the slot (a restart past it)


def test_slot_rule_before_at_after_and_once_per_day():
    job = access_log_loop.ACCESS_LOG
    assert job.next_run(BEFORE) == SLOT  # before: today's slot
    assert job.next_run(SLOT) == SLOT  # at the slot: now
    assert job.next_run(LATE) == LATE  # past it, not run today: catch-up now
    job._last_run_day = LATE.date()
    assert job.next_run(LATE + HOUR) == TOMORROW_SLOT  # run today: tomorrow


def test_erase_retention_uses_the_same_slot_rule():
    for now in (BEFORE, SLOT + HOUR / 6, DAY + 23 * HOUR):
        for last in (None, now.date()):
            assert erase_retention.next_run_at(now, last) == interval_job.daily_next_run(
                now, last, erase_retention.SLOT_HOUR_UTC, erase_retention.SLOT_MINUTE_UTC
            )


class _Clock:
    """Fake time: each sleep advances it; the loop stops after ``stop_after`` sleeps."""

    def __init__(self, start: datetime, stop_after: int) -> None:
        self.now = start
        self.sleeps: list[float] = []
        self.stop_after = stop_after

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > self.stop_after:
            raise asyncio.CancelledError
        self.now += timedelta(seconds=seconds)


async def test_loop_catches_up_once_after_the_grace_then_waits_for_tomorrow():
    runs: list[datetime] = []
    clock = _Clock(LATE, stop_after=3)

    async def fn():
        runs.append(clock.now)
        return {"deleted": 2, "retention_days": 90}

    with pytest.raises(asyncio.CancelledError):
        await access_log_loop.ACCESS_LOG.loop(fn, grace_s=300, sleep=clock.sleep, now_fn=clock)
    assert runs == [LATE + timedelta(seconds=300)]  # once, right after the grace
    assert clock.sleeps[0] == 300
    assert all(s <= interval_job.DAILY_MAX_SLEEP_S for s in clock.sleeps[1:])
    st = access_log_loop.ACCESS_LOG.status()
    assert st["runs"] == 1 and st["last_deleted"] == 2
    assert st["next_run_at"] == TOMORROW_SLOT.isoformat(timespec="seconds")
    assert st["schedule"] == "daily 04:25 UTC"


async def test_loop_waits_for_the_slot_when_started_before_it():
    runs: list[datetime] = []
    clock = _Clock(BEFORE, stop_after=5)

    async def fn():
        runs.append(clock.now)
        return {"deleted": 0, "retention_days": 90}

    with pytest.raises(asyncio.CancelledError):
        await access_log_loop.ACCESS_LOG.loop(fn, grace_s=300, sleep=clock.sleep, now_fn=clock)
    assert runs == [SLOT]


async def test_a_failing_run_is_recorded_and_the_loop_goes_on(caplog):
    clock = _Clock(LATE, stop_after=2)

    async def fn():
        raise sqlite3.OperationalError("synthetic secret detail")

    caplog.set_level("ERROR", logger="oncofiles.interval_job")
    with pytest.raises(asyncio.CancelledError):
        await access_log_loop.ACCESS_LOG.loop(fn, grace_s=300, sleep=clock.sleep, now_fn=clock)
    st = access_log_loop.ACCESS_LOG.status()
    assert st["errors"] == 1 and st["last_error_type"] == "OperationalError"
    assert st["last_deleted"] is None
    assert "dashboard_access_log_retention: iteration failed (OperationalError)" in caplog.text
    assert "secret" not in caplog.text


# ── one run: the purge under the semaphore, count only ───────────────────


async def test_iteration_purges_under_the_semaphore(db, monkeypatch, caplog):
    now = datetime.now(UTC)
    await _seed(db, [150, 150, 100, 3], now=now)
    sem = asyncio.Semaphore(1)
    held: list[bool] = []
    real = dashboard_audit.purge_expired_access_log

    async def spy(*args, **kwargs):
        held.append(sem.locked())
        return await real(*args, **kwargs)

    monkeypatch.setattr(dashboard_audit, "purge_expired_access_log", spy)
    caplog.set_level("INFO", logger="oncofiles.access_log_loop")
    out = await access_log_loop.access_log_iteration(db, sem)
    assert out == {"deleted": 3, "retention_days": 90}
    assert held == [True]
    assert await _count(db) == 1
    assert "removed 3 rows older than 90 days" in caplog.text


async def test_iteration_clamps_a_low_env_like_the_purge(db, monkeypatch):
    monkeypatch.setenv(dashboard_audit.RETENTION_ENV, "5")
    now = datetime.now(UTC)
    await _seed(db, [40, 20], now=now)
    out = await access_log_loop.access_log_iteration(db, asyncio.Semaphore(1))
    assert out == {"deleted": 1, "retention_days": 30}


# ── lifespan: started whatever SYNC_ENABLED says, never twice ────────────


async def test_loop_starts_with_sync_disabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    async with server.lifespan(MagicMock()):
        assert scheduler_singleton.current_scheduler() is None
        assert scheduler_singleton.background_loop_running(JOB_ID)
        task = scheduler_singleton._loops[JOB_ID]
    await asyncio.sleep(0)
    assert task.cancelled() or task.done()
    assert not scheduler_singleton.background_loop_running(JOB_ID)


async def test_not_an_apscheduler_job_with_sync_enabled(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    async with server.lifespan(MagicMock()):
        sched, _ = scheduler_singleton.current_scheduler()
        assert JOB_ID not in {j.id for j in sched.get_jobs()}
        async with server.lifespan(MagicMock()):  # a second lifespan reuses it
            assert scheduler_singleton._loop_refs[JOB_ID] == 2
        assert scheduler_singleton.background_loop_running(JOB_ID)
    await asyncio.sleep(0)
    assert not scheduler_singleton.background_loop_running(JOB_ID)


async def test_loop_runs_in_its_local_only_zone(monkeypatch, lifespan_env):  # noqa: F811
    server = lifespan_env
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    seen: dict = {}

    async def fake_loop(db, semaphore):
        seen["zone"] = (local_only_job(), semaphore)

    monkeypatch.setattr(access_log_loop, "access_log_retention_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["zone"] == (JOB_ID, server._sync_semaphore)


def test_the_734_scan_reaches_the_purge_from_the_loop():
    from tests.test_no_automatic_deletion import (
        JOB_SQL_ALLOWLIST,
        _job_offenders,
        _job_registrations,
        _load_modules,
    )

    modules = _load_modules()
    roots, ids = _job_registrations(modules["oncofiles.server"])
    assert "access_log_retention_loop" in roots and JOB_ID not in ids
    original = set(JOB_SQL_ALLOWLIST)
    try:
        JOB_SQL_ALLOWLIST.discard("dashboard_access_log")
        offenders = _job_offenders(modules)
    finally:
        JOB_SQL_ALLOWLIST.clear()
        JOB_SQL_ALLOWLIST.update(original)
    assert (
        "dashboard_audit.py",
        "purge_expired_access_log",
        "DELETE FROM dashboard_access_log",
    ) in offenders


# ── telemetry: operator /readiness + admin job list ──────────────────────


async def test_readiness_carries_loop_telemetry_with_last_deleted(monkeypatch):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)

    async def one():
        return {"deleted": 7, "retention_days": 90}

    await access_log_loop.ACCESS_LOG.run_once(one)
    request, _ = _request(operator=True)
    body = json.loads((await server.readiness(request)).body)
    job = body["jobs"][JOB_ID]
    assert job["loop_running"] is False
    assert job["schedule"] == "daily 04:25 UTC"
    assert job["last_ok"] and job["runs"] == 1 and job["errors"] == 0
    assert job["last_deleted"] == 7
    assert job["last_summary"] == {"deleted": 7, "retention_days": 90}
    public, _ = _request(operator=False)
    assert "jobs" not in json.loads((await server.readiness(public)).body)


def test_admin_job_list_shows_the_loop():
    assert JOB_ID in scheduled_jobs.BACKGROUND_LOOP_JOBS
    entry = scheduled_jobs.background_loop_entry(
        JOB_ID, running=True, state=scheduled_jobs.background_loop_state(JOB_ID)
    )
    assert entry["registered"] is True and entry["known"] is True
    assert entry["trigger"] == {"kind": "cron", "cadence": "daily", "text": "daily 04:25 UTC"}
    off = scheduled_jobs.background_loop_entry(JOB_ID, running=False, state={})
    assert off["reason"] == "loop_not_running"


# ── job_dryrun dashboard_access_log_retention ────────────────────────────


async def test_dry_run_counts_match_one_real_run(tmp_path):
    now = datetime.now(UTC)
    ages = [500, 200, 120, 91, 89, 10, 1]  # the oldest: 410 days past the cutoff
    path = await _file_db(tmp_path / "copy.db", ages, now=now)
    report = job_dryrun.run(JOB_ID, path, now=now)
    assert report["would_delete"] == 4 and report["rows_total"] == 7
    assert report["retention_days"] == 90 and report["retention_clamped"] is False
    assert report["delete_chunks"] == 1
    assert report["oldest_expired_bucket"] == "over_365_days_past_cutoff"

    db = Database(str(path))
    await db.connect()
    try:
        deleted = await dashboard_audit.purge_expired_access_log(db)
    finally:
        await db.close()
    assert deleted == report["would_delete"]


async def test_dry_run_retention_days_and_clamp(tmp_path):
    path = await _file_db(tmp_path / "copy.db", [400, 45, 35, 20], now=NOW_DT)
    assert job_dryrun.run(JOB_ID, path, now=NOW, retention_days=40)["would_delete"] == 2
    low = job_dryrun.run(JOB_ID, path, now=NOW, retention_days=7)
    assert low["retention_days"] == 30 and low["retention_clamped"] is True
    assert low["would_delete"] == 3


async def test_dry_run_on_an_empty_table(tmp_path):
    path = await _file_db(tmp_path / "copy.db", [], now=NOW_DT)
    report = job_dryrun.run(JOB_ID, path, now=NOW)
    assert report["would_delete"] == 0 and report["delete_chunks"] == 0
    assert report["oldest_expired_bucket"] is None


async def test_dry_run_is_read_only_and_leaves_no_file(tmp_path):
    path = await _file_db(tmp_path / "copy.db", [200, 10], now=NOW_DT)
    before = sorted(p.name for p in tmp_path.iterdir())
    digest = path.read_bytes()
    job_dryrun.run(JOB_ID, path, now=NOW)
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert path.read_bytes() == digest


async def test_dry_run_counts_only(tmp_path):
    path = await _file_db(tmp_path / "copy.db", [200, 10], now=NOW_DT)
    text = json.dumps(job_dryrun.run(JOB_ID, path, now=NOW))
    assert "synthhash" not in text and "dashboard_verify" not in text and "2027" not in text


def test_helper_shares_the_production_predicate():
    src = Path(dashboard_audit.__file__).read_text()
    dry = Path(job_dryrun.__file__).read_text()
    assert "access_log_sql.EXPIRED_WHERE" in src and "access_log_sql.cutoff(" in src
    assert "q.EXPIRED_WHERE" in dry and "q.cutoff(" in dry
    assert "created_at <" not in src  # never a second copy of the predicate


def test_cli_offline_no_config_no_socket(tmp_path):
    path = asyncio.run(_file_db(tmp_path / "copy.db", [200, 120, 10], now=NOW_DT))
    script = (
        "import json, socket, sys\n"
        "from oncofiles import job_dryrun\n"
        f"rc = job_dryrun.main(['{JOB_ID}', '--db', {str(path)!r}, '--now', {NOW!r}, "
        "'--retention-days', '100'])\n"
        "assert rc == 0\n"
        "assert 'oncofiles.config' not in sys.modules, 'config imported'\n"
        "assert 'oncofiles.server' not in sys.modules\n"
        "try:\n"
        "    socket.create_connection(('127.0.0.1', 9))\n"
        "except Exception as exc:\n"
        "    assert type(exc).__name__ == 'NetworkForbiddenError', type(exc).__name__\n"
        "else:\n"
        "    raise AssertionError('socket was not blocked')\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path)}
    out = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    report = json.loads(out.stdout)
    assert report["job"] == JOB_ID
    assert report["retention_days"] == 100 and report["would_delete"] == 2
