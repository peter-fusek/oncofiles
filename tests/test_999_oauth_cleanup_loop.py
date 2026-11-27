"""#999 job 4: the MCP OAuth token cleanup runs without APScheduler.

Production runs ``SYNC_ENABLED=false`` (#784), so the daily
``oauth_token_cleanup`` job, registered in APScheduler, never ran there. It is
now a lifespan loop (``oncofiles.oauth_cleanup_loop`` on
``oncofiles.interval_job.DailyJob``), its only owner. Covered:

* the daily slot (the former cron slot) and once per UTC day;
* the delete keeps its semantics: INTEGER ``expires_at`` against an integer
  cutoff with the 30-day grace, rows without an expiry kept;
* the loop starts with ``SYNC_ENABLED=false``, is not an APScheduler job with
  true (never twice), runs in its local-only zone, the delete under
  ``_sync_semaphore``; a failing run is recorded (class only);
* operator ``/readiness.jobs.oauth_token_cleanup`` (incl. ``last_deleted``)
  and the admin job list carry its telemetry;
* ``oncofiles.job_dryrun oauth_token_cleanup`` counts what one real run
  deletes (the shared predicate), read-only, offline, no config, counts only.

Synthetic only: token keys are invented digests; fixed times are in 2027.
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
    interval_job,
    job_dryrun,
    oauth_cleanup_loop,
    oauth_cleanup_sql,
    persistent_oauth,
    scheduled_jobs,
    scheduler_singleton,
)
from oncofiles.cloud_guard import local_only_job
from oncofiles.database import Database
from tests.test_856_erase_retention_loop import lifespan_env  # noqa: F401 - fixture

JOB_ID = "oauth_token_cleanup"
NOW_DT = datetime(2027, 5, 10, 12, 0, tzinfo=UTC)
NOW = "2027-05-10T12:00:00Z"
DAY_S = 86400


@pytest.fixture(autouse=True)
def _clean():
    scheduler_singleton._reset_for_tests()
    oauth_cleanup_loop._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()
    oauth_cleanup_loop._reset_for_tests()


async def _seed(db, rows: list[tuple[str, int | None]], *, now: datetime) -> None:
    """rows: (token_type, expiry offset in days from ``now`` or None)."""
    epoch = int(now.timestamp())
    await db.db.executemany(
        "INSERT INTO mcp_oauth_tokens (token, token_type, client_id, expires_at) "
        "VALUES (?, ?, 'synthclient', ?)",
        [
            (f"sha256:synth{i:04d}", kind, None if off is None else epoch + off * DAY_S)
            for i, (kind, off) in enumerate(rows)
        ],
    )
    await db.db.commit()


async def _count(db) -> int:
    async with db.db.execute("SELECT COUNT(*) AS n FROM mcp_oauth_tokens") as cur:
        return (await cur.fetchone())["n"]


async def _file_db(path: Path, rows, *, now: datetime) -> Path:
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    await _seed(db, rows, now=now)
    await db.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    await db.close()
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size == 0:
        wal.unlink()
    return path


# Synthetic mix: two long expired (one per type), one expired inside the
# grace, one still valid, one without an expiry.
MIX = [("access", -45), ("refresh", -31), ("access", -29), ("refresh", 20), ("access", None)]


# ── the daily slot ───────────────────────────────────────────────────────


SLOT = NOW_DT.replace(
    hour=oauth_cleanup_loop.SLOT_HOUR_UTC, minute=oauth_cleanup_loop.SLOT_MINUTE_UTC
)


def test_slot_is_the_former_cron_slot_once_per_day():
    job = oauth_cleanup_loop.OAUTH_CLEANUP
    assert (job.hour, job.minute) == (4, 0)
    assert job.schedule_text == "daily 04:00 UTC"
    assert job.next_run(SLOT - timedelta(hours=1)) == SLOT
    late = SLOT + timedelta(hours=3)
    assert job.next_run(late) == late  # catch-up after a restart past the slot
    job._last_run_day = late.date()
    assert job.next_run(late) == SLOT + timedelta(days=1)


class _Clock:
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


async def test_loop_catches_up_once_after_the_grace():
    runs: list[datetime] = []
    clock = _Clock(SLOT + timedelta(hours=2), stop_after=3)

    async def fn():
        runs.append(clock.now)
        return {"deleted": 3}

    with pytest.raises(asyncio.CancelledError):
        await oauth_cleanup_loop.OAUTH_CLEANUP.loop(
            fn, grace_s=300, sleep=clock.sleep, now_fn=clock
        )
    assert len(runs) == 1
    assert all(s <= interval_job.DAILY_MAX_SLEEP_S for s in clock.sleeps[1:])
    st = oauth_cleanup_loop.OAUTH_CLEANUP.status()
    assert st["runs"] == 1 and st["last_deleted"] == 3


async def test_a_failing_run_is_recorded_and_the_loop_goes_on(caplog):
    clock = _Clock(SLOT + timedelta(hours=2), stop_after=2)

    async def fn():
        raise sqlite3.OperationalError("synthetic secret detail")

    caplog.set_level("ERROR", logger="oncofiles.interval_job")
    with pytest.raises(asyncio.CancelledError):
        await oauth_cleanup_loop.OAUTH_CLEANUP.loop(
            fn, grace_s=300, sleep=clock.sleep, now_fn=clock
        )
    st = oauth_cleanup_loop.OAUTH_CLEANUP.status()
    assert st["errors"] == 1 and st["last_error_type"] == "OperationalError"
    assert "secret" not in caplog.text


# ── the delete: same semantics, under the semaphore ──────────────────────


async def test_delete_keeps_its_semantics(db):
    await _seed(db, MIX, now=NOW_DT)
    assert await persistent_oauth.delete_expired_oauth_tokens(db, now=NOW_DT) == 2
    assert await _count(db) == 3  # inside the grace, valid and no expiry stay
    assert persistent_oauth.EXPIRED_TOKEN_GRACE_S == 30 * DAY_S


async def test_iteration_deletes_under_the_semaphore(db, monkeypatch, caplog):
    await _seed(db, MIX, now=datetime.now(UTC))
    sem = asyncio.Semaphore(1)
    held: list[bool] = []
    real = persistent_oauth.delete_expired_oauth_tokens

    async def spy(*args, **kwargs):
        held.append(sem.locked())
        return await real(*args, **kwargs)

    monkeypatch.setattr(persistent_oauth, "delete_expired_oauth_tokens", spy)
    caplog.set_level("INFO", logger="oncofiles.oauth_cleanup_loop")
    out = await oauth_cleanup_loop.oauth_cleanup_iteration(db, sem)
    assert out == {"deleted": 2} and held == [True]
    assert "removed 2 expired tokens" in caplog.text
    assert "synth" not in caplog.text


# ── lifespan: started whatever SYNC_ENABLED says, never twice ────────────


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
        async with server.lifespan(MagicMock()):
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

    monkeypatch.setattr(oauth_cleanup_loop, "oauth_token_cleanup_loop", fake_loop)
    async with server.lifespan(MagicMock()):
        await asyncio.sleep(0)
    assert seen["zone"] == (JOB_ID, server._sync_semaphore)


def test_the_734_scan_reaches_the_delete_from_the_loop():
    from tests.test_no_automatic_deletion import (
        JOB_SQL_ALLOWLIST,
        _job_offenders,
        _job_registrations,
        _load_modules,
    )

    modules = _load_modules()
    roots, ids = _job_registrations(modules["oncofiles.server"])
    assert "oauth_token_cleanup_loop" in roots and JOB_ID not in ids
    original = set(JOB_SQL_ALLOWLIST)
    try:
        JOB_SQL_ALLOWLIST.discard("mcp_oauth_tokens")
        offenders = _job_offenders(modules)
    finally:
        JOB_SQL_ALLOWLIST.clear()
        JOB_SQL_ALLOWLIST.update(original)
    assert any(
        f == "persistent_oauth.py" and fn == "delete_expired_oauth_tokens" for f, fn, _ in offenders
    )


# ── telemetry ────────────────────────────────────────────────────────────


async def test_readiness_carries_loop_telemetry_with_last_deleted(monkeypatch):
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)

    async def one():
        return {"deleted": 4}

    await oauth_cleanup_loop.OAUTH_CLEANUP.run_once(one)
    request, _ = _request(operator=True)
    job = json.loads((await server.readiness(request)).body)["jobs"][JOB_ID]
    assert job["schedule"] == "daily 04:00 UTC"
    assert job["runs"] == 1 and job["last_deleted"] == 4
    assert job["last_summary"] == {"deleted": 4}


def test_admin_job_list_shows_the_loop():
    assert JOB_ID in scheduled_jobs.BACKGROUND_LOOP_JOBS
    entry = scheduled_jobs.background_loop_entry(
        JOB_ID, running=True, state=scheduled_jobs.background_loop_state(JOB_ID)
    )
    assert entry["registered"] is True and entry["known"] is True
    assert entry["trigger"]["text"] == "daily 04:00 UTC"


# ── job_dryrun oauth_token_cleanup ───────────────────────────────────────


async def test_dry_run_counts_match_one_real_run(tmp_path):
    path = await _file_db(tmp_path / "copy.db", MIX, now=NOW_DT)
    report = job_dryrun.run(JOB_ID, path, now=NOW)
    assert report["rows_total"] == 5 and report["would_delete"] == 2
    assert report["would_delete_by_type"] == {"access": 1, "refresh": 1}
    assert report["kept_no_expiry"] == 1
    assert report["kept_expired_within_grace"] == 1 and report["kept_valid"] == 1
    assert report["grace_days"] == 30

    db = Database(str(path))
    await db.connect()
    try:
        deleted = await persistent_oauth.delete_expired_oauth_tokens(db, now=NOW_DT)
    finally:
        await db.close()
    assert deleted == report["would_delete"]


async def test_dry_run_is_read_only_and_counts_only(tmp_path):
    path = await _file_db(tmp_path / "copy.db", MIX, now=NOW_DT)
    before = sorted(p.name for p in tmp_path.iterdir())
    digest = path.read_bytes()
    text = json.dumps(job_dryrun.run(JOB_ID, path, now=NOW))
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert path.read_bytes() == digest
    assert "synth" not in text and "sha256" not in text


def test_helper_shares_the_production_predicate():
    src = Path(persistent_oauth.__file__).read_text()
    dry = Path(job_dryrun.__file__).read_text()
    assert "oauth_cleanup_sql.EXPIRED_WHERE" in src and "oauth_cleanup_sql.cutoff(" in src
    assert "q.EXPIRED_WHERE" in dry and "q.cutoff(" in dry
    assert "strftime('%s'" not in src  # never a second copy of the predicate
    assert oauth_cleanup_sql.cutoff(NOW_DT) == int(NOW_DT.timestamp()) - 30 * DAY_S


def test_cli_offline_no_config_no_socket(tmp_path):
    path = asyncio.run(_file_db(tmp_path / "copy.db", MIX, now=NOW_DT))
    script = (
        "import json, socket, sys\n"
        "from oncofiles import job_dryrun\n"
        f"rc = job_dryrun.main(['{JOB_ID}', '--db', {str(path)!r}, '--now', {NOW!r}])\n"
        "assert rc == 0\n"
        "assert 'oncofiles.config' not in sys.modules, 'config imported'\n"
        "assert 'oncofiles.persistent_oauth' not in sys.modules\n"
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
    assert report["job"] == JOB_ID and report["would_delete"] == 2
