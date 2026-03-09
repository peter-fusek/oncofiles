"""Tests for GDrive sync logic (#v1.0)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.sync import _sync_lock, sync, sync_from_gdrive, sync_to_gdrive
from tests.helpers import make_doc


def _mock_files():
    """Create a mock FilesClient."""
    files = MagicMock()
    files.upload.return_value = MagicMock(
        id="new_file_id", mime_type="application/pdf", size_bytes=1024
    )
    files.download.return_value = b"fake-pdf-content"
    return files


def _mock_gdrive(gdrive_files: list[dict] | None = None, folder_map: dict | None = None):
    """Create a mock GDriveClient with configurable file list and folder ops."""
    gdrive = MagicMock()
    gdrive.list_folder_with_structure.return_value = (gdrive_files or [], folder_map or {})
    gdrive.list_folder.return_value = gdrive_files or []
    gdrive.download.return_value = b"fake-pdf-content"
    gdrive.upload.return_value = {"id": "gdrive_new_id", "modifiedTime": "2026-03-01T00:00:00Z"}
    gdrive.find_folder.return_value = None
    gdrive.create_folder.side_effect = lambda name, parent: f"folder_{name}"
    gdrive.rename_file.return_value = None
    gdrive.update.return_value = {"id": "updated_id", "modifiedTime": "2026-03-01T00:00:00Z"}
    gdrive.get_file_parents.return_value = ["some_unorganized_folder"]
    return gdrive


# ── sync_from_gdrive ────────────────────────────────────────────────────────


async def test_sync_from_gdrive_new_file(db: Database):
    """New file on GDrive gets imported."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "20260301 ErikaFusekova-NOU-LabVysledky.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value={"diagnoses": []}),
    ):
        stats = await sync_from_gdrive(db, files, gdrive, "folder123")

    assert stats["new"] == 1
    assert stats["unchanged"] == 0
    assert stats["errors"] == 0

    # Verify document in DB
    docs = await db.list_documents()
    assert len(docs) == 1
    assert docs[0].gdrive_id == "gd_1"
    assert docs[0].institution == "NOU"
    assert docs[0].sync_state == "synced"


async def test_sync_from_gdrive_unchanged(db: Database):
    """Existing file with same modifiedTime is skipped."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 3, 1, 10, 0))
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": doc.filename,
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00+00:00",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", enhance=False)
    assert stats["unchanged"] == 1
    assert stats["new"] == 0


async def test_sync_from_gdrive_updated(db: Database):
    """File with newer modifiedTime on GDrive gets re-imported."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 1, 1))
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": doc.filename,
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value={"diagnoses": []}),
    ):
        stats = await sync_from_gdrive(db, files, gdrive, "folder123")

    assert stats["updated"] == 1


async def test_sync_from_gdrive_dry_run(db: Database):
    """Dry run counts files but doesn't create them."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "20260301 ErikaFusekova-NOU-LabVysledky.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", dry_run=True)
    assert stats["new"] == 1

    # No documents should be in the DB
    docs = await db.list_documents()
    assert len(docs) == 0


async def test_sync_from_gdrive_skips_unsupported(db: Database):
    """Files with unsupported extensions are skipped."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "spreadsheet.xlsx",
                "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123")
    assert stats["skipped"] == 1
    assert stats["new"] == 0


async def test_sync_from_gdrive_skips_metadata_files(db: Database):
    """Manifest and markdown metadata files are skipped."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "_manifest.json",
                "mimeType": "application/json",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
            {
                "id": "gd_2",
                "name": "treatment-timeline.md",
                "mimeType": "text/markdown",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123")
    assert stats["skipped"] == 2
    assert stats["new"] == 0


async def test_sync_from_gdrive_error_handling(db: Database):
    """Download errors are counted, not raised."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "20260301 ErikaFusekova-NOU-LabVysledky.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )
    gdrive.download.side_effect = Exception("network error")

    stats = await sync_from_gdrive(db, files, gdrive, "folder123")
    assert stats["errors"] == 1
    assert stats["new"] == 0


async def test_sync_from_gdrive_detects_missing(db: Database):
    """Files in DB but not on GDrive are flagged as missing."""
    doc = make_doc(gdrive_id="gd_missing")
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive([])  # Empty GDrive

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", enhance=False)
    assert stats["missing"] == 1


