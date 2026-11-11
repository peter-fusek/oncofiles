"""#607: query_db never gives an admin caller an implicit patient scope.

Battery B1 (2026-11-09) saw an admin OAuth caller read the ``documents``
table through ``query_db`` without ``patient_slug``. The middleware binds
admins to a default/selected patient, and the resolver handed that pid back,
so a query filtered on it ran against "whichever patient was current" —
the implicit admin scope the tool's contract (#429/#486) forbids.

These tests use the real ``_resolve_patient_id`` (no mock) and two seeded
synthetic patients.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from oncofiles.database import Database
from oncofiles.persistent_oauth import _verified_caller_is_admin
from oncofiles.tools.db_query import query_db
from tests.helpers import ERIKA_UUID, make_doc

ERIKA_SLUG = "jana-test"
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
    await db.insert_document(make_doc(file_id="jana-f1"), patient_id=ERIKA_UUID)
    await db.insert_document(make_doc(file_id="bob-f1"), patient_id=BOB_UUID)
    await db.insert_document(make_doc(file_id="bob-f2"), patient_id=BOB_UUID)
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


async def _run(db: Database, sql: str, **kw) -> dict:
    return json.loads(await query_db(_ctx(db), sql, **kw))


# ── Admin without patient_slug: rejected on patient-scoped tables ──────


async def test_admin_without_slug_unfiltered_count_rejected(two_patients, as_admin):
    """The exact battery call."""
    result = await _run(two_patients, "SELECT COUNT(*) FROM documents")
    assert "error" in result
    assert "patient_slug" in result["error"]


async def test_admin_without_slug_cannot_use_bound_patient_pid(two_patients, as_admin):
    """The middleware-bound patient (Jana) must not become an implicit scope."""
    result = await _run(
        two_patients, f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{ERIKA_UUID}'"
    )
    assert "error" in result, result
    assert "patient_slug" in result["error"]


async def test_admin_without_slug_cannot_read_other_patient(two_patients, as_admin):
    result = await _run(
        two_patients, f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{BOB_UUID}'"
    )
    assert "error" in result, result


async def test_admin_without_slug_can_still_read_admin_only_tables(two_patients, as_admin):
    result = await _run(two_patients, "SELECT COUNT(*) AS n FROM schema_migrations")
    assert "error" not in result, result
    assert result["rows"][0]["n"] > 0


async def test_admin_without_slug_join_of_admin_and_patient_table_rejected(two_patients, as_admin):
    result = await _run(
        two_patients,
        "SELECT COUNT(*) FROM sync_history s JOIN documents d ON d.patient_id = s.patient_id",
    )
    assert "error" in result


# ── Admin with patient_slug: scoped to exactly that patient ────────────


async def test_admin_with_slug_reads_that_patient(two_patients, as_admin):
    result = await _run(
        two_patients,
        f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{BOB_UUID}'",
        patient_slug=BOB_SLUG,
    )
    assert "error" not in result, result
    assert result["rows"][0]["n"] == 2


async def test_admin_with_slug_cannot_filter_on_another_pid(two_patients, as_admin):
    result = await _run(
        two_patients,
        f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{ERIKA_UUID}'",
        patient_slug=BOB_SLUG,
    )
    assert "error" in result


async def test_admin_with_slug_unfiltered_still_rejected(two_patients, as_admin):
    result = await _run(two_patients, "SELECT COUNT(*) FROM documents", patient_slug=BOB_SLUG)
    assert "error" in result


# ── Non-admin: bound patient remains the scope (unchanged behaviour) ────


async def test_caregiver_without_slug_uses_bound_patient(two_patients, as_caregiver):
    result = await _run(
        two_patients, f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{ERIKA_UUID}'"
    )
    assert "error" not in result, result
    assert result["rows"][0]["n"] == 1


async def test_caregiver_cannot_read_other_patient(two_patients, as_caregiver):
    result = await _run(
        two_patients, f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{BOB_UUID}'"
    )
    assert "error" in result


async def test_caregiver_cannot_slug_into_other_patient(two_patients, as_caregiver):
    result = await _run(
        two_patients,
        f"SELECT COUNT(*) AS n FROM documents WHERE patient_id = '{BOB_UUID}'",
        patient_slug=BOB_SLUG,
    )
    assert "error" in result
    assert "access denied" in result["error"]
