"""#839: the MCP folder tools name their patient and share the wizard's guard.

``gdrive_set_folder`` and the REST handler call one helper
(``drive_folder_link.connect_folder``); ``setup_gdrive`` runs the same
container check before it creates anything. Every id, name and address here
is invented; the fake Drive tree is the one from the #835 tests.
"""

from __future__ import annotations

import inspect
import json
from unittest.mock import MagicMock

import pytest

from oncofiles import curated_folder, drive_folder_link, server
from oncofiles.database import Database
from oncofiles.models import OAuthToken
from oncofiles.persistent_oauth import _verified_caller_email, _verified_caller_is_admin
from oncofiles.tools import gdrive as gdrive_tools
from oncofiles.tools.gdrive import gdrive_set_folder, setup_gdrive
from tests.conftest import PRIMARY_TEST_UUID
from tests.test_835_wizard_folder_picker import (
    ALFA,
    PRAZDNY,
    RODINA,
    SUBOR,
    VOLNY,
    WIDE,
    _client,
    _wide_tree,
)

JANA_SLUG = "jana-test"
BOB_UUID = "00000000-0000-4000-8000-000000000002"
BOB_SLUG = "bob-test"
BOB_EMAIL = "bob.caregiver@example.com"
JANA_OLD_FOLDER = "idJanaStary0000001"
BOB_OLD_FOLDER = "idBobStary00000001"


class _StubCtx:
    class _Req:
        def __init__(self, db, gdrive=None):
            self.lifespan_context = {"db": db, "gdrive": gdrive}

    def __init__(self, db, gdrive=None):
        self.request_context = self._Req(db, gdrive)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    curated_folder.clear_cache()
    monkeypatch.setattr(server, "_folder_404_counts", {})
    monkeypatch.setattr(server, "_patient_clients_cache", {})
    monkeypatch.setattr("oncofiles.oauth.is_token_expired", lambda *_a, **_k: False)
    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")
    yield
    _verified_caller_is_admin.set(False)
    _verified_caller_email.set("")
    curated_folder.clear_cache()


@pytest.fixture
async def two(db: Database) -> Database:
    """Two patients, each with a Drive token and an already connected folder."""
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        (JANA_SLUG, "jana.caregiver@example.com", PRIMARY_TEST_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test", BOB_EMAIL),
    )
    await db.db.commit()
    for pid, folder in ((PRIMARY_TEST_UUID, JANA_OLD_FOLDER), (BOB_UUID, BOB_OLD_FOLDER)):
        await db.upsert_oauth_token(
            OAuthToken(
                patient_id=pid,
                access_token="access-invented",
                refresh_token="refresh-invented",
                gdrive_folder_id=folder,
            )
        )
    return db


def _use(monkeypatch, tree=None):
    client, files = _client(tree)
    monkeypatch.setattr(server.GDriveClient, "from_oauth", classmethod(lambda cls, *a, **k: client))
    return client, files


async def _folder(db: Database, pid: str) -> str | None:
    return (await db.get_oauth_token(patient_id=pid)).gdrive_folder_id


async def _call(db, **kw) -> dict:
    return json.loads(await gdrive_set_folder(_StubCtx(db), **kw))


def _no_drive_writes(client, files) -> None:
    client.create_folder.assert_not_called()
    assert files.created == []


# ── who is the patient ───────────────────────────────────────────────


async def test_admin_must_name_the_patient(two, monkeypatch):
    client, files = _use(monkeypatch)
    _verified_caller_is_admin.set(True)
    for dry_run in (True, False):
        out = await _call(two, folder_id=VOLNY, dry_run=dry_run)
        assert "must pass patient_slug" in out["error"]
    assert files.list_queries == []  # refused before any Drive call
    assert await _folder(two, PRIMARY_TEST_UUID) == JANA_OLD_FOLDER
    assert await _folder(two, BOB_UUID) == BOB_OLD_FOLDER


async def test_non_admin_bound_to_another_patient_is_denied(two, monkeypatch):
    """The db fixture binds the caller to Jana; Bob's slug must not resolve."""
    client, files = _use(monkeypatch)
    out = await _call(two, folder_id=VOLNY, patient_slug=BOB_SLUG, dry_run=False)
    assert "access denied" in out["error"]
    assert files.list_queries == []
    assert await _folder(two, BOB_UUID) == BOB_OLD_FOLDER


