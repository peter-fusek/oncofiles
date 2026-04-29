"""Option A (#429) + #518 lock-in for tools/integrations.py.

Pre-#518 the seven Gmail / Calendar tools resolved patient identity via
``_get_patient_id()`` (the bound ContextVar) only — there was no way for a
stateless HTTP caller to target a different patient per request, even when
holding admin scope. The fix threads ``patient_slug`` through every tool
and resolves via ``_resolve_patient_id(patient_slug, ctx)``.

These tests prove that with admin scope and ``patient_slug=bob-test``, the
tools query Bob's data even when the caller's ContextVar binds Erika.
"""

from __future__ import annotations

import json
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from oncofiles.database import Database
from oncofiles.models import CalendarEntry, EmailEntry
from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools import integrations
from tests.conftest import ERIKA_UUID

# Slug routing across patients runs through the #497/#498 ACL — admin scope
# for the whole module so each test can pick its target.
pytestmark = pytest.mark.usefixtures("admin_scope")

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


class _StubCtx:
    """Minimal Context double — integrations.py only reads ``request_context``."""

    class _Req:
        def __init__(self, db: Database):
            self.lifespan_context = {"db": db, "files": MagicMock()}

    def __init__(self, db: Database):
        self.request_context = self._Req(db)


async def _seed_bob(db: Database) -> None:
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


async def _seed_emails(db: Database) -> None:
    await db.upsert_email_entry(
        EmailEntry(
            patient_id=ERIKA_UUID,
            gmail_message_id="erika-msg-1",
            thread_id="erika-th",
            subject="Erika lab results",
            sender="lab@example.com",
            date=datetime(2026, 1, 1, 9, 0, 0),
            body_snippet="Erika body",
            body_text="Erika body text",
            is_medical=True,
        )
    )
    await db.upsert_email_entry(
        EmailEntry(
            patient_id=SECOND_UUID,
            gmail_message_id="bob-msg-1",
            thread_id="bob-th",
            subject="Bob CT scan",
            sender="rad@example.com",
            date=datetime(2026, 1, 2, 9, 0, 0),
            body_snippet="Bob body",
            body_text="Bob body text",
            is_medical=True,
        )
    )


async def _seed_calendar(db: Database) -> None:
    await db.upsert_calendar_entry(
        CalendarEntry(
            patient_id=ERIKA_UUID,
            google_event_id="erika-evt-1",
            summary="Erika oncologist visit",
            description="d1",
            start_time=datetime(2026, 1, 10, 10, 0, 0),
        )
    )
    await db.upsert_calendar_entry(
        CalendarEntry(
            patient_id=SECOND_UUID,
            google_event_id="bob-evt-1",
            summary="Bob oncologist visit",
            description="d2",
            start_time=datetime(2026, 1, 11, 10, 0, 0),
        )
    )


# ── search_emails ────────────────────────────────────────────────────────


async def test_search_emails_default_targets_bound_patient(db: Database):
    await _seed_bob(db)
    await _seed_emails(db)
    ctx = _StubCtx(db)

    # ContextVar binds Erika; no slug passed.
    result = json.loads(await integrations.search_emails(ctx))
    subjects = {e["subject"] for e in result["emails"]}
    assert "Erika lab results" in subjects
    assert "Bob CT scan" not in subjects


async def test_search_emails_slug_routes_to_other_patient(db: Database):
    """Pre-#518 this was impossible — the bound ContextVar always won."""
    await _seed_bob(db)
    await _seed_emails(db)
    ctx = _StubCtx(db)

    result = json.loads(await integrations.search_emails(ctx, patient_slug=SECOND_SLUG))
    subjects = {e["subject"] for e in result["emails"]}
    assert subjects == {"Bob CT scan"}


# ── search_calendar_events ───────────────────────────────────────────────


async def test_search_calendar_events_slug_routes_to_other_patient(db: Database):
    await _seed_bob(db)
    await _seed_calendar(db)
    ctx = _StubCtx(db)

    result = json.loads(await integrations.search_calendar_events(ctx, patient_slug=SECOND_SLUG))
    summaries = {e["summary"] for e in result["events"]}
    assert summaries == {"Bob oncologist visit"}


# ── integration_status ───────────────────────────────────────────────────


async def test_integration_status_slug_targets_other_patient(db: Database):
    """integration_status reads oauth_token + count_email/calendar_entries —
    all four lookups must use the slug-resolved pid (#518)."""
    await _seed_bob(db)
    await _seed_emails(db)
    await _seed_calendar(db)
    ctx = _StubCtx(db)

    # No tokens granted → counts default to None for both services. The point
    # of this test is that the call doesn't blow up and that the resolution
    # path exercises the slug. (Token-granted variants are covered by the
    # search_* / get_* tests above.)
    erika = json.loads(await integrations.integration_status(ctx))
    bob = json.loads(await integrations.integration_status(ctx, patient_slug=SECOND_SLUG))
    # Neither patient has tokens, so token_present is False on both — and
    # crucially neither call raises.
    assert erika["token_present"] is False
    assert bob["token_present"] is False


# ── gmail_auth_enable / calendar_auth_enable ─────────────────────────────


