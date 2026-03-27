"""Tests for GDrive folder hygiene tools."""

import json
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.models import DocumentCategory
from oncofiles.tools.hygiene import (
    _DOCTYPE_TO_CATEGORY,
    _UNMANAGED_FOLDER_MAP,
    qa_analysis,
    reconcile_gdrive,
    validate_categories,
)
from tests.helpers import make_doc

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _mock_ctx(
    db: Database,
    gdrive: MagicMock | None = None,
    folder_id: str = "root_folder_123",
) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "files": MagicMock(),
        "gdrive": gdrive,
        "oauth_folder_id": folder_id,
    }
    return ctx


# ── reconcile_gdrive ─────────────────────────────────────────────────────────


async def test_reconcile_no_gdrive(db: Database):
    ctx = _mock_ctx(db, gdrive=None)
    result = json.loads(await reconcile_gdrive(ctx))
    assert "error" in result


@patch("oncofiles.config.GOOGLE_DRIVE_FOLDER_ID", "")
async def test_reconcile_no_folder_id(db: Database):
    ctx = _mock_ctx(db, gdrive=MagicMock(), folder_id="")
    result = json.loads(await reconcile_gdrive(ctx))
    assert "error" in result


@patch("oncofiles.tools.hygiene._count_files_in_folder", return_value=0)
@patch("oncofiles.tools.hygiene._list_root_items")
async def test_reconcile_detects_unknown_folders(mock_list, mock_count, db: Database):
    """Detects unknown folders, backups, root files, and empty managed folders."""
    mock_list.return_value = [
        {"id": "f1", "name": "labs — laboratórne výsledky", "mimeType": _FOLDER_MIME},
        {"id": "f2", "name": "Guidelines", "mimeType": _FOLDER_MIME},
        {"id": "f3", "name": ".zaloha_nazvov_20260302", "mimeType": _FOLDER_MIME},
        {"id": "f4", "name": "MyDoc.pdf", "mimeType": "application/pdf", "size": "1234"},
    ]

    ctx = _mock_ctx(db, gdrive=MagicMock())
    result = json.loads(await reconcile_gdrive(ctx, dry_run=True))

    assert result["summary"]["unknown_folders"] == 1
    assert result["unknown_folders"][0]["name"] == "Guidelines"
    assert result["unknown_folders"][0]["suggested_category"] == "reference"
    assert result["summary"]["backup_folders"] == 1
    assert result["summary"]["root_files"] == 1
    assert result["summary"]["empty_folders"] == 1
    assert result["empty_folders"][0]["category"] == "labs"


@patch("oncofiles.tools.hygiene._count_files_in_folder", return_value=0)
@patch("oncofiles.tools.hygiene._list_root_items")
async def test_reconcile_skips_metadata_folders(mock_list, mock_count, db: Database):
    """Empty metadata folders should not be flagged."""
    mock_list.return_value = [
        {"id": "f1", "name": "conversations — záznamy rozhovorov", "mimeType": _FOLDER_MIME},
        {"id": "f2", "name": "treatment — priebeh liečby", "mimeType": _FOLDER_MIME},
        {"id": "f3", "name": "research — výskum", "mimeType": _FOLDER_MIME},
    ]

    ctx = _mock_ctx(db, gdrive=MagicMock())
    result = json.loads(await reconcile_gdrive(ctx, dry_run=True))
    assert result["summary"]["empty_folders"] == 0


@patch("oncofiles.tools.hygiene._list_root_items")
async def test_reconcile_skips_manifest_files(mock_list, db: Database):
    """Root files starting with _ should be ignored."""
    mock_list.return_value = [
        {"id": "f1", "name": "_manifest.json", "mimeType": "application/json"},
    ]

    ctx = _mock_ctx(db, gdrive=MagicMock())
    result = json.loads(await reconcile_gdrive(ctx, dry_run=True))
    assert result["summary"]["root_files"] == 0


# ── validate_categories ───────────────────────────────────────────────────────


async def test_validate_no_docs(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=True))
    assert result["summary"]["mismatches_found"] == 0


async def test_validate_detects_discharge_summary(db: Database):
    """category=report + doc_type=discharge_summary → flagged."""
    doc = make_doc(
        file_id="file_ds1",
        filename="20260227_ErikaFusekova_NOU_Report_Test.pdf",
        category=DocumentCategory.REPORT,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    await db.db.execute(
        "UPDATE documents SET structured_metadata = ? WHERE id = ?",
        (json.dumps({"document_type": "discharge_summary"}), doc.id),
    )
    await db.db.commit()

    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=True))
    assert result["summary"]["mismatches_found"] >= 1

    m = next(x for x in result["mismatches"] if x["doc_id"] == doc.id)
    assert m["current_category"] == "report"
    assert m["suggested_category"] == "discharge"


