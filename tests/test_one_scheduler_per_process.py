"""#784 (c): exactly one APScheduler and one curated loop per process.

* Within a process, starting either twice returns the running one.
* Operator /readiness exposes a ``scheduler`` block (instance id, pid,
  started_at, job count, curated loop) so infra can verify after a rolling
  deploy that one instance serves; public /readiness never shows it.
* Across processes, the startup grace covers the rolling-deploy overlap: no
  scheduled job's first run falls inside ``STARTUP_GRACE_S``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import scheduler_singleton

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
OPERATOR_TOKEN = "synthetic-operator-token-784"


@pytest.fixture(autouse=True)
def _clean_registry():
    scheduler_singleton._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()


def _start(monkeypatch, *, nightly_only: bool = True, run_on_startup: bool = False):
    import oncofiles.server as server

    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_NIGHTLY_ONLY", nightly_only)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", run_on_startup)
    return server._start_sync_scheduler(
        MagicMock(), MagicMock(), None, "", MagicMock(), MagicMock(), primary_uuid=""
    )


# ── 1. idempotent start within a process ──────────────────────────────────


async def test_second_start_returns_the_running_scheduler(monkeypatch, caplog):
    first, tracker1 = _start(monkeypatch)
    jobs = len(first.get_jobs())
    with caplog.at_level(logging.WARNING, logger="oncofiles.scheduler_singleton"):
        second, tracker2 = _start(monkeypatch)
        third, _ = _start(monkeypatch)
    assert second is first and third is first
    assert tracker2 is tracker1
    assert len(first.get_jobs()) == jobs, "no job registered twice"
    dup = [r for r in caplog.records if "already running" in r.getMessage()]
    assert len(dup) == 1, "logged once, not on every call"

    # Reference counted: only the last holder may shut it down.
    assert scheduler_singleton.release_scheduler(first) is False
    assert scheduler_singleton.release_scheduler(first) is False
    assert scheduler_singleton.release_scheduler(first) is True
    first.shutdown(wait=False)


async def test_a_stopped_scheduler_can_be_replaced(monkeypatch):
    first, _ = _start(monkeypatch)
    first.shutdown(wait=False)
    await asyncio.sleep(0)  # AsyncIOScheduler queues the stop on its loop
    second, _ = _start(monkeypatch)
    assert second is not first
    assert second.running


async def test_register_refuses_a_second_live_scheduler(monkeypatch):
    _start(monkeypatch)
    other = SimpleNamespace(running=True)
    with pytest.raises(RuntimeError, match="second APScheduler"):
        scheduler_singleton.register_scheduler(other, {})


async def test_curated_loop_starts_once(caplog):
    created: list[asyncio.Task] = []

    def factory():
        task = asyncio.create_task(asyncio.sleep(3600))
        created.append(task)
        return task

    with caplog.at_level(logging.WARNING, logger="oncofiles.scheduler_singleton"):
        task_a, started_a = scheduler_singleton.ensure_curated_loop(factory)
        task_b, started_b = scheduler_singleton.ensure_curated_loop(factory)
    assert (started_a, started_b) == (True, False)
    assert task_b is task_a and len(created) == 1
    assert scheduler_singleton.curated_loop_running()
    assert sum("already running" in r.getMessage() for r in caplog.records) == 1

    assert scheduler_singleton.release_curated_loop(task_a) is False
    assert scheduler_singleton.release_curated_loop(task_a) is True
    task_a.cancel()
    await asyncio.gather(task_a, return_exceptions=True)
    assert not scheduler_singleton.curated_loop_running()

    # A finished loop is replaced, not reused.
    task_c, started_c = scheduler_singleton.ensure_curated_loop(factory)
    assert started_c and task_c is not task_a
    task_c.cancel()
    await asyncio.gather(task_c, return_exceptions=True)


def test_scheduler_and_curated_loop_have_one_construction_site():
    """Nothing else in src/ may build an APScheduler or spawn the curated loop."""
    sched_sites, loop_sites = [], []
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        sched_sites += [path.name for _ in re.finditer(r"\w*Scheduler\(\)", text)]
        loop_sites += [
            path.name for _ in re.finditer(r"create_task\(\s*curated_nightly_loop\(", text)
        ]
    assert sched_sites == ["server.py"]
    assert loop_sites == ["server.py"]
    server_src = (SRC / "server.py").read_text()
    assert "ensure_curated_loop(\n            lambda: asyncio.create_task(curated_nightly_loop" in (
        server_src
    )
    assert "scheduler_singleton.existing_scheduler()" in server_src


# ── 2. operator /readiness.scheduler ──────────────────────────────────────


def _request(*, operator: bool):
    db = MagicMock()
    db.ping = AsyncMock(return_value=None)
    lifespan = {"db": db, "job_tracker": {}}
    app = SimpleNamespace(state=SimpleNamespace(fastmcp_server=SimpleNamespace()))
    app.state.fastmcp_server._lifespan_result = lifespan
    headers = {"authorization": f"Bearer {OPERATOR_TOKEN}"} if operator else {}
    return SimpleNamespace(app=app, headers=headers), db


async def test_readiness_scheduler_block_for_operator(monkeypatch):
    import oncofiles.server as server

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    sched, _ = _start(monkeypatch)
    loop_task, _ = scheduler_singleton.ensure_curated_loop(
        lambda: asyncio.create_task(asyncio.sleep(3600))
    )
    try:
        request, _db = _request(operator=True)
        body = json.loads((await server.readiness(request)).body)
    finally:
        loop_task.cancel()
        await asyncio.gather(loop_task, return_exceptions=True)

    block = body["scheduler"]
    assert block["instance_id"] == scheduler_singleton.INSTANCE_ID
    assert re.fullmatch(r"[0-9a-f]{12}", block["instance_id"])
    assert block["pid"] > 0
    assert block["running"] is True
    assert block["started_at"]
    assert block["job_count"] == len(sched.get_jobs()) > 0
    assert block["curated_loop_running"] is True


async def test_readiness_scheduler_block_absent_for_public(monkeypatch):
    import oncofiles.server as server

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    _start(monkeypatch)
    request, _db = _request(operator=False)
    body = json.loads((await server.readiness(request)).body)
    assert "scheduler" not in body
    assert set(body) == {"status", "version", "db"}


async def test_scheduler_status_needs_no_db():
    """The block is pure in-memory state: no database argument, no I/O."""
    status = scheduler_singleton.status()
    assert status["running"] is False
    assert status["job_count"] == 0
    assert status["curated_loop_running"] is False
    assert status["instance_id"] == scheduler_singleton.INSTANCE_ID


def test_health_does_not_expose_scheduler_block():
    import inspect

    import oncofiles.server as server

    assert "scheduler_singleton" not in inspect.getsource(server.health)


# ── 3. no first job slot inside the startup grace ─────────────────────────


@pytest.mark.parametrize(
    ("nightly_only", "run_on_startup"),
    [(True, False), (True, True), (False, False)],
    ids=["nightly", "nightly+run-on-startup", "legacy"],
)
async def test_every_job_first_runs_after_the_startup_grace(
    monkeypatch, gmail_calendar_on, nightly_only, run_on_startup
):
    from oncofiles.tools.curated import STARTUP_GRACE_S

    before = datetime.now().astimezone()
    sched, _ = _start(monkeypatch, nightly_only=nightly_only, run_on_startup=run_on_startup)
    floor = before + timedelta(seconds=STARTUP_GRACE_S)
    early = {
        job.id: job.next_run_time
        for job in sched.get_jobs()
        if job.next_run_time is not None and job.next_run_time < floor
    }
    assert not early, f"jobs would fire inside the startup grace: {early}"
    ids = {job.id for job in sched.get_jobs()}
    # The jobs that used to fire before the grace are still scheduled (deferred, not dropped).
    assert "startup_catchup" in ids
    if run_on_startup:
        assert "nightly_ai_oneoff" in ids
    if not nightly_only:
        assert {"gmail_startup_sync", "calendar_startup_sync", "gdrive_sync"} <= ids


async def test_a_cron_slot_inside_the_grace_is_deferred_not_skipped():
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    import oncofiles.server as server

    sched = AsyncIOScheduler()
    soon = datetime.now() + timedelta(minutes=2)
    sched.add_job(
        lambda: None,
        CronTrigger(hour=soon.hour, minute=soon.minute),
        id="nightly_like",
    )
    sched.add_job(lambda: None, CronTrigger(hour=(soon.hour + 12) % 24), id="far")
    sched.start()
    try:
        far_before = sched.get_job("far").next_run_time
        deferred = server._defer_jobs_past_startup_grace(sched, grace_s=300)
        assert deferred == ["nightly_like"]
        nrt = sched.get_job("nightly_like").next_run_time
        now = datetime.now(sched.timezone)
        # Runs once the grace is over (today), not tomorrow.
        assert timedelta(seconds=290) < nrt - now <= timedelta(seconds=300)
        assert sched.get_job("far").next_run_time == far_before
    finally:
        sched.shutdown(wait=False)
