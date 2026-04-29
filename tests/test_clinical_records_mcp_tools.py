"""MCP-tool-level tests for clinical_records (#450 Phase 1).

The DB-layer tests in test_clinical_records.py cover the mixin methods
directly. This file covers the MCP tool wrappers in tools/clinical_records.py
— argument shape, patient_slug resolution, cross-patient rejection, JSON
response contracts. Future v5.12 Phase 2 tools will extend this suite.
"""

from __future__ import annotations

import json

import pytest

from oncofiles.database import Database
from oncofiles.models import ClinicalRecord
from oncofiles.tools import clinical_records as cr_tools
from tests.conftest import ERIKA_UUID

# These tests verify slug-routing across patients — admin scope per #497/#498.
pytestmark = pytest.mark.usefixtures("admin_scope")

SECOND_UUID = "00000000-0000-4000-8000-000000000002"
SECOND_SLUG = "bob-test"


async def _seed_two_patients(db: Database) -> None:
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (SECOND_UUID, SECOND_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


# ── add_clinical_record ────────────────────────────────────────────────────


async def test_add_clinical_record_returns_structured_response(db: Database):
    ctx = _StubCtx(db)
    result = json.loads(
        await cr_tools.add_clinical_record(
            ctx,
            record_type="lab",
            source="manual",
            param="CEA",
            value_num=5.2,
            unit="ng/mL",
            occurred_at="2026-03-15",
            created_by="caregiver@example.com",
            reason="initial upload",
        )
    )
    assert result["status"] == "created"
    assert result["record_type"] == "lab"
    assert result["param"] == "CEA"
    assert result["id"] is not None
    assert result["created_at"] is not None


async def test_add_clinical_record_targets_slug_patient(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)

    # Default ContextVar = Erika. Explicit slug overrides to Bob.
    result = json.loads(
        await cr_tools.add_clinical_record(
            ctx,
            record_type="biomarker",
            source="manual",
            param="KRAS",
            value_text="G12S",
            patient_slug=SECOND_SLUG,
        )
    )
    stored_id = result["id"]
    # Record was created under SECOND_UUID via slug routing; fetch under that scope.
    stored = await db.get_clinical_record(stored_id, patient_id=SECOND_UUID)
    assert stored.patient_id == SECOND_UUID
    assert stored.patient_id != ERIKA_UUID
    # Defence-in-depth: from Erika's scope the row is invisible (#499).
    assert await db.get_clinical_record(stored_id, patient_id=ERIKA_UUID) is None


async def test_add_clinical_record_with_audit_reason(db: Database):
    """The `reason` argument surfaces in the auto-generated audit row."""
    ctx = _StubCtx(db)
    result = json.loads(
        await cr_tools.add_clinical_record(
            ctx,
            record_type="lab",
            source="oncoteam",
            param="PLT",
            value_num=150,
            reason="Oncoteam trend-review backfill",
        )
    )
    audit = await db.list_clinical_record_audit(result["id"])
    assert len(audit) == 1
    assert audit[0].action == "create"
    assert audit[0].reason == "Oncoteam trend-review backfill"
    assert audit[0].source == "oncoteam"


# ── get_clinical_record ────────────────────────────────────────────────────


async def test_get_clinical_record_includes_audit_when_requested(db: Database):
    ctx = _StubCtx(db)
    rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=ERIKA_UUID,
            record_type="lab",
            param="CEA",
            value_num=3.5,
            source="manual",
        )
    )
    result = json.loads(await cr_tools.get_clinical_record(ctx, rec.id, include_audit=True))
    assert "record" in result
    assert "audit" in result
    assert len(result["audit"]) == 1
    assert result["audit"][0]["action"] == "create"


async def test_get_clinical_record_includes_notes_when_requested(db: Database):
    ctx = _StubCtx(db)
    rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=ERIKA_UUID,
            record_type="lab",
            param="CEA",
            value_num=3.5,
            source="manual",
        )
    )
    from oncofiles.models import ClinicalRecordNote

    await db.insert_clinical_record_note(
        ClinicalRecordNote(
            record_id=rec.id,
            note_text="Asked Dr. Kovac",
            source="dashboard",
        )
    )
    result = json.loads(await cr_tools.get_clinical_record(ctx, rec.id, include_notes=True))
    assert "notes" in result
    assert len(result["notes"]) == 1
    assert result["notes"][0]["note_text"] == "Asked Dr. Kovac"


async def test_get_clinical_record_not_found(db: Database):
    ctx = _StubCtx(db)
    result = json.loads(await cr_tools.get_clinical_record(ctx, 99_999))
    assert result["error"] == "not_found"


async def test_get_clinical_record_blocks_cross_patient(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)
    # Bob's record — Erika's ContextVar should NOT be able to read it
    bob_rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=SECOND_UUID,
            record_type="lab",
            param="CEA",
            value_num=9.9,
            source="manual",
        )
    )
    # Default scope (Erika) sees not_found — SQL filter (#499) blocks the row
    result = json.loads(await cr_tools.get_clinical_record(ctx, bob_rec.id))
    assert result["error"] == "not_found"
    # Content must NOT leak through the error response
    assert "9.9" not in json.dumps(result)

    # Explicit Bob slug unlocks
    bob_ok = json.loads(
        await cr_tools.get_clinical_record(ctx, bob_rec.id, patient_slug=SECOND_SLUG)
    )
    assert "record" in bob_ok
    assert bob_ok["record"]["value_num"] == 9.9


