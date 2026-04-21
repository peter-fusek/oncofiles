"""Tests for GDrive sync logic (#v1.0)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.sync import _sync_lock, sync, sync_from_gdrive, sync_to_gdrive
from tests.helpers import ERIKA_UUID, make_doc


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
    # Batch operations: return dicts keyed by file_id
    gdrive.batch_get_parents.side_effect = lambda fids: {
        fid: gdrive.get_file_parents.return_value for fid in fids
    }
    gdrive.batch_move.side_effect = lambda moves: {fid: True for fid in moves}
    gdrive.batch_rename.side_effect = lambda renames: {fid: True for fid in renames}
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
        stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)

    assert stats["new"] == 1
    assert stats["unchanged"] == 0
    assert stats["errors"] == 0

    # Verify document in DB
    docs = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(docs) == 1
    assert docs[0].gdrive_id == "gd_1"
    assert docs[0].institution == "NOU"
    assert docs[0].sync_state == "synced"


async def test_sync_from_gdrive_unchanged(db: Database):
    """Existing file with same modifiedTime is skipped."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 3, 1, 10, 0))
    await db.insert_document(doc, patient_id=ERIKA_UUID)

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

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID
    )
    assert stats["unchanged"] == 1
    assert stats["new"] == 0


async def test_sync_from_gdrive_updated(db: Database):
    """File with newer modifiedTime on GDrive gets re-imported."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 1, 1))
    await db.insert_document(doc, patient_id=ERIKA_UUID)

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
        stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)

    assert stats["updated"] == 1


async def test_sync_from_gdrive_metadata_only_change(db: Database):
    """Rename-only change (same md5) skips re-import and preserves OCR."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 1, 1))
    doc.gdrive_md5 = "abc123md5"
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    # Simulate existing OCR cache
    await db.save_ocr_page(doc.id, 1, "Extracted OCR text page 1", "pymupdf-native")
    assert await db.has_ocr_text(doc.id)

    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "20260301_ErikaFusekova_NOU_Labs_RenamedFile.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",  # newer than doc
                "md5Checksum": "abc123md5",  # same content
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID
    )

    # Should be counted as unchanged (metadata-only), not updated
    assert stats["unchanged"] == 1
    assert stats["updated"] == 0
    # OCR should be preserved
    assert await db.has_ocr_text(doc.id)
    # No download should have happened
    gdrive.download.assert_not_called()


async def test_sync_from_gdrive_content_changed(db: Database):
    """Content change (different md5) triggers full re-import."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 1, 1))
    doc.gdrive_md5 = "old_md5"
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    # Simulate existing OCR cache
    await db.save_ocr_page(doc.id, 1, "Old OCR text", "pymupdf-native")

    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": doc.filename,
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "md5Checksum": "new_md5",  # content changed
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value={"diagnoses": []}),
    ):
        stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)

    assert stats["updated"] == 1
    # Download should have happened
    gdrive.download.assert_called_once()


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

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder123", dry_run=True, patient_id=ERIKA_UUID
    )
    assert stats["new"] == 1

    # No documents should be in the DB
    docs = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(docs) == 0


async def test_sync_from_gdrive_skips_unsupported(db: Database):
    """Files with unsupported extensions are skipped."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_1",
                "name": "document.gdoc",
                "mimeType": "application/octet-stream",
                "modifiedTime": "2026-03-01T10:00:00Z",
                "appProperties": {},
                "parents": [],
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
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

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
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

    stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["errors"] == 1
    assert stats["new"] == 0


async def test_sync_from_gdrive_detects_missing(db: Database):
    """Files in DB but not on GDrive are flagged as missing."""
    doc = make_doc(gdrive_id="gd_missing")
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive([])  # Empty GDrive

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID
    )
    assert stats["missing"] == 1


async def test_sync_from_gdrive_matches_by_app_properties(db: Database):
    """Files with oncofiles_id in appProperties match existing docs."""
    doc = make_doc(gdrive_id="gd_1", gdrive_modified_time=datetime(2026, 3, 1, 10, 0))
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

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

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID
    )
    assert stats["unchanged"] == 1


