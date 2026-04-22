"""Tests for repair_gdrive_folder_hygiene (#457)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.models import OAuthToken
from oncofiles.tools.hygiene import repair_gdrive_folder_hygiene
from tests.helpers import ERIKA_UUID


def _make_gdrive_with_tree(folders: dict[str, dict], children: dict[str, list[dict]]):
    """Build a mock GDrive client that answers list queries from a fixture tree.

    folders: {folder_id: {"name": str, "parents": [parent_id, ...]}}
    children: {folder_id: [{id, name, mimeType}, ...]}  — returned for subtree walk
    """
    gdrive = MagicMock()

    def _list(*, q: str, fields: str, pageSize: int, pageToken=None):  # noqa: N803
        # Parse the parent_id out of "'X' in parents ..." — the helper calls
        # with a single-quoted parent_id as the first token.
        parent_id = q.split("'", 2)[1]
        if "application/vnd.google-apps.folder" in q:
            # Folder-only query — used by the BFS walk in the repair tool
            result_files = [
                {
                    "id": fid,
                    "name": info["name"],
                    "mimeType": "application/vnd.google-apps.folder",
                    "parents": info.get("parents", []),
                }
                for fid, info in folders.items()
                if parent_id in info.get("parents", [])
            ]
        else:
            # Full children query — used by _list_children_all
            result_files = children.get(parent_id, [])
        resp = MagicMock()
        resp.execute.return_value = {
            "files": result_files,
            "nextPageToken": None,
        }
        return resp

    list_mock = MagicMock(side_effect=_list)
    gdrive._service.files.return_value.list = list_mock
    gdrive.move_file = MagicMock()
    gdrive.trash_file = MagicMock()
    return gdrive


def _mock_ctx(db: Database, gdrive) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "files": MagicMock(),
        "gdrive": gdrive,
        "oauth_folder_id": "",
    }
    return ctx


async def _seed_oauth(db: Database, folder_id: str = "root"):
    await db.upsert_oauth_token(
        OAuthToken(
            patient_id=ERIKA_UUID,
            provider="google",
            access_token="a",
            refresh_token="r",
            gdrive_folder_id=folder_id,
            granted_scopes="[]",
        )
    )


# ── nested YYYY-MM: dry-run ────────────────────────────────────────────────


async def test_repair_nested_ym_dry_run_reports_plan(db: Database):
    """dry_run=True must never touch GDrive — only plan the move+trash."""
    await _seed_oauth(db)

    # Tree:
    #   root
    #   └── labs  (outer_cat)
    #       └── 2026-02  (outer_ym) — CORRECT position
    #           └── 2026-02  (inner_ym) — DUPLICATE, must be flattened
    #               ├── file_a.pdf
    #               └── file_b.pdf
    folders = {
        "labs_cat": {"name": "labs", "parents": ["root"]},
        "outer_ym": {"name": "2026-02", "parents": ["labs_cat"]},
        "inner_ym": {"name": "2026-02", "parents": ["outer_ym"]},
    }
    children = {
        "inner_ym": [
            {"id": "file_a", "name": "a.pdf", "mimeType": "application/pdf"},
            {"id": "file_b", "name": "b.pdf", "mimeType": "application/pdf"},
        ],
    }
    gdrive = _make_gdrive_with_tree(folders, children)
    ctx = _mock_ctx(db, gdrive)

    with patch("oncofiles.tools.hygiene._get_gdrive", return_value=gdrive):
        raw = await repair_gdrive_folder_hygiene(ctx, dry_run=True)

    result = json.loads(raw)
    assert result["dry_run"] is True
    assert len(result["nested_fixed"]) == 1
    entry = result["nested_fixed"][0]
    assert entry["inner_folder_id"] == "inner_ym"
    assert entry["outer_parent_id"] == "outer_ym"
    assert entry["children_to_move"] == 2
    assert entry["moved"] == 0
    assert entry["trashed_inner"] is False
    gdrive.move_file.assert_not_called()
    gdrive.trash_file.assert_not_called()


# ── nested YYYY-MM: live run ───────────────────────────────────────────────


async def test_repair_nested_ym_live_moves_and_trashes(db: Database):
    """dry_run=False moves children then trashes the inner folder."""
    await _seed_oauth(db)

    folders = {
        "labs_cat": {"name": "labs", "parents": ["root"]},
        "outer_ym": {"name": "2026-02", "parents": ["labs_cat"]},
        "inner_ym": {"name": "2026-02", "parents": ["outer_ym"]},
    }
    children = {
        "inner_ym": [
            {"id": "file_a", "name": "a.pdf", "mimeType": "application/pdf"},
            {"id": "file_b", "name": "b.pdf", "mimeType": "application/pdf"},
        ],
    }
    gdrive = _make_gdrive_with_tree(folders, children)
    ctx = _mock_ctx(db, gdrive)

    with patch("oncofiles.tools.hygiene._get_gdrive", return_value=gdrive):
        raw = await repair_gdrive_folder_hygiene(ctx, dry_run=False)

    result = json.loads(raw)
    assert result["dry_run"] is False
    entry = result["nested_fixed"][0]
    assert entry["moved"] == 2
    assert entry["trashed_inner"] is True
    # Both children moved into the OUTER yyyy-mm, not some other target
    move_targets = {c.args[1] for c in gdrive.move_file.call_args_list}
    assert move_targets == {"outer_ym"}
    gdrive.trash_file.assert_called_once_with("inner_ym")


# ── idempotency: second run finds nothing to fix ───────────────────────────


async def test_repair_nested_ym_idempotent(db: Database):
    """After one successful repair, a second dry-run returns an empty plan."""
    await _seed_oauth(db)

    # Tree after a prior repair — the inner YYYY-MM is gone.
    folders = {
        "labs_cat": {"name": "labs", "parents": ["root"]},
        "outer_ym": {"name": "2026-02", "parents": ["labs_cat"]},
    }
    children: dict[str, list[dict]] = {}
    gdrive = _make_gdrive_with_tree(folders, children)
    ctx = _mock_ctx(db, gdrive)

    with patch("oncofiles.tools.hygiene._get_gdrive", return_value=gdrive):
        raw = await repair_gdrive_folder_hygiene(ctx, dry_run=True)

    result = json.loads(raw)
    assert result["nested_fixed"] == []
    assert result["errors"] == []


# ── no GDrive folder configured ────────────────────────────────────────────


async def test_repair_errors_when_no_gdrive_folder(db: Database):
    """If the patient has no gdrive_folder_id, tool returns an error cleanly."""
    # Seed oauth but with no folder_id
    await db.upsert_oauth_token(
        OAuthToken(
            patient_id=ERIKA_UUID,
            provider="google",
            access_token="a",
            refresh_token="r",
            gdrive_folder_id=None,
            granted_scopes="[]",
        )
    )
    ctx = _mock_ctx(db, MagicMock())
    raw = await repair_gdrive_folder_hygiene(ctx, dry_run=True)
    result = json.loads(raw)
    assert "error" in result
    assert "gdrive_set_folder" in result["error"]
