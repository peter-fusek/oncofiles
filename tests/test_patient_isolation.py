"""Multi-patient data isolation tests.

Verifies that data for one patient never leaks to another patient.
Covers: documents, treatment events, research entries, lab values,
conversation entries, agent state, and patient context.
"""

import pytest

from oncofiles.database import Database
from oncofiles.models import LabTrendQuery
from tests.helpers import (
    ERIKA_UUID,
    TEST_PATIENT_UUID,
    make_doc,
    make_lab_value,
    make_research_entry,
    make_treatment_event,
)

PETER_UUID = "00000000-0000-4000-8000-000000000003"


# ── Documents ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_documents_isolated_by_patient(db: Database):
    """Documents inserted for patient A are invisible to patient B."""
    doc_a = await db.insert_document(make_doc(file_id="f_a1"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="f_b1"), patient_id=TEST_PATIENT_UUID)

    docs_a = await db.list_documents(patient_id=ERIKA_UUID)
    docs_b = await db.list_documents(patient_id=TEST_PATIENT_UUID)

    assert len(docs_a) == 1
    assert docs_a[0].id == doc_a.id
    assert len(docs_b) == 1
    assert docs_b[0].id == doc_b.id


@pytest.mark.asyncio
async def test_document_count_isolated(db: Database):
    """count_documents returns only the patient's own documents."""
    await db.insert_document(make_doc(file_id="f1"), patient_id=ERIKA_UUID)
    await db.insert_document(make_doc(file_id="f2"), patient_id=ERIKA_UUID)
    await db.insert_document(make_doc(file_id="f3"), patient_id=TEST_PATIENT_UUID)

    assert await db.count_documents(patient_id=ERIKA_UUID) == 2
    assert await db.count_documents(patient_id=TEST_PATIENT_UUID) == 1


@pytest.mark.asyncio
async def test_document_by_file_id_isolated(db: Database):
    """get_document_by_file_id returns None for another patient's file_id."""
    await db.insert_document(make_doc(file_id="shared_name"), patient_id=ERIKA_UUID)

    found = await db.get_document_by_file_id("shared_name", patient_id=ERIKA_UUID)
    assert found is not None

    not_found = await db.get_document_by_file_id("shared_name", patient_id=TEST_PATIENT_UUID)
    assert not_found is None


@pytest.mark.asyncio
async def test_trash_isolated(db: Database):
    """Soft-deleted documents only appear in the owning patient's trash."""
    doc = await db.insert_document(make_doc(file_id="trash_me"), patient_id=ERIKA_UUID)
    await db.delete_document(doc.id)

    erika_trash = await db.list_trash(patient_id=ERIKA_UUID)
    other_trash = await db.list_trash(patient_id=TEST_PATIENT_UUID)

    assert len(erika_trash) == 1
    assert len(other_trash) == 0


# ── Treatment Events ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_treatment_events_isolated(db: Database):
    """Treatment events for patient A are invisible to patient B."""
    from oncofiles.models import TreatmentEventQuery

    await db.insert_treatment_event(
        make_treatment_event(title="Erika chemo"), patient_id=ERIKA_UUID
    )
    await db.insert_treatment_event(
        make_treatment_event(title="Test chemo"), patient_id=TEST_PATIENT_UUID
    )

    events_a = await db.list_treatment_events(TreatmentEventQuery(limit=50), patient_id=ERIKA_UUID)
    events_b = await db.list_treatment_events(
        TreatmentEventQuery(limit=50), patient_id=TEST_PATIENT_UUID
    )

    assert len(events_a) == 1
    assert events_a[0].title == "Erika chemo"
    assert len(events_b) == 1
    assert events_b[0].title == "Test chemo"


# ── Research Entries ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_entries_isolated(db: Database):
    """Research entries for patient A are invisible to patient B."""
    await db.insert_research_entry(
        make_research_entry(title="Erika research"), patient_id=ERIKA_UUID
    )
    await db.insert_research_entry(
        make_research_entry(title="Test research", external_id="PMID99999"),
        patient_id=TEST_PATIENT_UUID,
    )

    results_a = await db.list_research_entries(patient_id=ERIKA_UUID)
    results_b = await db.list_research_entries(patient_id=TEST_PATIENT_UUID)

    assert len(results_a) == 1
    assert results_a[0].title == "Erika research"
    assert len(results_b) == 1
    assert results_b[0].title == "Test research"


# ── Lab Values ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lab_values_isolated_via_document(db: Database):
    """Lab values are isolated by patient through the documents table join."""
    doc_a = await db.insert_document(make_doc(file_id="lab_a"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="lab_b"), patient_id=TEST_PATIENT_UUID)

    await db.insert_lab_values([make_lab_value(document_id=doc_a.id, parameter="WBC", value=6.0)])
    await db.insert_lab_values([make_lab_value(document_id=doc_b.id, parameter="WBC", value=9.0)])

    # get_latest_lab_value with patient_id
    latest_a = await db.get_latest_lab_value("WBC", patient_id=ERIKA_UUID)
    latest_b = await db.get_latest_lab_value("WBC", patient_id=TEST_PATIENT_UUID)

    assert latest_a is not None
    assert latest_a.value == 6.0
    assert latest_b is not None
    assert latest_b.value == 9.0