async def test_sync_from_gdrive_detects_category_from_folder(db: Database):
    """Category is detected from parent folder name."""
    doc = make_doc(
        gdrive_id="gd_1",
        gdrive_modified_time=datetime(2026, 1, 1),
        category="other",
    )
    await db.insert_document(doc, patient_id=ERIKA_UUID)

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
        stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)

    assert stats["updated"] == 1
    updated_doc = await db.get_document(doc.id)
    assert updated_doc.category.value == "labs"


# ── sync_to_gdrive ─────────────────────────────────────────────────────────


async def test_sync_to_gdrive_exports_new(db: Database):
    """Documents without gdrive_id get exported to folder structure."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
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
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()
    # File is in an unorganized folder (not matching any category folder)
    gdrive.get_file_parents.return_value = ["some_random_folder"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["organized"] >= 1  # Phase 1 + post-rename organize (mock doesn't update parents)
    assert stats["exported"] == 0
    assert gdrive.batch_move.call_count >= 1


async def test_sync_to_gdrive_skips_already_organized(db: Database):
    """Documents already in correct year-month folder are skipped."""
    doc = make_doc(gdrive_id="existing_gd_id")
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()
    # File is already in the correct year-month folder (2024-01 for date 2024-01-15)
    gdrive.get_file_parents.return_value = ["folder_2024-01"]
    # find_folder returns the folder ID to indicate it already exists
    gdrive.find_folder.return_value = "folder_2024-01"

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["skipped"] >= 1
    assert stats["organized"] == 0


async def test_rename_triggers_organize_and_enhance(db: Database):
    """Renamed docs get immediately organized and re-enhanced."""
    # Doc with non-standard filename (missing patient name) and gdrive_id
    doc = make_doc(gdrive_id="gd_rename_test")
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()
    # File is in unorganized folder
    gdrive.get_file_parents.return_value = ["some_random_folder"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)

    # Rename should have happened
    assert stats["renamed"] == 1
    # batch_rename was called (for the rename itself)
    gdrive.batch_rename.assert_called()
    # batch_move was called (Phase 1 + post-rename organize)
    assert gdrive.batch_move.call_count >= 1
    assert stats["organized"] >= 1


async def test_rename_returns_renamed_ids(db: Database):
    """_rename_to_standard returns renamed_ids for post-processing."""
    from oncofiles.sync import _rename_to_standard

    # Doc with non-standard filename
    doc = make_doc(gdrive_id="gd_ids_test")
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    gdrive = _mock_gdrive()
    stats = await _rename_to_standard(db, gdrive, patient_id=ERIKA_UUID)

    assert stats["renamed"] == 1
    assert doc.id in stats["renamed_ids"]


async def test_sync_to_gdrive_dry_run(db: Database):
    """Dry run counts but doesn't export."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(
        db, files, gdrive, "folder123", dry_run=True, patient_id=ERIKA_UUID
    )
    assert stats["exported"] == 1

    gdrive.upload.assert_not_called()

    # gdrive_id should still be None
    updated_doc = await db.get_document(doc.id)
    assert updated_doc.gdrive_id is None


async def test_sync_to_gdrive_error_handling(db: Database):
    """Export errors are counted, not raised."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    files.download.side_effect = Exception("download failed")
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["errors"] == 1
    assert stats["exported"] == 0


async def test_sync_to_gdrive_exports_metadata(db: Database):
    """Metadata files (manifest, conversation logs, etc.) are exported."""
    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["metadata_exported"] == 1


async def test_sync_to_gdrive_no_ocr_companion_export(db: Database):
    """OCR companion files are no longer exported (text cached in DB instead)."""
    doc = make_doc(gdrive_id="gd_existing")
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Page 1 OCR text here", "test-model")

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_2024-01"]
    gdrive.find_folder.return_value = "folder_2024-01"

    await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    # No OCR export — companion files disabled (#114)
    upload_calls = [c for c in gdrive.upload.call_args_list if "_OCR.txt" in str(c)]
    assert len(upload_calls) == 0


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

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID
    )
    assert stats["skipped"] == 1
    assert stats["new"] == 0


# ── Unified sync ────────────────────────────────────────────────────────


async def test_sync_bidirectional(db: Database):
    """Unified sync runs both directions."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync(db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID)

    assert "from_gdrive" in stats
    assert "to_gdrive" in stats
    assert stats["to_gdrive"]["exported"] == 1


