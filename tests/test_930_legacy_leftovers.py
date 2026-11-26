"""Lock tests for #930 part 1: legacy leftovers on the non-curated side.

1. A calendar keyword match never creates a TreatmentEvent row.
2. ``get_related_documents`` says so explicitly when there are no links, computes
   only the deterministic ``same_visit`` link (same patient, date and known
   institution; no model call, nothing written), and the server instructions
   promise nothing more.
3. Sync never invents a document date (no Drive mtime, created_at or literal
   placeholder date in a rebuilt filename or as ``document_date``).
4. The primary-clinic institution fallbacks never touch curated / ``gdrive:`` rows.
5. ``validate_categories`` real runs no longer crash (``corrected`` is a list).
6. ``filename_parser`` / ``sync`` log a name length, never the filename.

All values are synthetic (invented names, 2027+ dates).
"""

from __future__ import annotations

import ast
import json
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oncofiles import patient_context
from oncofiles.calendar_sync import (
    KEYWORD_EVENT_TYPE,
    _auto_create_treatment_event,
    _calendar_sync_inner,
    _classify_event_medical,
)
from oncofiles.database import Database
from oncofiles.models import CalendarEntry, DocumentCategory
from tests.helpers import PATIENT_A_UUID, make_doc

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"


def _mock_ctx(db: Database, gdrive: MagicMock | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "files": MagicMock(),
        "gdrive": gdrive,
        "oauth_folder_id": "root_folder_930",
    }
    return ctx


# ── 1. Calendar: keyword matches never create treatment events ─────────────


async def _treatment_event_count(db: Database) -> int:
    async with db.db.execute(
        "SELECT COUNT(*) AS n FROM treatment_events WHERE patient_id = ?", (PATIENT_A_UUID,)
    ) as cur:
        return (await cur.fetchone())["n"]


def _calendar(events: list[dict]) -> MagicMock:
    cal = MagicMock()
    cal.list_events.return_value = {"items": events}
    return cal


def _event(event_id: str, summary: str) -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "description": "",
        "location": "",
        "start": {"dateTime": "2027-04-12T09:00:00Z"},
        "end": {"dateTime": "2027-04-12T09:30:00Z"},
    }


async def test_keyword_fallback_marks_possibly_medical_with_keyword_type(db: Database):
    with patch("oncofiles.cloud_guard.cloud_ai_enabled", return_value=False):
        is_medical, _, event_type = await _classify_event_medical(
            "Lab visit", "", "", patient_id=PATIENT_A_UUID
        )
    assert is_medical is True
    assert event_type == KEYWORD_EVENT_TYPE


async def test_calendar_keyword_match_creates_no_treatment_event(db: Database):
    cal = _calendar([_event("ev_930_a", "CT kontrola"), _event("ev_930_b", "Lab")])
    with patch("oncofiles.cloud_guard.cloud_ai_enabled", return_value=False):
        stats = await _calendar_sync_inner(db, cal, True, patient_id=PATIENT_A_UUID)

    assert stats["medical"] == 2  # still shown as possibly medical
    assert stats["treatment_events_created"] == 0
    assert await _treatment_event_count(db) == 0
    entries = await db.list_calendar_entries(patient_id=PATIENT_A_UUID)
    assert {e.google_event_id for e in entries} == {"ev_930_a", "ev_930_b"}
    assert all(e.is_medical for e in entries)
    assert all(e.treatment_event_id is None for e in entries)


async def test_calendar_model_classification_still_creates_treatment_event(db: Database):
    cal = _calendar([_event("ev_930_c", "Synthetic infusion")])
    with patch(
        "oncofiles.calendar_sync._classify_event_medical",
        return_value=(True, 0.9, "chemo"),
    ):
        stats = await _calendar_sync_inner(db, cal, True, patient_id=PATIENT_A_UUID)
    assert stats["treatment_events_created"] == 1
    assert await _treatment_event_count(db) == 1


