"""Tests for GDrive sync logic (#v1.0)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.sync import sync, sync_from_gdrive, sync_to_gdrive
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
    gdrive.update.return_value = {"id": "updated_id", "modifiedTime": "2026-03-01T00:00:00Z"}
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

    with patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')):
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

    with patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')):
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
    folder_map = {"folder_labs": "labs"}
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

    with patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')):
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


async def test_sync_to_gdrive_skips_existing(db: Database):
    """Documents with gdrive_id are skipped."""
    doc = make_doc(gdrive_id="existing_gd_id")
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["skipped"] == 1
    assert stats["exported"] == 0


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
