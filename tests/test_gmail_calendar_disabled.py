"""GMAIL_CALENDAR_ENABLED kill switch (#618/#699).

The privacy text says Gmail and Google Calendar are not used. Before this flag
that was only true because SYNC_ENABLED=false kept APScheduler off; Drive must
stay on (GOOGLE_INTEGRATION_ENABLED=true) for the curated pipeline. These tests
lock the default-off behaviour on every Gmail/Calendar surface:

* config default is off when the env var is unset
* the scheduler registers no gmail/calendar jobs even with SYNC_ENABLED=true
* the auth-enable tools refuse and never emit an OAuth URL
* no OAuth URL (per-service, combined ``all``, or the HTTP route) carries the
  gmail/calendar scopes
* the read tools refuse
* integration_status reports gmail and calendar as disabled

All values are synthetic.
"""

from __future__ import annotations

import importlib
import json
from unittest.mock import MagicMock

import pytest

from oncofiles.config import GMAIL_CALENDAR_DISABLED_MESSAGE
from oncofiles.database import Database
from oncofiles.oauth import (
    ALL_SCOPES,
    CALENDAR_SCOPES,
    GMAIL_SCOPES,
    SCOPE_CALENDAR,
    SCOPE_DRIVE,
    SCOPE_GMAIL,
    allowed_scopes,
    get_auth_url_for_scopes,
)
from oncofiles.tools import integrations

GMAIL_CALENDAR_JOB_IDS = {
    "gmail_sync",
    "calendar_sync",
    "gmail_startup_sync",
    "calendar_startup_sync",
}


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    """Explicitly off — independent of whatever a local .env might say."""
    monkeypatch.setattr("oncofiles.config.GMAIL_CALENDAR_ENABLED", False)