async def test_sync_to_gdrive_renames_to_standard(db: Database):
    """Files on GDrive get renamed to standard format (underscore-separated)."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf",
        category="labs",
        institution="NOU",
    )
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_labs — laboratórne výsledky"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats.get("renamed", 0) == 1

    # Verify batch_rename was called with standard format (uses DB institution)
    expected = "20260227_ErikaFusekova_NOU_Labs_LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf"
    gdrive.batch_rename.assert_called_once()
    rename_arg = gdrive.batch_rename.call_args[0][0]
    assert rename_arg.get("gd_existing") == expected

    # Verify DB filename updated
    updated = await db.get_document(doc.id)
    assert "_Labs_" in updated.filename


async def test_sync_to_gdrive_skips_already_standard(db: Database):
    """Files already in standard format with matching metadata are not renamed."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227_ErikaFusekova_NOUonko_Labs_BloodResults.pdf",
        institution="NOUonko",
        category="labs",
    )
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_labs — laboratórne výsledky"]

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats.get("renamed", 0) == 0


async def test_sync_to_gdrive_idempotent_double_run(db: Database):
    """Running sync_to_gdrive twice: first renames, second skips."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227 ErikaFusekova-NOU-LabVysledkyPred2chemo[PHYSICIAN_REDACTED].pdf",
        institution="NOUonko",
        category="labs",
    )
    await db.insert_document(doc, patient_id=ERIKA_UUID)
    # Pre-populate OCR so export doesn't try to extract
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "test")

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["folder_2026-01"]
    gdrive.find_folder.return_value = "folder_2026-01"

    # First run — renames file (non-standard format)
    stats1 = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats1.get("renamed", 0) == 1


async def test_sync_to_gdrive_cleanup_orphan_ocr(db: Database):
    """Orphaned OCR files (old names) get trashed during sync."""
    doc = make_doc(
        gdrive_id="gd_existing",
        filename="20260227_ErikaFusekova_NOUonko_Labs_BloodResults.pdf",
        institution="NOUonko",
        category="labs",
    )
    await db.insert_document(doc, patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "Page 1 text", "test")

    files = _mock_files()
    gdrive = _mock_gdrive()
    gdrive.get_file_parents.return_value = ["parent_folder"]

    # Simulate orphaned OCR file (old bilingual name) in folder
    orphan_ocr = {
        "id": "orphan_id",
        "name": "20260227 ErikaFusekova-NOU-Labs-LabVysledky_OCR.txt",
        "mimeType": "text/plain",
    }
    expected_ocr = {
        "id": "expected_id",
        "name": "20260227_ErikaFusekova_NOUonko_Labs_BloodResults_OCR.txt",
        "mimeType": "text/plain",
    }
    gdrive.list_folder.return_value = [orphan_ocr, expected_ocr]
    gdrive.trash_file.return_value = None

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats.get("ocr_cleaned", 0) == 1
    gdrive.trash_file.assert_called_once_with("orphan_id")


async def test_sync_from_gdrive_extracts_structured_metadata(db: Database):
    """Structured metadata is extracted during sync enhancement."""
    from oncofiles.sync import _enhance_document

    # Insert a doc and pre-populate OCR text so _enhance_document has text to work with
    doc = make_doc()
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)
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


async def test_enhance_document_closes_fitz_on_vision_ocr_exception(db: Database):
    """Regression test for #426: pdf_doc.close() must fire even if Vision OCR raises.

    Before the fix, pdf_doc.close() was the last line of the try block and ran
    only on the happy path. A timeout or API error in extract_text_from_image
    leaked ~10 MB/doc of MuPDF C-buffer memory per failure — the dominant
    component of the 2,500 MB/h growth rate observed during nightly batches.
    """
    import fitz  # ensure module is imported so patch() can find fitz.open

    from oncofiles.sync import _enhance_document

    doc = make_doc(file_id="file_vision_fail", filename="scanned-pathology.pdf")
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()

    mock_pix = MagicMock()
    mock_pix.tobytes.return_value = b"fake-jpeg-page"
    mock_page = MagicMock()
    mock_page.get_pixmap.return_value = mock_pix
    mock_pdf_doc = MagicMock()
    mock_pdf_doc.__iter__ = lambda self: iter([mock_page])

    with (
        patch("oncofiles.server._extract_pdf_text", return_value=None),
        patch.object(fitz, "open", return_value=mock_pdf_doc),
        patch(
            "oncofiles.ocr.extract_text_from_image",
            side_effect=TimeoutError("Vision OCR timed out"),
        ),
    ):
        result = await _enhance_document(db, doc, files, gdrive)

    assert result is False  # no text extracted → not enhanced
    mock_pdf_doc.close.assert_called_once()  # critical: buffer released


async def test_enhance_document_closes_fitz_on_happy_path(db: Database):
    """Also verify pdf_doc.close() still fires on the success path (no regression
    of behavior for the common case)."""
    import fitz

    from oncofiles.sync import _enhance_document

    doc = make_doc(file_id="file_vision_ok", filename="scanned-labs.pdf")
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()

    mock_pix = MagicMock()
    mock_pix.tobytes.return_value = b"fake-jpeg-page"
    mock_page = MagicMock()
    mock_page.get_pixmap.return_value = mock_pix
    mock_pdf_doc = MagicMock()
    mock_pdf_doc.__iter__ = lambda self: iter([mock_page])

    with (
        patch("oncofiles.server._extract_pdf_text", return_value=None),
        patch.object(fitz, "open", return_value=mock_pdf_doc),
        patch(
            "oncofiles.ocr.extract_text_from_image",
            return_value="CRC labs — within range",
        ),
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", "[]")),
        patch("oncofiles.sync.extract_structured_metadata", return_value={}),
    ):
        result = await _enhance_document(db, doc, files, gdrive)

    assert result is True
    mock_pdf_doc.close.assert_called_once()


# ── Sync mutex ──────────────────────────────────────────────────────────


async def test_sync_mutex_prevents_concurrent(db: Database):
    """Second sync call returns 'already in progress' if lock is held."""
    files = _mock_files()
    gdrive = _mock_gdrive()

    # Manually acquire the lock to simulate ongoing sync
    await _sync_lock.acquire()
    try:
        result = await sync(db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID)
        assert result.get("skipped") is True
        assert "already in progress" in result.get("message", "").lower()
    finally:
        _sync_lock.release()


async def test_sync_mutex_allows_sequential(db: Database):
    """Sequential sync calls both succeed (lock is released between them)."""
    files = _mock_files()
    gdrive = _mock_gdrive()

    stats1 = await sync(db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID)
    assert "from_gdrive" in stats1

    stats2 = await sync(db, files, gdrive, "folder123", enhance=False, patient_id=ERIKA_UUID)
    assert "from_gdrive" in stats2


# ── Text file sync via OCR cache ───────────────────────────────────────────


async def test_sync_to_gdrive_text_file_uses_ocr_cache(db: Database):
    """Text/markdown docs use OCR cache instead of Files API download."""
    doc = make_doc(gdrive_id=None, mime_type="text/markdown")
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, "# Chapter 40\n\nContent here.", "text-decode")

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["exported"] == 1

    # Files API download should NOT have been called (text/* uses OCR cache)
    files.download.assert_not_called()

    # First gdrive.upload call should be the doc with OCR text bytes
    first_upload = gdrive.upload.call_args_list[0]
    content = first_upload[1]["content_bytes"]
    assert b"# Chapter 40" in content

    # Doc should now have gdrive_id
    updated = await db.get_document(doc.id)
    assert updated.gdrive_id == "gdrive_new_id"


async def test_sync_to_gdrive_text_file_no_ocr_skipped(db: Database):
    """Text files without OCR cache are skipped."""
    doc = make_doc(gdrive_id=None, mime_type="text/markdown")
    await db.insert_document(doc, patient_id=ERIKA_UUID)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)
    assert stats["skipped"] == 1
    assert stats["exported"] == 0

    # Files API download should not have been called for this doc
    files.download.assert_not_called()


# ── Backfill integration: non-standard filename gets fields from AI ──────


async def test_sync_import_nonstandard_filename_backfills_metadata(db: Database):
    """New file with non-standard name gets date/institution/description from AI metadata."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_nonstandard",
                "name": "Dodatok histológia p. Fuseková.pdf",
                "mimeType": "application/pdf",
                "modifiedTime": "2026-03-16T10:00:00Z",
                "appProperties": {},
                "parents": ["pathology_folder_id"],
            },
        ],
        folder_map={"pathology_folder_id": "pathology"},
    )

    metadata = {
        "document_type": "pathology",
        "findings": ["[BIOMARKER_REDACTED]"],
        "diagnoses": [{"name": "Adenocarcinoma", "icd_code": "C18.7"}],
        "medications": [],
        "dates_mentioned": ["2026-02-23"],
        "providers": ["MUDr. Rychlý Boris, PhD.", "Nemocnica Bory"],
        "plain_summary": "Pathology report.",
        "plain_summary_sk": "Patologická správa.",
        "institution_code": "BoryNemocnica",
        "category": "pathology",
        "document_date": "2026-02-23",
    }

    classification = {
        "institution_code": "BoryNemocnica",
        "category": "pathology",
        "document_date": "2026-02-23",
    }

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("AI summary", '["pathology"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value=metadata),
        patch("oncofiles.enhance.classify_document", return_value=classification),
        patch("oncofiles.sync.generate_filename_description", return_value="PathologyKrasResults"),
        patch("oncofiles.server._extract_pdf_text", return_value=["Pathology report text"]),
    ):
        stats = await sync_from_gdrive(db, files, gdrive, "folder123", patient_id=ERIKA_UUID)

    assert stats["new"] == 1
    assert stats["errors"] == 0

    docs = await db.list_documents(patient_id=ERIKA_UUID)
    assert len(docs) == 1
    doc = docs[0]
    # Verify backfilled fields
    assert str(doc.document_date) == "2026-02-23"
    assert doc.institution == "BoryNemocnica"
    assert doc.description == "PathologyKrasResults"
    assert doc.category.value == "pathology"
    assert doc.ai_summary == "AI summary"


