"""#868: 90-day retention of ``dashboard_access_log``.

Maintainer decision 2026-11-16: access log rows are deleted after 90 days
(``DASHBOARD_ACCESS_LOG_RETENTION_DAYS``, minimum 30). Only that table, in
chunks of at most 500 rows committed one by one, by the daily
``dashboard_access_log_retention`` job. This is the one #734 allowlist entry
for the table (SQL + scheduler reachability).

Synthetic rows only: no patients, no e-mails.
"""

from __future__ import annotations

import inspect
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from apscheduler.triggers.cron import CronTrigger

from oncofiles import dashboard_audit, scheduler_singleton, server
from tests import test_no_automatic_deletion as lock

JOB_ID = "dashboard_access_log_retention"
BELOW_MINIMUM = ["-7", "0", "12", "29"]


def _iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


async def _insert(db, days_ago: float, n: int = 1) -> None:
    await db.db.executemany(
        "INSERT INTO dashboard_access_log (event, caller_hash, is_admin, route, created_at) "
        "VALUES ('sign_in', 'h', 0, 'dashboard_verify', ?)",
        [(_iso(days_ago),)] * n,
    )
    await db.db.commit()


async def _ages(db) -> list[str]:
    async with db.db.execute(
        "SELECT created_at FROM dashboard_access_log ORDER BY created_at"
    ) as cur:
        return [r["created_at"] for r in await cur.fetchall()]


async def _table_counts(db) -> dict[str, int]:
    async with db.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ) as cur:
        names = [r["name"] for r in await cur.fetchall()]
    counts = {}
    for name in names:
        async with db.db.execute(f'SELECT COUNT(*) AS n FROM "{name}"') as cur:
            counts[name] = (await cur.fetchone())["n"]
    return counts


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv(dashboard_audit.RETENTION_ENV, raising=False)


# ── deletion window ───────────────────────────────────────────────────────


async def test_rows_older_than_90_days_deleted_newer_kept(db):
    await _insert(db, 400)
    await _insert(db, 91)
    await _insert(db, 90.01)
    await _insert(db, 89.9)
    await _insert(db, 30)
    await _insert(db, 0)

    deleted = await dashboard_audit.purge_expired_access_log(db)

    assert deleted == 3
    remaining = await _ages(db)
    assert len(remaining) == 3
    cutoff = _iso(90)
    assert all(ts >= cutoff for ts in remaining)


async def test_nothing_to_delete_returns_zero(db):
    await _insert(db, 10, n=3)
    assert await dashboard_audit.purge_expired_access_log(db) == 0
    assert len(await _ages(db)) == 3


async def test_no_other_table_touched(db):
    await _insert(db, 200, n=5)
    await _insert(db, 1, n=2)
    before = await _table_counts(db)

    assert await dashboard_audit.purge_expired_access_log(db) == 5

    after = await _table_counts(db)
    changed = {t for t in before if before[t] != after[t]}
    assert changed == {"dashboard_access_log"}
    assert after["dashboard_access_log"] == 2


def test_purge_sql_names_only_the_access_log_table():
    source = inspect.getsource(dashboard_audit)
    tables = {m.group(1) for m in lock.DELETE_RE.finditer(source)}
    assert tables == {"dashboard_access_log"}


# ── chunking ──────────────────────────────────────────────────────────────


class _CountingConn:
    """Forward to the real aiosqlite connection, recording DELETE row counts."""

    def __init__(self, real):
        self._real = real
        self.commits = 0
        self.limits: list[int] = []

    def execute(self, sql, params=()):
        if "DELETE FROM dashboard_access_log" in sql:
            self.limits.append(params[1])
        return self._real.execute(sql, params)

    async def commit(self):
        self.commits += 1
        await self._real.commit()


async def test_chunks_of_at_most_500_each_committed(db):
    await _insert(db, 120, n=1203)
    await _insert(db, 5, n=4)
    conn = _CountingConn(db.db)

    deleted = await dashboard_audit.purge_expired_access_log(SimpleNamespace(db=conn))

    assert deleted == 1203
    # two full chunks, then the remainder
    assert conn.limits == [500] * 3
    assert conn.commits == 3
    assert len(await _ages(db)) == 4


async def test_chunk_size_never_above_500(db):
    await _insert(db, 120, n=10)
    conn = _CountingConn(db.db)
    await dashboard_audit.purge_expired_access_log(SimpleNamespace(db=conn), chunk_size=50_000)
    assert conn.limits == [500]


async def test_small_chunks_still_delete_everything_expired(db):
    await _insert(db, 120, n=7)
    conn = _CountingConn(db.db)
    deleted = await dashboard_audit.purge_expired_access_log(SimpleNamespace(db=conn), chunk_size=3)
    assert deleted == 7
    # chunks of three: two full, then the last row
    assert conn.commits == 3
    assert await _ages(db) == []


# ── retention setting ─────────────────────────────────────────────────────


def test_default_is_90_days():
    assert dashboard_audit.retention_days() == 90


@pytest.mark.parametrize("value", BELOW_MINIMUM)
def test_low_values_clamp_to_30_with_warning(monkeypatch, caplog, value):
    monkeypatch.setenv(dashboard_audit.RETENTION_ENV, value)
    with caplog.at_level(logging.WARNING, logger="oncofiles.dashboard_audit"):
        assert dashboard_audit.retention_days() == 30
    assert any("clamped to 30" in r.getMessage() for r in caplog.records)


