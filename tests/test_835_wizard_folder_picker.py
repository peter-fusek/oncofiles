"""#835: New Patient wizard folder step — subfolder list, container guard, curated step 3.

All folder names and Drive ids are invented.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import curated_folder, server
from oncofiles.gdrive_client import GDriveClient

SRC = Path(server.__file__).parent
FOLDER = "application/vnd.google-apps.folder"

# Invented tree:
#   My Drive ─ Rodina ─┬─ PacientAlfa   (README.md + _index.json, has 2 subfolders)
#                      ├─ PacientBeta   (README.md only)
#                      └─ Recepty
#   My Drive ─ Volny ─── Podpriecinok
#   My Drive ─ Prazdny
#   My Drive ─ poznamky.txt (a file)
RODINA = "idRodina0000000001"
ALFA = "idPacientAlfa00001"
BETA = "idPacientBeta00001"
RECEPTY = "idRecepty000000001"
VOLNY = "idVolny00000000001"
PODP = "idPodpriecinok0001"
PRAZDNY = "idPrazdny000000001"
SUBOR = "idPoznamkyTxt00001"


def _tree() -> dict[str, dict]:
    def folder(name, parent):
        return {"name": name, "parent": parent, "mimeType": FOLDER}

    def file(name, parent):
        return {"name": name, "parent": parent, "mimeType": "text/plain"}

    return {
        RODINA: folder("Rodina", "root"),
        ALFA: folder("PacientAlfa", RODINA),
        BETA: folder("PacientBeta", RODINA),
        RECEPTY: folder("Recepty", RODINA),
        "idAlfaInbox0000001": folder("00_inbox", ALFA),
        "idAlfaLabs00000001": folder("01_labs", ALFA),
        "idAlfaReadme000001": file("README.md", ALFA),
        "idAlfaIndex0000001": file("_index.json", ALFA),
        "idBetaReadme000001": file("README.md", BETA),
        VOLNY: folder("Volny", "root"),
        PODP: folder("Podpriecinok", VOLNY),
        PRAZDNY: folder("Prazdny", "root"),
        SUBOR: file("poznamky.txt", "root"),
    }


class FakeFiles:
    """Answers the Drive queries the picker code sends, from an in-memory tree."""

    def __init__(self, tree: dict[str, dict]):
        self.tree = tree
        self.list_queries: list[str] = []
        self.created: list[dict] = []
        self.fail_list_matching: str | None = None

    def list(self, q: str = "", **_kw):
        self.list_queries.append(q)
        if self.fail_list_matching and self.fail_list_matching in q:
            raise RuntimeError("drive listing failed (test)")
        parents = re.findall(r"'([^']+)' in parents", q)
        names = re.findall(r"name = '([^']+)'", q)
        only_folders = "mimeType = 'application/vnd.google-apps.folder'" in q
        out = []
        for fid, node in self.tree.items():
            if parents and node["parent"] not in parents:
                continue
            if names and node["name"] not in names:
                continue
            if only_folders and node["mimeType"] != FOLDER:
                continue
            out.append({"id": fid, "name": node["name"], "parents": [node["parent"]]})
        out.sort(key=lambda f: f["name"])
        return MagicMock(execute=MagicMock(return_value={"files": out}))

    def get(self, fileId: str = "", **_kw):  # noqa: N803 - Drive API keyword
        node = self.tree[fileId]
        meta = {"id": fileId, "name": node["name"], "mimeType": node["mimeType"]}
        if node["parent"] != "root":
            meta["parents"] = [node["parent"]]
        return MagicMock(execute=MagicMock(return_value=meta))

    def create(self, body=None, **_kw):
        self.created.append(body)
        return MagicMock(execute=MagicMock(return_value={"id": "idCreated000000001"}))


def _client(tree: dict[str, dict] | None = None) -> tuple[GDriveClient, FakeFiles]:
    files = FakeFiles(tree or _tree())
    c = object.__new__(GDriveClient)
    c._service = MagicMock()
    c._service.files.return_value = files
    c._creds = MagicMock()
    c.owner_email = None
    c.get_folder_owner = MagicMock(return_value="owner@example.com")
    c.create_folder = MagicMock(return_value="idCreated000000001")
    c.find_folder = MagicMock(return_value=None)
    return c, files


def _db() -> MagicMock:
    db = MagicMock()
    db.resolve_patient_id = AsyncMock(return_value="pid-835")
    token = MagicMock(token_expiry=None, access_token="a", refresh_token="r", gdrive_folder_id=None)
    db.get_oauth_token = AsyncMock(return_value=token)
    db.update_oauth_folder = AsyncMock()
    db.update_oauth_owner_email = AsyncMock()
    return db


@pytest.fixture(autouse=True)
def _clear_cache():
    curated_folder.clear_cache()
    yield
    curated_folder.clear_cache()


async def _folders(parent: str | None, client: GDriveClient):
    db = _db()
    req = MagicMock()
    req.query_params = {"parent": parent} if parent else {}
    req.app.state.fastmcp_server._lifespan_result = {"db": db}
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(server, "_get_dashboard_patient_id", AsyncMock(return_value="pid-835")),
        patch("oncofiles.oauth.is_token_expired", return_value=False),
        patch.object(server.GDriveClient, "from_oauth", return_value=client),
    ):
        resp = await server.api_gdrive_folders(req)
    return resp.status_code, json.loads(resp.body)


async def _set_folder(body: dict, client: GDriveClient):
    db = _db()
    req = MagicMock()
    req.json = AsyncMock(return_value={"patient_id": "pacient-835", **body})
    req.app.state.fastmcp_server._lifespan_result = {"db": db}
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(server, "_require_body_patient_access", AsyncMock(return_value=None)),
        patch("oncofiles.oauth.is_token_expired", return_value=False),
        patch.object(server.GDriveClient, "from_oauth", return_value=client),
    ):
        resp = await server.api_gdrive_set_folder(req)
    return resp.status_code, json.loads(resp.body), db


# ── /api/gdrive-folders: what is the folder being browsed? ───────────────


async def test_folders_reports_container_of_curated_folders():
    client, files = _client()
    status, data = await _folders(RODINA, client)
    assert status == 200
    assert [f["name"] for f in data["folders"]] == ["PacientAlfa", "PacientBeta", "Recepty"]
    assert data["current"] == {
        "curated": False,
        "subfolder_count": 3,
        "curated_children": 1,  # PacientBeta has only a README: not curated
        "container_reason": "curated_children",
    }
    # Cost: child folders + own markers + one query for the children's markers.
    assert len(files.list_queries) == 3


async def test_folders_reports_curated_folder_without_looking_at_children():
    client, files = _client()
    status, data = await _folders(ALFA, client)
    assert status == 200
    assert data["current"]["curated"] is True
    assert data["current"]["subfolder_count"] == 2
    assert data["current"]["container_reason"] is None
    assert len(files.list_queries) == 2  # child folders + own markers


async def test_folders_reports_plain_folder_and_root():
    client, _ = _client()
    _, plain = await _folders(VOLNY, client)
    assert plain["current"] == {
        "curated": False,
        "subfolder_count": 1,
        "curated_children": 0,
        "container_reason": None,
    }
    _, root = await _folders(None, client)
    assert root["parent_id"] == "root"
    assert root["current"]["curated"] is False
    assert root["current"]["subfolder_count"] == 3


async def test_folders_overview_failure_asks_instead_of_guessing():
    client, files = _client()
    files.fail_list_matching = "README.md"
    status, data = await _folders(RODINA, client)
    assert status == 200  # browsing still works
    assert data["current"]["curated"] is False
    assert data["current"]["container_reason"] == "check_failed"


# ── folder_overview ──────────────────────────────────────────────────────


def _wide_tree(n: int, curated_at: int | None = None) -> dict[str, dict]:
    tree = {"idSiroky000000001": {"name": "Siroky", "parent": "root", "mimeType": FOLDER}}
    for i in range(n):
        cid = f"idDieta{i:011d}"
        tree[cid] = {"name": f"Dieta{i:03d}", "parent": "idSiroky000000001", "mimeType": FOLDER}
        if i == curated_at:
            for marker in curated_folder.CURATED_MARKERS:
                tree[f"id{marker[:5].strip('_.')}{i:011d}"] = {
                    "name": marker,
                    "parent": cid,
                    "mimeType": "text/plain",
                }
    return tree


def test_overview_checks_only_first_50_children_in_two_queries():
    client, files = _client(_wide_tree(60, curated_at=55))  # curated one is past the bound
    out = curated_folder.folder_overview(client, "idSiroky000000001")
    assert out["subfolder_count"] == 60
    assert out["curated_children"] == 0
    assert out["container_reason"] == "too_many_subfolders"
    child_marker_queries = [q for q in files.list_queries if q.count(" in parents") > 1]
    assert len(child_marker_queries) == 2
    assert sum(q.count(" in parents") for q in child_marker_queries) == 50


def test_overview_finds_curated_child_in_second_chunk():
    client, _ = _client(_wide_tree(40, curated_at=30))
    out = curated_folder.folder_overview(client, "idSiroky000000001")
    assert out["curated_children"] == 1
    assert out["container_reason"] == "curated_children"


def test_overview_refuses_ids_outside_the_drive_alphabet():
    client, _ = _client()
    out = curated_folder.folder_overview(
        client, VOLNY, child_folders=[{"id": "x' or name = 'y", "name": "Zly"}]
    )
    assert out["container_reason"] == "check_failed"


# ── /api/gdrive-set-folder: the container guard ──────────────────────────


async def test_set_folder_refuses_parent_of_curated_folders():
    client, files = _client()
    status, data, db = await _set_folder({"folder_id": RODINA}, client)
    assert status == 409
    assert data["code"] == "container_folder"
    assert data["reason"] == "curated_children"
    assert data["curated_children"] == 1 and data["subfolder_count"] == 3
    assert data["confirmable"] is False and data["confirm_field"] is None
    db.update_oauth_folder.assert_not_called()
    db.update_oauth_owner_email.assert_not_called()
    client.create_folder.assert_not_called()
    assert len(files.list_queries) == 3  # own markers, child folders, children's markers


@pytest.mark.parametrize("flag", ["true", 1, "yes", False, None])
async def test_set_folder_confirmation_must_be_json_true(flag):
    client, _ = _client(_wide_tree(60))
    status, data, db = await _set_folder(
        {"folder_id": "idSiroky000000001", "confirm_container": flag}, client
    )
    assert status == 409 and data["code"] == "container_folder"
    db.update_oauth_folder.assert_not_called()


async def test_set_folder_refuses_parent_of_curated_folders_even_when_confirmed():
    """Isolation, not UX: the flag cannot connect a parent of other patients' folders."""
    client, files = _client()
    status, data, db = await _set_folder({"folder_id": RODINA, "confirm_container": True}, client)
    assert status == 409
    assert data["code"] == "container_folder" and data["reason"] == "curated_children"
    assert data["confirmable"] is False and data["confirm_field"] is None
    db.update_oauth_folder.assert_not_called()
    db.update_oauth_owner_email.assert_not_called()
    client.create_folder.assert_not_called()
    assert files.created == []


