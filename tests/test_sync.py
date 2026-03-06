"""Tests for GDrive sync logic (#v0.9)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.sync import sync_from_gdrive, sync_to_gdrive
from tests.helpers import make_doc


def _mock_files():
    """Create a mock FilesClient."""
    files = MagicMock()
    files.upload.return_value = MagicMock(
        id="new_file_id", mime_type="application/pdf", size_bytes=1024
    )
    files.download.return_value = b"fake-pdf-content"
    return files


def _mock_gdrive(gdrive_files: list[dict] | None = None):
    """Create a mock GDriveClient with configurable file list."""
    gdrive = MagicMock()
    gdrive.list_folder.return_value = gdrive_files or []
    gdrive.download.return_value = b"fake-pdf-content"
    gdrive.upload.return_value = {"id": "gdrive_new_id", "modifiedTime": "2026-03-01T00:00:00Z"}
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
            },
        ]
    )

    stats = await sync_from_gdrive(db, files, gdrive, "folder123")
    assert stats["skipped"] == 1
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
            },
        ]
    )
    gdrive.download.side_effect = Exception("network error")

    stats = await sync_from_gdrive(db, files, gdrive, "folder123")
    assert stats["errors"] == 1
    assert stats["new"] == 0


# ── sync_to_gdrive ─────────────────────────────────────────────────────────


async def test_sync_to_gdrive_exports_new(db: Database):
    """Documents without gdrive_id get exported."""
    doc = make_doc(gdrive_id=None)
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["exported"] == 1
    assert stats["skipped"] == 0

    gdrive.upload.assert_called_once()

    # Verify gdrive_id was set
    updated_doc = await db.get_document(doc.id)
    assert updated_doc.gdrive_id == "gdrive_new_id"


async def test_sync_to_gdrive_skips_existing(db: Database):
    """Documents with gdrive_id are skipped."""
    doc = make_doc(gdrive_id="existing_gd_id")
    await db.insert_document(doc)

    files = _mock_files()
    gdrive = _mock_gdrive()

    stats = await sync_to_gdrive(db, files, gdrive, "folder123")
    assert stats["skipped"] == 1
    assert stats["exported"] == 0

    gdrive.upload.assert_not_called()


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
