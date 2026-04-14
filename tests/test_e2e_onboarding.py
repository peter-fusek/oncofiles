"""End-to-end onboarding flow test (#351).

Simulates: patient creation → token auth → document insertion → tool access.
External services (GDrive, Gmail) are not tested here — see test_sync.py.
"""

from __future__ import annotations

import pytest

from oncofiles.database import Database
from oncofiles.models import DocumentCategory, Patient
from oncofiles.patient_middleware import _current_patient_id
from tests.helpers import make_doc


@pytest.fixture
async def fresh_db():
    """Create a fresh in-memory database with no patient context set."""
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


# ── Full onboarding flow ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_onboarding_flow(fresh_db):
    """Simulate complete new-user onboarding: create patient → token → docs → query."""
    db = fresh_db

    # Step 1: Create patient (simulates POST /api/patients)
    patient = Patient(
        patient_id="e2e-test",
        display_name="E2E Test Patient",
        caregiver_email="e2e@example.com",
        preferred_lang="en",
    )
    created = await db.insert_patient(patient)
    assert created.slug == "e2e-test"
    pid = created.patient_id
    assert len(pid) == 36  # UUID

    # Step 2: Generate bearer token (simulates token shown in wizard)
    token = await db.create_patient_token(pid, label="e2e-initial")
    assert token.startswith("onco_")

    # Step 3: Verify token resolves back to patient
    resolved = await db.resolve_patient_from_token(token)
    assert resolved == pid

    # Step 4: Patient has zero documents initially
    count = await db.count_documents(patient_id=pid)
    assert count == 0

    # Step 5: Simulate first sync — insert documents
    doc1 = make_doc(
        file_id="e2e_file_1",
        filename="20260414_E2ETest_Labs_BloodCount.pdf",
        institution="TestHospital",
        category=DocumentCategory.LABS,
    )
    doc2 = make_doc(
        file_id="e2e_file_2",
        filename="20260414_E2ETest_Report_Consultation.pdf",
        institution="TestHospital",
        category=DocumentCategory.REPORT,
    )
    await db.insert_document(doc1, patient_id=pid)
    await db.insert_document(doc2, patient_id=pid)

    # Step 6: Verify documents appear
    count = await db.count_documents(patient_id=pid)
    assert count == 2

    docs = await db.list_documents(limit=10, offset=0, patient_id=pid)
    assert len(docs) == 2
    filenames = {d.filename for d in docs}
    assert "20260414_E2ETest_Labs_BloodCount.pdf" in filenames
    assert "20260414_E2ETest_Report_Consultation.pdf" in filenames

    # Step 7: Verify patient isolation — other patients can't see these docs
    other = Patient(patient_id="e2e-other", display_name="Other Patient")
    other_created = await db.insert_patient(other)
    other_count = await db.count_documents(patient_id=other_created.patient_id)
    assert other_count == 0

    other_docs = await db.list_documents(limit=10, offset=0, patient_id=other_created.patient_id)
    assert len(other_docs) == 0


@pytest.mark.asyncio
async def test_patient_id_guard_in_tools(fresh_db):
    """_get_patient_id raises when no patient context is set."""
    from oncofiles.tools._helpers import _get_patient_id

    # No patient context set — should raise
    token = _current_patient_id.set("")
    try:
        with pytest.raises(ValueError, match="No patient selected"):
            _get_patient_id()
    finally:
        _current_patient_id.reset(token)


@pytest.mark.asyncio
async def test_patient_id_guard_allows_bootstrapping(fresh_db):
    """_get_patient_id(required=False) works for list_patients/select_patient."""
    from oncofiles.tools._helpers import _get_patient_id

    token = _current_patient_id.set("")
    try:
        result = _get_patient_id(required=False)
        assert result == ""
    finally:
        _current_patient_id.reset(token)


@pytest.mark.asyncio
async def test_document_limit_blocks_over_limit(fresh_db):
    """Document limit prevents insertion beyond MAX_DOCUMENTS_PER_PATIENT."""
    from unittest.mock import patch

    db = fresh_db
    patient = Patient(patient_id="limit-e2e", display_name="Limit E2E")
    created = await db.insert_patient(patient)
    pid = created.patient_id

    with patch("oncofiles.config.MAX_DOCUMENTS_PER_PATIENT", 3):
        for i in range(3):
            doc = make_doc(file_id=f"limit_e2e_{i}", filename=f"doc_{i}.pdf")
            await db.insert_document(doc, patient_id=pid)

        # 4th should fail
        doc4 = make_doc(file_id="limit_e2e_4", filename="doc_4.pdf")
        with pytest.raises(ValueError, match="Document limit reached"):
            await db.insert_document(doc4, patient_id=pid)

    # Count should still be 3
    assert await db.count_documents(patient_id=pid) == 3


@pytest.mark.asyncio
async def test_slug_resolution_roundtrip(fresh_db):
    """Patient slug resolves to UUID and back."""
    db = fresh_db
    patient = Patient(patient_id="slug-e2e", display_name="Slug E2E")
    created = await db.insert_patient(patient)

    # Slug → UUID
    resolved = await db.resolve_patient_id("slug-e2e")
    assert resolved == created.patient_id

    # UUID → patient
    fetched = await db.get_patient(created.patient_id)
    assert fetched is not None
    assert fetched.display_name == "Slug E2E"


@pytest.mark.asyncio
async def test_multiple_tokens_per_patient(fresh_db):
    """A patient can have multiple bearer tokens (recovery scenario)."""
    db = fresh_db
    patient = Patient(patient_id="multi-token", display_name="Multi Token")
    created = await db.insert_patient(patient)
    pid = created.patient_id

    token1 = await db.create_patient_token(pid, label="initial")
    token2 = await db.create_patient_token(pid, label="recovery")

    assert token1 != token2
    assert await db.resolve_patient_from_token(token1) == pid
    assert await db.resolve_patient_from_token(token2) == pid