# ── add_clinical_record_note ──────────────────────────────────────────────


async def test_add_note_wraps_tags_as_json(db: Database):
    ctx = _StubCtx(db)
    rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=ERIKA_UUID,
            record_type="lab",
            param="CEA",
            value_num=3.5,
            source="manual",
        )
    )
    result = json.loads(
        await cr_tools.add_clinical_record_note(
            ctx,
            record_id=rec.id,
            note_text="flagged for oncologist review",
            source="mcp-claude",
            tags=["flagged", "ask-oncologist"],
            session_id="conv-abc-123",
        )
    )
    assert result["status"] == "created"
    assert result["tags"] == ["flagged", "ask-oncologist"]

    # Verify persisted as JSON string
    note = await db.get_clinical_record_note(result["id"])
    assert json.loads(note.tags) == ["flagged", "ask-oncologist"]
    assert note.session_id == "conv-abc-123"


async def test_add_note_blocks_cross_patient(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)
    bob_rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=SECOND_UUID,
            record_type="lab",
            param="CEA",
            source="manual",
        )
    )
    # Default scope (Erika) — can't attach a note to Bob's record
    result = json.loads(
        await cr_tools.add_clinical_record_note(
            ctx,
            record_id=bob_rec.id,
            note_text="trying to leak",
            source="mcp-claude",
        )
    )
    assert result["error"] == "not_found"


async def test_add_note_record_not_found(db: Database):
    ctx = _StubCtx(db)
    result = json.loads(
        await cr_tools.add_clinical_record_note(
            ctx, record_id=99_999, note_text="orphan", source="mcp-claude"
        )
    )
    assert result["error"] == "not_found"


async def test_add_note_no_tags_stores_null(db: Database):
    """Omitting `tags` should store SQL NULL, not an empty JSON array."""
    ctx = _StubCtx(db)
    rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=ERIKA_UUID,
            record_type="lab",
            param="CEA",
            source="manual",
        )
    )
    result = json.loads(
        await cr_tools.add_clinical_record_note(
            ctx, record_id=rec.id, note_text="plain note", source="dashboard"
        )
    )
    note = await db.get_clinical_record_note(result["id"])
    assert note.tags is None


# ── list_clinical_record_notes ─────────────────────────────────────────────


async def test_list_notes_filters_by_tags_any(db: Database):
    ctx = _StubCtx(db)
    rec = await db.insert_clinical_record(
        ClinicalRecord(
            patient_id=ERIKA_UUID,
            record_type="lab",
            param="CEA",
            source="manual",
        )
    )
    for tag_set in (["flagged"], ["side-effect"], None):
        await cr_tools.add_clinical_record_note(
            ctx,
            record_id=rec.id,
            note_text=f"note tags={tag_set}",
            source="dashboard",
            tags=tag_set,
        )
    result = json.loads(
        await cr_tools.list_clinical_record_notes(ctx, record_id=rec.id, tags_any=["flagged"])
    )
    assert result["count"] == 1
    assert "flagged" in result["notes"][0]["tags"]


async def test_list_notes_patient_scope_via_slug(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)
    erika_rec = await db.insert_clinical_record(
        ClinicalRecord(patient_id=ERIKA_UUID, record_type="lab", source="manual")
    )
    bob_rec = await db.insert_clinical_record(
        ClinicalRecord(patient_id=SECOND_UUID, record_type="lab", source="manual")
    )
    await cr_tools.add_clinical_record_note(
        ctx, record_id=erika_rec.id, note_text="Erika-only", source="dashboard"
    )
    await cr_tools.add_clinical_record_note(
        ctx,
        record_id=bob_rec.id,
        note_text="Bob-only",
        source="dashboard",
        patient_slug=SECOND_SLUG,
    )
    erika_listing = json.loads(await cr_tools.list_clinical_record_notes(ctx))
    bob_listing = json.loads(
        await cr_tools.list_clinical_record_notes(ctx, patient_slug=SECOND_SLUG)
    )
    assert {n["note_text"] for n in erika_listing["notes"]} == {"Erika-only"}
    assert {n["note_text"] for n in bob_listing["notes"]} == {"Bob-only"}


async def test_list_notes_by_specific_record_id(db: Database):
    ctx = _StubCtx(db)
    r1 = await db.insert_clinical_record(
        ClinicalRecord(patient_id=ERIKA_UUID, record_type="lab", source="manual")
    )
    r2 = await db.insert_clinical_record(
        ClinicalRecord(patient_id=ERIKA_UUID, record_type="lab", source="manual")
    )
    await cr_tools.add_clinical_record_note(
        ctx, record_id=r1.id, note_text="on r1", source="dashboard"
    )
    await cr_tools.add_clinical_record_note(
        ctx, record_id=r2.id, note_text="on r2", source="dashboard"
    )
    result = json.loads(await cr_tools.list_clinical_record_notes(ctx, record_id=r1.id))
    assert result["count"] == 1
    assert result["notes"][0]["note_text"] == "on r1"


async def test_list_notes_wrong_patient_record_id(db: Database):
    await _seed_two_patients(db)
    ctx = _StubCtx(db)
    bob_rec = await db.insert_clinical_record(
        ClinicalRecord(patient_id=SECOND_UUID, record_type="lab", source="manual")
    )
    # Erika's default scope + Bob's record_id → not_found (SQL filter, #499)
    result = json.loads(await cr_tools.list_clinical_record_notes(ctx, record_id=bob_rec.id))
    assert result["error"] == "not_found"
