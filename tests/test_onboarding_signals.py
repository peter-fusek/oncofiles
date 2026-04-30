"""Tests for onboarding signals (#468) — T0 created hook + admin notifier.

v5.19 Session 2 ships only the T0 hook + real-time Resend admin email.
Later events (oauth_ok / folder_set / first_sync / first_ai / stuck_24h /
oauth_failure / doc_limit_hit) and the daily digest dispatcher arrive in
v5.20.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest


def _make_request(body: dict, dashboard_email: str = "peterfusek1980@gmail.com") -> MagicMock:
    """Build a minimal mock request that the api_create_patient handler will accept."""
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"
    # Dashboard auth: cookie-based session header so _check_dashboard_auth passes.
    # The newsletter pattern uses the same shape.
    request.headers = {}
    request.cookies = {}

    async def _json():
        return body

    request.json = _json
    request.app = MagicMock()
    return request


# ── DB helper ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_onboarding_event_inserts_row(db):
    """insert_onboarding_event inserts a new row and returns its id."""
    row_id = await db.insert_onboarding_event(
        "patient-uuid-123",
        "created",
        meta={"slug": "test-slug", "display_name": "Test"},
    )
    assert isinstance(row_id, int)
    event = await db.get_onboarding_event("patient-uuid-123", "created")
    assert event is not None
    assert event["event_type"] == "created"
    assert json.loads(event["meta_json"])["slug"] == "test-slug"


@pytest.mark.asyncio
async def test_insert_onboarding_event_dedupes_one_time_events(db):
    """Second insert for the same (patient, one-time event_type) is silently ignored."""
    first = await db.insert_onboarding_event("p1", "created", meta={"v": 1})
    second = await db.insert_onboarding_event("p1", "created", meta={"v": 2})
    assert isinstance(first, int)
    assert second is None  # dedupe'd by partial UNIQUE index

    # Only the first row survives — meta from the second insert was dropped.
    rows = await db.list_onboarding_events_for_patient("p1")
    assert len(rows) == 1
    assert json.loads(rows[0]["meta_json"])["v"] == 1


@pytest.mark.asyncio
async def test_insert_onboarding_event_repeatable_types_not_deduped(db):
    """Repeatable event types (e.g. oauth_failure) are NOT covered by the partial UNIQUE index."""
    # The migration's partial UNIQUE index covers only one-time events; non-listed
    # types (oauth_failure / stuck_24h / doc_limit_hit) can have many rows per patient.
    a = await db.insert_onboarding_event("p2", "oauth_failure", meta={"err": "first"})
    b = await db.insert_onboarding_event("p2", "oauth_failure", meta={"err": "second"})
    assert isinstance(a, int)
    assert isinstance(b, int)
    assert a != b

    rows = await db.list_onboarding_events_for_patient("p2")
    assert len(rows) == 2


# ── Notifier env-missing path ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notifier_silent_noop_when_env_missing(monkeypatch, caplog):
    """Notifier returns without raising and without HTTP when Resend env is unset."""
    from oncofiles.server import _notify_admin_onboarding_event

    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("NOTIFY_FROM_EMAIL", raising=False)
    monkeypatch.delenv("NOTIFY_TO_EMAIL", raising=False)

    caplog.set_level("WARNING")
    await _notify_admin_onboarding_event(
        patient_slug="t-slug",
        display_name="Test User",
        caregiver_email="t@example.com",
        event_type="created",
    )
    # No raise; warning logged so missing-config is visible in Railway logs.
    assert any("SKIPPED" in record.message for record in caplog.records)


# ── Notifier happy path (mocked Resend) ──────────────────────────────────


@pytest.mark.asyncio
async def test_notifier_calls_resend_with_correct_shape(monkeypatch):
    """Notifier POSTs to Resend with the expected headers + body when env is set."""
    import httpx

    from oncofiles.server import _notify_admin_onboarding_event

    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_123")
    monkeypatch.setenv("NOTIFY_FROM_EMAIL", "Onco Notifier <hello@oncofiles.com>")
    monkeypatch.setenv("NOTIFY_TO_EMAIL", "admin@oncofiles.com")

    captured: dict = {}

    class _FakeResponse:
        status_code = 200
        text = "ok"

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers=None, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    await _notify_admin_onboarding_event(
        patient_slug="q1b",
        display_name="Erika Fusekova",
        caregiver_email="peter.fusek@instarea.sk",
        event_type="created",
    )

    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["headers"]["Authorization"] == "Bearer re_test_key_123"
    assert captured["json"]["from"] == "Onco Notifier <hello@oncofiles.com>"
    assert captured["json"]["to"] == ["admin@oncofiles.com"]
    assert "Nový pacient: Erika Fusekova" in captured["json"]["subject"]
    body = captured["json"]["text"]
    assert "patient_slug:    q1b" in body
    assert "caregiver_email: peter.fusek@instarea.sk" in body
    assert "https://oncofiles.com/dashboard?patient=q1b" in body
    # Tags carry the kind + event_type for Resend filtering
    tag_kinds = {t["name"]: t["value"] for t in captured["json"]["tags"]}
    assert tag_kinds["kind"] == "onboarding"
    assert tag_kinds["event_type"] == "created"


@pytest.mark.asyncio
async def test_notifier_swallows_resend_4xx(monkeypatch, caplog):
    """A 4xx from Resend logs a warning and does NOT raise."""
    import httpx

    from oncofiles.server import _notify_admin_onboarding_event

    monkeypatch.setenv("RESEND_API_KEY", "re_test_key_123")
    monkeypatch.setenv("NOTIFY_FROM_EMAIL", "from@oncofiles.com")
    monkeypatch.setenv("NOTIFY_TO_EMAIL", "to@oncofiles.com")

    class _FakeResponse:
        status_code = 422
        text = "validation error"

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, *, headers=None, json=None):
            return _FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)

    caplog.set_level("WARNING")
    # Must not raise — caller's request must succeed even when notifier fails.
    await _notify_admin_onboarding_event(
        patient_slug="x",
        display_name="X",
        caregiver_email="x@y.com",
        event_type="created",
    )
    assert any("Resend onboarding notification failed" in r.message for r in caplog.records)


# ── End-to-end: api_create_patient triggers the hook ─────────────────────


@pytest.mark.asyncio
async def test_api_create_patient_inserts_t0_event_and_fires_notifier(db, monkeypatch):
    """POST /api/patients inserts a T0 onboarding row and schedules the admin notifier."""
    from oncofiles import server

    # Capture notifier calls — fire-and-forget shape must be preserved.
    calls: list[dict] = []

    async def _fake_notify(*, patient_slug, display_name, caregiver_email, event_type):
        calls.append(
            {
                "patient_slug": patient_slug,
                "display_name": display_name,
                "caregiver_email": caregiver_email,
                "event_type": event_type,
            }
        )

    monkeypatch.setattr(server, "_notify_admin_onboarding_event", _fake_notify)

    # Bypass the dashboard auth + rate-limit gates (out of scope for this test).
    monkeypatch.setattr(server, "_check_dashboard_auth", lambda req: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda label: None)
    monkeypatch.setattr(server, "_get_dashboard_email", lambda req: "peter.fusek@instarea.sk")

    request = _make_request(
        {
            "patient_id": "new-onboarding-test",
            "display_name": "Onboarding Test",
            "caregiver_email": "caregiver@example.com",
            "preferred_lang": "sk",
        }
    )
    request.app.state.fastmcp_server._lifespan_result = {"db": db}

    response = await server.api_create_patient(request)
    assert response.status_code == 201

    # Give the fire-and-forget asyncio.create_task a chance to run.
    await asyncio.sleep(0)

    # The T0 event row exists, scoped to the new patient's UUID (not the slug).
    created_uuid = (await db.get_patient_by_slug("new-onboarding-test")).patient_id
    event = await db.get_onboarding_event(created_uuid, "created")
    assert event is not None
    meta = json.loads(event["meta_json"])
    assert meta["slug"] == "new-onboarding-test"
    assert meta["display_name"] == "Onboarding Test"
    assert meta["caregiver_email"] == "caregiver@example.com"

    # Notifier was scheduled with the slug (not the UUID) — slug is the
    # operator-friendly identifier and matches the dashboard URL shape.
    assert len(calls) == 1
    assert calls[0]["patient_slug"] == "new-onboarding-test"
    assert calls[0]["event_type"] == "created"
    assert calls[0]["caregiver_email"] == "caregiver@example.com"