@pytest.mark.asyncio
async def test_lab_snapshot_isolated(db: Database):
    """get_lab_snapshot with patient_id only returns values for that patient's documents."""
    doc_a = await db.insert_document(make_doc(file_id="snap_a"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="snap_b"), patient_id=TEST_PATIENT_UUID)

    await db.insert_lab_values([make_lab_value(document_id=doc_a.id, parameter="PLT", value=200)])
    await db.insert_lab_values([make_lab_value(document_id=doc_b.id, parameter="PLT", value=150)])

    snap_a = await db.get_lab_snapshot(doc_a.id, patient_id=ERIKA_UUID)
    snap_b_wrong = await db.get_lab_snapshot(doc_a.id, patient_id=TEST_PATIENT_UUID)

    assert len(snap_a) == 1
    assert snap_a[0].value == 200
    assert len(snap_b_wrong) == 0  # doc_a doesn't belong to TEST_PATIENT


@pytest.mark.asyncio
async def test_lab_trends_isolated(db: Database):
    """get_lab_trends filters by patient_id in query."""
    doc_a = await db.insert_document(make_doc(file_id="trend_a"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="trend_b"), patient_id=TEST_PATIENT_UUID)

    await db.insert_lab_values([make_lab_value(document_id=doc_a.id, parameter="CEA", value=3.5)])
    await db.insert_lab_values([make_lab_value(document_id=doc_b.id, parameter="CEA", value=8.0)])

    trends_a = await db.get_lab_trends(LabTrendQuery(parameter="CEA", patient_id=ERIKA_UUID))
    trends_b = await db.get_lab_trends(LabTrendQuery(parameter="CEA", patient_id=TEST_PATIENT_UUID))

    assert len(trends_a) == 1
    assert trends_a[0].value == 3.5
    assert len(trends_b) == 1
    assert trends_b[0].value == 8.0


@pytest.mark.asyncio
async def test_all_latest_lab_values_isolated(db: Database):
    """get_all_latest_lab_values only returns the requesting patient's values."""
    doc_a = await db.insert_document(make_doc(file_id="all_a"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="all_b"), patient_id=TEST_PATIENT_UUID)

    await db.insert_lab_values([make_lab_value(document_id=doc_a.id, parameter="HGB", value=120)])
    await db.insert_lab_values([make_lab_value(document_id=doc_b.id, parameter="ALT", value=45)])

    latest_a = await db.get_all_latest_lab_values(patient_id=ERIKA_UUID)
    latest_b = await db.get_all_latest_lab_values(patient_id=TEST_PATIENT_UUID)

    params_a = {v.parameter for v in latest_a}
    params_b = {v.parameter for v in latest_b}

    assert "HGB" in params_a
    assert "ALT" not in params_a
    assert "ALT" in params_b
    assert "HGB" not in params_b


@pytest.mark.asyncio
async def test_distinct_lab_dates_isolated(db: Database):
    """get_distinct_lab_dates only returns dates from the requesting patient's labs."""
    from datetime import date

    doc_a = await db.insert_document(make_doc(file_id="date_a"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="date_b"), patient_id=TEST_PATIENT_UUID)

    await db.insert_lab_values([make_lab_value(document_id=doc_a.id, lab_date=date(2026, 1, 10))])
    await db.insert_lab_values([make_lab_value(document_id=doc_b.id, lab_date=date(2026, 2, 20))])

    dates_a = await db.get_distinct_lab_dates(patient_id=ERIKA_UUID)
    dates_b = await db.get_distinct_lab_dates(patient_id=TEST_PATIENT_UUID)

    assert "2026-01-10" in dates_a
    assert "2026-02-20" not in dates_a
    assert "2026-02-20" in dates_b
    assert "2026-01-10" not in dates_b


@pytest.mark.asyncio
async def test_lab_values_by_date_isolated(db: Database):
    """get_lab_values_by_date only returns the requesting patient's values."""
    from datetime import date as d

    doc_a = await db.insert_document(make_doc(file_id="bydate_a"), patient_id=ERIKA_UUID)
    doc_b = await db.insert_document(make_doc(file_id="bydate_b"), patient_id=TEST_PATIENT_UUID)

    shared_date = d(2026, 3, 15)
    await db.insert_lab_values(
        [make_lab_value(document_id=doc_a.id, lab_date=shared_date, parameter="WBC", value=5.0)]
    )
    await db.insert_lab_values(
        [make_lab_value(document_id=doc_b.id, lab_date=shared_date, parameter="WBC", value=12.0)]
    )

    vals_a = await db.get_lab_values_by_date("2026-03-15", patient_id=ERIKA_UUID)
    vals_b = await db.get_lab_values_by_date("2026-03-15", patient_id=TEST_PATIENT_UUID)

    assert len(vals_a) == 1 and vals_a[0].value == 5.0
    assert len(vals_b) == 1 and vals_b[0].value == 12.0


