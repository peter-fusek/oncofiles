"""Tests for Google Drive tool wrappers."""

import json
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.tools.gdrive import (
    gdrive_auth_status,
    gdrive_set_folder,
    gdrive_sync,
    setup_gdrive,
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


# ── setup_gdrive ──────────────────────────────────────────────────────────


async def test_setup_gdrive_no_client(db: Database):
    """setup_gdrive returns error when GDrive client is not configured."""
    ctx = _mock_ctx(db, gdrive=None)
    result = json.loads(await setup_gdrive(ctx, root_folder_id="root123"))
    assert "error" in result
    assert "not configured" in result["error"]


async def test_setup_gdrive_creates_all_folders(db: Database):
    """setup_gdrive creates all 20 folders when none exist."""
    mock_gdrive = MagicMock()
    mock_gdrive.find_folder.return_value = None  # Nothing exists
    mock_gdrive.create_folder.return_value = "new_folder_id"
    ctx = _mock_ctx(db, gdrive=mock_gdrive)

    result = json.loads(await setup_gdrive(ctx, root_folder_id="root123"))

    assert result["status"] == "ok"
    assert result["root_folder_id"] == "root123"
    assert result["total_folders"] == 17  # 14 active categories + 3 metadata
    assert len(result["created"]) == 17
    assert len(result["skipped"]) == 0
    assert len(result["renamed"]) == 0
    assert mock_gdrive.create_folder.call_count == 17


async def test_setup_gdrive_skips_existing(db: Database):
    """setup_gdrive skips folders that already exist with bilingual names."""
    mock_gdrive = MagicMock()
    # All folders already exist (bilingual name found)
    mock_gdrive.find_folder.return_value = "existing_id"
    ctx = _mock_ctx(db, gdrive=mock_gdrive)

    result = json.loads(await setup_gdrive(ctx, root_folder_id="root123"))

    assert result["status"] == "ok"
    assert len(result["created"]) == 0
    assert len(result["skipped"]) == 17
    assert mock_gdrive.create_folder.call_count == 0


async def test_setup_gdrive_renames_old_folders(db: Database):
    """setup_gdrive renames EN-only folders to bilingual format."""
    mock_gdrive = MagicMock()

    def find_folder_side_effect(name, parent_id):
        # Bilingual names not found, but EN-only names found
        if " — " in name:
            return None
        return "old_folder_id"

    mock_gdrive.find_folder.side_effect = find_folder_side_effect
    ctx = _mock_ctx(db, gdrive=mock_gdrive)

    result = json.loads(await setup_gdrive(ctx, root_folder_id="root123"))

    assert result["status"] == "ok"
    assert len(result["renamed"]) == 17
    assert len(result["created"]) == 0
    assert mock_gdrive.rename_file.call_count == 17


async def test_setup_gdrive_idempotent_mixed(db: Database):
    """setup_gdrive handles mix of existing, old, and missing folders."""
    mock_gdrive = MagicMock()

    call_count = {"n": 0}

    def find_folder_side_effect(name, parent_id):
        call_count["n"] += 1
        # First call per folder is bilingual check, second is EN-only check
        # Simulate: first folder exists (bilingual), rest need creation
        if call_count["n"] == 1:
            return "existing_bilingual_id"  # First folder bilingual found
        return None  # All others not found

    mock_gdrive.find_folder.side_effect = find_folder_side_effect
    mock_gdrive.create_folder.return_value = "new_id"
    ctx = _mock_ctx(db, gdrive=mock_gdrive)

    result = json.loads(await setup_gdrive(ctx, root_folder_id="root123"))

    assert result["status"] == "ok"
    assert len(result["skipped"]) == 1  # First folder was found
    assert len(result["created"]) == 16  # Rest were created
    assert "summary" in result