async def test_explicit_slug_drives_the_patient_not_the_context(two, monkeypatch):
    """Option A (#429): the request is bound to Jana, the slug says Bob."""
    client, _ = _use(monkeypatch)
    _verified_caller_is_admin.set(True)
    server._folder_404_counts.update({BOB_UUID: 3, PRIMARY_TEST_UUID: 3})
    out = await _call(two, folder_id=VOLNY, patient_slug=BOB_SLUG, dry_run=False)
    assert out["status"] == "ok" and out["patient_id"] == BOB_UUID
    assert await _folder(two, BOB_UUID) == VOLNY
    assert (
        await _folder(two, PRIMARY_TEST_UUID) == JANA_OLD_FOLDER
    )  # the bound patient is untouched
    assert server._folder_404_counts == {PRIMARY_TEST_UUID: 3}
    bob = await two.get_oauth_token(patient_id=BOB_UUID)
    assert bob.gdrive_folder_name == "Volny" and bob.owner_email == "owner@example.com"
    assert (await two.get_oauth_token(patient_id=PRIMARY_TEST_UUID)).owner_email is None


async def test_caregiver_may_name_their_own_patient(two, monkeypatch):
    _use(monkeypatch)
    _verified_caller_email.set(BOB_EMAIL)
    out = await _call(two, folder_id=VOLNY, patient_slug=BOB_SLUG, dry_run=False)
    assert out["status"] == "ok"
    assert await _folder(two, BOB_UUID) == VOLNY
    assert await _folder(two, PRIMARY_TEST_UUID) == JANA_OLD_FOLDER


async def test_non_admin_without_slug_still_acts_on_own_patient(two, monkeypatch):
    _use(monkeypatch)
    out = await _call(two, folder_id=VOLNY, dry_run=False)
    assert out["status"] == "ok" and out["patient_id"] == PRIMARY_TEST_UUID
    assert await _folder(two, PRIMARY_TEST_UUID) == VOLNY
    assert await _folder(two, BOB_UUID) == BOB_OLD_FOLDER


# ── dry run ──────────────────────────────────────────────────────────


async def test_dry_run_is_the_default_and_writes_nothing(two, monkeypatch):
    client, files = _use(monkeypatch)
    server._folder_404_counts[PRIMARY_TEST_UUID] = 3
    server._patient_clients_cache[PRIMARY_TEST_UUID] = ("cached",)
    before = await two.get_oauth_token(patient_id=PRIMARY_TEST_UUID)
    out = await _call(two, folder_id=VOLNY, patient_slug=JANA_SLUG)
    assert out["status"] == "dry_run" and out["dry_run"] is True
    assert out["folder_id"] == VOLNY and out["folder_name"] == "Volny"
    assert out["current_folder_id"] == JANA_OLD_FOLDER and out["would_change"] is True
    assert out["curated"] is False
    assert out["overview"] == {
        "curated": False,
        "subfolder_count": 1,
        "curated_children": 0,
        "container_reason": None,
    }
    after = await two.get_oauth_token(patient_id=PRIMARY_TEST_UUID)
    assert after == before
    assert server._folder_404_counts == {PRIMARY_TEST_UUID: 3}
    assert server._patient_clients_cache == {PRIMARY_TEST_UUID: ("cached",)}
    client.get_folder_owner.assert_not_called()
    _no_drive_writes(client, files)


async def test_dry_run_reports_a_curated_folder_and_refusals(two, monkeypatch):
    client, files = _use(monkeypatch)
    out = await _call(two, folder_id=ALFA, patient_slug=JANA_SLUG)
    assert out["status"] == "dry_run"
    assert out["curated"] is True and out["curated_root"] is True
    refused = await _call(two, folder_id=RODINA, patient_slug=JANA_SLUG)
    assert refused["reason"] == "curated_children" and "status" not in refused
    assert await _folder(two, PRIMARY_TEST_UUID) == JANA_OLD_FOLDER
    _no_drive_writes(client, files)


async def test_dry_run_does_not_store_a_refreshed_token(two, monkeypatch):
    _use(monkeypatch)
    monkeypatch.setattr("oncofiles.oauth.is_token_expired", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "oncofiles.oauth.refresh_access_token",
        lambda _r: {"access_token": "access-new-invented", "expires_in": 3600},
    )
    await _call(two, folder_id=VOLNY, patient_slug=JANA_SLUG)
    assert (
        await two.get_oauth_token(patient_id=PRIMARY_TEST_UUID)
    ).access_token == "access-invented"
    await _call(two, folder_id=VOLNY, patient_slug=JANA_SLUG, dry_run=False)
    assert (
        await two.get_oauth_token(patient_id=PRIMARY_TEST_UUID)
    ).access_token == "access-new-invented"


# ── the guard, with the REST reason codes ────────────────────────────


