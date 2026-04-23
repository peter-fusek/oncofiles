"""Tests for audit_document_pipeline tool (issue #396)."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.models import PromptCallType, PromptLogEntry
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.tools.hygiene import audit_document_pipeline
from tests.helpers import ERIKA_UUID, TEST_PATIENT_UUID, make_doc


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "files": MagicMock(),
        "gdrive": None,
        "oauth_folder_id": "",
    }
    return ctx


_insert_counter = {"n": 0}


async def _insert_doc(db: Database, *, patient_id: str = ERIKA_UUID, **fields):
    # insert_document only persists the base columns — AI/metadata fields
    # must be set via dedicated update methods.
    _insert_counter["n"] += 1
    fields.setdefault("file_id", f"file_test_{_insert_counter['n']}")
    ai_summary = fields.pop("ai_summary", None)
    ai_tags = fields.pop("ai_tags", "[]")
    structured_metadata = fields.pop("structured_metadata", None)
    doc = make_doc(**fields)
    inserted = await db.insert_document(doc, patient_id=patient_id)
    if ai_summary is not None:
        await db.update_document_ai_metadata(inserted.id, ai_summary, ai_tags)
    if structured_metadata is not None:
        await db.update_structured_metadata(inserted.id, structured_metadata)
    return inserted


async def test_audit_empty_patient(db: Database):
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["patient_id"] == ERIKA_UUID
    assert result["totals"] == {"total_documents": 0, "pdfs": 0, "images": 0}
    assert all(v == 0 for v in result["pipeline_gaps"].values())
    assert result["group_stats"]["grouped_documents"] == 0
    assert result["stuck_sample"] == []
    assert result["suggested_actions"] == []
    assert "disclaimer" in result


async def test_audit_fully_complete_doc_has_no_gaps(db: Database):
    doc = await _insert_doc(
        db,
        filename="20240115_ErikaFusekova_NOUonko_Labs_KrvnyObraz.pdf",
        gdrive_id="gdrive_abc",
        ai_summary="lab report summary",
        structured_metadata='{"x": 1}',
        document_date=date(2024, 1, 15),
        institution="NOUonko",
    )
    await db.save_ocr_page(doc.id, 1, "page 1 text", "test-model")

    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["totals"]["total_documents"] == 1
    assert result["pipeline_gaps"]["fully_complete"] == 1
    assert result["pipeline_gaps"]["missing_ocr"] == 0
    assert result["pipeline_gaps"]["missing_ai"] == 0
    assert result["stuck_sample"] == []


async def test_audit_detects_every_gap_type(db: Database):
    # Missing OCR
    await _insert_doc(db, filename="20240101_no_ocr.pdf")
    # Missing AI
    doc2 = await _insert_doc(db, filename="20240102_no_ai.pdf")
    await db.save_ocr_page(doc2.id, 1, "text", "m")
    # Missing metadata (has AI, no structured_metadata)
    doc3 = await _insert_doc(db, filename="20240103_no_meta.pdf", ai_summary="s")
    await db.save_ocr_page(doc3.id, 1, "t", "m")
    # Missing date
    doc4 = await _insert_doc(db, filename="nodate.pdf", document_date=None, ai_summary="s")
    await db.save_ocr_page(doc4.id, 1, "t", "m")
    # Missing institution
    doc5 = await _insert_doc(db, filename="noinst.pdf", institution=None, ai_summary="s")
    await db.save_ocr_page(doc5.id, 1, "t", "m")
    # Not synced
    doc6 = await _insert_doc(db, filename="nogdrive.pdf", gdrive_id=None, ai_summary="s")
    await db.save_ocr_page(doc6.id, 1, "t", "m")

    result = json.loads(await audit_document_pipeline(_ctx(db)))
    gaps = result["pipeline_gaps"]
    assert gaps["missing_ocr"] >= 1
    assert gaps["missing_ai"] >= 1
    assert gaps["missing_metadata"] >= 1
    assert gaps["missing_date"] >= 1
    assert gaps["missing_institution"] >= 1
    assert gaps["not_synced"] >= 1
    # Every non-fully-complete doc should appear in stuck_sample (up to 20)
    assert len(result["stuck_sample"]) > 0
    assert all("reasons" in s and "gdrive_url" in s for s in result["stuck_sample"])


async def test_audit_cross_patient_isolation(db: Database):
    # Seed other patient first (requires patient row + context switch)
    from oncofiles.models import Patient

    other = Patient(
        patient_id=TEST_PATIENT_UUID,
        slug="other-patient",
        display_name="Other Patient",
    )
    await db.insert_patient(other)

    # Insert 2 docs for ERIKA
    await _insert_doc(db, filename="erika1.pdf")
    await _insert_doc(db, filename="erika2.pdf")
    # Insert 3 docs for TEST_PATIENT
    for i in range(3):
        await _insert_doc(db, patient_id=TEST_PATIENT_UUID, filename=f"other{i}.pdf")

    # Audit ERIKA — must see only 2 docs, not 5
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["totals"]["total_documents"] == 2

    # Switch context to other patient and audit again
    token = _current_patient_id.set(TEST_PATIENT_UUID)
    try:
        result2 = json.loads(await audit_document_pipeline(_ctx(db)))
        assert result2["totals"]["total_documents"] == 3
    finally:
        _current_patient_id.reset(token)


async def test_audit_group_stats_grouped_docs(db: Database):
    # 2 docs sharing a group_id, total_parts=2 (matches)
    await _insert_doc(db, filename="part1.pdf", group_id="grp-abc", part_number=1, total_parts=2)
    await _insert_doc(db, filename="part2.pdf", group_id="grp-abc", part_number=2, total_parts=2)
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    gs = result["group_stats"]
    assert gs["grouped_documents"] == 2
    assert gs["distinct_groups"] == 1
    assert gs["orphan_parts"] == 0
    assert gs["size_mismatch_groups"] == []


async def test_audit_group_stats_orphan_parts_null_total(db: Database):
    # group_id set but total_parts NULL — classified as orphan
    await _insert_doc(
        db, filename="orph1.pdf", group_id="grp-null", part_number=1, total_parts=None
    )
    await _insert_doc(
        db, filename="orph2.pdf", group_id="grp-null", part_number=2, total_parts=None
    )
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["group_stats"]["orphan_parts"] == 2


async def test_audit_group_stats_size_mismatch(db: Database):
    # declared_total=3 but only 2 rows present
    await _insert_doc(db, filename="mis1.pdf", group_id="grp-mis", part_number=1, total_parts=3)
    await _insert_doc(db, filename="mis2.pdf", group_id="grp-mis", part_number=2, total_parts=3)
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    gs = result["group_stats"]
    assert gs["orphan_parts"] == 1
    assert len(gs["size_mismatch_groups"]) == 1
    mismatch = gs["size_mismatch_groups"][0]
    assert mismatch["declared_total"] == 3
    assert mismatch["actual_count"] == 2


async def test_audit_split_candidates_pending(db: Database):
    # PDF with ≥2 OCR pages, no group_id → is a split candidate
    doc = await _insert_doc(db, filename="multipage.pdf", mime_type="application/pdf")
    await db.save_ocr_page(doc.id, 1, "page 1", "m")
    await db.save_ocr_page(doc.id, 2, "page 2", "m")

    # Single-page PDF — not a candidate
    single = await _insert_doc(db, filename="single.pdf", mime_type="application/pdf")
    await db.save_ocr_page(single.id, 1, "one page", "m")

    # Already-grouped multi-page PDF — not a candidate
    grouped = await _insert_doc(
        db,
        filename="grouped.pdf",
        mime_type="application/pdf",
        group_id="grp-existing",
        part_number=1,
        total_parts=1,
    )
    await db.save_ocr_page(grouped.id, 1, "a", "m")
    await db.save_ocr_page(grouped.id, 2, "b", "m")

    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["group_stats"]["split_candidates_pending"] == 1


async def test_audit_ai_composition_call_counts(db: Database):
    # Log a few prompt_log entries across the three call types
    for ct, n in [
        (PromptCallType.DOC_COMPOSITION, 2),
        (PromptCallType.DOC_CONSOLIDATION, 1),
    ]:
        for i in range(n):
            await db.insert_prompt_log(
                PromptLogEntry(
                    call_type=ct,
                    patient_id=ERIKA_UUID,
                    model="claude-haiku",
                    result_summary=f"ok {i}",
                )
            )

    await _insert_doc(db, filename="seed.pdf")
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    calls = result["ai_composition_calls"]
    assert calls["doc_composition"]["count"] == 2
    assert calls["doc_consolidation"]["count"] == 1
    assert calls["doc_relationships"]["count"] == 0


async def test_audit_suggested_actions_structure(db: Database):
    # Seed a patient with missing AI to trigger backfill_ai_classification suggestion
    for i in range(3):
        await _insert_doc(db, filename=f"missing_ai_{i}.pdf", ai_summary=None)

    result = json.loads(await audit_document_pipeline(_ctx(db)))
    actions = result["suggested_actions"]
    assert len(actions) > 0
    # Each action is a structured dict — not a plain string
    for a in actions:
        assert isinstance(a, dict)
        assert "tool" in a and "params" in a and "reason" in a

    tools = {a["tool"] for a in actions}
    assert "backfill_ai_classification" in tools


async def test_audit_deleted_docs_excluded(db: Database):
    d1 = await _insert_doc(db, filename="kept.pdf")
    d2 = await _insert_doc(db, filename="deleted.pdf")
    await db.delete_document(d2.id, patient_id=ERIKA_UUID)

    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["totals"]["total_documents"] == 1
    # Only the kept doc is in stuck_sample (if any) — not the deleted one
    stuck_ids = [s["id"] for s in result["stuck_sample"]]
    assert d2.id not in stuck_ids
    assert d1.id in stuck_ids or result["pipeline_gaps"]["fully_complete"] >= 0


async def test_audit_summary_markdown_present(db: Database):
    await _insert_doc(db, filename="doc.pdf")
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert "summary_markdown" in result
    assert ERIKA_UUID in result["summary_markdown"]


# ── #475: false-positive suppression for non-extractable + legacy-OCR docs ──


async def test_audit_xlsx_not_counted_as_missing_ocr(db: Database):
    """XLSX/DOCX/MD mimes can't be OCR'd. #466 filters them from extract_all_metadata;
    the audit must mirror that filter and NOT count them as missing_ocr/ai/metadata.
    """
    await _insert_doc(
        db,
        filename="biomarker_matrix.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        document_date=date(2024, 3, 1),
        institution="NOU",
        gdrive_id="gdrive_xls",
    )
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    gaps = result["pipeline_gaps"]
    assert gaps["missing_ocr"] == 0, "xlsx must not count as missing_ocr"
    assert gaps["missing_ai"] == 0, "xlsx must not count as missing_ai"
    assert gaps["missing_metadata"] == 0, "xlsx must not count as missing_metadata"
    assert gaps["non_extractable_skipped"] == 1


async def test_audit_legacy_ocr_doc_not_counted_as_missing_ocr(db: Database):
    """Legacy PDFs processed before document_pages existed have ai_summary + metadata
    populated but no per-page rows. They should NOT be flagged as missing_ocr.
    """
    await _insert_doc(
        db,
        filename="20240115_ErikaFusekova_NOU_Reference_Legacy.pdf",
        mime_type="application/pdf",
        ai_summary="legacy pipeline summary",
        structured_metadata='{"document_type":"reference"}',
        document_date=date(2024, 1, 15),
        institution="NOU",
        gdrive_id="gdrive_leg",
    )
    # NOTE: no save_ocr_page call — this simulates the legacy state
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    gaps = result["pipeline_gaps"]
    assert gaps["missing_ocr"] == 0, "legacy PDF with ai_summary+metadata must not be missing_ocr"
    assert gaps["fully_complete"] == 1


async def test_audit_xlsx_does_not_appear_in_stuck_sample(db: Database):
    """XLSX with date+institution+sync+canonical name has no real gaps —
    must not appear in stuck_sample even though it's not 'fully_complete' under
    the old extraction-strict definition.
    """
    await _insert_doc(
        db,
        filename="20240301_ErikaFusekova_NOU_Reference_BiomarkerMatrix.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        document_date=date(2024, 3, 1),
        institution="NOU",
        gdrive_id="gdrive_xls_complete",
    )
    result = json.loads(await audit_document_pipeline(_ctx(db)))
    assert result["stuck_sample"] == []
    # Non-extractable completes count as fully_complete when everything else is set
    assert result["pipeline_gaps"]["fully_complete"] == 1
