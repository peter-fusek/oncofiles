"""#691 (isolation): the export_manifest MCP tool is patient-scoped.

The tool used to call ``manifest.export_manifest(db)`` without the required
``patient_id`` and raised TypeError on every call. The fix must resolve the
patient through ``_resolve_patient_id`` (ACL gate #497) — never "drop the
param and export the whole DB". Two synthetic patients, real resolver.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

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


async def test_admin_never_gets_whole_db(two_patients, as_admin):
    for slug in (None, ERIKA_SLUG, BOB_SLUG):
        out = json.loads(await export_manifest(_ctx(two_patients), patient_slug=slug))
        ids = _file_ids(out)
        assert not ({"a-only-1"} <= ids and {"b-only-1"} <= ids), slug


async def test_unknown_slug_is_an_error(two_patients, as_admin):
    out = json.loads(await export_manifest(_ctx(two_patients), patient_slug="no-such-patient"))
    assert "error" in out
