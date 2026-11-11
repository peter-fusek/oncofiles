"""GMAIL_CALENDAR_ENABLED=false never builds Gmail/Calendar clients (#746).

Follow-up to #745: with the flag off, a stored OAuth token that already holds
the gmail/calendar scopes must not lead to a Gmail or Calendar client being
constructed, neither at startup (lifespan) nor per patient
(``_create_patient_clients``, which every per-request factory goes through).
The client constructors are patched and must not be called. With the flag on,
the clients are built exactly as before.

All values are synthetic.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import server
from oncofiles.models import OAuthToken
from oncofiles.oauth import SCOPE_CALENDAR, SCOPE_DRIVE, SCOPE_GMAIL

PID = "00000000-0000-4000-8000-000000000746"


def _token() -> OAuthToken:
    return OAuthToken(
        patient_id=PID,
        access_token="synthetic-access",
        refresh_token="synthetic-refresh",
        token_expiry=datetime.now(UTC) + timedelta(hours=1),
        gdrive_folder_id="synthetic-folder-id",
        owner_email="caregiver@example.test",
        granted_scopes=json.dumps([SCOPE_DRIVE, SCOPE_GMAIL, SCOPE_CALENDAR]),
    )


@pytest.fixture
def ctors(monkeypatch):
    """Patch every Google client constructor; return the Gmail/Calendar mocks."""
    import oncofiles.calendar_client as cal_mod
    import oncofiles.gmail_client as gmail_mod

    gmail = MagicMock(name="GmailClient.from_oauth", return_value=MagicMock(name="gmail"))
    calendar = MagicMock(name="CalendarClient.from_oauth", return_value=MagicMock(name="cal"))
    monkeypatch.setattr(gmail_mod.GmailClient, "from_oauth", gmail)
    monkeypatch.setattr(cal_mod.CalendarClient, "from_oauth", calendar)
    monkeypatch.setattr(server.GDriveClient, "from_oauth", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", True)
    monkeypatch.setattr(server, "GOOGLE_OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setattr(server, "_mark_patient_curated", AsyncMock(return_value=False))
    monkeypatch.setattr(server, "_patient_clients_cache", {})
    return SimpleNamespace(gmail=gmail, calendar=calendar)


def _set_flag(monkeypatch, value: bool) -> None:
    monkeypatch.setattr("oncofiles.config.GMAIL_CALENDAR_ENABLED", value)


def _fake_db() -> MagicMock:
    db = MagicMock()
    db.get_oauth_token = AsyncMock(return_value=_token())
    db.upsert_oauth_token = AsyncMock()
    return db


# ── per-patient clients (also the per-request factory path) ───────────────


async def test_patient_clients_flag_off_builds_no_gmail_calendar(monkeypatch, ctors):
    _set_flag(monkeypatch, False)
    clients = await server._create_patient_clients(_fake_db(), PID)

    assert clients is not None
    gdrive, gmail, calendar, folder_id = clients
    assert gdrive is not None  # Drive is unaffected by this flag
    assert gmail is None and calendar is None
    assert folder_id == "synthetic-folder-id"
    ctors.gmail.assert_not_called()
    ctors.calendar.assert_not_called()


async def test_patient_clients_flag_on_builds_both(monkeypatch, ctors):
    _set_flag(monkeypatch, True)
    clients = await server._create_patient_clients(_fake_db(), PID)

    assert clients[1] is ctors.gmail.return_value
    assert clients[2] is ctors.calendar.return_value
    ctors.gmail.assert_called_once()
    ctors.calendar.assert_called_once()


async def test_patient_clients_cache_hit_strips_clients_when_flag_off(monkeypatch, ctors):
    """Clients cached while the flag was on are never handed out once it is off."""
    _set_flag(monkeypatch, True)
    db = _fake_db()
    await server._create_patient_clients(db, PID)

    _set_flag(monkeypatch, False)
    gdrive, gmail, calendar, _ = await server._create_patient_clients(db, PID)
    assert gdrive is not None
    assert gmail is None and calendar is None


async def test_request_factories_return_none_when_flag_off(monkeypatch, ctors):
    """``_get_gmail_client`` / ``_get_calendar_client`` resolve through
    ``_create_patient_clients`` and fall back to the (empty) lifespan slots."""
    from oncofiles.tools import _helpers

    _set_flag(monkeypatch, False)
    db = _fake_db()
    ctx = SimpleNamespace(
        request_context=SimpleNamespace(
            lifespan_context={"db": db, "gmail_client": None, "calendar_client": None}
        )
    )
    monkeypatch.setattr(_helpers, "_get_patient_id", lambda required=False: PID)

    assert await _helpers._get_gmail_client(ctx) is None
    assert await _helpers._get_calendar_client(ctx) is None
    ctors.gmail.assert_not_called()
    ctors.calendar.assert_not_called()


# ── startup (lifespan) ─────────────────────────────────────────────────────


@pytest.fixture
def lifespan_env(monkeypatch, tmp_path, ctors):
    """Run the real lifespan against a temp DB with a stored gmail/calendar token.

    Global side effects (logging setup, MCP auth, patient context, curated
    guard/stop flag, scheduler) are stubbed so other tests are unaffected.
    """
    from oncofiles import patient_context
    from oncofiles.curated import indexer
    from oncofiles.database import Database
    from oncofiles.tools import curated

    monkeypatch.setattr(server, "DATABASE_PATH", str(tmp_path / "t746.db"))
    monkeypatch.setattr(server, "TURSO_DATABASE_URL", "")
    monkeypatch.setattr(server, "SYNC_ENABLED", False)
    monkeypatch.setattr(server, "GOOGLE_DRIVE_FOLDER_ID", "")
    monkeypatch.setattr(server, "_setup_logging", lambda: None)
    monkeypatch.setattr(server, "auth", SimpleNamespace())
    monkeypatch.setattr(server, "create_gdrive_client", MagicMock())
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
        AsyncMock(return_value=SimpleNamespace(patient_id=PID)),
    )
    monkeypatch.setattr(Database, "get_oauth_token", AsyncMock(return_value=_token()))
    return ctors


async def _run_lifespan() -> dict:
    async with server.lifespan(MagicMock()) as state:
        return dict(state)


async def test_lifespan_flag_off_builds_no_gmail_calendar(monkeypatch, lifespan_env):
    _set_flag(monkeypatch, False)
    state = await _run_lifespan()

    assert state["gdrive"] is not None
    assert state["gmail_client"] is None
    assert state["calendar_client"] is None
    lifespan_env.gmail.assert_not_called()
    lifespan_env.calendar.assert_not_called()


async def test_lifespan_flag_on_builds_both(monkeypatch, lifespan_env):
    _set_flag(monkeypatch, True)
    state = await _run_lifespan()

    assert state["gmail_client"] is lifespan_env.gmail.return_value
    assert state["calendar_client"] is lifespan_env.calendar.return_value
    lifespan_env.gmail.assert_called_once()
    lifespan_env.calendar.assert_called_once()


def test_no_direct_gmail_calendar_construction_outside_helper():
    """Every construction goes through ``_build_gmail_calendar_clients``, which
    checks the flag first — a new call site cannot silently skip the gate."""
    import inspect

    source = inspect.getsource(server)
    helper = inspect.getsource(server._build_gmail_calendar_clients)
    assert source.count("GmailClient.from_oauth(") == helper.count("GmailClient.from_oauth(") == 1
    assert (
        source.count("CalendarClient.from_oauth(")
        == helper.count("CalendarClient.from_oauth(")
        == 1
    )
    assert helper.index("gmail_calendar_enabled()") < helper.index("GmailClient")