async def test_set_folder_confirmation_lifts_failed_check():
    client, files = _client()
    files.fail_list_matching = "README.md"
    status, data, db = await _set_folder({"folder_id": VOLNY, "confirm_container": True}, client)
    assert status == 200
    db.update_oauth_folder.assert_awaited_once()
    # Curated detection fails closed too: connected, but nothing is created in it.
    assert data["curated"] is True and data["curated_root"] is False
    client.create_folder.assert_not_called()


async def test_set_folder_confirmation_lifts_unchecked_many_subfolders():
    client, _ = _client(_wide_tree(60))
    status, data, db = await _set_folder(
        {"folder_id": "idSiroky000000001", "confirm_container": True}, client
    )
    assert status == 200
    db.update_oauth_folder.assert_awaited_once()


async def test_set_folder_accepts_curated_folder_directly():
    client, _ = _client()
    status, data, db = await _set_folder({"folder_id": ALFA}, client)
    assert status == 200
    assert data["curated"] is True and data["curated_root"] is True
    assert data["subfolders_created"] == 0
    db.update_oauth_folder.assert_awaited_once()
    client.create_folder.assert_not_called()


async def test_set_folder_accepts_plain_folder_with_ordinary_subfolders():
    """The server only blocks parents of curated folders; the wizard asks about the rest."""
    client, _ = _client()
    status, data, db = await _set_folder({"folder_id": VOLNY}, client)
    assert status == 200
    assert "curated" not in data
    assert data["subfolders_created"] > 0
    db.update_oauth_folder.assert_awaited_once()


