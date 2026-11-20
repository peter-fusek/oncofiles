"""Phase 3 of the v5.15 security sweep (#487).

Locks down:
  * `_is_admin_caller()` + `_verified_caller_is_admin` ContextVar wiring
  * `_check_ownership_or_admin` enforcement on integer-id read tools
  * `_require_admin_or_raise` enforcement on admin-only tools
  * `list_patients` filter by caller caregiver_email (non-admin)
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from oncofiles.database import Database
from oncofiles.models import Patient
from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools._helpers import (
    _caller_email,
    _check_ownership_or_admin,
    _is_admin_caller,
    _require_admin_or_raise,
)
from tests.helpers import PRIMARY_TEST_UUID


def _make_patient(slug: str, name: str, caregiver_email: str) -> Patient:
    return Patient(
        patient_id=str(__import__("uuid").uuid4()),
        slug=slug,
        display_name=name,
        caregiver_email=caregiver_email,
    )


# ── Admin scope helpers ─────────────────────────────────────────────


def test_is_admin_caller_defaults_false():
    _verified_caller_is_admin.set(False)
    assert _is_admin_caller() is False


def test_is_admin_caller_true_when_flag_set():
    _verified_caller_is_admin.set(True)
    assert _is_admin_caller() is True
    _verified_caller_is_admin.set(False)


def test_caller_email_reads_contextvar():
    _verified_caller_email.set("user@example.com")
    assert _caller_email() == "user@example.com"
    _verified_caller_email.set("")
    assert _caller_email() == ""


def test_require_admin_raises_for_non_admin():
    _verified_caller_is_admin.set(False)
    with pytest.raises(ValueError, match="requires admin scope"):
        _require_admin_or_raise("reassign_document")


def test_require_admin_passes_for_admin():
    _verified_caller_is_admin.set(True)
    # No raise = pass
    _require_admin_or_raise("reassign_document")
    _verified_caller_is_admin.set(False)


# ── Ownership check ─────────────────────────────────────────────────


def test_ownership_allows_own_patient():
    _verified_caller_is_admin.set(False)
    err = _check_ownership_or_admin("email_entry", 5, PRIMARY_TEST_UUID, PRIMARY_TEST_UUID)
    assert err is None


def test_ownership_denies_other_patient():
    _verified_caller_is_admin.set(False)
    err = _check_ownership_or_admin("email_entry", 5, "other-uuid", PRIMARY_TEST_UUID)
    assert err is not None
    assert "access denied" in err
    # Must NOT leak the real owner's UUID in the error.
    assert "other-uuid" not in err


def test_ownership_admin_bypasses_pid_check():
    _verified_caller_is_admin.set(True)
    err = _check_ownership_or_admin("email_entry", 5, "other-uuid", PRIMARY_TEST_UUID)
    assert err is None
    _verified_caller_is_admin.set(False)


def test_ownership_rejects_no_caller_pid():
    _verified_caller_is_admin.set(False)
    err = _check_ownership_or_admin("email_entry", 5, "other-uuid", "")
    assert err is not None
    assert "no authenticated patient" in err


def test_ownership_rejects_entry_with_null_owner():
    """Defensive: a legacy entry with patient_id='' should not be accessible."""
    _verified_caller_is_admin.set(False)
    err = _check_ownership_or_admin("email_entry", 5, "", PRIMARY_TEST_UUID)
    assert err is not None


# ── reassign_document admin gate ────────────────────────────────────


async def test_reassign_document_rejects_non_admin(db: Database):
    from oncofiles.tools.documents import reassign_document

    _verified_caller_is_admin.set(False)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await reassign_document(ctx, doc_id=1, target_patient_slug="jana"))
    assert "error" in result
    assert "admin scope" in result["error"]


# ── audit_patient_isolation admin gate ──────────────────────────────


async def test_audit_patient_isolation_rejects_non_admin(db: Database):
    from oncofiles.tools.documents import audit_patient_isolation

    _verified_caller_is_admin.set(False)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await audit_patient_isolation(ctx))
    assert "error" in result
    assert "admin scope" in result["error"]


# ── list_patients filtering ─────────────────────────────────────────


async def test_list_patients_admin_sees_all(db: Database):
    from oncofiles.tools.patient import list_patients

    # Seed a second patient to verify admin sees both.
    await db.insert_patient(_make_patient("test-2", "Test Two", "other@example.com"))

    _verified_caller_is_admin.set(True)
    _verified_caller_email.set("")
    _verified_patient_id.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await list_patients(ctx))
    slugs = {p["slug"] for p in result if isinstance(p, dict) and "slug" in p}
    assert "test-2" in slugs
    _verified_caller_is_admin.set(False)


async def test_list_patients_non_admin_filters_by_caregiver_email(db: Database):
    from oncofiles.tools.patient import list_patients

    await db.insert_patient(_make_patient("alpha", "Alpha", "alpha@example.com"))
    await db.insert_patient(_make_patient("bravo", "Bravo", "bravo@example.com"))

    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("alpha@example.com")
    _verified_patient_id.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await list_patients(ctx))
    slugs = {p["slug"] for p in result if isinstance(p, dict) and "slug" in p}
    assert "alpha" in slugs
    assert "bravo" not in slugs


async def test_list_patients_non_admin_no_email_restricted_to_bound_pid(db: Database):
    """Non-admin caller with no OAuth email (e.g. patient bearer token): only
    the bound patient surfaces; other patients' slugs/names must not leak.
    """
    from oncofiles.tools.patient import list_patients

    # Seed a second patient that the caller does NOT own.
    other = _make_patient("omega", "Omega", "omega@example.com")
    await db.insert_patient(other)

    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")
    # Caller is a patient bearer bound to Jana (the seeded test default).
    _verified_patient_id.set(PRIMARY_TEST_UUID)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await list_patients(ctx))
    if isinstance(result, dict):
        # Empty shape — no patients matched the filter. Acceptable.
        assert result.get("patients") == []
        return
    slugs = {p["slug"] for p in result if isinstance(p, dict) and "slug" in p}
    # "omega" must NOT appear — we bound only to Jana.
    assert "omega" not in slugs


# ── get_email / get_calendar_event ownership ────────────────────────


OTHER_UUID = "00000000-0000-4000-8000-000000000033"
OTHER_SLUG = "other-test"


async def _seed_other_patient_entries(db: Database) -> tuple[int, int]:
    """A second patient with one email and one calendar entry (invented)."""
    from datetime import datetime

    from oncofiles.models import CalendarEntry, EmailEntry

    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (OTHER_UUID, OTHER_SLUG, "Other Test", "other@example.com"),
    )
    await db.db.commit()
    email = await db.upsert_email_entry(
        EmailEntry(
            patient_id=OTHER_UUID,
            gmail_message_id="other-msg-1",
            thread_id="other-th",
            subject="invented private subject",
            sender="sender@example.com",
            date=datetime(2031, 4, 22, hour=11, minute=20),
            body_snippet="snippet",
            body_text="body",
            is_medical=True,
        )
    )
    event = await db.upsert_calendar_entry(
        CalendarEntry(
            patient_id=OTHER_UUID,
            google_event_id="other-evt-1",
            summary="invented private appointment",
            description="d",
            start_time=datetime(2031, 4, 3, 10, 0, 0),
        )
    )
    return email.id, event.id


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


async def test_get_email_other_patient_is_not_found(db: Database, gmail_calendar_on):
    """Integer-id brute force across patients (#487 C1, #533): the SQL filter
    drops the row, so a foreign id answers exactly like a missing id."""
    from oncofiles.tools.integrations import get_email

    email_id, _ = await _seed_other_patient_entries(db)
    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")

    foreign = json.loads(await get_email(_ctx(db), email_entry_id=email_id))
    missing = json.loads(await get_email(_ctx(db), email_entry_id=email_id + 1000))
    assert foreign == {"error": f"Email entry not found: {email_id}"}
    assert missing == {"error": f"Email entry not found: {email_id + 1000}"}
    assert "invented private subject" not in json.dumps(foreign)


async def test_get_email_admin_needs_the_slug_for_another_patient(db: Database, gmail_calendar_on):
    """An admin reads another patient's entry only by naming the patient."""
    from oncofiles.tools.integrations import get_email

    email_id, _ = await _seed_other_patient_entries(db)
    _verified_caller_is_admin.set(True)
    _verified_caller_email.set("")
    try:
        without_slug = json.loads(await get_email(_ctx(db), email_entry_id=email_id))
        assert without_slug == {"error": f"Email entry not found: {email_id}"}
        with_slug = json.loads(
            await get_email(_ctx(db), email_entry_id=email_id, patient_slug=OTHER_SLUG)
        )
        assert "error" not in with_slug, with_slug
        assert with_slug["id"] == email_id
    finally:
        _verified_caller_is_admin.set(False)


async def test_get_calendar_event_other_patient_is_not_found(db: Database, gmail_calendar_on):
    from oncofiles.tools.integrations import get_calendar_event

    _, event_id = await _seed_other_patient_entries(db)
    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")

    foreign = json.loads(await get_calendar_event(_ctx(db), calendar_entry_id=event_id))
    missing = json.loads(await get_calendar_event(_ctx(db), calendar_entry_id=event_id + 1000))
    assert foreign == {"error": f"Calendar entry not found: {event_id}"}
    assert missing == {"error": f"Calendar entry not found: {event_id + 1000}"}
    assert "invented private appointment" not in json.dumps(foreign)


async def test_get_calendar_event_admin_needs_the_slug_for_another_patient(
    db: Database, gmail_calendar_on
):
    from oncofiles.tools.integrations import get_calendar_event

    _, event_id = await _seed_other_patient_entries(db)
    _verified_caller_is_admin.set(True)
    _verified_caller_email.set("")
    try:
        without_slug = json.loads(await get_calendar_event(_ctx(db), calendar_entry_id=event_id))
        assert without_slug == {"error": f"Calendar entry not found: {event_id}"}
        with_slug = json.loads(
            await get_calendar_event(_ctx(db), calendar_entry_id=event_id, patient_slug=OTHER_SLUG)
        )
        assert "error" not in with_slug, with_slug
        assert with_slug["id"] == event_id
    finally:
        _verified_caller_is_admin.set(False)


# ── get_patient_context ownership (#493) ─────────────────────────────


async def test_get_patient_context_blocks_non_caregiver_slug(db: Database):
    """#493: get_patient_context(patient_slug=X) must not leak name/DOB/surgeries
    when the caller has no caregiver_email match and is not bound to X's pid.
    """
    from oncofiles import patient_context
    from oncofiles.tools.patient import get_patient_context

    victim = _make_patient("victim-slug", "Victim Patient", "owner@example.com")
    await db.insert_patient(victim)
    # Populate PHI for the victim so we can prove it's NOT leaked.
    patient_context._contexts[victim.patient_id] = {
        "name": "Victim Patient",
        "date_of_birth": "1980-05-06",
        "sex": "male",
        "surgeries": [{"date": "1998-06-25", "procedure": "appendectomy"}],
    }

    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("different@example.com")
    # No bound patient_id (OAuth caller whose email doesn't match any patient).
    _verified_patient_id.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

    raw = await get_patient_context(ctx, patient_slug="victim-slug")
    result = json.loads(raw)

    # Canonical empty shape (list_patients style), NO PHI fields.
    assert result.get("patients") == []
    assert "guidance" in result
    assert "name" not in result
    assert "date_of_birth" not in result
    assert "surgeries" not in result
    assert "sex" not in result
    # Sensitive values must not appear anywhere in the payload.
    assert "Victim Patient" not in raw
    assert "1980-05-06" not in raw
    assert "1998-06-25" not in raw


async def test_get_patient_context_admin_sees_full_context(db: Database):
    """Admin callers keep full access — used by Oncoteam and DASHBOARD_ADMIN_EMAILS."""
    from oncofiles import patient_context
    from oncofiles.tools.patient import get_patient_context

    victim = _make_patient("admin-target", "Admin Target", "owner@example.com")
    await db.insert_patient(victim)
    patient_context._contexts[victim.patient_id] = {
        "name": "Admin Target",
        "date_of_birth": "1980-05-06",
    }

    _verified_caller_is_admin.set(True)
    _verified_caller_email.set("")
    _verified_patient_id.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

    result = json.loads(await get_patient_context(ctx, patient_slug="admin-target"))
    assert result.get("name") == "Admin Target"
    assert result.get("date_of_birth") == "1980-05-06"
    _verified_caller_is_admin.set(False)


async def test_get_patient_context_caregiver_email_match_grants_access(db: Database):
    """OAuth caller whose email matches caregiver_email sees full context."""
    from oncofiles import patient_context
    from oncofiles.tools.patient import get_patient_context

    target = _make_patient("friend-slug", "Friend Target", "friend@example.com")
    await db.insert_patient(target)
    patient_context._contexts[target.patient_id] = {
        "name": "Friend Target",
        "date_of_birth": "1970-01-01",
    }

    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("friend@example.com")
    _verified_patient_id.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

    result = json.loads(await get_patient_context(ctx, patient_slug="friend-slug"))
    assert result.get("name") == "Friend Target"
    assert result.get("date_of_birth") == "1970-01-01"


async def test_get_patient_context_bearer_bound_to_own_pid_sees_context(db: Database):
    """Patient-bearer caller bound to exactly this pid retains access."""
    from oncofiles import patient_context
    from oncofiles.patient_middleware import _current_patient_id
    from oncofiles.tools.patient import get_patient_context

    target = _make_patient("bearer-slug", "Bearer Target", "someone@example.com")
    await db.insert_patient(target)
    patient_context._contexts[target.patient_id] = {
        "name": "Bearer Target",
        "date_of_birth": "1990-12-12",
    }

    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")
    # _get_patient_id reads from patient_middleware._current_patient_id, not
    # _verified_patient_id — override the fixture's JANA binding to simulate
    # a patient-bearer token bound to this specific pid.
    token = _current_patient_id.set(target.patient_id)
    try:
        ctx = MagicMock()
        ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

        result = json.loads(await get_patient_context(ctx, patient_slug="bearer-slug"))
        assert result.get("name") == "Bearer Target"
        assert result.get("date_of_birth") == "1990-12-12"
    finally:
        _current_patient_id.reset(token)