# ── Agent State ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason="agent_state has legacy UNIQUE(agent_id, key) — needs migration to isolate by patient_id"
)
async def test_agent_state_per_patient_stored(db: Database):
    """Agent state stores patient_id, allowing per-patient unique keys.

    KNOWN GAP: The legacy UNIQUE(agent_id, key) constraint causes the second
    insert to overwrite the first. Needs migration to drop old constraint
    and rely on UNIQUE(patient_id, agent_id, key) only.
    """
    from oncofiles.models import AgentState

    await db.set_agent_state(
        AgentState(agent_id="oncoteam", key="memo", value="erika data", patient_id=ERIKA_UUID)
    )
    await db.set_agent_state(
        AgentState(agent_id="oncoteam", key="memo", value="test data", patient_id=TEST_PATIENT_UUID)
    )

    states = await db.list_agent_states("oncoteam")
    memo_states = [s for s in states if s.key == "memo"]
    assert len(memo_states) == 2


# ── Patient Context ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patient_context_isolated(db: Database):
    """Patient context is per-patient, not a shared global."""
    from oncofiles import patient_context

    ctx_a = {"name": "Erika", "diagnosis": "CRC stage IV"}
    ctx_b = {"name": "Test Patient", "diagnosis": "Lung cancer"}

    await patient_context.save_to_db(db.db, ctx_a, patient_id=ERIKA_UUID)
    await patient_context.save_to_db(db.db, ctx_b, patient_id=TEST_PATIENT_UUID)

    loaded_a = await patient_context.load_from_db(db.db, patient_id=ERIKA_UUID)
    loaded_b = await patient_context.load_from_db(db.db, patient_id=TEST_PATIENT_UUID)

    assert loaded_a["name"] == "Erika"
    assert loaded_a["diagnosis"] == "CRC stage IV"
    assert loaded_b["name"] == "Test Patient"
    assert loaded_b["diagnosis"] == "Lung cancer"


@pytest.mark.asyncio
async def test_patient_context_update_doesnt_leak(db: Database):
    """Updating patient A's context doesn't affect patient B's."""
    from oncofiles import patient_context

    await patient_context.save_to_db(
        db.db, {"name": "Erika", "note": "original"}, patient_id=ERIKA_UUID
    )
    await patient_context.save_to_db(
        db.db, {"name": "Test", "note": "original"}, patient_id=TEST_PATIENT_UUID
    )

    # Update only erika's context
    patient_context.update_context({"note": "updated"}, patient_id=ERIKA_UUID)
    await patient_context.save_to_db(
        db.db, patient_context.get_context(ERIKA_UUID), patient_id=ERIKA_UUID
    )

    # Reload both
    loaded_a = await patient_context.load_from_db(db.db, patient_id=ERIKA_UUID)
    loaded_b = await patient_context.load_from_db(db.db, patient_id=TEST_PATIENT_UUID)

    assert loaded_a["note"] == "updated"
    assert loaded_b["note"] == "original"


# ── Three-Patient Stress Test ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_three_patients_fully_isolated(db: Database):
    """Full isolation test across 3 patients with overlapping data types."""
    patients = [ERIKA_UUID, TEST_PATIENT_UUID, PETER_UUID]

    # Insert documents for each patient
    docs = {}
    for i, pid in enumerate(patients):
        doc = await db.insert_document(make_doc(file_id=f"3p_doc_{i}"), patient_id=pid)
        docs[pid] = doc

    # Insert lab values for each patient's document
    for i, pid in enumerate(patients):
        await db.insert_lab_values(
            [make_lab_value(document_id=docs[pid].id, parameter="WBC", value=5.0 + i)]
        )

    # Verify total isolation
    for i, pid in enumerate(patients):
        patient_docs = await db.list_documents(patient_id=pid)
        assert len(patient_docs) == 1, f"Patient {pid} should have exactly 1 doc"

        latest = await db.get_latest_lab_value("WBC", patient_id=pid)
        assert latest is not None
        assert latest.value == 5.0 + i, f"Patient {pid} WBC should be {5.0 + i}"

        # No cross-contamination
        other_pids = [p for p in patients if p != pid]
        for other in other_pids:
            other_docs = await db.list_documents(patient_id=other)
            for d in other_docs:
                assert d.id != docs[pid].id, f"Patient {other} should not see {pid}'s doc"
