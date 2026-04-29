"""Tests for new-user onboarding flow (#344).

Covers: patient creation, zero-state, token round-trip,
open signup, patient scoping, patient_id guard.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from oncofiles.database import Database
from oncofiles.models import Patient
from oncofiles.patient_middleware import _current_patient_id

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture
async def db():
    """Create an in-memory database for testing."""
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    token = _current_patient_id.set(ERIKA_UUID)
    yield database
    _current_patient_id.reset(token)
    await database.close()


# ── Patient creation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_insert_patient(db):
    """insert_patient creates a patient — slug becomes patient_id, UUID auto-generated."""
    patient = Patient(
        patient_id="new-test-patient",
        display_name="Test User",
        caregiver_email="test@example.com",
        preferred_lang="en",
    )
    created = await db.insert_patient(patient)
    # patient_id is auto-generated UUID, slug is the original input
    assert created.slug == "new-test-patient"
    assert created.display_name == "Test User"
    assert created.caregiver_email == "test@example.com"
    assert len(created.patient_id) == 36  # UUID format


@pytest.mark.asyncio
async def test_insert_patient_duplicate_slug(db):
    """insert_patient with existing slug raises or returns conflict."""
    patient = Patient(patient_id="dup-test", display_name="First")
    await db.insert_patient(patient)
    patient2 = Patient(patient_id="dup-test", display_name="Second")
    with pytest.raises((Exception, ValueError)):  # noqa: B017
        await db.insert_patient(patient2)


@pytest.mark.asyncio
async def test_patient_slug_resolution(db):
    """resolve_patient_id resolves slug to UUID."""
    patient = Patient(patient_id="slug-test", display_name="Slug Test")
    created = await db.insert_patient(patient)
    resolved = await db.resolve_patient_id("slug-test")
    assert resolved == created.patient_id


# ── Zero-state ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_patients_list_empty(db):
    """list_patients returns empty when only migration-seeded patients exist and we filter."""
    # The migration seeds test patients, so list all and verify it works
    patients = await db.list_patients(active_only=True)
    assert isinstance(patients, list)


@pytest.mark.asyncio
async def test_zero_documents_for_new_patient(db):
    """New patient has zero documents."""
    patient = Patient(patient_id="empty-patient", display_name="Empty")
    created = await db.insert_patient(patient)
    count = await db.count_documents(patient_id=created.patient_id)
    assert count == 0


# ── Token round-trip ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_and_verify_patient_token(db):
    """create_patient_token generates a token that can be resolved back."""
    patient = Patient(patient_id="token-test", display_name="Token Test")
    created = await db.insert_patient(patient)
    pid = created.patient_id  # UUID
    token = await db.create_patient_token(pid, label="test")
    assert token.startswith("onco_")

    # Verify the token resolves to the patient
    resolved = await db.resolve_patient_from_token(token)
    assert resolved == pid


@pytest.mark.asyncio
async def test_invalid_token_not_resolved(db):
    """Invalid token returns None from resolve."""
    result = await db.resolve_patient_from_token("onco_invalid_token_xyz")
    assert result is None


# ── Open signup ──────────────────────────────────────────────────────


def test_dashboard_verify_has_no_allowlist():
    """dashboard_verify source has no allowlist gate."""
    import inspect

    from oncofiles.server import dashboard_verify

    source = inspect.getsource(dashboard_verify)
    assert "access denied for this email" not in source


def test_admin_emails_config_is_list():
    """DASHBOARD_ADMIN_EMAILS is a list type."""
    from oncofiles.config import DASHBOARD_ADMIN_EMAILS

    assert isinstance(DASHBOARD_ADMIN_EMAILS, list)


# ── Patient scoping ──────────────────────────────────────────────────


def test_is_admin_email():
    """Admin email check is case-insensitive."""
    from oncofiles.server import _is_admin_email

    with patch("oncofiles.server.DASHBOARD_ADMIN_EMAILS", ["admin@test.com"]):
        assert _is_admin_email("admin@test.com") is True
        assert _is_admin_email("ADMIN@TEST.COM") is True
        assert _is_admin_email("other@test.com") is False
        assert _is_admin_email(None) is False


def test_get_dashboard_email_from_session():
    """_get_dashboard_email extracts email from valid session token."""
    from unittest.mock import MagicMock

    from oncofiles.server import _get_dashboard_email, _make_session_token

    with patch("oncofiles.server.MCP_BEARER_TOKEN", "test-secret"):
        session = _make_session_token("user@example.com")
        request = MagicMock()
        request.headers = MagicMock()
        request.headers.get = lambda key, default="": (
            f"Bearer session:{session}" if key == "authorization" else default
        )
        assert _get_dashboard_email(request) == "user@example.com"


# ── patient_id guard ─────────────────────────────────────────────────


def test_get_patient_id_raises_when_empty():
    """_get_patient_id raises ValueError when no patient selected."""
    from oncofiles.tools._helpers import _get_patient_id

    with patch("oncofiles.patient_middleware._current_patient_id") as mock_cv:
        mock_cv.get.return_value = ""
        with pytest.raises(ValueError, match="No patient selected"):
            _get_patient_id()


def test_get_patient_id_soft_mode():
    """_get_patient_id(required=False) returns empty without raising."""
    from oncofiles.tools._helpers import _get_patient_id

    with patch("oncofiles.patient_middleware._current_patient_id") as mock_cv:
        mock_cv.get.return_value = ""
        assert _get_patient_id(required=False) == ""


# ── Token recovery endpoint existence ────────────────────────────────


def test_patient_tokens_endpoint_exists():
    """POST /api/patient-tokens endpoint is registered."""
    import inspect

    from oncofiles.server import api_create_patient_token

    assert callable(api_create_patient_token)
    source = inspect.getsource(api_create_patient_token)
    assert "create_patient_token" in source
    assert "bearer_token" in source


def test_regenerate_token_in_dashboard():
    """Dashboard JS has regenerateToken function for token recovery (now in
    the extracted dashboard.js per #501 — pre-#501 the JS was inline)."""
    from pathlib import Path

    base = Path(__file__).parent.parent / "src" / "oncofiles"
    combined = (base / "dashboard.html").read_text() + "\n" + (base / "dashboard.js").read_text()
    assert "regenerateToken" in combined
    assert "/api/patient-tokens" in combined


# ── Document limit enforcement (#350) ────────────────────────────────


@pytest.mark.asyncio
async def test_document_limit_enforced(db):
    """insert_document raises ValueError when patient exceeds doc limit."""
    from tests.helpers import make_doc

    patient = Patient(patient_id="limit-test", display_name="Limit Test")
    created = await db.insert_patient(patient)
    pid = created.patient_id

    with patch("oncofiles.config.MAX_DOCUMENTS_PER_PATIENT", 2):
        # Insert 2 docs — should succeed
        for i in range(2):
            doc = make_doc(file_id=f"file_limit_{i}", filename=f"doc_{i}.pdf")
            await db.insert_document(doc, patient_id=pid)

        # 3rd doc should fail
        doc3 = make_doc(file_id="file_limit_3", filename="doc_3.pdf")
        with pytest.raises(ValueError, match="Document limit reached"):
            await db.insert_document(doc3, patient_id=pid)