async def test_sync_from_gdrive_matches_by_app_properties(db: Database):
    """Files with oncofiles_id in appProperties match existing docs."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 3, 1, 10, 0))
    doc = await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": doc.filename,
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00+00:00",
                "appProperties": {"oncofiles_id": str(doc.id)},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", enhance=False)
    assert stats["unchanged"] == 1


async def test_sync_from_gdrive_detects_category_from_folder(db: Database):
    """Category is detected from parent folder name."""
    doc = make_doc(
        gdrive_id="gd_1",
        gdrive_modified_time=datetime(2026, 1, 1),
        category="other",
    )
    await db.insert_document(doc)

    files = _mock_files()
    folder_map = {"folder_labs": "labs — laboratórne výsledky"}
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": doc.filename,
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": ["folder_labs"],
            },
        ],
        folder_map=folder_map,
    )

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value={"diagnoses": []}),
    ):
        stats = await sync_from_gdrive(db, files, gdrive, "folder123")

    assert stats["updated"] == 1
    updated_doc = await db.get_document(doc.id)
    assert updated_doc.category.value == "labs"


# ── sync_to_gdrive ─────────────────────────────────────────────────────────


async def test_sync_to_gdrive_exports_new(db: Database):
    """Documents without gdrive_id get exported to folder structure."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["exported"] == 1
    assert stats["skipped"] == 0

    gdrive.upload.assert_called()

    # Verify gdrive_id was set
    updated_doc = await db.get_document(doc.id)
    assert updated_doc.gdrive_id == "gdrive_new_id"
    assert updated_doc.sync_state == "synced"


async def test_sync_to_gdrive_organizes_existing(db: Database):
    """Documents with gdrive_id in unorganized folders get moved."""
    doc = make_doc(gdrive_id="existing_gd_id")
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()
    # File is in an unorganized folder (not matching any category folder)
    gdrive.get_file_parents.return_value = ["some_random_folder"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["organized"] == 1
    assert stats["exported"] == 0
    gdrive.move_file.assert_called_once()


async def test_sync_to_gdrive_skips_already_organized(db: Database):
    """Documents already in correct organized folder are skipped."""
    doc = make_doc(gdrive_id="existing_gd_id")
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()
    # File is already in an organized category folder (bilingual name)
    gdrive.get_file_parents.return_value = ["folder_report — lekárske správy"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["skipped"] == 1
    assert stats["organized"] == 0
    gdrive.move_file.assert_not_called()


async def test_sync_to_gdrive_dry_run(db: Database):
    """Dry run counts but doesn't export."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", dry_run=True)
    assert stats["exported"] == 1

    gdrive.upload.assert_not_called()

    # gdrive_id should still be None
    updated_doc = await db.get_document(doc.id)
    assert updated_doc.gdrive_id is None


async def test_sync_to_gdrive_error_handling(db: Database):
    """Export errors are counted, not raised."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc)

    files = _mock_files()
    files.download.side_effect = Exception("download failed")
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["errors"] == 1
    assert stats["exported"] == 0


async def test_sync_to_gdrive_exports_metadata(db: Database):
    """Metadata files (manifest, conversation logs, etc.) are exported."""
    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["metadata_exported"] == 1


async def test_sync_to_gdrive_exports_ocr_companion(db: Database):
    """OCR text is exported as companion _OCR.txt alongside the original."""
    doc = make_doc(gdrive_id="gd_existing")
    doc = await db.insert_document(doc)
    await db.save_ocr_page(doc.id, 1, "Page 1 OCR text here", "test-model")
    await db.save_ocr_page(doc.id, 2, "Page 2 OCR text here", "test-model")

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["parent_folder_id"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["ocr_exported"] == 1

    # Verify the OCR text was uploaded to the same parent folder
    upload_calls = [c for c in gdrive.upload.call_args_list if "_OCR.txt" in str(c)]
    assert len(upload_calls) >= 1
    call_kwargs = upload_calls[0]
    # Check filename ends with _OCR.txt
    call_args = call_kwargs[1] if call_kwargs[1] else call_kwargs[0]
    assert call_args["filename"].endswith("_OCR.txt")
    assert call_args["folder_id"] == "parent_folder_id"
    assert call_args["mime_type"] == "text/plain"


async def test_sync_to_gdrive_extracts_ocr_for_pdf(db: Database):
    """PDFs without OCR text get auto-extracted during sync."""
    doc = make_doc(gdrive_id="gd_pdf", mime_type="application/pdf")
    doc = await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["parent_folder_id"]

    # Patch _extract_pdf_text to return some text
    with patch("oncofiles.tools._helpers._extract_pdf_text", return_value=["Page 1 text from PDF"]):
        stats = await sync_to_gdrive(db, files, gdrive, "folder123")

    assert stats.get("ocr_extracted", 0) == 1
    assert stats["ocr_exported"] == 1
    # Verify OCR was cached in DB
    assert await db.has_ocr_text(doc.id)


async def test_sync_from_gdrive_skips_ocr_txt(db: Database):
    """OCR companion files are skipped during import."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_ocr",
                "name": "20260301 ErikaFusekova-NOU-LabVysledky_OCR.txt",
                "mimeType": "text/plain",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": ["root"],
            }
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", enhance=False)
    assert stats["skipped"] == 1
    assert stats["new"] == 0


# ── Unified sync ────────────────────────────────────────────────────────


async def test_sync_bidirectional(db: Database):
    """Unified sync runs both directions."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync(db, files, gdrive, "folder123", enhance=False)

    assert "from_gdrive" in stats
    assert "to_gdrive" in stats
    assert stats["to_gdrive"]["exported"] == 1


async def test_sync_to_gdrive_renames_to_bilingual(db: Database):
    """Files on GDrive get renamed to bilingual format (EN category prefix)."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227 ErikaFusekova-NOU-LabVysledkyPred2chemoMudrPorsok.pdf",
        category="labs",
    )
    doc = await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_labs — laboratórne výsledky"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats.get("renamed", 0) == 1

    # Verify GDrive rename was called
    gdrive.rename_file.assert_any_call(
        "gd_existing",
        "20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemoMudrPorsok.pdf",
    )

    # Verify DB filename updated
    updated = await db.get_document(doc.id)
    assert "Labs-" in updated.filename


async def test_sync_to_gdrive_skips_already_bilingual(db: Database):
    """Files already with bilingual prefix are not renamed again."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemoMudrPorsok.pdf",
        category="labs",
    )
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_labs — laboratórne výsledky"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats.get("renamed", 0) == 0


async def test_sync_to_gdrive_idempotent_double_run(db: Database):
    """Running sync_to_gdrive twice produces no changes on second run."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227 ErikaFusekova-NOU-LabVysledkyPred2chemoMudrPorsok.pdf",
        category="labs",
    )
    await db.insert_document(doc)
    # Pre-populate OCR so export doesn't try to extract
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "test")

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_labs — laboratórne výsledky"]

    # First run — renames file
    stats1 = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats1.get("renamed", 0) == 1

    # Reset mock call counts but keep behavior
    gdrive.rename_file.reset_mock()

    # Second run — should skip (already renamed)
    stats2 = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats2.get("renamed", 0) == 0
    # rename_file should only be called for OCR companion (not for doc itself)
    # since doc is already bilingual
    doc_rename_calls = [c for c in gdrive.rename_file.call_args_list if "_OCR.txt" not in str(c)]
    assert len(doc_rename_calls) == 0


async def test_sync_to_gdrive_cleanup_orphan_ocr(db: Database):
    """Orphaned OCR files (old names) get trashed during sync."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemoMudrPorsok.pdf",
        category="labs",
    )
    await db.insert_document(doc)
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "test")

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["parent_folder"]

    # Simulate orphaned OCR file (old name without Labs- prefix) in folder
    orphan_ocr = {
        "id": "orphan_id",
        "name": "20260227 ErikaFusekova-NOU-LabVysledkyPred2chemoMudrPorsok_OCR.txt",
        "mimeType": "text/plain",
    }
    expected_ocr = {
        "id": "expected_id",
        "name": "20260227 ErikaFusekova-NOU-Labs-LabVysledkyPred2chemoMudrPorsok_OCR.txt",
        "mimeType": "text/plain",
    }
    gdrive.list_folder.return_value = [orphan_ocr, expected_ocr]
    gdrive.trash_file.return_value = None

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats.get("ocr_cleaned", 0) == 1
    gdrive.trash_file.assert_called_once_with("orphan_id")


