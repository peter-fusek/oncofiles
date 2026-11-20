"""get_pipeline_status lists the scheduler's real jobs, never a hand-written catalogue (#704)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from oncofiles import scheduler_singleton
from oncofiles.database import Database
from oncofiles.tools import hygiene as hygiene_tools


class _FakeScheduler:
    running = True

    def __init__(self, jobs):
        self._jobs = jobs

    def get_jobs(self):
        return list(self._jobs)


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


@pytest.fixture
def clean_registry():
    scheduler_singleton._reset_for_tests()
    yield
    scheduler_singleton._reset_for_tests()


async def test_no_scheduler_in_process_gives_null(db: Database, clean_registry):
    out = json.loads(await hygiene_tools.get_pipeline_status(_ctx(db)))
    assert out["scheduled_jobs"] is None


async def test_lists_exactly_the_registered_jobs(db: Database, clean_registry):
    nxt = datetime(2031, 7, 17, 13, 47, tzinfo=UTC)
    jobs = [
        SimpleNamespace(id="sync_watchdog", trigger="interval[1:00:00]", next_run_time=nxt),
        SimpleNamespace(id="memory_check", trigger="interval[0:05:00]", next_run_time=None),
    ]
    tracker = {"sync_watchdog": {"running": False, "last_ok": "2031-07-17T12:47:00+00:00"}}
    scheduler_singleton.register_scheduler(_FakeScheduler(jobs), tracker)

    out = json.loads(await hygiene_tools.get_pipeline_status(_ctx(db)))
    listed = out["scheduled_jobs"]
    assert [j["id"] for j in listed] == ["memory_check", "sync_watchdog"]
    watchdog = listed[1]
    assert watchdog["trigger"] == "interval[1:00:00]"
    assert watchdog["next_run_at"] == "2031-07-17T13:47:00+00:00"
    assert watchdog["last_ok"] == "2031-07-17T12:47:00+00:00"
    assert listed[0]["next_run_at"] is None and listed[0]["last_ok"] is None
    # the old static catalogue named jobs that no longer exist
    assert "rss_monitor" not in json.dumps(out)


def test_snapshot_takes_no_reference(clean_registry):
    sched = _FakeScheduler([])
    scheduler_singleton.register_scheduler(sched, {})
    assert scheduler_singleton.job_snapshot() == []
    # still the only holder: one release shuts it down
    assert scheduler_singleton.release_scheduler(sched) is True
