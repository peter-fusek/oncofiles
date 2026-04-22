"""Tests for #450 Phase 2 MCP tools (v5.13).

Seven tools split across two axes:
- Mutations (update/delete/restore) — must validate patient_id before write.
- Reads (list/search/audit) — must scope queries by patient_id.
- Analyses (add_clinical_analysis) — record_ids cross-patient guard.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.models import ClinicalRecord, ClinicalRecordNote
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.tools.clinical_records import (
    add_clinical_analysis,
    delete_clinical_record,
    get_record_audit,
    list_clinical_records,
    restore_clinical_record,
    search_notes,
    update_clinical_record,
)
from tests.helpers import ERIKA_UUID, TEST_PATIENT_UUID


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


async def _seed_second_patient(db: Database) -> None:
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (TEST_PATIENT_UUID, "bob-test", "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


def _make_record(patient_id: str = ERIKA_UUID, **overrides) -> ClinicalRecord:
    defaults = {
        "patient_id": patient_id,
        "record_type": "lab",
        "param": "CEA",
        "value_num": 5.2,
        "unit": "ng/mL",
        "occurred_at": "2026-01-15",
        "source": "manual",
        "created_by": "test@example.com",
    }
    defaults.update(overrides)
    return ClinicalRecord(**defaults)


# ── update_clinical_record ─────────────────────────────────────────────


async def test_update_changes_field_and_emits_audit(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    result = json.loads(
        await update_clinical_record(
            _ctx(db),
            record_id=stored.id,
            source="manual",
            value_num=8.1,
            reason="lab resample",
            changed_by="caregiver@example.com",
        )
    )
    assert result["status"] == "updated"
    assert result["changed_fields"] == ["value_num"]

    after = await db.get_clinical_record(stored.id)
    assert after.value_num == 8.1

    audit = await db.list_clinical_record_audit(stored.id)
    # 2 rows: create + update
    assert len(audit) == 2
    assert audit[0].action == "update"
    assert audit[0].reason == "lab resample"


async def test_update_noop_returns_no_change(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    result = json.loads(
        await update_clinical_record(
            _ctx(db),
            record_id=stored.id,
            source="manual",
        )
    )
    assert result["status"] == "no_change"


async def test_update_not_found(db: Database):
    result = json.loads(
        await update_clinical_record(
            _ctx(db),
            record_id=999_999,
            source="manual",
            value_num=1.0,
        )
    )
    assert result["error"] == "not_found"


async def test_update_blocks_wrong_patient(db: Database):
    """A record owned by patient A cannot be updated from patient B's context."""
    await _seed_second_patient(db)
    other = await db.insert_clinical_record(_make_record(patient_id=TEST_PATIENT_UUID))

    # Context is ERIKA_UUID (conftest fixture default) — try to hit TEST_PATIENT_UUID's record
    result = json.loads(
        await update_clinical_record(
            _ctx(db),
            record_id=other.id,
            source="manual",
            value_num=999.0,
        )
    )
    assert result["error"] == "wrong_patient"
    # Record unchanged
    after = await db.get_clinical_record(other.id)
    assert after.value_num == 5.2


# ── delete_clinical_record ─────────────────────────────────────────────


async def test_delete_soft_deletes_and_idempotent(db: Database):
    stored = await db.insert_clinical_record(_make_record())

    result = json.loads(
        await delete_clinical_record(
            _ctx(db),
            record_id=stored.id,
            source="manual",
            reason="retracted — duplicate",
            deleted_by="caregiver@example.com",
        )
    )
    assert result["status"] == "deleted"
    after = await db.get_clinical_record(stored.id, include_deleted=True)
    assert after.deleted_at is not None

    # Second delete is a no-op
    result2 = json.loads(
        await delete_clinical_record(
            _ctx(db),
            record_id=stored.id,
            source="manual",
        )
    )
    assert result2["status"] == "no_change"
    assert result2["reason"] == "already_deleted"


async def test_delete_blocks_wrong_patient(db: Database):
    await _seed_second_patient(db)
    other = await db.insert_clinical_record(_make_record(patient_id=TEST_PATIENT_UUID))
    result = json.loads(await delete_clinical_record(_ctx(db), record_id=other.id, source="manual"))
    assert result["error"] == "wrong_patient"
    after = await db.get_clinical_record(other.id)
    assert after.deleted_at is None