async def test_auto_create_refuses_keyword_type_directly(db: Database):
    entry = CalendarEntry(
        google_event_id="ev_930_d",
        summary="Lab",
        start_time=datetime(2027, 4, 12, 9, 0, tzinfo=UTC),
        is_medical=True,
    )
    entry.patient_id = PATIENT_A_UUID
    entry = await db.upsert_calendar_entry(entry)
    assert await _auto_create_treatment_event(db, entry, KEYWORD_EVENT_TYPE) is None
    assert await _treatment_event_count(db) == 0


# ── 2. Related documents: explicit note, no promise of automatic links ─────


async def test_related_documents_empty_returns_note(db: Database):
    from oncofiles.tools.documents import RELATED_DOCUMENTS_NONE_NOTE, get_related_documents

    doc = await db.insert_document(make_doc(file_id="file_930_rel"), patient_id=PATIENT_A_UUID)
    result = json.loads(await get_related_documents(_mock_ctx(db), doc.id))
    assert result["related"] == []
    assert result["total"] == 0
    assert result["note"] == RELATED_DOCUMENTS_NONE_NOTE


def test_server_instructions_do_not_promise_automatic_links():
    from oncofiles.server import _MCP_INSTRUCTIONS

    assert "automatically linked" not in _MCP_INSTRUCTIONS
    assert "links appear when available" in _MCP_INSTRUCTIONS
    # The only automatic link is the computed date-and-place rule; nearby dates or
    # shared diagnoses are no longer promised.
    assert "nearby dates" not in _MCP_INSTRUCTIONS
    assert "`same_visit` (`source: computed`)" in _MCP_INSTRUCTIONS


async def _insert(db: Database, file_id: str, *, patient_id: str = PATIENT_A_UUID, **kw):
    return await db.insert_document(make_doc(file_id=file_id, **kw), patient_id=patient_id)


async def test_related_documents_same_visit_computed_locally(db: Database):
    from oncofiles.tools.documents import SAME_VISIT_BASIS, get_related_documents

    day = date(2027, 6, 14)
    doc = await _insert(db, "file_930_sv_a", document_date=day, institution="ZZZClinic")
    same = await _insert(db, "file_930_sv_b", document_date=day, institution=" zzzclinic ")
    await _insert(db, "file_930_sv_c", document_date=date(2027, 6, 15), institution="ZZZClinic")
    await _insert(db, "file_930_sv_d", document_date=day, institution="QQQHospital")
    await _insert(db, "file_930_sv_e", document_date=day, institution=None)
    gone = await _insert(db, "file_930_sv_f", document_date=day, institution="ZZZClinic")
    await db.delete_document(gone.id, patient_id=PATIENT_A_UUID)

    with patch("oncofiles.enhance._get_client", side_effect=AssertionError("no model call")):
        result = json.loads(await get_related_documents(_mock_ctx(db), doc.id))

    assert [r["id"] for r in result["related"]] == [same.id]
    item = result["related"][0]
    assert item["relationship"] == "same_visit"
    assert item["source"] == "computed"
    assert item["basis"] == SAME_VISIT_BASIS
    assert "note" not in result
    # Read only: nothing is stored.
    async with db.db.execute("SELECT COUNT(*) AS n FROM document_cross_references") as cur:
        assert (await cur.fetchone())["n"] == 0


@pytest.mark.parametrize("institution", [None, "", "Unknown", "  unknown "])
async def test_related_documents_no_same_visit_without_known_institution(db: Database, institution):
    from oncofiles.tools.documents import RELATED_DOCUMENTS_NONE_NOTE, get_related_documents

    day = date(2027, 6, 14)
    doc = await _insert(db, "file_930_ui_a", document_date=day, institution=institution)
    await _insert(db, "file_930_ui_b", document_date=day, institution=institution)

    result = json.loads(await get_related_documents(_mock_ctx(db), doc.id))
    assert result["related"] == []
    assert result["note"] == RELATED_DOCUMENTS_NONE_NOTE


