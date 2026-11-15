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

    def list(self, q: str = "", pageSize: int = 1000, pageToken=None, **_kw):  # noqa: N803
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
        offset = int(pageToken or 0)
        page = {"files": out[offset : offset + pageSize]}
        if offset + pageSize < len(out):
            page["nextPageToken"] = str(offset + pageSize)
        return MagicMock(execute=MagicMock(return_value=page))

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


def _child_marker_queries(files: FakeFiles) -> list[str]:
    return [q for q in files.list_queries if "name = '_index.json'" in q and " or '" in q]


def _folder_list_queries(files: FakeFiles) -> list[str]:
    return [q for q in files.list_queries if "mimeType" in q]


WIDE = "idSiroky000000001"
CHUNK = curated_folder._CONTAINER_QUERY_CHUNK


def test_overview_checks_every_child_not_only_the_first_fifty():
    client, files = _client(_wide_tree(60, curated_at=55))
    out = curated_folder.folder_overview(client, WIDE)
    assert out["subfolder_count"] == 60
    assert out["curated_children"] == 1
    assert out["container_reason"] == "curated_children"


def test_overview_passes_many_ordinary_children_after_a_complete_check():
    client, files = _client(_wide_tree(60))
    out = curated_folder.folder_overview(client, WIDE)
    assert out["container_reason"] is None and out["curated_children"] == 0
    assert [q.count(" in parents") for q in _child_marker_queries(files)] == [CHUNK] * 6


def test_overview_stops_querying_after_the_first_hit():
    client, files = _client(_wide_tree(60, curated_at=13))
    out = curated_folder.folder_overview(client, WIDE)
    assert out["container_reason"] == "curated_children"
    assert len(_child_marker_queries(files)) == 2  # the hit is in the second chunk


def test_overview_refuses_more_children_than_the_limit_without_marker_queries():
    over = curated_folder.CONTAINER_CHECK_LIMIT + 1
    client, files = _client(_wide_tree(over, curated_at=3))
    out = curated_folder.folder_overview(client, WIDE)
    assert out["container_reason"] == "too_many_to_check"
    assert out["subfolder_count"] == over
    assert _child_marker_queries(files) == []
    # At exactly the limit everything is checked: one query per chunk.
    client, files = _client(_wide_tree(curated_folder.CONTAINER_CHECK_LIMIT))
    out = curated_folder.folder_overview(client, WIDE)
    assert out["container_reason"] is None
    assert len(_child_marker_queries(files)) == curated_folder.CONTAINER_CHECK_LIMIT // CHUNK


def test_overview_paginates_the_child_listing():
    client, files = _client(_wide_tree(150, curated_at=140))  # the hit is on the second page
    out = curated_folder.folder_overview(client, WIDE)
    assert len(_folder_list_queries(files)) == 2  # pages of one hundred
    assert out["subfolder_count"] == 150
    assert out["container_reason"] == "curated_children"


def test_overview_chunks_child_marker_query_by_ten():
    client, files = _client(_wide_tree(23))
    out = curated_folder.folder_overview(client, WIDE)
    assert out["container_reason"] is None
    queries = _child_marker_queries(files)
    assert [q.count(" in parents") for q in queries] == [CHUNK, CHUNK, 3]
    shape = re.compile(
        r"^\('[A-Za-z0-9_-]+' in parents(?: or '[A-Za-z0-9_-]+' in parents)*\) "
        r"and \(name = 'README\.md' or name = '_index\.json'\) and trashed = false$"
    )
    assert all(shape.match(q) for q in queries), queries


def test_overview_does_not_use_the_retrying_client_listing():
    client, _ = _client()
    client.list_child_folders = MagicMock(side_effect=AssertionError("retry wrapper used"))
    out = curated_folder.folder_overview(client, VOLNY)
    assert out["subfolder_count"] == 1 and out["container_reason"] is None


def test_overview_validates_the_folder_id_itself():
    client, files = _client()
    out = curated_folder.folder_overview(client, "x' or name = 'y")
    assert out["container_reason"] == "check_failed"
    assert files.list_queries == []  # nothing was interpolated


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
    assert data["confirmable"] is False and "confirm_field" not in data
    db.update_oauth_folder.assert_not_called()
    db.update_oauth_owner_email.assert_not_called()
    client.create_folder.assert_not_called()
    assert len(files.list_queries) == 3  # own markers, child folders, children's markers


def _assert_nothing_persisted(db, client, files):
    db.update_oauth_folder.assert_not_called()
    db.update_oauth_owner_email.assert_not_called()
    client.create_folder.assert_not_called()
    assert files.created == []


@pytest.mark.parametrize("extra", [{}, {"confirm_container": True}])
async def test_set_folder_refuses_curated_child_past_the_first_fifty(extra):
    """The hole infra found: a curated folder at position 55 of 60 must still block."""
    client, files = _client(_wide_tree(60, curated_at=55))
    status, data, db = await _set_folder({"folder_id": WIDE, **extra}, client)
    assert status == 409
    assert data["code"] == "container_folder" and data["reason"] == "curated_children"
    assert data["confirmable"] is False
    _assert_nothing_persisted(db, client, files)


