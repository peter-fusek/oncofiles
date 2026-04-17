"""Tests for backfill_institution_from_patient_context + unblock_stuck_documents (#404)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from oncofiles import patient_context
from oncofiles.database import Database
from oncofiles.enhance import backfill_institution_from_patient_context
from oncofiles.tools.enhance_tools import unblock_stuck_documents
from tests.helpers import ERIKA_UUID, make_doc

_insert_counter = {"n": 0}


async def _insert_doc(db: Database, *, patient_id: str = ERIKA_UUID, **fields):
    _insert_counter["n"] += 1
    fields.setdefault("file_id", f"file_fb_{_insert_counter['n']}")
    doc = make_doc(**fields)
    return await db.insert_document(doc, patient_id=patient_id)


def _set_treatment_institution(pid: str, inst: str | None) -> None:
    """Helper: set patient_context.treatment.institution for a patient."""
    ctx = dict(patient_context._contexts.get(pid, {"name": "Erika Fusekova"}))
    if inst:
        ctx["treatment"] = {"institution": inst}
    else:
        ctx["treatment"] = {}
    patient_context._contexts[pid] = ctx


def _ctx(db: Database) -> MagicMock:
    c = MagicMock()
    c.request_context.lifespan_context = {
        "db": db,
        "files": MagicMock(),
        "gdrive": None,
        "oauth_folder_id": "",
    }
    return c


async def test_fallback_fills_chemo_sheet_from_patient_context(db: Database):
    _set_treatment_institution(ERIKA_UUID, "NOU")
    await _insert_doc(
        db,
        filename="20230326_ErikaFusekova_Unknown_ChemoSheet_Cycle3.pdf",
        category="chemo_sheet",
        institution=None,
    )

    stats = await backfill_institution_from_patient_context(
        db.db, patient_id=ERIKA_UUID, dry_run=False
    )
    assert stats["updated"] == 1
    assert stats["skipped_no_context_institution"] == 0

    docs = await db.list_documents(limit=10, patient_id=ERIKA_UUID)
    assert docs[0].institution == "NOU"


async def test_fallback_dry_run_does_not_write(db: Database):
    _set_treatment_institution(ERIKA_UUID, "NOU")
    await _insert_doc(
        db,
        filename="chemo.pdf",
        category="chemo_sheet",
        institution=None,
    )

    stats = await backfill_institution_from_patient_context(
        db.db, patient_id=ERIKA_UUID, dry_run=True
    )
    assert stats["updated"] == 1
    assert stats["dry_run"] is True

    docs = await db.list_documents(limit=10, patient_id=ERIKA_UUID)
    assert docs[0].institution is None


async def test_fallback_skips_labs_category(db: Database):
    # Labs are often outsourced to external facilities — NOT in the safe list.
    _set_treatment_institution(ERIKA_UUID, "NOU")
    await _insert_doc(db, filename="lab.pdf", category="labs", institution=None)
    await _insert_doc(db, filename="imaging.pdf", category="imaging", institution=None)
    await _insert_doc(db, filename="pathology.pdf", category="pathology", institution=None)

    stats = await backfill_institution_from_patient_context(
        db.db, patient_id=ERIKA_UUID, dry_run=False
    )
    assert stats["updated"] == 0
    assert stats["skipped_unsafe_category"] == 3

    for d in await db.list_documents(limit=10, patient_id=ERIKA_UUID):
        assert d.institution is None


async def test_fallback_applies_to_prescription_and_discharge(db: Database):
    _set_treatment_institution(ERIKA_UUID, "BoryNemocnica")
    await _insert_doc(db, filename="rx.pdf", category="prescription", institution=None)
    await _insert_doc(db, filename="disch.pdf", category="discharge", institution=None)
    # Mixed: one safe + one unsafe, only the safe one gets updated
    await _insert_doc(db, filename="ref.pdf", category="referral", institution=None)

    stats = await backfill_institution_from_patient_context(
        db.db, patient_id=ERIKA_UUID, dry_run=False
    )
    assert stats["updated"] == 2
    assert stats["skipped_unsafe_category"] == 1


async def test_fallback_noop_when_context_institution_empty(db: Database):
    _set_treatment_institution(ERIKA_UUID, None)
    await _insert_doc(db, filename="chemo.pdf", category="chemo_sheet", institution=None)

    stats = await backfill_institution_from_patient_context(
        db.db, patient_id=ERIKA_UUID, dry_run=False
    )
    assert stats["updated"] == 0
    assert stats["skipped_no_context_institution"] == 1


async def test_fallback_does_not_overwrite_existing_institution(db: Database):
    _set_treatment_institution(ERIKA_UUID, "NOU")
    await _insert_doc(db, filename="chemo.pdf", category="chemo_sheet", institution="Medirex")

    stats = await backfill_institution_from_patient_context(
        db.db, patient_id=ERIKA_UUID, dry_run=False
    )
    assert stats["updated"] == 0

    docs = await db.list_documents(limit=10, patient_id=ERIKA_UUID)
    assert docs[0].institution == "Medirex"


async def test_unblock_mcp_tool_returns_next_steps(db: Database):
    _set_treatment_institution(ERIKA_UUID, "NOU")
    await _insert_doc(db, filename="chemo.pdf", category="chemo_sheet", institution=None)

    result = json.loads(await unblock_stuck_documents(_ctx(db), dry_run=True))
    assert result["stats"]["updated"] == 1
    assert result["patient_id"] == ERIKA_UUID
    assert any("unblock_stuck_documents(dry_run=False)" in s for s in result["next_steps"])


async def test_unblock_mcp_tool_flags_missing_context(db: Database):
    _set_treatment_institution(ERIKA_UUID, None)
    await _insert_doc(db, filename="chemo.pdf", category="chemo_sheet", institution=None)

    result = json.loads(await unblock_stuck_documents(_ctx(db), dry_run=False))
    assert result["stats"]["updated"] == 0
    assert any("update_patient_context" in s for s in result["next_steps"])