async def test_related_documents_no_same_visit_without_date(db: Database):
    from oncofiles.tools.documents import get_related_documents

    doc = await _insert(db, "file_930_nd_a", document_date=None, institution="ZZZClinic")
    await _insert(db, "file_930_nd_b", document_date=None, institution="ZZZClinic")

    result = json.loads(await get_related_documents(_mock_ctx(db), doc.id))
    assert result["related"] == []


async def test_same_visit_never_crosses_patients(db: Database):
    from oncofiles.tools.documents import get_related_documents

    other_pid = "00000000-0000-4000-8000-000000000930"
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (other_pid, "other-930-test", "Other Testova", "other930@example.com"),
    )
    await db.db.commit()
    day = date(2027, 6, 14)
    doc = await _insert(db, "file_930_xp_a", document_date=day, institution="ZZZClinic")
    await _insert(
        db, "file_930_xp_b", patient_id=other_pid, document_date=day, institution="ZZZClinic"
    )

    result = json.loads(await get_related_documents(_mock_ctx(db), doc.id))
    assert result["related"] == []
    found = await db.find_same_visit_documents(
        doc.id, patient_id=PATIENT_A_UUID, document_date=day.isoformat(), institution="ZZZClinic"
    )
    assert found == []


# ── 3. Never invent a document date ────────────────────────────────────────


def test_sync_source_has_no_invented_dates():
    src = (SRC / "sync.py").read_text()
    tree = ast.parse(src)
    literals = {
        n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert "20260101" not in literals
    assert "20260201" not in literals
    # The Drive modified time is never formatted into a date.
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "strftime"
        ):
            target = ast.unparse(node.func.value)
            assert "modified" not in target, target
            assert "created_at" not in target, target


async def test_rename_without_document_date_keeps_name(db: Database):
    from oncofiles.sync import _rename_to_standard

    doc = make_doc(
        file_id="file_930_nodate",
        filename="scan kontrola.pdf",
        original_filename="scan kontrola.pdf",
        document_date=None,
        gdrive_id="gd_930_nodate",
        gdrive_modified_time=datetime(2031, 11, 23, 17, 41, tzinfo=UTC),
    )
    await db.insert_document(doc, patient_id=PATIENT_A_UUID)
    gdrive = MagicMock()
    gdrive.batch_rename.side_effect = lambda renames: {fid: True for fid in renames}

    stats = await _rename_to_standard(db, gdrive, patient_id=PATIENT_A_UUID)

    assert stats["renamed"] == 0
    gdrive.batch_rename.assert_not_called()
    docs = await db.list_documents(patient_id=PATIENT_A_UUID)
    assert [d.filename for d in docs] == ["scan kontrola.pdf"]


async def test_rename_with_document_date_uses_that_date(db: Database):
    from oncofiles.sync import _rename_to_standard

    doc = make_doc(
        file_id="file_930_dated",
        filename="scan kontrola.pdf",
        original_filename="scan kontrola.pdf",
        document_date=date(2027, 3, 9),
        gdrive_id="gd_930_dated",
        gdrive_modified_time=datetime(2031, 11, 23, 17, 41, tzinfo=UTC),
    )
    await db.insert_document(doc, patient_id=PATIENT_A_UUID)
    gdrive = MagicMock()
    gdrive.batch_rename.side_effect = lambda renames: {fid: True for fid in renames}
    gdrive.get_file_parents.return_value = []

    stats = await _rename_to_standard(db, gdrive, patient_id=PATIENT_A_UUID)

    assert stats["renamed"] == 1
    (renames,) = gdrive.batch_rename.call_args.args
    assert renames["gd_930_dated"].startswith("20270309_")


# ── 4. Institution fallbacks never touch curated / gdrive: rows ────────────


@pytest.fixture
def primary_clinic():
    patient_context._contexts[PATIENT_A_UUID] = {
        "name": "Jana Testova",
        "primary_institution": "ZZZClinic",
        "treatment": {"institution": "ZZZClinic"},
    }
    yield
    patient_context._contexts.pop(PATIENT_A_UUID, None)