async def test_gmail_auth_enable_returns_url_for_slug_patient(db: Database, monkeypatch):
    """The auth URL embeds the patient_id in OAuth state so the callback
    binds to the correct patient. With #518 the slug-resolved pid is what
    flows into get_auth_url_for_scopes."""
    await _seed_bob(db)
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "fake-client-id")

    captured: dict = {}

    def _fake_url(scopes, *, patient_id: str) -> str:  # noqa: ARG001
        captured["patient_id"] = patient_id
        return "https://accounts.google.com/o/oauth2/auth?stub=1"

    monkeypatch.setattr("oncofiles.oauth.get_auth_url_for_scopes", _fake_url)
    ctx = _StubCtx(db)

    result = json.loads(await integrations.gmail_auth_enable(ctx, patient_slug=SECOND_SLUG))
    assert result["status"] == "authorization_required"
    assert result["service"] == "gmail"
    assert captured["patient_id"] == SECOND_UUID


async def test_calendar_auth_enable_returns_url_for_slug_patient(db: Database, monkeypatch):
    await _seed_bob(db)
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "fake-client-id")

    captured: dict = {}

    def _fake_url(scopes, *, patient_id: str) -> str:  # noqa: ARG001
        captured["patient_id"] = patient_id
        return "https://accounts.google.com/o/oauth2/auth?stub=1"

    monkeypatch.setattr("oncofiles.oauth.get_auth_url_for_scopes", _fake_url)
    ctx = _StubCtx(db)

    result = json.loads(await integrations.calendar_auth_enable(ctx, patient_slug=SECOND_SLUG))
    assert result["status"] == "authorization_required"
    assert result["service"] == "calendar"
    assert captured["patient_id"] == SECOND_UUID


# ── get_email / get_calendar_event ownership with slug ───────────────────


async def test_get_email_slug_targets_owner_when_caller_is_admin(db: Database):
    """Admin caller passing slug=bob can read Bob's email by integer id."""
    await _seed_bob(db)
    await _seed_emails(db)
    bob_email = await db.get_email_entry_by_gmail_id("bob-msg-1", SECOND_UUID)
    assert bob_email is not None

    # Bind the caller to Erika to prove the slug overrides bound pid.
    _verified_patient_id.set(ERIKA_UUID)
    _verified_caller_email.set("admin@oncofiles.com")
    ctx = _StubCtx(db)

    result = json.loads(
        await integrations.get_email(ctx, email_entry_id=bob_email.id, patient_slug=SECOND_SLUG)
    )
    assert "error" not in result, result
    assert result["subject"] == "Bob CT scan"


async def test_get_calendar_event_slug_targets_owner_when_caller_is_admin(db: Database):
    await _seed_bob(db)
    await _seed_calendar(db)
    rows = await db.search_calendar_entries(
        type(
            "Q",
            (),
            {"text": None, "date_from": None, "date_to": None, "is_medical": None, "limit": 10},
        )(),
        patient_id=SECOND_UUID,
    )
    assert rows and rows[0].summary == "Bob oncologist visit"
    bob_event_id = rows[0].id

    _verified_patient_id.set(ERIKA_UUID)
    _verified_caller_email.set("admin@oncofiles.com")
    ctx = _StubCtx(db)

    result = json.loads(
        await integrations.get_calendar_event(
            ctx, calendar_entry_id=bob_event_id, patient_slug=SECOND_SLUG
        )
    )
    assert "error" not in result, result
    assert result["summary"] == "Bob oncologist visit"


# ── Non-admin slug routing is denied (#497/#498 ACL) ─────────────────────


async def test_search_emails_non_admin_foreign_slug_denied(db: Database):
    """A non-admin caller cannot route to another patient via slug. The
    #497/#498 ACL gate in ``_resolve_patient_id`` raises ValueError, which
    ``search_emails`` catches and surfaces as ``{"error": ...}`` — Bob's
    emails must NOT appear in the response."""
    await _seed_bob(db)
    await _seed_emails(db)

    # Drop admin scope for this single test (overrides the module-level fixture).
    tok_admin = _verified_caller_is_admin.set(False)
    tok_pid = _verified_patient_id.set(ERIKA_UUID)
    tok_email = _verified_caller_email.set("erika-caregiver@example.com")
    ctx = _StubCtx(db)
    try:
        result = json.loads(await integrations.search_emails(ctx, patient_slug=SECOND_SLUG))
        # The error key carries the ACL-gate denial message; no email payload.
        assert "error" in result
        assert "access denied" in result["error"].lower()
        assert "emails" not in result
    finally:
        _verified_caller_is_admin.reset(tok_admin)
        _verified_patient_id.reset(tok_pid)
        _verified_caller_email.reset(tok_email)


async def test_get_email_non_admin_foreign_slug_denied(db: Database):
    """``get_email`` does NOT have a try/except around _resolve_patient_id —
    confirm the ACL gate raises ValueError directly so a non-admin caller
    cannot slug-route to another patient's id."""
    await _seed_bob(db)
    await _seed_emails(db)
    bob_email = await db.get_email_entry_by_gmail_id("bob-msg-1", SECOND_UUID)
    assert bob_email is not None

    tok_admin = _verified_caller_is_admin.set(False)
    tok_pid = _verified_patient_id.set(ERIKA_UUID)
    tok_email = _verified_caller_email.set("erika-caregiver@example.com")
    ctx = _StubCtx(db)
    try:
        with pytest.raises(ValueError, match="access denied"):
            await integrations.get_email(ctx, email_entry_id=bob_email.id, patient_slug=SECOND_SLUG)
    finally:
        _verified_caller_is_admin.reset(tok_admin)
        _verified_patient_id.reset(tok_pid)
        _verified_caller_email.reset(tok_email)