async def _refused(two, monkeypatch, folder_id, tree=None, fail=None) -> dict:
    client, files = _use(monkeypatch, tree)
    if fail:
        files.fail_list_matching = fail
    out = await _call(two, folder_id=folder_id, patient_slug=JANA_SLUG, dry_run=False)
    assert await _folder(two, PRIMARY_TEST_UUID) == JANA_OLD_FOLDER
    assert (await two.get_oauth_token(patient_id=PRIMARY_TEST_UUID)).owner_email is None
    client.get_folder_owner.assert_not_called()
    _no_drive_writes(client, files)
    return out


async def test_parent_of_curated_folders_is_refused(two, monkeypatch):
    out = await _refused(two, monkeypatch, RODINA)
    assert out["code"] == "container_folder" and out["reason"] == "curated_children"
    assert out["confirmable"] is False
    assert out["curated_children"] == 1 and out["subfolder_count"] == 3
    assert "other patients" in out["error"]


async def test_failed_check_is_refused(two, monkeypatch):
    out = await _refused(two, monkeypatch, VOLNY, fail="README.md")
    assert out["code"] == "container_folder" and out["reason"] == "check_failed"


async def test_more_children_than_the_limit_is_refused(two, monkeypatch):
    tree = _wide_tree(curated_folder.CONTAINER_CHECK_LIMIT + 1)
    out = await _refused(two, monkeypatch, WIDE, tree=tree)
    assert out["code"] == "container_folder" and out["reason"] == "too_many_to_check"


async def test_a_file_id_is_refused(two, monkeypatch):
    out = await _refused(two, monkeypatch, SUBOR)
    assert "not a folder" in out["error"]


async def test_garbage_and_missing_token(two, monkeypatch, db):
    client, files = _use(monkeypatch)
    out = await _call(two, folder_id="not a link", patient_slug=JANA_SLUG, dry_run=False)
    assert "folder link or id" in out["error"] and files.list_queries == []
    await db.db.execute("DELETE FROM oauth_tokens WHERE patient_id = ?", (PRIMARY_TEST_UUID,))
    await db.db.commit()
    out = await _call(two, folder_id=VOLNY, patient_slug=JANA_SLUG, dry_run=False)
    assert "No OAuth tokens" in out["error"]


