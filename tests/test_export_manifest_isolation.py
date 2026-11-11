"""#691 (isolation): the export_manifest MCP tool is patient-scoped.

The tool used to call ``manifest.export_manifest(db)`` without the required
``patient_id`` and raised TypeError on every call. The fix must resolve the
patient through ``_resolve_patient_id`` (ACL gate #497) — never "drop the
param and export the whole DB". Two synthetic patients, real resolver.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.database import Database
from oncofiles.models import AgentState
from oncofiles.persistent_oauth import _verified_caller_is_admin
from oncofiles.tools.gdrive import export_manifest
from tests.helpers import ERIKA_UUID, make_doc

ERIKA_SLUG = "erika-test"
BOB_UUID = "00000000-0000-4000-8000-000000000002"
BOB_SLUG = "bob-test"


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock()}
    return ctx


@pytest.fixture
async def two_patients(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ? WHERE patient_id = ?", (ERIKA_SLUG, ERIKA_UUID)
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test"),
    )
    await db.db.commit()
    await db.insert_document(
        make_doc(
            file_id="a-only-1",
            filename="20270311_A_SynthClinic_Labs_PatientAOnly.pdf",
            document_date=date(2027, 3, 11),
        ),
        patient_id=ERIKA_UUID,
    )
    for i in (1, 2):
        await db.insert_document(
            make_doc(
                file_id=f"b-only-{i}",
                filename=f"20270422_B_SynthClinic_Labs_PatientBOnly{i}.pdf",
                document_date=date(2027, 4, 22),
            ),
            patient_id=BOB_UUID,
        )
    await db.set_agent_state(AgentState(key="a_key", value='{"who": "A"}', patient_id=ERIKA_UUID))
    await db.set_agent_state(AgentState(key="b_key", value='{"who": "B"}', patient_id=BOB_UUID))
    return db


@pytest.fixture
def as_admin():
    tok = _verified_caller_is_admin.set(True)
    yield
    _verified_caller_is_admin.reset(tok)


@pytest.fixture
def as_caregiver():
    tok = _verified_caller_is_admin.set(False)
    yield
    _verified_caller_is_admin.reset(tok)


def _file_ids(manifest: dict) -> set[str]:
    return {d["file_id"] for d in manifest["documents"]}


def _state_keys(manifest: dict) -> set[str]:
    return {s["key"] for s in manifest["agent_state"]}


async def test_caregiver_gets_only_own_patient(two_patients, as_caregiver):
    out = json.loads(await export_manifest(_ctx(two_patients)))
    assert _file_ids(out) == {"a-only-1"}
    assert _state_keys(out) == {"a_key"}
    assert "PatientBOnly" not in json.dumps(out)


async def test_caregiver_cannot_slug_into_other_patient(two_patients, as_caregiver):
    out = json.loads(await export_manifest(_ctx(two_patients), patient_slug=BOB_SLUG))
    assert "access denied" in out["error"]
    assert "documents" not in out


async def test_admin_with_slug_gets_that_patient(two_patients, as_admin):
    out = json.loads(await export_manifest(_ctx(two_patients), patient_slug=BOB_SLUG))
    assert _file_ids(out) == {"b-only-1", "b-only-2"}
    assert _state_keys(out) == {"b_key"}
    assert "PatientAOnly" not in json.dumps(out)


async def test_admin_without_slug_is_refused(two_patients, as_admin):
    """No implicit admin scope (#686 rule): the bound/default patient (A here)
    must not be exported just because it is current."""
    out = json.loads(await export_manifest(_ctx(two_patients)))
    assert "patient_slug" in out["error"]
    assert "documents" not in out
    assert "PatientAOnly" not in json.dumps(out)


async def test_admin_never_gets_whole_db(two_patients, as_admin):
    for slug in (ERIKA_SLUG, BOB_SLUG):
        out = json.loads(await export_manifest(_ctx(two_patients), patient_slug=slug))
        ids = _file_ids(out)
        assert not ({"a-only-1"} <= ids and {"b-only-1"} <= ids), slug


# ── Paths that bypass the tool: /api/manifest and the sync Drive backup ──


async def test_api_manifest_passes_explicit_patient_id(two_patients):
    from starlette.responses import JSONResponse

    from oncofiles import manifest as manifest_mod
    from oncofiles import server

    request = MagicMock()
    request.app.state.fastmcp_server._lifespan_result = {"db": two_patients}
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(server, "_get_dashboard_patient_id", new=AsyncMock(return_value=BOB_UUID)),
        # api_manifest imports export_manifest from the module at call time.
        patch.object(manifest_mod, "export_manifest", wraps=manifest_mod.export_manifest) as spy,
    ):
        resp = await server.api_manifest(request)
    assert isinstance(resp, JSONResponse) and resp.status_code == 200
    spy.assert_awaited_once()
    assert spy.await_args.kwargs == {"patient_id": BOB_UUID}
    body = json.loads(resp.body)
    assert _file_ids(body) == {"b-only-1", "b-only-2"}
    assert _state_keys(body) == {"b_key"}


async def test_api_manifest_no_access_sentinel_exports_nothing(two_patients):
    from oncofiles import server
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL

    request = MagicMock()
    request.app.state.fastmcp_server._lifespan_result = {"db": two_patients}
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(
            server,
            "_get_dashboard_patient_id",
            new=AsyncMock(return_value=NO_PATIENT_ACCESS_SENTINEL),
        ),
    ):
        resp = await server.api_manifest(request)
    body = json.loads(resp.body)
    assert body["documents"] == [] and body["agent_state"] == []


async def test_sync_drive_backup_manifest_is_patient_scoped(two_patients):
    """_export_metadata writes _manifest.json to the patient's Drive root
    with that patient's records only."""
    from oncofiles import sync as sync_mod

    uploads: dict[str, str] = {}

    def _capture(gdrive, name, content, *args, **kwargs):
        uploads[name] = content

    gdrive = MagicMock()
    gdrive.list_folder.return_value = []
    with patch.object(sync_mod, "_upload_or_update_text", side_effect=_capture):
        await sync_mod._export_metadata(
            two_patients, gdrive, "synthetic-root", {}, patient_id=BOB_UUID
        )
    manifest = json.loads(uploads["_manifest.json"])
    assert _file_ids(manifest) == {"b-only-1", "b-only-2"}
    assert _state_keys(manifest) == {"b_key"}
    assert "PatientAOnly" not in uploads["_manifest.json"]


async def test_unknown_slug_is_an_error(two_patients, as_admin):
    out = json.loads(await export_manifest(_ctx(two_patients), patient_slug="no-such-patient"))
    assert "error" in out