async def test_set_folder_requires_confirmation_when_check_fails():
    client, files = _client()
    files.fail_list_matching = "README.md"
    status, data, db = await _set_folder({"folder_id": VOLNY}, client)
    assert status == 409
    assert data["code"] == "container_folder" and data["reason"] == "check_failed"
    db.update_oauth_folder.assert_not_called()


async def test_set_folder_requires_confirmation_for_unchecked_many_subfolders():
    client, _ = _client(_wide_tree(60))
    status, data, db = await _set_folder({"folder_id": "idSiroky000000001"}, client)
    assert status == 409 and data["reason"] == "too_many_subfolders"
    assert data["confirmable"] is True and data["confirm_field"] == "confirm_container"
    db.update_oauth_folder.assert_not_called()


async def test_set_folder_still_rejects_a_file_id():
    client, _ = _client()
    for body in ({"folder_id": SUBOR}, {"folder_id": SUBOR, "confirm_container": True}):
        status, data, db = await _set_folder(body, client)
        assert status == 400
        assert "not a folder" in data["error"]
        db.update_oauth_folder.assert_not_called()


async def test_set_folder_new_folder_needs_no_confirmation():
    client, _ = _client()
    tree = client._service.files.return_value.tree
    tree["idCreated000000001"] = {"name": "Novy", "parent": PRAZDNY, "mimeType": FOLDER}
    status, data, db = await _set_folder({"folder_name": "Novy", "parent_id": PRAZDNY}, client)
    assert status == 200
    client.create_folder.assert_any_call("Novy", PRAZDNY)
    db.update_oauth_folder.assert_awaited_once()


