"""Tests for onboarding signals (#468) — T0 created hook + admin notifier.

v5.19 Session 2 ships only the T0 hook + real-time admin email (SMTP since #834).
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
    """Notifier returns without raising and without SMTP when mail env is unset."""
    from oncofiles.server import _notify_admin_onboarding_event
    from tests import fake_smtp

    instances = fake_smtp.install(monkeypatch, env=False)

    caplog.set_level("WARNING")
    await _notify_admin_onboarding_event(
        patient_slug="t-slug",
        display_name="Test User",
        caregiver_email="t@example.com",
        event_type="created",
    )
    # No raise, no connection; a warning so missing config is visible in logs.
    assert instances == []
    assert any("SKIPPED" in record.message for record in caplog.records)
    assert not any("t@example.com" in r.getMessage() for r in caplog.records)


# ── Notifier happy path (fake SMTP, #834) ────────────────────────────────


@pytest.mark.asyncio
async def test_notifier_sends_over_smtp_with_correct_shape(monkeypatch):
    """Notifier sends one SMTP message to NOTIFY_TO_EMAIL; no patient name (#834)."""
    from oncofiles.server import _notify_admin_onboarding_event
    from tests import fake_smtp

    instances = fake_smtp.install(monkeypatch)

    await _notify_admin_onboarding_event(
        patient_slug="q1b",
        display_name="Jana Testova",
        caregiver_email="caregiver@example.com",
        event_type="created",
    )

    assert len(instances) == 1
    (msg, rcpt) = instances[0].sent[0]
    assert rcpt == ["admin@oncofiles.com"]
    assert msg["From"] == "Oncofiles <noreply@oncofiles.com>"
    assert msg["Subject"] == "[Oncofiles] Nový pacient: q1b"
    body = msg.get_content()
    assert "patient_slug:    q1b" in body
    assert "caregiver_email: caregiver@example.com" in body
    assert "https://oncofiles.com/dashboard?patient=q1b" in body
    # No patient data in e-mails: the display name is not sent.
    assert "Jana Testova" not in msg.as_string()
    assert msg["X-Mailin-Tag"] == "onboarding, created"


@pytest.mark.asyncio
async def test_notifier_swallows_smtp_errors(monkeypatch, caplog):
    """An SMTP rejection logs a warning (code only) and does NOT raise."""
    import smtplib

    from oncofiles.server import _notify_admin_onboarding_event
    from tests import fake_smtp

    fake_smtp.install(
        monkeypatch,
        fail_with=smtplib.SMTPRecipientsRefused({"admin@oncofiles.com": (550, b"no such user")}),
    )

    caplog.set_level("WARNING")
    # Must not raise — caller's request must succeed even when notifier fails.
    await _notify_admin_onboarding_event(
        patient_slug="x",
        display_name="X",
        caregiver_email="x@example.com",
        event_type="created",
    )
    failed = [r.getMessage() for r in caplog.records if "Mail not sent" in r.getMessage()]
    assert failed and "SMTPRecipientsRefused" in failed[0]
    assert not any("@" in r.getMessage() for r in caplog.records)


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
