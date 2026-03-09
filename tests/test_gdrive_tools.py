"""Tests for Google Drive tool wrappers."""

import json
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.tools.gdrive import (
    gdrive_auth_status,
    gdrive_set_folder,
    gdrive_sync,
    sync_from_gdrive,
    sync_to_gdrive,
)


def _mock_ctx(
    db: Database,
    files: MagicMock | None = None,
    gdrive: MagicMock | None = None,
    folder_id: str = "",
) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "files": files or MagicMock(),
        "gdrive": gdrive,
        "oauth_folder_id": folder_id,
    }
    return ctx


# ── gdrive_auth_status ─────────────────────────────────────────────────────


async def test_gdrive_auth_status_no_token(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await gdrive_auth_status(ctx))
    assert result["connected"] is False
    assert "gdrive_auth_url" in result["message"]


# ── gdrive_set_folder ──────────────────────────────────────────────────────


async def test_gdrive_set_folder_no_token(db: Database):
    ctx = _mock_ctx(db)
    result = json.loads(await gdrive_set_folder(ctx, folder_id="test_folder_123"))
    assert "error" in result
    assert "No OAuth tokens" in result["error"]


# ── gdrive_sync no client ──────────────────────────────────────────────────


@patch("oncofiles.tools.gdrive.GOOGLE_DRIVE_FOLDER_ID", "")
async def test_gdrive_sync_no_client(db: Database):
    ctx = _mock_ctx(db, gdrive=None)
    result = json.loads(await gdrive_sync(ctx))
    assert "error" in result
    assert "not configured" in result["error"]


@patch("oncofiles.tools.gdrive.GOOGLE_DRIVE_FOLDER_ID", "")
async def test_gdrive_sync_no_folder(db: Database):
    mock_gdrive = MagicMock()
    ctx = _mock_ctx(db, gdrive=mock_gdrive, folder_id="")
    result = json.loads(await gdrive_sync(ctx))
    assert "error" in result
    assert "No sync folder" in result["error"]


# ── sync_from_gdrive / sync_to_gdrive error paths ─────────────────────────


@patch("oncofiles.tools.gdrive.GOOGLE_DRIVE_FOLDER_ID", "")
async def test_sync_from_gdrive_no_client(db: Database):
    ctx = _mock_ctx(db, gdrive=None)
    result = json.loads(await sync_from_gdrive(ctx))
    assert "error" in result


@patch("oncofiles.tools.gdrive.GOOGLE_DRIVE_FOLDER_ID", "")
async def test_sync_to_gdrive_no_client(db: Database):
    ctx = _mock_ctx(db, gdrive=None)
    result = json.loads(await sync_to_gdrive(ctx))
    assert "error" in result


@patch("oncofiles.tools.gdrive.GOOGLE_DRIVE_FOLDER_ID", "")
async def test_sync_from_gdrive_no_folder(db: Database):
    mock_gdrive = MagicMock()
    ctx = _mock_ctx(db, gdrive=mock_gdrive, folder_id="")
    result = json.loads(await sync_from_gdrive(ctx))
    assert "error" in result
    assert "No sync folder" in result["error"]


# ── gdrive_sync dry_run success path ──────────────────────────────────────


@patch("oncofiles.tools.gdrive.GOOGLE_DRIVE_FOLDER_ID", "folder123")
async def test_gdrive_sync_dry_run(db: Database):
    mock_gdrive = MagicMock()
    mock_gdrive.list_folder_with_structure.return_value = ([], {})
    mock_files = MagicMock()
    ctx = _mock_ctx(db, files=mock_files, gdrive=mock_gdrive, folder_id="folder123")

    with (
        patch("oncofiles.tools.gdrive._get_sync_folder_id", return_value="folder123"),
        patch("oncofiles.sync.sync_from_gdrive") as mock_from,
        patch("oncofiles.sync.sync_to_gdrive") as mock_to,
    ):
        mock_from.return_value = {"new": 0, "unchanged": 0}
        mock_to.return_value = {"exported": 0, "skipped": 0}
        result = json.loads(await gdrive_sync(ctx, dry_run=True))

    assert "from_gdrive" in result or "skipped" in result or "exported" in str(result)