# ── dashboard markup and script (static) ─────────────────────────────────


def _html() -> str:
    return (SRC / "dashboard.html").read_text(encoding="utf-8")


def _js() -> str:
    return (SRC / "dashboard.js").read_text(encoding="utf-8")


def _element(html: str, element_id: str) -> str:
    m = re.search(rf'<[a-z0-9]+ [^>]*\bid="{re.escape(element_id)}"[^>]*>', html)
    assert m, f"#{element_id} missing"
    return m.group(0)


def _between(text: str, start: str, end: str) -> str:
    begin = text.index(start)
    return text[begin : text.index(end, begin)]


def _folder_js() -> str:
    js = _js()
    return js[
        js.index("// #563 / #835: folder browser") : js.index("async function wizardStep3Sync")
    ]


def test_select_with_current_folder_option_is_gone():
    html, js = _html(), _js()
    assert "wiz-folder-select" not in html and "wiz-folder-select" not in js
    assert "wiz-folder-open" not in html  # the button that looked dead
    assert "wizardFolderChanged" not in js
    assert "\\u2713 " not in js and "hereOpt" not in js
    assert 'id="wiz-folder-list"' in html
    # Rows are built with createElement + textContent and go through the dispatcher.
    section = _folder_js()
    assert "innerHTML" not in section
    assert "row.dataset.action = 'wizardFolderOpen|@this'" in section
    assert "name.textContent = f.name" in section