@pytest.fixture
def _oauth_configured(monkeypatch):
    import oncofiles.config as cfg
    import oncofiles.oauth as oauth

    monkeypatch.setattr(cfg, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(cfg, "MCP_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(oauth, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(oauth, "MCP_BEARER_TOKEN", "test-token")


class _StubCtx:
    class _Req:
        def __init__(self, db: Database):
            self.lifespan_context = {"db": db, "files": MagicMock()}

    def __init__(self, db: Database):
        self.request_context = self._Req(db)


def _assert_refused(raw: str, service: str) -> None:
    data = json.loads(raw)
    assert data["status"] == "disabled"
    assert data["service"] == service
    assert data["error"] == GMAIL_CALENDAR_DISABLED_MESSAGE
    assert "auth_url" not in data
    assert "googleapis.com/auth" not in raw


# ── config default ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [(None, False), ("", False), ("false", False), ("no", False), ("true", True), ("1", True)],
)
def test_config_default_is_off(monkeypatch, env_value, expected):
    """Reload config under a patched env: unset → off; only true/1/yes enable it."""
    from oncofiles import config

    if env_value is None:
        monkeypatch.delenv("GMAIL_CALENDAR_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GMAIL_CALENDAR_ENABLED", env_value)
    # Independent of the Google master switch and of SYNC_ENABLED.
    monkeypatch.setenv("GOOGLE_INTEGRATION_ENABLED", "true")
    monkeypatch.setenv("SYNC_ENABLED", "true")
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # ignore any local .env
    try:
        reloaded = importlib.reload(config)
        assert reloaded.GMAIL_CALENDAR_ENABLED is expected
        assert reloaded.gmail_calendar_enabled() is expected
        assert reloaded.GOOGLE_INTEGRATION_ENABLED is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ── scheduler ──────────────────────────────────────────────────────────────


def _job_ids_from_scheduler(monkeypatch, *, nightly_only: bool) -> set[str]:
    import oncofiles.server as server

    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_NIGHTLY_ONLY", nightly_only)
    scheduler, _tracker = server._start_sync_scheduler(
        MagicMock(), MagicMock(), None, "", MagicMock(), MagicMock(), primary_uuid=""
    )
    try:
        return {job.id for job in scheduler.get_jobs()}
    finally:
        scheduler.shutdown(wait=False)


@pytest.mark.parametrize("nightly_only", [True, False])
async def test_scheduler_registers_no_gmail_calendar_jobs_when_off(monkeypatch, nightly_only):
    """SYNC_ENABLED=true + flag off: no gmail/calendar jobs, every other job still there."""
    ids = _job_ids_from_scheduler(monkeypatch, nightly_only=nightly_only)
    assert not ids & GMAIL_CALENDAR_JOB_IDS
    # The rest of the scheduler is untouched.
    assert {"memory_check", "oauth_token_cleanup"} <= ids  # db_keepalive is gone (#617)
    assert ("nightly_ai_pipeline" if nightly_only else "gdrive_sync") in ids


@pytest.mark.parametrize("nightly_only", [True, False])
async def test_scheduler_registers_gmail_calendar_jobs_when_on(
    monkeypatch, gmail_calendar_on, nightly_only
):
    ids = _job_ids_from_scheduler(monkeypatch, nightly_only=nightly_only)
    assert {"gmail_sync", "calendar_sync"} <= ids
    if not nightly_only:
        assert {"gmail_startup_sync", "calendar_startup_sync"} <= ids


# ── OAuth scopes ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("scopes", [GMAIL_SCOPES, CALENDAR_SCOPES, ALL_SCOPES])
def test_auth_url_never_requests_gmail_or_calendar_when_off(_oauth_configured, scopes):
    url = get_auth_url_for_scopes(scopes, patient_id="00000000-0000-4000-8000-0000000000aa")
    assert "gmail.readonly" not in url
    assert "calendar.readonly" not in url
    assert "auth%2Fdrive" in url  # Drive still requested


def test_allowed_scopes_filters_only_when_off(gmail_calendar_on):
    assert allowed_scopes(ALL_SCOPES) == ALL_SCOPES


def test_allowed_scopes_off_keeps_drive_only():
    assert allowed_scopes(ALL_SCOPES) == [SCOPE_DRIVE]
    assert allowed_scopes([SCOPE_GMAIL]) == [SCOPE_DRIVE]
    assert SCOPE_CALENDAR not in allowed_scopes(CALENDAR_SCOPES)


@pytest.mark.parametrize("service", ["gmail", "calendar"])
async def test_oauth_authorize_route_refuses_gmail_calendar(_oauth_configured, service):
    import oncofiles.server as server

    request = MagicMock()
    request.path_params = {"service": service}
    request.query_params = {"patient_id": "synthetic-slug"}
    request.client.host = "203.0.113.7"
    request.headers = {}
    resp = await server.oauth_authorize(request)
    assert resp.status_code == 403
    assert GMAIL_CALENDAR_DISABLED_MESSAGE in resp.body.decode()


# ── MCP tools ──────────────────────────────────────────────────────────────


async def test_gmail_auth_enable_refuses(db: Database, _oauth_configured):
    _assert_refused(await integrations.gmail_auth_enable(_StubCtx(db)), "gmail")


async def test_calendar_auth_enable_refuses(db: Database, _oauth_configured):
    _assert_refused(await integrations.calendar_auth_enable(_StubCtx(db)), "calendar")


async def test_search_emails_refuses(db: Database):
    _assert_refused(await integrations.search_emails(_StubCtx(db), query="x"), "gmail")


async def test_get_email_refuses(db: Database):
    _assert_refused(await integrations.get_email(_StubCtx(db), email_entry_id=1), "gmail")


async def test_search_calendar_events_refuses(db: Database):
    _assert_refused(await integrations.search_calendar_events(_StubCtx(db), query="x"), "calendar")


async def test_get_calendar_event_refuses(db: Database):
    _assert_refused(
        await integrations.get_calendar_event(_StubCtx(db), calendar_entry_id=1), "calendar"
    )


async def test_integration_status_reports_disabled(db: Database):
    data = json.loads(await integrations.integration_status(_StubCtx(db)))
    for service in ("gmail", "calendar"):
        entry = data["services"][service]
        assert entry["enabled"] is False
        assert entry["server_disabled"] is True
        assert entry["message"] == GMAIL_CALENDAR_DISABLED_MESSAGE
    assert "drive" in data["services"]


async def test_integration_status_enabled_shape_unchanged(db: Database, gmail_calendar_on):
    data = json.loads(await integrations.integration_status(_StubCtx(db)))
    assert "server_disabled" not in data["services"]["gmail"]
    assert "server_disabled" not in data["services"]["calendar"]