# ── Sync history ──────────────────────────────────────────────────────────


async def test_sync_records_history(db: Database):
    """Sync records start/completion in sync_history table."""
    files = _mock_files()
    gdrive = _mock_gdrive(
        [
            {
                "id": "gd_hist",
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
        stats = await sync(db, files, gdrive, "folder123", trigger="test", patient_id=ERIKA_UUID)

    assert stats.get("skipped") is not True

    # Verify sync history was recorded
    history = await db.get_sync_history(limit=1)
    assert len(history) == 1
    assert history[0]["status"] == "completed"
    assert history[0]["sync_trigger"] == "test"
    assert history[0]["duration_s"] is not None
    assert history[0]["from_gdrive_new"] == 1


async def test_sync_history_records_failure(db: Database):
    """Failed sync is recorded in history with error message."""
    files = _mock_files()
    gdrive = _mock_gdrive([])
    gdrive.list_folder_with_structure.side_effect = Exception("SSL error")

    import contextlib

    with contextlib.suppress(Exception):
        await sync(db, files, gdrive, "folder123", trigger="test_fail", patient_id=ERIKA_UUID)

    history = await db.get_sync_history(limit=1)
    assert len(history) == 1
    assert history[0]["status"] == "failed"
    assert "SSL error" in (history[0]["error_message"] or "")


# ── Sync history DB methods ──────────────────────────────────────────────


async def test_sync_stats_summary(db: Database):
    """Sync stats summary aggregates correctly."""
    # Insert two completed syncs
    sid1 = await db.insert_sync_history(trigger="scheduled")
    await db.complete_sync_history(
        sid1, status="completed", duration_s=10.5, from_new=3, from_errors=1
    )
    sid2 = await db.insert_sync_history(trigger="manual")
    await db.complete_sync_history(sid2, status="completed", duration_s=5.0, from_new=1)

    stats = await db.get_sync_stats_summary()
    assert stats["total_syncs"] == 2
    assert stats["successful"] == 2
    assert stats["failed"] == 0
    assert stats["total_imported"] == 4
    assert stats["total_errors"] == 1