@pytest.mark.parametrize(
    "element_id",
    [
        "wiz-folder-list-label",
        "wiz-folder-empty",
        "wiz-folder-new-toggle",
        "wiz-folder-curated-note",
        "wiz-folder-target-label",
        "wiz-folder-btn",
        "wiz-folder-confirm-go",
        "wiz-step3-curated-root",
        "wiz-step3-curated-overlap",
        "wiz-curated-continue",
    ],
)
def test_new_wizard_strings_are_bilingual(element_id):
    tag = _element(_html(), element_id)
    assert re.search(r'data-sk="[^"]+"', tag) and re.search(r'data-en="[^"]+"', tag), tag


def test_wizard_states_which_folder_gets_connected():
    html, section = _html(), _folder_js()
    assert 'data-en="Connect this folder:"' in html
    assert 'id="wiz-folder-target-path"' in html
    assert "not the one chosen above" in html  # a pasted link wins, and says so
    assert "'Connect this folder:'" in section
    assert "My Drive itself cannot be connected" in section


def test_container_confirmation_is_an_in_wizard_step():
    html, section = _html(), _folder_js()
    assert 'id="wiz-folder-confirm"' in html
    assert 'data-action="wizardFolderConfirmBack"' in html
    assert 'data-action="wizardFolderConfirmGo"' in html
    assert 'data-en="Connect anyway"' in html and 'data-en="Back"' in html
    assert not re.search(r"(?<![A-Za-z_.])confirm\(", section)  # no JS confirm() dialog
    # The flag is set in exactly one place: the "Connect anyway" handler.
    assert section.count("confirm_container = true") == 1
    go = section[section.index("function wizardFolderConfirmGo") :]
    assert "confirm_container = true" in go[: go.index("\n    }\n")]
    assert "data.code === 'container_folder'" in section  # server 409 opens the same step
    assert "Oncofiles will create its category folders inside it" in section


def test_parent_of_patient_folders_has_no_connect_anyway():
    section = _folder_js()
    ask = _between(section, "function wizardFolderAskConfirm", "function wizardFolderConfirmBack")
    # One flag decides the variant, and it hides the only "Connect anyway" control.
    assert "=== 'curated_children' || m > 0;" in ask
    assert "wizShow('wiz-folder-confirm-go', !hard);" in ask
    hard_branch = _between(ask, "if (hard) {", "} else if (n > 0) {")
    assert "cannot be connected because it contains other patients" in hard_branch
    assert "nedá pripojiť, pretože obsahuje priečinky iných pacientov" in hard_branch
    assert "anyway" not in hard_branch.lower()
    # The markup has exactly one such control, so hiding it leaves "Back" only.
    assert _html().count("wizardFolderConfirmGo") == 1
    # A 409 for this reason opens the same step even if the flag was sent.
    assert "data.reason === 'curated_children'" in section


def test_step3_has_a_curated_variant_without_first_sync():
    html, js = _html(), _js()
    legacy = _between(html, 'id="wiz-step3-legacy"', 'id="wiz-step3-curated"')
    curated = _between(html, 'id="wiz-step3-curated"', "<!-- Step 4")
    assert "wizardStep3Sync" in legacy
    assert "wizardStep3Sync" not in curated and "Start First Sync" not in curated
    assert "processed by the curated pipeline (local OCR)" in curated
    assert curated.count("<button") == 1 and 'data-en="Continue"' in curated
    assert "function wizardSetStep3Variant" in js
    assert "wizardSetStep3Variant(data);" in js


def test_leftover_wizard_strings_are_bilingual():
    js = _js()
    assert "btn.textContent = 'Create Patient'" not in js
    assert "wizardResetCreateBtn(btn);" in js
    assert (
        "wizSetBi(tokenEl, '(vygenerujte cez tlačidlo Zdieľať)', '(generate via Share button)')"
        in js
    )
