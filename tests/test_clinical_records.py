"""Tests for clinical_records + audit trail + notes (#450 Phase 1)."""

from __future__ import annotations

import json

import pytest

from oncofiles.database import Database
from oncofiles.models import ClinicalRecord, ClinicalRecordNote, ClinicalRecordQuery
from tests.conftest import ERIKA_UUID

SECOND_UUID = "00000000-0000-4000-8000-000000000002"


async def _seed_second_patient(db: Database) -> None:
    """Add a second patient so cross-patient isolation can be tested."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, "bob-test", "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


def _make_record(
    patient_id: str = ERIKA_UUID,
    *,
    record_type: str = "lab",
    param: str = "CEA",
    value_num: float | None = 5.2,
    **overrides,
) -> ClinicalRecord:
    defaults = {
        "patient_id": patient_id,
        "record_type": record_type,
        "param": param,
        "value_num": value_num,
        "unit": "ng/mL",
        "occurred_at": "2026-01-15",
        "source": "manual",
        "created_by": "caregiver@example.com",
    }
    defaults.update(overrides)
    return ClinicalRecord(**defaults)


# ── Basic CRUD ──────────────────────────────────────────────────────────


async def test_insert_returns_stored_record_with_id(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    assert stored.id is not None
    assert stored.patient_id == ERIKA_UUID
    assert stored.param == "CEA"
    assert stored.value_num == 5.2
    assert stored.created_at is not None
    assert stored.updated_at is not None


async def test_get_returns_none_for_unknown_id(db: Database):
    assert await db.get_clinical_record(999_999, patient_id=ERIKA_UUID) is None


async def test_list_returns_inserted_record(db: Database):
    await db.insert_clinical_record(_make_record())
    results = await db.list_clinical_records(ClinicalRecordQuery(), patient_id=ERIKA_UUID)
    assert len(results) == 1
    assert results[0].param == "CEA"


async def test_list_filters_by_record_type(db: Database):
    await db.insert_clinical_record(_make_record(record_type="lab"))
    await db.insert_clinical_record(
        _make_record(record_type="biomarker", param="KRAS", value_num=None, value_text="G12S")
    )
    labs = await db.list_clinical_records(
        ClinicalRecordQuery(record_type="lab"), patient_id=ERIKA_UUID
    )
    biomarkers = await db.list_clinical_records(
        ClinicalRecordQuery(record_type="biomarker"), patient_id=ERIKA_UUID
    )
    assert len(labs) == 1 and labs[0].param == "CEA"
    assert len(biomarkers) == 1 and biomarkers[0].param == "KRAS"


async def test_list_filters_by_date_range(db: Database):
    await db.insert_clinical_record(_make_record(occurred_at="2026-01-01"))
    await db.insert_clinical_record(_make_record(occurred_at="2026-03-01"))
    await db.insert_clinical_record(_make_record(occurred_at="2026-06-01"))
    mid = await db.list_clinical_records(
        ClinicalRecordQuery(since="2026-02-01", until="2026-05-01"),
        patient_id=ERIKA_UUID,
    )
    assert len(mid) == 1
    assert mid[0].occurred_at == "2026-03-01"


async def test_list_orders_newest_first(db: Database):
    await db.insert_clinical_record(_make_record(occurred_at="2026-01-01"))
    await db.insert_clinical_record(_make_record(occurred_at="2026-06-01"))
    await db.insert_clinical_record(_make_record(occurred_at="2026-03-01"))
    results = await db.list_clinical_records(ClinicalRecordQuery(), patient_id=ERIKA_UUID)
    assert [r.occurred_at for r in results] == ["2026-06-01", "2026-03-01", "2026-01-01"]


async def test_update_applies_changes(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    updated = await db.update_clinical_record(
        rec.id,
        {"value_num": 6.8, "status": "abnormal"},
        changed_by="caregiver@example.com",
        source="manual",
        patient_id=ERIKA_UUID,
    )
    assert updated is not None
    assert updated.value_num == 6.8
    assert updated.status == "abnormal"
    assert updated.unit == "ng/mL"  # unchanged


async def test_update_ignores_unknown_fields(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    # patient_id and id are NOT in the allow-list — must be dropped silently
    updated = await db.update_clinical_record(
        rec.id,
        {"patient_id": SECOND_UUID, "value_num": 9.9},
        changed_by="caregiver@example.com",
        source="manual",
        patient_id=ERIKA_UUID,
    )
    assert updated.patient_id == ERIKA_UUID  # patient_id was rejected
    assert updated.value_num == 9.9


async def test_update_noop_when_no_effective_changes(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    before_audit = await db.list_clinical_record_audit(rec.id)
    # Same value — should be a no-op, no audit row written
    result = await db.update_clinical_record(
        rec.id,
        {"value_num": 5.2},
        changed_by="caregiver@example.com",
        source="manual",
        patient_id=ERIKA_UUID,
    )
    after_audit = await db.list_clinical_record_audit(rec.id)
    assert result.value_num == 5.2
    assert len(before_audit) == len(after_audit)


async def test_update_on_unknown_id_returns_none(db: Database):
    result = await db.update_clinical_record(
        999_999,
        {"value_num": 1.0},
        changed_by="x@y.com",
        source="manual",
        patient_id=ERIKA_UUID,
    )
    assert result is None


# ── Soft delete + restore ───────────────────────────────────────────────


async def test_delete_hides_record_from_default_get(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    assert await db.delete_clinical_record(
        rec.id,
        deleted_by="caregiver@example.com",
        source="manual",
        patient_id=ERIKA_UUID,
    )
    assert await db.get_clinical_record(rec.id, patient_id=ERIKA_UUID) is None
    still = await db.get_clinical_record(rec.id, patient_id=ERIKA_UUID, include_deleted=True)
    assert still is not None
    assert still.deleted_at is not None


async def test_delete_is_idempotent(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    assert await db.delete_clinical_record(
        rec.id, deleted_by="x@y.com", source="manual", patient_id=ERIKA_UUID
    )
    assert (
        await db.delete_clinical_record(
            rec.id, deleted_by="x@y.com", source="manual", patient_id=ERIKA_UUID
        )
        is False
    )


async def test_restore_clears_deleted_markers(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    await db.delete_clinical_record(
        rec.id, deleted_by="x@y.com", source="manual", patient_id=ERIKA_UUID
    )
    restored = await db.restore_clinical_record(
        rec.id,
        restored_by="x@y.com",
        source="manual",
        reason="oops",
        patient_id=ERIKA_UUID,
    )
    assert restored is not None
    assert restored.deleted_at is None
    assert restored.deleted_by is None


async def test_restore_on_non_deleted_returns_none(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    result = await db.restore_clinical_record(
        rec.id, restored_by="x@y.com", source="manual", patient_id=ERIKA_UUID
    )
    assert result is None


async def test_list_include_deleted_returns_all(db: Database):
    rec1 = await db.insert_clinical_record(_make_record(param="CEA"))
    await db.insert_clinical_record(_make_record(param="CA19-9"))
    await db.delete_clinical_record(
        rec1.id, deleted_by="x@y.com", source="manual", patient_id=ERIKA_UUID
    )
    active = await db.list_clinical_records(ClinicalRecordQuery(), patient_id=ERIKA_UUID)
    all_rows = await db.list_clinical_records(
        ClinicalRecordQuery(include_deleted=True), patient_id=ERIKA_UUID
    )
    assert len(active) == 1
    assert len(all_rows) == 2


# ── Audit trail ─────────────────────────────────────────────────────────


async def test_insert_emits_create_audit_row(db: Database):
    rec = await db.insert_clinical_record(_make_record(), reason="initial upload")
    trail = await db.list_clinical_record_audit(rec.id)
    assert len(trail) == 1
    assert trail[0].action == "create"
    assert trail[0].before_json is None
    assert trail[0].after_json is not None
    assert trail[0].reason == "initial upload"


async def test_update_emits_update_audit_with_changed_fields(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    await db.update_clinical_record(
        rec.id,
        {"value_num": 7.1, "status": "high"},
        changed_by="alice@example.com",
        source="oncoteam",
        reason="Oncoteam trend review",
        patient_id=ERIKA_UUID,
    )
    trail = await db.list_clinical_record_audit(rec.id)
    # Newest first
    assert trail[0].action == "update"
    assert set(trail[0].changed_fields.split(",")) == {"value_num", "status"}
    assert trail[0].source == "oncoteam"
    assert trail[0].reason == "Oncoteam trend review"
    assert trail[0].before_json is not None
    before = json.loads(trail[0].before_json)
    after = json.loads(trail[0].after_json)
    assert before["value_num"] == 5.2
    assert after["value_num"] == 7.1


async def test_full_lifecycle_produces_four_audit_rows(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    await db.update_clinical_record(
        rec.id,
        {"value_num": 6.0},
        changed_by="x@y.com",
        source="manual",
        patient_id=ERIKA_UUID,
    )
    await db.delete_clinical_record(
        rec.id, deleted_by="x@y.com", source="manual", patient_id=ERIKA_UUID
    )
    await db.restore_clinical_record(
        rec.id, restored_by="x@y.com", source="manual", patient_id=ERIKA_UUID
    )
    trail = await db.list_clinical_record_audit(rec.id)
    # Newest first: restore, delete, update, create
    assert [row.action for row in trail] == ["restore", "delete", "update", "create"]


async def test_audit_preserves_session_id_and_caller(db: Database):
    rec = await db.insert_clinical_record(
        _make_record(
            source="mcp-claude",
            session_id="conv-abc-123",
            caller_identity="sha256:tokhash",
        )
    )
    trail = await db.list_clinical_record_audit(rec.id)
    assert trail[0].session_id == "conv-abc-123"
    assert trail[0].caller_identity == "sha256:tokhash"


# ── Cross-patient isolation ─────────────────────────────────────────────


async def test_list_scopes_to_single_patient(db: Database):
    await _seed_second_patient(db)
    await db.insert_clinical_record(_make_record(patient_id=ERIKA_UUID, param="CEA"))
    await db.insert_clinical_record(_make_record(patient_id=SECOND_UUID, param="CA19-9"))

    erika = await db.list_clinical_records(ClinicalRecordQuery(), patient_id=ERIKA_UUID)
    bob = await db.list_clinical_records(ClinicalRecordQuery(), patient_id=SECOND_UUID)

    assert {r.param for r in erika} == {"CEA"}
    assert {r.param for r in bob} == {"CA19-9"}


async def test_notes_patient_scope_via_join(db: Database):
    await _seed_second_patient(db)
    erika_rec = await db.insert_clinical_record(_make_record(patient_id=ERIKA_UUID))
    bob_rec = await db.insert_clinical_record(_make_record(patient_id=SECOND_UUID))

    await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=erika_rec.id,
            note_text="Erika asked about this",
            source="dashboard",
        )
    )
    await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=bob_rec.id,
            note_text="Bob asked about this",
            source="dashboard",
        )
    )

    erika_notes = await db.list_clinical_record_notes(patient_id=ERIKA_UUID)
    bob_notes = await db.list_clinical_record_notes(patient_id=SECOND_UUID)

    assert len(erika_notes) == 1
    assert len(bob_notes) == 1
    assert "Erika" in erika_notes[0].note_text
    assert "Bob" in bob_notes[0].note_text


# ── Notes CRUD + tagging ────────────────────────────────────────────────


async def test_insert_note_round_trips(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    note = await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=rec.id,
            note_text="Asked Dr. Kovac on 2026-04-20, keep monitoring",
            tags=json.dumps(["flagged", "ask-oncologist"]),
            source="mcp-claude",
            session_id="conv-xyz",
            created_by="caregiver@example.com",
        )
    )
    assert note.id is not None
    assert note.note_text.startswith("Asked Dr. Kovac")
    assert json.loads(note.tags) == ["flagged", "ask-oncologist"]
    assert note.session_id == "conv-xyz"


async def test_list_notes_filters_by_tags_any(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=rec.id, note_text="flagged note", tags='["flagged"]', source="dashboard"
        )
    )
    await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=rec.id,
            note_text="side-effect note",
            tags='["side-effect"]',
            source="dashboard",
        )
    )
    await db.insert_clinical_record_note(
        ClinicalRecordNote(record_id=rec.id, note_text="no tags", tags=None, source="dashboard")
    )
    flagged = await db.list_clinical_record_notes(record_id=rec.id, tags_any=["flagged"])
    assert len(flagged) == 1
    assert flagged[0].note_text == "flagged note"

    combo = await db.list_clinical_record_notes(
        record_id=rec.id, tags_any=["flagged", "side-effect"]
    )
    assert len(combo) == 2


async def test_delete_note_is_soft(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    note = await db.insert_clinical_record_note(
        ClinicalRecordNote(record_id=rec.id, note_text="temp", source="dashboard")
    )
    assert await db.delete_clinical_record_note(note.id, deleted_by="x@y.com")
    assert await db.get_clinical_record_note(note.id) is None
    still = await db.get_clinical_record_note(note.id, include_deleted=True)
    assert still is not None
    assert still.deleted_at is not None


async def test_list_notes_requires_record_or_patient(db: Database):
    with pytest.raises(ValueError):
        await db.list_clinical_record_notes()


async def test_list_notes_default_excludes_deleted(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    a = await db.insert_clinical_record_note(
        ClinicalRecordNote(record_id=rec.id, note_text="alive", source="dashboard")
    )
    b = await db.insert_clinical_record_note(
        ClinicalRecordNote(record_id=rec.id, note_text="deleted", source="dashboard")
    )
    await db.delete_clinical_record_note(b.id, deleted_by="x@y.com")
    active = await db.list_clinical_record_notes(record_id=rec.id)
    all_rows = await db.list_clinical_record_notes(record_id=rec.id, include_deleted=True)
    assert [n.id for n in active] == [a.id]
    assert len(all_rows) == 2


# ── Record-type variety ──────────────────────────────────────────────────


async def test_biomarker_stores_text_value(db: Database):
    rec = await db.insert_clinical_record(
        _make_record(
            record_type="biomarker",
            param="KRAS",
            value_num=None,
            value_text="G12S pathogenic",
        )
    )
    stored = await db.get_clinical_record(rec.id, patient_id=ERIKA_UUID)
    assert stored.record_type == "biomarker"
    assert stored.value_num is None
    assert stored.value_text == "G12S pathogenic"


async def test_lab_with_ref_range(db: Database):
    rec = await db.insert_clinical_record(
        _make_record(ref_range_low=0.0, ref_range_high=3.8, status="high")
    )
    stored = await db.get_clinical_record(rec.id, patient_id=ERIKA_UUID)
    assert stored.ref_range_low == 0.0
    assert stored.ref_range_high == 3.8
    assert stored.status == "high"
