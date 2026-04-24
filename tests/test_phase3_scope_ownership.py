"""Phase 3 of the v5.15 security sweep (#487).

Locks down:
  * `_is_admin_caller()` + `_verified_caller_is_admin` ContextVar wiring
  * `_check_ownership_or_admin` enforcement on integer-id read tools
  * `_require_admin_or_raise` enforcement on admin-only tools
  * `list_patients` filter by caller caregiver_email (non-admin)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

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
from tests.helpers import ERIKA_UUID


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
    err = _check_ownership_or_admin("email_entry", 5, ERIKA_UUID, ERIKA_UUID)
    assert err is None


def test_ownership_denies_other_patient():
    _verified_caller_is_admin.set(False)
    err = _check_ownership_or_admin("email_entry", 5, "other-uuid", ERIKA_UUID)
    assert err is not None
    assert "access denied" in err
    # Must NOT leak the real owner's UUID in the error.
    assert "other-uuid" not in err


def test_ownership_admin_bypasses_pid_check():
    _verified_caller_is_admin.set(True)
    err = _check_ownership_or_admin("email_entry", 5, "other-uuid", ERIKA_UUID)
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
    err = _check_ownership_or_admin("email_entry", 5, "", ERIKA_UUID)
    assert err is not None


# ── reassign_document admin gate ────────────────────────────────────


async def test_reassign_document_rejects_non_admin(db: Database):
    from oncofiles.tools.documents import reassign_document

    _verified_caller_is_admin.set(False)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await reassign_document(ctx, doc_id=1, target_patient_slug="erika"))
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
    # Caller is a patient bearer bound to Erika (the seeded test default).
    _verified_patient_id.set(ERIKA_UUID)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    result = json.loads(await list_patients(ctx))
    if isinstance(result, dict):
        # Empty shape — no patients matched the filter. Acceptable.
        assert result.get("patients") == []
        return
    slugs = {p["slug"] for p in result if isinstance(p, dict) and "slug" in p}
    # "omega" must NOT appear — we bound only to Erika.
    assert "omega" not in slugs


# ── get_email / get_calendar_event ownership ────────────────────────


async def test_get_email_denies_other_patient(db: Database):
    """Integer-id brute force across patients must be blocked (#487 C1)."""
    from oncofiles.tools.integrations import get_email

    # Insert a fake email entry via direct query — we only need a row with
    # patient_id != caller.
    entry = MagicMock()
    entry.id = 42
    entry.patient_id = "owner-of-this-email"
    entry.gmail_message_id = "x"
    entry.thread_id = "t"
    entry.subject = "secret"
    entry.sender = "sender@example.com"
    entry.recipients = "[]"
    entry.date = MagicMock()
    entry.date.isoformat = lambda: "2026-01-01T00:00:00"
    entry.body_snippet = "snippet"
    entry.body_text = "body"
    entry.labels = "[]"
    entry.has_attachments = False
    entry.is_medical = True
    entry.ai_summary = None
    entry.ai_relevance_score = None
    entry.structured_metadata = None
    entry.linked_document_ids = "[]"
    entry.created_at = None

    _verified_caller_is_admin.set(False)
    _verified_patient_id.set("caller-pid-not-owner")
    _verified_caller_email.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

    with patch.object(db, "get_email_entry", new=AsyncMock(return_value=entry)):
        result = json.loads(await get_email(ctx, email_entry_id=42))
    assert "error" in result
    assert "access denied" in result["error"]
    # Sensitive fields must not appear in the denial response.
    assert "secret" not in result.get("error", "")
    assert "sender@example.com" not in result.get("error", "")


async def test_get_email_admin_sees_other_patient(db: Database):
    from oncofiles.tools.integrations import get_email

    entry = MagicMock()
    entry.id = 42
    entry.patient_id = "owner-of-this-email"
    entry.gmail_message_id = "x"
    entry.thread_id = "t"
    entry.subject = "subject"
    entry.sender = "s@example.com"
    entry.recipients = "[]"
    entry.date = MagicMock()
    entry.date.isoformat = lambda: "2026-01-01T00:00:00"
    entry.body_snippet = "snippet"
    entry.body_text = "body"
    entry.labels = "[]"
    entry.has_attachments = False
    entry.is_medical = True
    entry.ai_summary = None
    entry.ai_relevance_score = None
    entry.structured_metadata = None
    entry.linked_document_ids = "[]"
    entry.created_at = None

    _verified_caller_is_admin.set(True)
    _verified_patient_id.set("")
    _verified_caller_email.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

    with patch.object(db, "get_email_entry", new=AsyncMock(return_value=entry)):
        result = json.loads(await get_email(ctx, email_entry_id=42))
    assert "error" not in result, result
    assert result["id"] == 42
    _verified_caller_is_admin.set(False)


async def test_get_calendar_event_denies_other_patient(db: Database):
    from oncofiles.tools.integrations import get_calendar_event

    entry = MagicMock()
    entry.id = 7
    entry.patient_id = "owner-of-this-event"
    entry.google_event_id = "g"
    entry.summary = "private appt"
    entry.description = "d"
    entry.start_time = MagicMock()
    entry.start_time.isoformat = lambda: "2026-01-01T00:00:00"
    entry.end_time = None
    entry.location = "x"
    entry.attendees = "[]"
    entry.recurrence = None
    entry.status = "confirmed"
    entry.is_medical = True
    entry.ai_summary = None
    entry.treatment_event_id = None
    entry.created_at = None

    _verified_caller_is_admin.set(False)
    _verified_patient_id.set("caller-pid-not-owner")
    _verified_caller_email.set("")
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}

    with patch.object(db, "get_calendar_entry", new=AsyncMock(return_value=entry)):
        result = json.loads(await get_calendar_event(ctx, calendar_entry_id=7))
    assert "error" in result
    assert "access denied" in result["error"]
    assert "private appt" not in result.get("error", "")