# ── restore_clinical_record ────────────────────────────────────────────


async def test_restore_clears_deleted_fields(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    await db.delete_clinical_record(stored.id, source="manual", deleted_by="x@example.com")

    result = json.loads(
        await restore_clinical_record(
            _ctx(db),
            record_id=stored.id,
            source="manual",
            restored_by="y@example.com",
            reason="oops, restore",
        )
    )
    assert result["status"] == "restored"
    after = await db.get_clinical_record(stored.id)
    assert after is not None and after.deleted_at is None
    assert after.updated_by == "y@example.com"

    audit = await db.list_clinical_record_audit(stored.id)
    # create + delete + restore
    assert len(audit) == 3
    assert audit[0].action == "restore"


async def test_restore_no_op_when_not_deleted(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    result = json.loads(
        await restore_clinical_record(
            _ctx(db),
            record_id=stored.id,
            source="manual",
        )
    )
    assert result["status"] == "no_change"
    assert result["reason"] == "not_deleted"


# ── list_clinical_records ──────────────────────────────────────────────


async def test_list_records_filters_by_type_and_param(db: Database):
    await db.insert_clinical_record(_make_record(record_type="lab", param="CEA"))
    await db.insert_clinical_record(_make_record(record_type="lab", param="CA19_9"))
    await db.insert_clinical_record(_make_record(record_type="biomarker", param="KRAS"))

    lab_only = json.loads(await list_clinical_records(_ctx(db), record_type="lab"))
    assert lab_only["count"] == 2

    cea_only = json.loads(await list_clinical_records(_ctx(db), param="CEA"))
    assert cea_only["count"] == 1
    assert cea_only["records"][0]["param"] == "CEA"


async def test_list_records_respects_patient_isolation(db: Database):
    """A list call from patient A never surfaces patient B's records."""
    await _seed_second_patient(db)
    await db.insert_clinical_record(_make_record(patient_id=ERIKA_UUID, param="CEA"))
    await db.insert_clinical_record(_make_record(patient_id=TEST_PATIENT_UUID, param="CEA"))

    result = json.loads(await list_clinical_records(_ctx(db)))
    assert result["count"] == 1
    assert all(r["patient_id"] == ERIKA_UUID for r in result["records"])


async def test_list_records_include_deleted_toggles(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    await db.delete_clinical_record(stored.id, source="manual")

    default = json.loads(await list_clinical_records(_ctx(db)))
    assert default["count"] == 0

    with_deleted = json.loads(await list_clinical_records(_ctx(db), include_deleted=True))
    assert with_deleted["count"] == 1


# ── get_record_audit ───────────────────────────────────────────────────


async def test_get_record_audit_returns_full_history(db: Database):
    stored = await db.insert_clinical_record(_make_record())
    await db.update_clinical_record(
        stored.id, {"value_num": 7.7}, changed_by="x@example.com", source="manual"
    )
    await db.delete_clinical_record(stored.id, source="manual")

    result = json.loads(await get_record_audit(_ctx(db), record_id=stored.id))
    assert result["count"] == 3
    actions = [a["action"] for a in result["audit"]]
    assert actions == ["delete", "update", "create"]


async def test_get_record_audit_blocks_wrong_patient(db: Database):
    await _seed_second_patient(db)
    other = await db.insert_clinical_record(_make_record(patient_id=TEST_PATIENT_UUID))
    result = json.loads(await get_record_audit(_ctx(db), record_id=other.id))
    assert result["error"] == "wrong_patient"


# ── add_clinical_analysis ──────────────────────────────────────────────


async def test_add_analysis_stores_row(db: Database):
    result = json.loads(
        await add_clinical_analysis(
            _ctx(db),
            analysis_type="sii_trend",
            result_json=json.dumps({"baseline": 1800, "current": 1200, "delta_pct": -33}),
            produced_by="oncoteam",
            result_summary="SII declined 33% after C1 — favorable",
            tags=["c1", "cbc"],
            session_id="sess-abc",
        )
    )
    assert result["status"] == "created"
    assert result["analysis_type"] == "sii_trend"
    assert result["tags"] == ["c1", "cbc"]

    fetched = await db.get_clinical_analysis(result["id"])
    assert fetched is not None
    assert fetched.patient_id == ERIKA_UUID
    assert fetched.result_summary == "SII declined 33% after C1 — favorable"


async def test_add_analysis_validates_record_ids_ownership(db: Database):
    """record_ids that belong to a different patient must be rejected."""
    await _seed_second_patient(db)
    other = await db.insert_clinical_record(_make_record(patient_id=TEST_PATIENT_UUID))

    result = json.loads(
        await add_clinical_analysis(
            _ctx(db),
            analysis_type="lab_delta",
            result_json="{}",
            produced_by="external-ai",
            record_ids=[other.id],
        )
    )
    assert result["error"] == "wrong_patient"


async def test_add_analysis_record_not_found(db: Database):
    result = json.loads(
        await add_clinical_analysis(
            _ctx(db),
            analysis_type="lab_delta",
            result_json="{}",
            produced_by="oncoteam",
            record_ids=[999_999],
        )
    )
    assert result["error"] == "record_not_found"


async def test_add_analysis_without_record_ids(db: Database):
    """Analyses without explicit record_ids (e.g. session_note) still work."""
    result = json.loads(
        await add_clinical_analysis(
            _ctx(db),
            analysis_type="session_note",
            result_json=json.dumps({"summary": "caregiver reviewed labs"}),
            produced_by="manual",
        )
    )
    assert result["status"] == "created"
    assert result["record_ids"] == []


async def test_list_clinical_analyses_scopes_by_patient(db: Database):
    await _seed_second_patient(db)

    # Log one for ERIKA
    await add_clinical_analysis(
        _ctx(db),
        analysis_type="sii_trend",
        result_json="{}",
        produced_by="oncoteam",
    )

    # Log one for TEST_PATIENT_UUID by flipping the context var
    token = _current_patient_id.set(TEST_PATIENT_UUID)
    try:
        await add_clinical_analysis(
            _ctx(db),
            analysis_type="lab_delta",
            result_json="{}",
            produced_by="oncoteam",
        )
    finally:
        _current_patient_id.reset(token)

    erika_rows = await db.list_clinical_analyses(patient_id=ERIKA_UUID)
    test_rows = await db.list_clinical_analyses(patient_id=TEST_PATIENT_UUID)
    assert len(erika_rows) == 1
    assert len(test_rows) == 1
    assert erika_rows[0].analysis_type == "sii_trend"
    assert test_rows[0].analysis_type == "lab_delta"


# ── search_notes ───────────────────────────────────────────────────────


async def _insert_note(db: Database, text: str, record_id: int) -> None:
    await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=record_id,
            note_text=text,
            source="dashboard",
        )
    )


async def test_search_notes_matches_substring(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    await _insert_note(db, "Asked Dr. Kovac about CEA trend", rec.id)
    await _insert_note(db, "Reviewed KRAS biomarker, unchanged", rec.id)
    await _insert_note(db, "no related note", rec.id)

    result = json.loads(await search_notes(_ctx(db), query="CEA"))
    assert result["count"] == 1
    assert "CEA" in result["notes"][0]["note_text"]

    result2 = json.loads(await search_notes(_ctx(db), query="KRAS"))
    assert result2["count"] == 1


async def test_search_notes_empty_query_returns_error(db: Database):
    result = json.loads(await search_notes(_ctx(db), query="   "))
    assert result["error"] == "empty_query"


async def test_search_notes_scopes_by_patient(db: Database):
    """A note on another patient's record must NOT appear in my search."""
    await _seed_second_patient(db)

    my_rec = await db.insert_clinical_record(_make_record(patient_id=ERIKA_UUID))
    await _insert_note(db, "CEA is trending down — my note", my_rec.id)

    their_rec = await db.insert_clinical_record(_make_record(patient_id=TEST_PATIENT_UUID))
    await _insert_note(db, "CEA unchanged — other patient note", their_rec.id)

    result = json.loads(await search_notes(_ctx(db), query="CEA"))
    assert result["count"] == 1
    assert "my note" in result["notes"][0]["note_text"]


async def test_search_notes_skips_deleted(db: Database):
    rec = await db.insert_clinical_record(_make_record())
    await _insert_note(db, "CEA observation", rec.id)
    # Find the note id and soft-delete
    all_notes = await db.list_clinical_record_notes(record_id=rec.id)
    assert len(all_notes) == 1
    await db.delete_clinical_record_note(all_notes[0].id, deleted_by="x@example.com")

    result = json.loads(await search_notes(_ctx(db), query="CEA"))
    assert result["count"] == 0
