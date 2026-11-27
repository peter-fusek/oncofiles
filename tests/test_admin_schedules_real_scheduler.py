"""Admin "Scheduled server jobs" panel reflects the RUNNING scheduler.

``GET /api/schedules`` used to return a static catalogue, so production showed
legacy jobs (5-minute Drive sync, Gmail, Calendar, sweeps) as "not run yet"
although they were never registered. These tests pin the new contract:

* the job list equals the jobs of the scheduler in ``scheduler_singleton``
  (plus one-off jobs that already fired, from the tracker);
* an unknown job id is shown by its id;
* known jobs that are not registered are listed under ``inactive`` with the
  switch that keeps them off;
* the curated loop is a pseudo-entry with aggregate counters only;
* no patient id, e-mail or error message text leaves the endpoint;
* non-admins still get 403.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from starlette.responses import JSONResponse

from oncofiles import scheduled_jobs, scheduler_singleton, server
from oncofiles.database._analytics import PromptStats

PID = "00000000-0000-4000-8000-00000000c0de"
ADMIN_EMAIL = "sched-admin@example.test"
CARER_EMAIL = "sched-carer@example.test"
SECRET_MSG = f"boom for patient {PID} file 20270101_Someone_Labs.pdf"


class _FakeScheduler:
    running = True

    def __init__(self, jobs):
        self._jobs = jobs

    def get_jobs(self):
        return list(self._jobs)


def _job(job_id, trigger, next_run=None):
    return SimpleNamespace(id=job_id, trigger=trigger, next_run_time=next_run, name="_fn")


def _fake_scheduler():
    nxt = datetime(2031, 7, 19, 22, 47, tzinfo=UTC)
    return _FakeScheduler(
        [
            _job("nightly_ai_pipeline", CronTrigger(hour=23, minute=0), nxt),
            _job("sync_watchdog", IntervalTrigger(minutes=5), nxt - timedelta(hours=1)),
            _job("weekly_analytics", CronTrigger(day_of_week="sun", hour=5, minute=0), nxt),
            _job("brand_new_job", CronTrigger(hour="*/6", minute=30), None),
        ]
    )


def _tracker():
    return {
        "sync_watchdog": {"running": False, "last_ok": "2031-07-19T21:55:00+00:00"},
        "weekly_analytics": {
            "running": False,
            "last_error": "2031-07-18T05:00:01+00:00",
            "last_error_msg": SECRET_MSG,
            "last_error_type": "RuntimeError",
        },
        # A one-off DateTrigger job leaves the scheduler after it fired.
        "startup_catchup": {"running": False, "last_ok": "2031-07-19T08:06:00+00:00"},
    }


def _probe_stats():
    return {
        PID: {
            "last_probe_at": "2031-07-19T21:58:00+00:00",
            "probes_total": 40,
            "runs_blocked": {"patient_busy": 2},
            "last_auto_run_at": "2031-07-19T20:00:00+00:00",
            "auto_runs_total": 3,
            "pending_inbox_count": 4,
        },
        "00000000-0000-4000-8000-00000000beef": {
            "last_probe_at": "2031-07-19T21:59:00+00:00",
            "probes_total": 2,
            "runs_blocked": {},
            "auto_runs_total": 0,
            "pending_inbox_count": None,
        },
    }


def _request(tracker=None):
    request = MagicMock()
    request.query_params = {}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
    db = MagicMock()
    db.get_prompt_stats = AsyncMock(return_value=PromptStats())
    request.app.state.fastmcp_server._lifespan_result = {
        "db": db,
        "files": None,
        "job_tracker": tracker if tracker is not None else {},
    }
    return request


@contextmanager
def _admin(email=ADMIN_EMAIL, admin=True):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


@contextmanager
def _running(sched, tracker, *, curated_running=True):
    with (
        patch.object(scheduler_singleton, "current_scheduler", return_value=(sched, tracker)),
        patch.object(scheduler_singleton, "curated_loop_running", return_value=curated_running),
        patch("oncofiles.tools.curated.probe_stats", return_value=_probe_stats()),
        patch.object(server, "SYNC_ENABLED", True),
        patch.object(server, "AI_NIGHTLY_ONLY", True),
        patch.object(server, "ENABLE_INTEGRITY_CHECK", False),
        patch.object(server, "gmail_calendar_enabled", return_value=False),
    ):
        yield


async def _get(tracker):
    resp = await server.api_schedules(_request(tracker))
    assert resp.status_code == 200, resp.body
    return resp, json.loads(resp.body)


# ── route ────────────────────────────────────────────────────────────────


async def test_jobs_are_the_registered_jobs_plus_fired_one_offs():
    sched, tracker = _fake_scheduler(), _tracker()
    with _admin(), _running(sched, tracker):
        _, body = await _get(tracker)
    registered = {j["id"] for j in body["jobs"] if j["registered"]}
    assert registered == {
        "nightly_ai_pipeline",
        "sync_watchdog",
        "weekly_analytics",
        "brand_new_job",
    }
    once = [j for j in body["jobs"] if not j["registered"]]
    assert [j["id"] for j in once] == ["startup_catchup"]
    assert once[0]["last_ok"] and once[0]["next_run_at"] is None
    assert body["scheduler"] == {
        "running": True,
        "started_at": None,
        "job_count": 4,
        "off_reason": None,
    }
    # The legacy jobs that started this issue are NOT shown as jobs.
    ids = {j["id"] for j in body["jobs"]}
    for legacy in ("gdrive_sync", "gmail_sync", "calendar_sync", "enhancement_sweep"):
        assert legacy not in ids


async def test_inactive_lists_known_unregistered_jobs_with_reason():
    sched, tracker = _fake_scheduler(), _tracker()
    with _admin(), _running(sched, tracker):
        _, body = await _get(tracker)
    inactive = {j["id"]: j for j in body["inactive"]}
    shown = {j["id"] for j in body["jobs"]}
    assert not set(inactive) & shown
    assert set(inactive) | shown >= set(scheduled_jobs.CATALOGUE)
    assert inactive["gdrive_sync"]["reason"] == "ai_nightly_only"
    assert inactive["gdrive_sync"]["switch"] == "AI_NIGHTLY_ONLY=true"
    assert inactive["gmail_sync"]["reason"] == "gmail_calendar_disabled"
    assert inactive["pipeline_integrity"]["reason"] == "integrity_check_off"
    assert inactive["nightly_ai_oneoff"]["reason"] == "run_on_startup_off"


async def test_unknown_job_id_is_shown_by_its_id():
    sched, tracker = _fake_scheduler(), _tracker()
    with _admin(), _running(sched, tracker):
        _, body = await _get(tracker)
    job = next(j for j in body["jobs"] if j["id"] == "brand_new_job")
    assert job["name"] == {"sk": "brand_new_job", "en": "brand_new_job"}
    assert job["group"] == "other"
    assert job["known"] is False
    assert job["trigger"]["kind"] == "cron"
    assert job["trigger"]["cadence"] == "custom"


async def test_trigger_and_next_run_come_from_the_scheduler():
    sched, tracker = _fake_scheduler(), _tracker()
    with _admin(), _running(sched, tracker):
        _, body = await _get(tracker)
    jobs = {j["id"]: j for j in body["jobs"]}
    assert jobs["nightly_ai_pipeline"]["trigger"]["cadence"] == "daily"
    assert jobs["nightly_ai_pipeline"]["next_run_at"] == "2031-07-19T22:47:00+00:00"
    assert jobs["weekly_analytics"]["trigger"]["cadence"] == "weekly"
    assert jobs["sync_watchdog"]["trigger"] == {
        "kind": "interval",
        "seconds": 300,
        "text": "interval[0:05:00]",
    }
    assert jobs["sync_watchdog"]["last_ok"] == "2031-07-19T21:55:00+00:00"
    # Sorted by next run: soonest first, jobs without one last.
    assert body["jobs"][0]["id"] == "sync_watchdog"
    assert body["jobs"][-1]["next_run_at"] is None


async def test_curated_loop_entry_has_aggregates_only():
    sched, tracker = _fake_scheduler(), _tracker()
    with _admin(), _running(sched, tracker):
        _, body = await _get(tracker)
    c = body["curated_loop"]
    assert c["running"] is True and c["reason"] is None
    assert c["patients_probed"] == 2
    assert c["probes_total"] == 42
    assert c["last_probe_at"] == "2031-07-19T21:59:00+00:00"
    assert c["auto_runs_total"] == 3
    assert c["runs_blocked_total"] == 2
    assert c["pending_inbox_total"] == 4
    assert isinstance(c["poll_s"], int)


async def test_response_carries_no_patient_data_or_error_text():
    sched, tracker = _fake_scheduler(), _tracker()
    with _admin(), _running(sched, tracker):
        resp, body = await _get(tracker)
    text = resp.body.decode()
    assert PID not in text
    assert "beef" not in text
    assert SECRET_MSG not in text and "Someone" not in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    weekly = next(j for j in body["jobs"] if j["id"] == "weekly_analytics")
    assert weekly["last_error_type"] == "RuntimeError"
    assert "last_error_msg" not in weekly


async def test_scheduler_off_lists_every_known_job_as_inactive():
    with (
        _admin(),
        patch.object(scheduler_singleton, "current_scheduler", return_value=None),
        patch.object(server, "SYNC_ENABLED", False),
        patch.object(server, "GOOGLE_INTEGRATION_ENABLED", False),
        patch.object(scheduler_singleton, "curated_loop_running", return_value=False),
        patch.dict("os.environ", {"CURATED_NIGHTLY_ENABLED": "true"}),
    ):
        _, body = await _get({})
    assert body["jobs"] == []
    assert body["scheduler"]["off_reason"] == {
        "reason": "sync_disabled",
        "switch": "SYNC_ENABLED=false",
    }
    assert {j["id"] for j in body["inactive"]} == set(scheduled_jobs.CATALOGUE)
    reasons = {j["id"]: j["reason"] for j in body["inactive"]}
    # #856: a lifespan loop is never off because of SYNC_ENABLED.
    for loop_id in scheduled_jobs.BACKGROUND_LOOP_JOBS:
        assert reasons.pop(loop_id) == "loop_not_running"
    assert set(reasons.values()) == {"sync_disabled"}
    assert body["curated_loop"]["running"] is False
    assert body["curated_loop"]["reason"] == "google_disabled"


async def test_non_admin_session_still_gets_403():
    peek = MagicMock(side_effect=AssertionError("no scheduler read for a non-admin"))
    with (
        _admin(CARER_EMAIL, admin=False),
        patch.object(scheduler_singleton, "current_scheduler", peek),
    ):
        resp = await server.api_schedules(_request(_tracker()))
    assert resp.status_code == 403
    peek.assert_not_called()


# ── pure helpers ─────────────────────────────────────────────────────────


def test_trigger_info_shapes():
    assert scheduled_jobs.trigger_info(DateTrigger(run_date=datetime(2027, 1, 1)))["kind"] == "date"
    daily = scheduled_jobs.trigger_info(CronTrigger(hour=4, minute=25, timezone="UTC"))
    assert daily["cadence"] == "daily"
    assert scheduled_jobs.trigger_info(object())["kind"] == "other"


def test_inactive_reason_legacy_mode():
    flags = {"scheduler_running": True, "ai_nightly_only": False, "gmail_calendar_enabled": True}
    assert scheduled_jobs.inactive_reason("nightly_ai_pipeline", flags)["reason"] == "legacy_mode"
    assert scheduled_jobs.inactive_reason("gmail_startup_sync", flags)["reason"] == "not_registered"


def test_current_scheduler_takes_no_reference():
    scheduler_singleton._reset_for_tests()
    try:
        sched = SimpleNamespace(running=True, get_jobs=lambda: [])
        scheduler_singleton.register_scheduler(sched, {"x": {}})
        assert scheduler_singleton.current_scheduler() == (sched, {"x": {}})
        # Still exactly one holder: the release shuts it down.
        assert scheduler_singleton.release_scheduler(sched) is True
        assert scheduler_singleton.current_scheduler() is None
    finally:
        scheduler_singleton._reset_for_tests()


def test_every_job_the_server_registers_has_a_description():
    """A new job id still shows (by its id), but should get sk/en texts."""
    src = Path(server.__file__).read_text()
    start = src.index("def _start_sync_scheduler(")
    end = src.index("\ndef _defer_jobs_past_startup_grace(", start)
    registered = set(re.findall(r'\bid="([a-z_]+)"', src[start:end]))
    assert registered, "no job ids found"
    assert registered <= set(scheduled_jobs.CATALOGUE), registered - set(scheduled_jobs.CATALOGUE)


def test_health_does_not_build_the_schedules_payload():
    src = Path(server.__file__).read_text()
    start = src.index("async def health(request")
    end = src.index("\n@mcp.custom_route", start)
    assert "_schedules_payload" not in src[start:end]
    assert "scheduled_jobs" not in src[start:end]
