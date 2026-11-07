"""Lock tests for the curated-folder legacy write guard (#562)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import curated_folder
from oncofiles.curated_folder import (
    DriveWriteBlockedError,
    is_curated_folder,
    mark_if_curated,
    write_block_reason,
)
from oncofiles.gdrive_client import GDriveClient, _is_connection_error

WRITE_CALLS = {
    "grant_access": ("f", "a@b.c"),
    "upload": ("x.pdf", b"x"),
    "update": ("f", b"x", "application/pdf"),
    "create_folder": ("labs", "root"),
    "set_app_properties": ("f", {"k": "v"}),
    "create_shortcut": ("f", "n", "p"),
    "copy_file": ("f", "n", "p"),
    "rename_file": ("f", "new.pdf"),
    "trash_file": ("f",),
    "move_file": ("f", "p"),
    "batch_rename": ({"f": "n"},),
    "batch_move": ({"f": ("p", "old")},),
}


@pytest.fixture(autouse=True)
def _clear_cache():
    curated_folder.clear_cache()
    yield
    curated_folder.clear_cache()


def _client(blocked: bool = True) -> GDriveClient:
    c = object.__new__(GDriveClient)
    c._service = MagicMock()
    c._creds = MagicMock()
    c.owner_email = None
    if blocked:
        c.write_block_reason = "curated (test)"
    return c


# ── detection over a folder tree ─────────────────────────────────────
#
#   root ─ Health ─┬─ Curated   (README.md + _index.json)
#                  │     └─ labs ─ deep
#                  └─ Plain     (README.md only)
#   root ─ Other

TREE = {  # id -> parent
    "Health": "rootid",
    "Curated": "Health",
    "labs": "Curated",
    "deep": "labs",
    "Plain": "Health",
    "Other": "rootid",
}
MARKERS = {"Curated": {"README.md", "_index.json"}, "Plain": {"README.md"}}


@pytest.fixture
def tree(monkeypatch):
    calls = {"n": 0}

    def marker_names(_g, fid):
        calls["n"] += 1
        return MARKERS.get(fid, set())

    def parents(_g, fid):
        return [TREE[fid]] if fid in TREE else []

    def index_parents(_g):
        return {f for f, m in MARKERS.items() if "_index.json" in m}

    monkeypatch.setattr(curated_folder, "_marker_names", marker_names)
    monkeypatch.setattr(curated_folder, "_parents", parents)
    monkeypatch.setattr(curated_folder, "_index_file_parents", index_parents)
    return calls


@pytest.mark.parametrize(
    "folder,expected",
    [
        ("Curated", "is a curated folder"),
        ("labs", "inside curated folder Curated"),
        ("deep", "inside curated folder Curated"),
        ("Health", "contains curated folder Curated"),
        ("root", "contains curated folder Curated"),
    ],
)
def test_overlapping_folders_are_curated(tree, folder, expected):
    reason = curated_folder.curated_reason(MagicMock(), folder)
    assert reason is not None and expected in reason


@pytest.mark.parametrize("folder", ["Plain", "Other"])
def test_unrelated_folders_are_free(tree, folder):
    assert is_curated_folder(MagicMock(), folder) is False


def test_detection_is_cached(tree):
    is_curated_folder(MagicMock(), "Other")
    n = tree["n"]
    is_curated_folder(MagicMock(), "Other")
    assert tree["n"] == n


def test_detection_fails_closed_and_is_not_cached(monkeypatch):
    def boom(_g, _f):
        raise RuntimeError("drive down")

    monkeypatch.setattr(curated_folder, "_marker_names", boom)
    assert is_curated_folder(MagicMock(), "X") is True
    monkeypatch.setattr(curated_folder, "_marker_names", lambda _g, _f: set())
    monkeypatch.setattr(curated_folder, "_parents", lambda _g, _f: [])
    monkeypatch.setattr(curated_folder, "_index_file_parents", lambda _g: set())
    assert is_curated_folder(MagicMock(), "X") is False


def test_empty_folder_id_is_not_curated():
    assert is_curated_folder(MagicMock(), "") is False


def test_mark_if_curated_sets_reason(tree):
    g = _client(blocked=False)
    assert mark_if_curated(g, "labs") is True
    assert write_block_reason(g) and "#562" in g.write_block_reason


def test_mark_if_curated_never_unblocks(tree):
    g = _client()
    assert mark_if_curated(g, "Other") is False
    assert write_block_reason(g) == "curated (test)"


def test_write_block_reason_ignores_mocks():
    assert write_block_reason(MagicMock()) is None
    assert write_block_reason(None) is None


# ── client guard ─────────────────────────────────────────────────────


@pytest.mark.parametrize("method", sorted(WRITE_CALLS))
def test_every_write_method_refuses_when_blocked(method):
    c = _client()
    with pytest.raises(DriveWriteBlockedError):
        getattr(c, method)(*WRITE_CALLS[method])
    c._service.files.assert_not_called()
    c._service.permissions.assert_not_called()


def test_unblocked_client_writes_normally():
    c = _client(blocked=False)
    c._service.files.return_value.update.return_value.execute.return_value = {}
    c.rename_file("f", "new.pdf")
    c._service.files.return_value.update.assert_called_once()


def test_contract_writes_lifts_block_only_inside_scope():
    c = _client()
    c._service.files.return_value.update.return_value.execute.return_value = {}
    with c.contract_writes():
        c.rename_file("f", "new.pdf")
    with pytest.raises(DriveWriteBlockedError):
        c.rename_file("f", "again.pdf")


def test_reads_are_not_blocked():
    c = _client()
    c._service.files.return_value.get.return_value.execute.return_value = {"id": "f"}
    assert c.get_file_metadata("f")["id"] == "f"


def test_refusal_is_never_retried():
    assert _is_connection_error(DriveWriteBlockedError("x")) is False


# ── legacy entry points skip early ───────────────────────────────────


@pytest.mark.parametrize("fn", ["sync", "sync_to_gdrive", "sync_from_gdrive"])
async def test_legacy_sync_skips_curated(fn):
    from oncofiles import sync as sync_mod

    db = MagicMock()
    db.reconnect_if_stale = AsyncMock()
    result = await getattr(sync_mod, fn)(db, MagicMock(), _client(), "root1", patient_id="p1")
    assert result["skipped"] is True and result["curated"] is True
    db.reconnect_if_stale.assert_not_called()


def test_ensure_folder_structure_creates_nothing_in_curated():
    from oncofiles.gdrive_folders import ensure_folder_structure

    c = _client()
    assert ensure_folder_structure(c, "root1") == {}
    c._service.files.assert_not_called()


# ── #563 link parsing + dashboard endpoints ──────────────────────────

FID = "1FakeFolderIdForTests0000"


@pytest.mark.parametrize(
    "value",
    [
        FID,
        f"https://drive.google.com/drive/folders/{FID}",
        f"https://drive.google.com/drive/u/0/folders/{FID}?usp=sharing",
        f"https://drive.google.com/open?id={FID}",
    ],
)
def test_parse_drive_folder_ref_accepts_links_and_ids(value):
    from oncofiles.curated_folder import parse_drive_folder_ref

    assert parse_drive_folder_ref(value) == FID


@pytest.mark.parametrize("value", ["", "short", "x' or name='y", "https://example.com/a b"])
def test_parse_drive_folder_ref_rejects_garbage(value):
    from oncofiles.curated_folder import parse_drive_folder_ref

    assert parse_drive_folder_ref(value) is None


def _set_folder_request(body: dict, db) -> MagicMock:
    req = MagicMock()
    req.json = AsyncMock(return_value=body)
    req.app.state.fastmcp_server._lifespan_result = {"db": db}
    return req


def _set_folder_db() -> MagicMock:
    db = MagicMock()
    db.resolve_patient_id = AsyncMock(return_value="pid-1")
    token = MagicMock(token_expiry=None, access_token="a", refresh_token="r")
    db.get_oauth_token = AsyncMock(return_value=token)
    db.update_oauth_folder = AsyncMock()
    db.update_oauth_owner_email = AsyncMock()
    return db


async def _call_set_folder(body: dict, drive_names: list[str]):
    from unittest.mock import patch

    from oncofiles import server

    db = _set_folder_db()
    client = _client(blocked=False)
    files = client._service.files.return_value
    files.get.return_value.execute.return_value = {
        "id": FID,
        "name": "Erika",
        "mimeType": "application/vnd.google-apps.folder",
    }
    files.list.return_value.execute.return_value = {"files": [{"name": n} for n in drive_names]}
    client.get_folder_owner = MagicMock(return_value="owner@example.com")
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(server, "_require_body_patient_access", AsyncMock(return_value=None)),
        patch("oncofiles.oauth.is_token_expired", return_value=False),
        patch.object(server.GDriveClient, "from_oauth", return_value=client),
    ):
        resp = await server.api_gdrive_set_folder(_set_folder_request(body, db))
    return resp, client, db


async def test_set_folder_on_curated_creates_nothing():
    import json as _json

    resp, client, db = await _call_set_folder(
        {"patient_id": "q1b", "folder_id": f"https://drive.google.com/drive/folders/{FID}"},
        ["README.md", "_index.json"],
    )
    data = _json.loads(resp.body)
    assert resp.status_code == 200 and data["curated"] is True
    assert data["subfolders_created"] == 0
    client._service.files.return_value.create.assert_not_called()
    db.update_oauth_folder.assert_awaited_once()
    assert db.update_oauth_folder.await_args.args[2] == FID  # link parsed to the id


async def test_set_folder_rejects_bad_folder_ref():
    resp, client, db = await _call_set_folder({"patient_id": "q1b", "folder_id": "not a link"}, [])
    assert resp.status_code == 400
    db.update_oauth_folder.assert_not_called()


async def test_cannot_create_new_folder_inside_curated_parent():
    resp, client, db = await _call_set_folder(
        {"patient_id": "q1b", "folder_name": "New", "parent_id": FID},
        ["README.md", "_index.json"],
    )
    assert resp.status_code == 400
    client._service.files.return_value.create.assert_not_called()


# ── server wiring (#562 review findings) ─────────────────────────────


async def test_mark_patient_curated_records_patient(tree):
    from oncofiles import server

    server._curated_patients.discard("pid-x")
    g = _client(blocked=False)
    assert await server._mark_patient_curated("pid-x", g, "deep") is True
    assert "pid-x" in server._curated_patients
    server._curated_patients.discard("pid-x")


async def test_mark_patient_curated_leaves_free_patients_alone(tree):
    from oncofiles import server

    g = _client(blocked=False)
    assert await server._mark_patient_curated("pid-y", g, "Other") is False
    assert "pid-y" not in server._curated_patients
    assert write_block_reason(g) is None


def test_scheduler_paths_consult_curated_registry():
    """Watchdog + nightly + weekly pipelines skip curated patients; the primary
    patient shortcut and the client cache hit re-check (review findings 1, 3, 4)."""
    import inspect

    from oncofiles import server

    src = inspect.getsource(server)
    assert src.count("if pid in _curated_patients:") == 2  # nightly + weekly
    assert "if p.patient_id in _curated_patients:" in src  # sync_watchdog
    shortcut = src[src.index("if pid == primary_uuid and gdrive and oauth_folder_id:") :][:400]
    assert "_mark_patient_curated" in shortcut
    cache_hit = src[src.index("if cached and (_time.monotonic() - cached[4])") :][:400]
    assert "_mark_patient_curated" in cache_hit