async def test_refusal_bodies_are_the_rest_bodies(two, monkeypatch):
    """Same reason codes and fields as the 409 of /api/gdrive-set-folder."""
    from unittest.mock import AsyncMock

    client, _ = _use(monkeypatch)
    mcp_out = await _call(two, folder_id=RODINA, patient_slug=JANA_SLUG, dry_run=False)
    curated_folder.clear_cache()
    req = MagicMock()
    req.json = AsyncMock(return_value={"patient_id": JANA_SLUG, "folder_id": RODINA})
    req.app.state.fastmcp_server._lifespan_result = {"db": two}
    monkeypatch.setattr(server, "_check_dashboard_auth", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_require_body_patient_access", AsyncMock(return_value=None))
    resp = await server.api_gdrive_set_folder(req)
    assert resp.status_code == 409
    assert json.loads(resp.body) == mcp_out


# ── accepted folders ─────────────────────────────────────────────────


async def test_curated_folder_is_set_and_nothing_is_created(two, monkeypatch):
    client, files = _use(monkeypatch)
    out = await _call(two, folder_id=ALFA, patient_slug=JANA_SLUG, dry_run=False)
    assert out["status"] == "ok"
    assert out["curated"] is True and out["curated_root"] is True
    assert out["subfolders_created"] == 0
    assert await _folder(two, PRIMARY_TEST_UUID) == ALFA
    _no_drive_writes(client, files)


async def test_plain_folder_is_set_without_category_folders(two, monkeypatch):
    """The tool never created category folders (setup_gdrive does); still so."""
    client, files = _use(monkeypatch)
    link = f"https://drive.google.com/drive/folders/{PRAZDNY}"
    out = await _call(two, folder_id=link, patient_slug=JANA_SLUG, dry_run=False)
    assert out["status"] == "ok" and out["folder_id"] == PRAZDNY
    assert "curated" not in out and "subfolders_created" not in out
    assert await _folder(two, PRIMARY_TEST_UUID) == PRAZDNY
    _no_drive_writes(client, files)


async def test_same_real_call_twice_is_idempotent(two, monkeypatch):
    _use(monkeypatch)
    kw = {"folder_id": VOLNY, "patient_slug": JANA_SLUG, "dry_run": False}
    first, second = await _call(two, **kw), await _call(two, **kw)
    assert first == second
    assert await _folder(two, PRIMARY_TEST_UUID) == VOLNY


# ── setup_gdrive ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_real_clients(monkeypatch):
    """No patient has a cached client here: setup_gdrive gets the lifespan one
    (the request's own patient only). Also keeps the tests off the network."""

    async def none(_db, _pid):
        return None

    monkeypatch.setattr(server, "_create_patient_clients", none)


async def _setup(db, client, **kw) -> dict:
    return json.loads(await setup_gdrive(_StubCtx(db, client), **kw))


async def test_setup_gdrive_admin_must_name_the_patient(two, monkeypatch):
    client, files = _client()
    _verified_caller_is_admin.set(True)
    out = await _setup(two, client, root_folder_id=VOLNY)
    assert "must pass patient_slug" in out["error"]
    _no_drive_writes(client, files)


async def test_setup_gdrive_denies_a_foreign_slug(two):
    client, files = _client()
    out = await _setup(two, client, root_folder_id=VOLNY, patient_slug=BOB_SLUG)
    assert "access denied" in out["error"]
    _no_drive_writes(client, files)


async def test_setup_gdrive_never_borrows_the_bound_patients_client(two):
    """An admin names Bob; Bob has no usable Drive client here. The lifespan
    client belongs to the request's own patient and must not be used."""
    client, files = _client()
    _verified_caller_is_admin.set(True)
    out = await _setup(two, client, root_folder_id=VOLNY, patient_slug=BOB_SLUG)
    assert "not configured" in out["error"]
    assert files.list_queries == []
    _no_drive_writes(client, files)


@pytest.mark.parametrize(
    ("folder", "fail", "reason"),
    [(RODINA, None, "curated_children"), (VOLNY, "README.md", "check_failed")],
)
async def test_setup_gdrive_refuses_a_container(two, folder, fail, reason):
    client, files = _client()
    files.fail_list_matching = fail
    out = await _setup(two, client, root_folder_id=folder)
    assert out["code"] == "container_folder" and out["reason"] == reason
    client.find_folder.assert_not_called()
    _no_drive_writes(client, files)


async def test_setup_gdrive_refuses_too_many_subfolders(two):
    client, files = _client(_wide_tree(curated_folder.CONTAINER_CHECK_LIMIT + 1))
    out = await _setup(two, client, root_folder_id=WIDE)
    assert out["reason"] == "too_many_to_check"
    _no_drive_writes(client, files)


async def test_setup_gdrive_skips_a_curated_folder(two):
    client, files = _client()
    out = await _setup(two, client, root_folder_id=ALFA)
    assert out == {**out, "skipped": True, "curated": True}
    client.find_folder.assert_not_called()
    _no_drive_writes(client, files)


async def test_setup_gdrive_creates_in_a_plain_folder_and_sets_no_folder(two):
    client, _ = _client()
    out = await _setup(two, client, root_folder_id=VOLNY)
    assert out["status"] == "ok" and out["root_folder_id"] == VOLNY
    assert client.create_folder.call_count == out["total_folders"] > 0
    assert await _folder(two, PRIMARY_TEST_UUID) == JANA_OLD_FOLDER


# ── source locks ─────────────────────────────────────────────────────


def test_rest_and_mcp_share_one_helper():
    for fn in (server.api_gdrive_set_folder, gdrive_set_folder):
        src = inspect.getsource(fn)
        assert "connect_folder(" in src
        assert "update_oauth_folder" not in src and "folder_overview" not in src


def test_only_the_helper_writes_a_folder_id():
    """A second writer would be a second place to forget the guard."""
    from pathlib import Path

    src = Path(drive_folder_link.__file__).parent
    writers = sorted(
        str(p.relative_to(src))
        for p in src.rglob("*.py")
        if ".update_oauth_folder(" in p.read_text(encoding="utf-8")
    )
    assert writers == ["drive_folder_link.py"]


def test_folder_tools_take_patient_slug_and_keep_their_names():
    sig = inspect.signature(gdrive_set_folder)
    assert sig.parameters["patient_slug"].default is None
    assert sig.parameters["dry_run"].default is True
    assert inspect.signature(setup_gdrive).parameters["patient_slug"].default is None
    assert gdrive_tools.gdrive_set_folder.__name__ == "gdrive_set_folder"


async def test_annotations_not_read_only_not_destructive_idempotent():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    ann = tools["gdrive_set_folder"].annotations
    assert ann.readOnlyHint is False
    assert ann.destructiveHint is False
    assert ann.idempotentHint is True
    doc = tools["gdrive_set_folder"].description
    assert "dashboard wizard" in doc and "PARENT of patient folders is refused" in doc