@pytest.mark.parametrize("extra", [{}, {"confirm_container": True}])
async def test_set_folder_refuses_more_subfolders_than_can_be_checked(extra):
    client, files = _client(_wide_tree(curated_folder.CONTAINER_CHECK_LIMIT + 1))
    status, data, db = await _set_folder({"folder_id": WIDE, **extra}, client)
    assert status == 409
    assert data["code"] == "container_folder" and data["reason"] == "too_many_to_check"
    assert data["confirmable"] is False
    _assert_nothing_persisted(db, client, files)


@pytest.mark.parametrize("extra", [{}, {"confirm_container": True}])
async def test_set_folder_refuses_failed_check(extra):
    """Fail closed: a Drive error means "try again", never a pass."""
    client, files = _client()
    files.fail_list_matching = "README.md"
    status, data, db = await _set_folder({"folder_id": VOLNY, **extra}, client)
    assert status == 409
    assert data["code"] == "container_folder" and data["reason"] == "check_failed"
    assert data["confirmable"] is False
    _assert_nothing_persisted(db, client, files)


@pytest.mark.parametrize("extra", [{}, {"confirm_container": True}])
async def test_set_folder_refuses_parent_of_curated_folders_whatever_the_body(extra):
    client, files = _client()
    status, data, db = await _set_folder({"folder_id": RODINA, **extra}, client)
    assert status == 409 and data["reason"] == "curated_children"
    _assert_nothing_persisted(db, client, files)


async def test_set_folder_accepts_sixty_ordinary_subfolders():
    """A complete check with no curated child leaves nothing for the server to ask."""
    client, _ = _client(_wide_tree(60))
    status, data, db = await _set_folder({"folder_id": WIDE}, client)
    assert status == 200
    db.update_oauth_folder.assert_awaited_once()


def test_server_has_no_confirmation_flag():
    import inspect

    assert "confirm_container" not in inspect.getsource(server.api_gdrive_set_folder).replace(
        "there is no ``confirm_container`` field any more", ""
    )
    assert "too_many_subfolders" not in inspect.getsource(curated_folder)


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


def _ask_confirm_js() -> str:
    return _between(
        _folder_js(), "function wizardFolderAskConfirm", "function wizardFolderConfirmBack"
    )


def test_container_confirmation_is_an_in_wizard_step():
    html, section = _html(), _folder_js()
    assert 'id="wiz-folder-confirm"' in html
    assert 'data-action="wizardFolderConfirmBack"' in html
    assert 'data-action="wizardFolderConfirmGo"' in html
    assert 'data-en="Connect anyway"' in html and 'data-en="Back"' in html
    assert not re.search(r"(?<![A-Za-z_.])confirm\(", section)  # no JS confirm() dialog
    assert "data.code === 'container_folder'" in section  # a server 409 opens the same box
    assert "Oncofiles will create its category folders inside it" in section


def test_connect_anyway_is_not_wired_to_any_server_flag():
    """ "Connect anyway" answers a UX hint; no body field can lift a server refusal."""
    html, js = _html(), _js()
    assert "confirm_container" not in js and "confirm_container" not in html
    assert "confirmable" not in js
    go = _between(_folder_js(), "function wizardFolderConfirmGo", "function wizardStep2SetFolder")
    assert "wizardFolderSend(wizardFolderPending);" in go
    assert not re.search(r"\bbody\.\w+\s*=", go)  # adds nothing to the request
    assert html.count("wizardFolderConfirmGo") == 1


def test_refusals_have_no_connect_anyway():
    ask = _ask_confirm_js()
    assert "var hard = reason === 'curated_children' || (info.curated_children || 0) > 0;" in ask
    assert "var tooMany = !hard && reason === 'too_many_to_check';" in ask
    # Any server reason at all hides the only "Connect anyway" control.
    assert "var blocked = hard || tooMany || retry || !!reason;" in ask
    assert "wizShow('wiz-folder-confirm-go', !blocked);" in ask
    hard_branch = _between(ask, "if (hard) {", "} else if (tooMany) {")
    assert "cannot be connected because it contains other patients" in hard_branch
    assert "nedá pripojiť, pretože obsahuje priečinky iných pacientov" in hard_branch
    many_branch = _between(ask, "} else if (tooMany) {", "} else if (blocked) {")
    assert "too many subfolders to check safely" in many_branch
    assert "or paste its link" in many_branch
    assert "príliš veľa podpriečinkov na bezpečnú kontrolu" in many_branch
    for branch in (hard_branch, many_branch):
        assert "anyway" not in branch.lower()


def test_try_again_exists_only_in_the_check_failed_variant():
    html, section, ask = _html(), _folder_js(), _ask_confirm_js()
    tag = _element(html, "wiz-folder-confirm-retry")
    assert "display:none" in tag  # hidden until the check_failed variant shows it
    assert 'data-action="wizardFolderConfirmRetry"' in tag
    assert 'data-en="Try again"' in tag and re.search(r'data-sk="[^"]+"', tag)
    assert html.count("wizardFolderConfirmRetry") == 1
    assert "var retry = !hard && reason === 'check_failed';" in ask
    # Shown by exactly one statement, driven by that flag alone.
    assert section.count("'wiz-folder-confirm-retry', ") == 1
    assert "wizShow('wiz-folder-confirm-retry', retry);" in ask
    retry_branch = _between(ask, "} else if (blocked) {", "} else {")
    assert "Could not check this folder right now. Try again in a moment." in retry_branch
    assert "Tento priečinok sa teraz nepodarilo skontrolovať" in retry_branch


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