def test_higher_values_are_kept(monkeypatch):
    monkeypatch.setenv(dashboard_audit.RETENTION_ENV, "365")
    assert dashboard_audit.retention_days() == 365


def test_garbage_falls_back_to_default(monkeypatch, caplog):
    monkeypatch.setenv(dashboard_audit.RETENTION_ENV, "ninety")
    with caplog.at_level(logging.WARNING, logger="oncofiles.dashboard_audit"):
        assert dashboard_audit.retention_days() == 90
    assert caplog.records


async def test_clamped_env_never_deletes_rows_younger_than_30_days(db, monkeypatch):
    monkeypatch.setenv(dashboard_audit.RETENTION_ENV, "1")
    await _insert(db, 31)
    await _insert(db, 29)
    await _insert(db, 2)
    assert await dashboard_audit.purge_expired_access_log(db) == 1
    assert len(await _ages(db)) == 2


async def test_explicit_days_below_minimum_clamp(db):
    await _insert(db, 20)
    assert await dashboard_audit.purge_expired_access_log(db, days=1) == 0


# ── scheduler ─────────────────────────────────────────────────────────────


@pytest.fixture
async def scheduler_with_db(monkeypatch, db):
    scheduler_singleton._reset_for_tests()
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_NIGHTLY_ONLY", True)
    scheduler, tracker = server._start_sync_scheduler(
        db, MagicMock(), None, "", MagicMock(), MagicMock(), primary_uuid=""
    )
    yield scheduler, tracker
    scheduler.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()


async def test_job_registered_with_cron_misfire_grace_and_coalesce(scheduler_with_db):
    scheduler, _ = scheduler_with_db
    job = scheduler.get_job(JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "4" and fields["minute"] == "25"
    assert str(job.trigger.timezone) == "UTC"
    assert job.misfire_grace_time == 1800
    assert job.coalesce is True
    assert job.max_instances == 1


def test_slot_is_outside_the_bratislava_night_window():
    """04:25 UTC is 05:25 (CET) or 06:25 (CEST) in Bratislava, never 00:30-02:30."""
    from zoneinfo import ZoneInfo

    for month in (1, 7):  # winter and summer time
        utc = datetime(2027, month, 15, 4, 25, tzinfo=UTC)
        local = utc.astimezone(ZoneInfo("Europe/Bratislava"))
        minutes = local.hour * 60 + local.minute
        assert not (30 <= minutes <= 150), local


async def test_job_deletes_under_semaphore_and_records_count(scheduler_with_db, db, monkeypatch):
    scheduler, tracker = scheduler_with_db
    await _insert(db, 150, n=4)
    await _insert(db, 3, n=1)

    held = []
    real_purge = dashboard_audit.purge_expired_access_log

    async def _spy(*args, **kwargs):
        held.append(server._sync_semaphore.locked())
        return await real_purge(*args, **kwargs)

    monkeypatch.setattr(dashboard_audit, "purge_expired_access_log", _spy)

    await scheduler.get_job(JOB_ID).func()

    assert held == [True]
    assert tracker[JOB_ID]["last_deleted"] == 4
    assert len(await _ages(db)) == 1


async def test_job_failure_is_raised_for_the_error_listener(scheduler_with_db, monkeypatch):
    """A failing purge must not look like a clean run (#552): the job raises."""
    scheduler, _ = scheduler_with_db

    async def _boom(*args, **kwargs):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(dashboard_audit, "purge_expired_access_log", _boom)
    with pytest.raises(RuntimeError):
        await scheduler.get_job(JOB_ID).func()


def test_readiness_jobs_expose_last_deleted():
    source = inspect.getsource(server.readiness)
    assert 'entry["last_deleted"] = info["last_deleted"]' in source


def test_job_listed_for_the_dashboard():
    assert f'"id": "{JOB_ID}"' in inspect.getsource(server)


# ── #734 lock: exactly this one entry ─────────────────────────────────────


def test_lock_allowlists_this_table_once_with_the_decision():
    entries = [k for k in lock.SQL_DELETE_ALLOWLIST if k[1] == "dashboard_access_log"]
    assert entries == [("dashboard_audit.py", "dashboard_access_log")]
    assert "maintainer decision 2026-11-16 (#868)" in lock.SQL_DELETE_ALLOWLIST[entries[0]]
    assert "dashboard_access_log" in lock.JOB_SQL_ALLOWLIST


def test_scheduler_lock_sees_the_retention_job():
    """Guard the guard: without the allowlist entry the walker flags the job."""
    modules = lock._load_modules()
    roots, ids = lock._job_registrations(modules["oncofiles.server"])
    assert "_run_dashboard_access_log_retention" in roots and JOB_ID in ids

    original = set(lock.JOB_SQL_ALLOWLIST)
    try:
        lock.JOB_SQL_ALLOWLIST.discard("dashboard_access_log")
        offenders = lock._job_offenders(modules)
    finally:
        lock.JOB_SQL_ALLOWLIST.clear()
        lock.JOB_SQL_ALLOWLIST.update(original)
    assert (
        "dashboard_audit.py",
        "purge_expired_access_log",
        "DELETE FROM dashboard_access_log",
    ) in offenders