async def _institution(db: Database, doc_id: int):
    async with db.db.execute("SELECT institution FROM documents WHERE id = ?", (doc_id,)) as cur:
        return (await cur.fetchone())["institution"]


async def test_backfill_missing_institutions_skips_gdrive_rows(db: Database, primary_clinic):
    from oncofiles.enhance import backfill_missing_institutions

    doc = make_doc(file_id="gdrive:synthetic930a", institution=None)
    doc.category = DocumentCategory.CHEMO_SHEET
    doc = await db.insert_document(doc, patient_id=PATIENT_A_UUID)
    await db.update_structured_metadata(doc.id, json.dumps({"providers": []}))

    stats = await backfill_missing_institutions(db.db)

    assert stats["skipped_curated"] == 1
    assert stats["updated_from_patient_context"] == 0
    assert await _institution(db, doc.id) is None


async def test_patient_context_fallback_skips_gdrive_and_curated_rows(db: Database, primary_clinic):
    from oncofiles.curated import indexer
    from oncofiles.enhance import backfill_institution_from_patient_context

    drive_only = make_doc(file_id="gdrive:synthetic930b", institution=None)
    drive_only.category = DocumentCategory.DISCHARGE
    drive_only = await db.insert_document(drive_only, patient_id=PATIENT_A_UUID)
    curated = make_doc(file_id="file_930_curated", institution=None)
    curated.category = DocumentCategory.DISCHARGE
    curated = await db.insert_document(curated, patient_id=PATIENT_A_UUID)
    legacy = make_doc(file_id="file_930_legacy", institution=None)
    legacy.category = DocumentCategory.DISCHARGE
    legacy = await db.insert_document(legacy, patient_id=PATIENT_A_UUID)

    indexer.register_curated_ids([curated.id])
    try:
        stats = await backfill_institution_from_patient_context(db.db, patient_id=PATIENT_A_UUID)
    finally:
        indexer.forget_curated_ids([curated.id])

    assert stats["skipped_curated"] == 2
    assert await _institution(db, drive_only.id) is None
    assert await _institution(db, curated.id) is None
    assert await _institution(db, legacy.id) == "ZZZClinic"


def test_unscheduled_institution_backfill_wrapper_is_gone():
    assert "_run_institution_backfill" not in (SRC / "server.py").read_text()


# ── 5. validate_categories real run ────────────────────────────────────────


async def test_validate_categories_real_run_does_not_crash(db: Database):
    from oncofiles.tools.hygiene import validate_categories

    doc = make_doc(
        file_id="file_930_adv",
        filename="20270115_JanaTestova_NOU_Report_PacientAdvokat.pdf",
        category=DocumentCategory.REPORT,
    )
    doc = await db.insert_document(doc, patient_id=PATIENT_A_UUID)
    await db.update_structured_metadata(doc.id, json.dumps({"document_type": "report"}))

    dry = json.loads(await validate_categories(_mock_ctx(db), dry_run=True))
    assert [m["action"] for m in dry["mismatches"] if m["doc_id"] == doc.id] == ["would_correct"]

    real = json.loads(await validate_categories(_mock_ctx(db), dry_run=False))
    entry = next(c for c in real["corrected"] if c["doc_id"] == doc.id)
    assert entry["action"] == "corrected"
    assert entry["suggested_category"] == "advocate"
    assert real["summary"]["corrected"] >= 1
    updated = await db.get_document(doc.id, patient_id=PATIENT_A_UUID)
    assert updated.category.value == "advocate"


# ── 6. Logs carry a name length, never the filename ────────────────────────


def test_filename_parser_unknown_institution_log_has_no_filename(caplog):
    from oncofiles.filename_parser import rename_to_standard

    src = "20270326_JanaTestova_Unknown_ChemoSheet_Zorvanx.pdf"
    with caplog.at_level(logging.INFO, logger="oncofiles.filename_parser"):
        assert rename_to_standard(src, patient_id=PATIENT_A_UUID) == src
    assert caplog.records, "expected the #404 info line"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Zorvanx" not in text
    assert "<name len=" in text