async def test_validate_corrects_surgical_report(db: Database):
    """dry_run=False updates category in DB."""
    doc = make_doc(
        file_id="file_sr1",
        filename="20260212_ErikaFusekova_NOU_Report_PICC.pdf",
        category=DocumentCategory.REPORT,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    await db.db.execute(
        "UPDATE documents SET structured_metadata = ? WHERE id = ?",
        (json.dumps({"document_type": "surgical_report"}), doc.id),
    )
    await db.db.commit()

    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=False))
    assert result["summary"]["corrected"] >= 1

    updated = await db.get_document(doc.id)
    assert updated.category == DocumentCategory.SURGERY


async def test_validate_skips_advocate(db: Database):
    """Advocate docs keep their category regardless of doc_type."""
    doc = make_doc(
        file_id="file_adv1",
        filename="20260301_Advocate_Notes.pdf",
        category=DocumentCategory.ADVOCATE,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    await db.db.execute(
        "UPDATE documents SET structured_metadata = ? WHERE id = ?",
        (json.dumps({"document_type": "chemo_sheet"}), doc.id),
    )
    await db.db.commit()

    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=True))
    flagged = [m for m in result["mismatches"] if m["doc_id"] == doc.id]
    assert len(flagged) == 0


async def test_validate_reference_by_filename(db: Database):
    """Docs in 'other' with DeVita in filename → reference."""
    doc = make_doc(
        file_id="file_ref1",
        filename="ErikaFusekova-Other-DeVita_Ch40_CRC.pdf",
        category=DocumentCategory.OTHER,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=True))
    assert result["summary"]["mismatches_found"] >= 1

    m = next(x for x in result["mismatches"] if x["doc_id"] == doc.id)
    assert m["suggested_category"] == "reference"


async def test_validate_genetics_by_filename(db: Database):
    """Pathology docs with genetics keywords → genetics."""
    doc = make_doc(
        file_id="file_gen1",
        filename="20260210_ErikaFusekova_NOU_Pathology_Genetika.pdf",
        category=DocumentCategory.PATHOLOGY,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=True))
    assert result["summary"]["mismatches_found"] >= 1

    m = next(x for x in result["mismatches"] if x["doc_id"] == doc.id)
    assert m["suggested_category"] == "genetics"


async def test_validate_corrects_genetics(db: Database):
    """Execute mode recategorizes genetics docs."""
    doc = make_doc(
        file_id="file_gen2",
        filename="20260210_ErikaFusekova_NOU_Pathology_Genetic.pdf",
        category=DocumentCategory.PATHOLOGY,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    ctx = _mock_ctx(db)
    result = json.loads(await validate_categories(ctx, dry_run=False))
    assert result["summary"]["corrected"] >= 1

    updated = await db.get_document(doc.id)
    assert updated.category == DocumentCategory.GENETICS


# ── qa_analysis ───────────────────────────────────────────────────────────────


async def test_qa_analysis_empty(db: Database):
    """Empty activity log returns zero findings."""
    ctx = _mock_ctx(db)
    result = json.loads(await qa_analysis(ctx, days=7))
    assert result["summary"]["total_calls"] == 0
    assert result["findings"] == []


async def test_qa_analysis_with_errors(db: Database):
    """Errors in activity log produce findings."""
    from oncofiles.models import ActivityLogEntry

    for _i in range(3):
        await db.insert_activity_log(
            ActivityLogEntry(
                session_id="test-session",
                agent_id="auto",
                tool_name="broken_tool",
                status="error",
                error_message="Connection failed",
            )
        )

    ctx = _mock_ctx(db)
    result = json.loads(await qa_analysis(ctx, days=7))
    assert result["summary"]["total_errors"] >= 3
    assert any(f["type"] == "recurring_error" for f in result["findings"])


# ── Constants ─────────────────────────────────────────────────────────────────


def test_unmanaged_folder_map():
    valid = {c.value for c in DocumentCategory}
    for folder, cat in _UNMANAGED_FOLDER_MAP.items():
        assert cat in valid, f"'{folder}' → invalid '{cat}'"


def test_doctype_to_category_map():
    valid = {c.value for c in DocumentCategory}
    for dtype, cat in _DOCTYPE_TO_CATEGORY.items():
        assert cat in valid, f"'{dtype}' → invalid '{cat}'"