async def test_sync_from_gdrive_extracts_structured_metadata(db: Database):
    """Structured metadata is extracted during sync enhancement."""
    from oncofiles.sync import _enhance_document

    # Insert a doc and pre-populate OCR text so _enhance_document has text to work with
    doc = make_doc()
    doc = await db.insert_document(doc)
    await db.save_ocr_page(doc.id, 1, "Patient diagnosed with CRC. Rx: FOLFOX.", "test")

    files = _mock_files()
    gdrive = _mock_gdrive()

    test_metadata = {"diagnoses": ["CRC"], "medications": ["FOLFOX"], "findings": []}
    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value=test_metadata),
    ):
        result = await _enhance_document(db, doc, files, gdrive)

    assert result is True
    updated = await db.get_document(doc.id)
    assert updated.structured_metadata is not None
    import json

    parsed = json.loads(updated.structured_metadata)
    assert parsed["diagnoses"] == ["CRC"]
    assert parsed["medications"] == ["FOLFOX"]


# ── Sync mutex ──────────────────────────────────────────────────────────


async def test_sync_mutex_prevents_concurrent(db: Database):
    """Second sync call returns 'already in progress' if lock is held."""
    files = _mock_files()
    gdrive = _mock_gdrive()

    # Manually acquire the lock to simulate ongoing sync
    await _sync_lock.acquire()
    try:
        result = await sync(db, files, gdrive, "folder123", enhance=False)
        assert result.get("skipped") is True
        assert "already in progress" in result.get("message", "").lower()
    finally:
        _sync_lock.release()


async def test_sync_mutex_allows_sequential(db: Database):
    """Sequential sync calls both succeed (lock is released between them)."""
    files = _mock_files()
    gdrive = _mock_gdrive()

    stats1 = await sync(db, files, gdrive, "folder123", enhance=False)
    assert "from_gdrive" in stats1

    stats2 = await sync(db, files, gdrive, "folder123", enhance=False)
    assert "from_gdrive" in stats2
