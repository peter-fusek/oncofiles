"""#590 — MCP tools never enumerate patients beyond the caller's own access.

Locks, per tool:

  * ``_resolve_patient_id``: an unknown slug and a foreign slug produce the
    byte-identical error (no existence oracle), with no foreign pid/email.
  * ``list_patients``: OAuth callers — admin included — see only their
    caregiver patients; an OAuth admin with no caregiver binding gets ``[]``;
    patient bearer / stdio see only the bound patient; the static operator
    bearer keeps the full list (Oncoteam registry sync).
  * ``select_patient``: goes through the ACL; foreign slug, foreign UUID and
    unknown slug are indistinguishable; never persists a selection for an
    email other than the caller's own caregiver email.
  * ``get_patient_context``, ``integration_status``, ``system_health``,
    ``query_db``: foreign == unknown, and no foreign identifier in output.
  * ``query_db``: the patient_id proof covers every table reference (the
    ``OR 1=1`` / second-alias / scalar sub-query enumeration bypasses).

All patients, slugs and emails are synthetic.
"""

from __future__ import annotations

import json

import pytest

from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
from oncofiles.database import Database
from oncofiles.models import Document, DocumentCategory
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.persistent_oauth import (
    _verified_auth_method,
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools._helpers import _resolve_patient_id
from oncofiles.tools.db_query import _validate_query
from tests.conftest import ERIKA_UUID

OWN_UUID = ERIKA_UUID
OWN_SLUG = "alpha-test"
OWN_NAME = "Alpha Example"
OWN_EMAIL = "alpha.caregiver@example.com"
FOREIGN_UUID = "00000000-0000-4000-8000-0000000000f2"
FOREIGN_SLUG = "bravo-test"
FOREIGN_NAME = "Bravo Example"
FOREIGN_EMAIL = "bravo.caregiver@example.com"
ADMIN_EMAIL = "operator@example.com"
UNKNOWN_SLUG = "no-such-patient"


class _Ctx:
    class _Req:
        def __init__(self, db, transport=""):
            self.lifespan_context = {"db": db, "transport": transport}

    def __init__(self, db, transport=""):
        self.request_context = self._Req(db, transport)


@pytest.fixture
async def pdb(db: Database) -> Database:
    """Two patients: OWN (the conftest-bound pid) and FOREIGN."""
    await db.db.execute(
        "UPDATE patients SET slug = ?, display_name = ?, caregiver_email = ? WHERE patient_id = ?",
        (OWN_SLUG, OWN_NAME, OWN_EMAIL, OWN_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (FOREIGN_UUID, FOREIGN_SLUG, FOREIGN_NAME, FOREIGN_EMAIL),
    )
    await db.db.commit()
    return db


@pytest.fixture(autouse=True)
def _scope():
    toks = [
        _verified_caller_is_admin.set(False),
        _verified_caller_email.set(""),
        _verified_patient_id.set(""),
        _verified_auth_method.set(""),
    ]
    yield
    for var, tok in zip(
        (
            _verified_caller_is_admin,
            _verified_caller_email,
            _verified_patient_id,
            _verified_auth_method,
        ),
        toks,
        strict=True,
    ):
        var.reset(tok)


def _as(*, admin=False, email="", bound=OWN_UUID, method=""):
    """Set the caller identity the way verify_token + middleware would."""
    _verified_caller_is_admin.set(admin)
    _verified_caller_email.set(email)
    _verified_auth_method.set(method)
    _current_patient_id.set(bound)


def _no_foreign(raw: str) -> None:
    for needle in (FOREIGN_UUID, FOREIGN_SLUG, FOREIGN_NAME, FOREIGN_EMAIL):
        assert needle not in raw, f"foreign identifier {needle!r} leaked: {raw[:300]}"


async def _error_of(coro) -> str:
    with pytest.raises(ValueError) as exc_info:
        await coro
    return str(exc_info.value)


# ── _resolve_patient_id: no existence oracle ─────────────────────────────


async def test_resolver_unknown_and_foreign_slug_messages_identical(pdb):
    _as(bound=OWN_UUID)
    ctx = _Ctx(pdb)
    unknown = await _error_of(_resolve_patient_id(UNKNOWN_SLUG, ctx))
    foreign = await _error_of(_resolve_patient_id(FOREIGN_SLUG, ctx))
    assert unknown.replace(UNKNOWN_SLUG, "X") == foreign.replace(FOREIGN_SLUG, "X")
    for needle in (FOREIGN_UUID, FOREIGN_NAME, FOREIGN_EMAIL):
        assert needle not in foreign


async def test_resolver_oauth_caller_foreign_slug_matches_unknown(pdb):
    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    ctx = _Ctx(pdb)
    unknown = await _error_of(_resolve_patient_id(UNKNOWN_SLUG, ctx))
    foreign = await _error_of(_resolve_patient_id(FOREIGN_SLUG, ctx))
    assert unknown.replace(UNKNOWN_SLUG, "X") == foreign.replace(FOREIGN_SLUG, "X")


# ── list_patients ────────────────────────────────────────────────────────


async def _listed_slugs(pdb) -> set[str]:
    from oncofiles.tools.patient import list_patients

    raw = await list_patients(_Ctx(pdb))
    data = json.loads(raw)
    if isinstance(data, dict):
        assert data["patients"] == []
        _no_foreign(raw)
        return set()
    return {p["slug"] for p in data}


async def test_list_patients_oauth_admin_without_caregiver_binding_gets_empty(pdb):
    _as(admin=True, email=ADMIN_EMAIL, bound=NO_PATIENT_ACCESS_SENTINEL, method="oauth")
    assert await _listed_slugs(pdb) == set()


async def test_list_patients_oauth_admin_sees_only_own_caregiver_patients(pdb):
    _as(admin=True, email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    assert await _listed_slugs(pdb) == {OWN_SLUG}


async def test_list_patients_oauth_non_admin_sees_only_own(pdb):
    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    assert await _listed_slugs(pdb) == {OWN_SLUG}


async def test_list_patients_second_caregiver_in_list_sees_patient(pdb):
    await pdb.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?",
        (f"{FOREIGN_EMAIL}, {OWN_EMAIL}", FOREIGN_UUID),
    )
    await pdb.db.commit()
    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    assert await _listed_slugs(pdb) == {OWN_SLUG, FOREIGN_SLUG}


async def test_list_patients_patient_bearer_sees_only_bound(pdb):
    _as(bound=OWN_UUID, method="patient_bearer")
    assert await _listed_slugs(pdb) == {OWN_SLUG}


async def test_list_patients_sentinel_sees_nothing(pdb):
    _as(bound=NO_PATIENT_ACCESS_SENTINEL, method="oauth", email="stranger@example.com")
    assert await _listed_slugs(pdb) == set()


async def test_list_patients_static_operator_bearer_keeps_full_list(pdb):
    """Server-to-server exception: Oncoteam's registry sync needs every patient."""
    _as(admin=True, bound=NO_PATIENT_ACCESS_SENTINEL, method="static_bearer")
    assert await _listed_slugs(pdb) == {OWN_SLUG, FOREIGN_SLUG}


# ── select_patient ───────────────────────────────────────────────────────


async def test_select_patient_foreign_unknown_and_uuid_indistinguishable(pdb):
    from oncofiles.tools.patient import select_patient

    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    ctx = _Ctx(pdb)
    unknown = json.loads(await select_patient(ctx, UNKNOWN_SLUG))["error"]
    foreign = json.loads(await select_patient(ctx, FOREIGN_SLUG))["error"]
    raw_uuid = await select_patient(ctx, FOREIGN_UUID)
    by_uuid = json.loads(raw_uuid)["error"]
    assert unknown.replace(UNKNOWN_SLUG, "X") == foreign.replace(FOREIGN_SLUG, "X")
    assert unknown.replace(UNKNOWN_SLUG, "X") == by_uuid.replace(FOREIGN_UUID, "X")
    assert FOREIGN_NAME not in raw_uuid and FOREIGN_EMAIL not in raw_uuid
    # The request must not have been re-bound to the foreign patient.
    assert _verified_patient_id.get() != FOREIGN_UUID


async def test_select_patient_patient_bearer_cannot_switch_to_foreign(pdb):
    from oncofiles.tools.patient import select_patient

    _as(bound=OWN_UUID, method="patient_bearer")
    raw = await select_patient(_Ctx(pdb), FOREIGN_SLUG)
    assert "error" in json.loads(raw)
    _no_foreign(raw.replace(repr(FOREIGN_SLUG), ""))


async def test_select_patient_oauth_caregiver_persists_under_own_email(pdb):
    from oncofiles.tools.patient import select_patient

    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    result = json.loads(await select_patient(_Ctx(pdb), OWN_SLUG))
    assert result["status"] == "switched"
    assert result["persisted_across_requests"] is True
    assert await pdb.get_patient_selection(OWN_EMAIL) == OWN_UUID


async def test_select_patient_operator_never_writes_another_users_selection(pdb):
    """The static bearer may select any slug for its own request, but must
    not persist a selection keyed by some other user's email."""
    from oncofiles.tools.patient import select_patient

    _as(admin=True, bound=NO_PATIENT_ACCESS_SENTINEL, method="static_bearer")
    result = json.loads(await select_patient(_Ctx(pdb), FOREIGN_SLUG))
    assert result["status"] == "switched_single_request"
    assert result["persisted_across_requests"] is False
    async with pdb.db.execute("SELECT COUNT(*) AS n FROM patient_selection") as cur:
        row = await cur.fetchone()
    assert row["n"] == 0


# ── get_patient_context ──────────────────────────────────────────────────


async def test_get_patient_context_foreign_equals_unknown(pdb):
    from oncofiles.tools.patient import get_patient_context

    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    ctx = _Ctx(pdb)
    unknown = await get_patient_context(ctx, patient_slug=UNKNOWN_SLUG)
    foreign = await get_patient_context(ctx, patient_slug=FOREIGN_SLUG)
    assert unknown == foreign
    _no_foreign(foreign)


# ── integration_status / system_health ───────────────────────────────────


@pytest.mark.parametrize("tool_name", ["integration_status", "system_health"])
async def test_status_tools_foreign_equals_unknown(pdb, tool_name):
    from oncofiles.tools import hygiene, integrations

    tool = getattr(integrations, tool_name, None) or getattr(hygiene, tool_name)
    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    ctx = _Ctx(pdb)
    unknown = await _error_of(tool(ctx, patient_slug=UNKNOWN_SLUG))
    foreign = await _error_of(tool(ctx, patient_slug=FOREIGN_SLUG))
    assert unknown.replace(UNKNOWN_SLUG, "X") == foreign.replace(FOREIGN_SLUG, "X")
    for needle in (FOREIGN_UUID, FOREIGN_NAME, FOREIGN_EMAIL):
        assert needle not in foreign


# ── query_db ─────────────────────────────────────────────────────────────


async def test_query_db_foreign_slug_error_equals_unknown(pdb):
    from oncofiles.tools.db_query import query_db

    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    ctx = _Ctx(pdb)
    sql = "SELECT COUNT(*) AS n FROM schema_migrations"
    unknown = json.loads(await query_db(ctx, sql, patient_slug=UNKNOWN_SLUG))["error"]
    foreign = json.loads(await query_db(ctx, sql, patient_slug=FOREIGN_SLUG))["error"]
    assert unknown.replace(UNKNOWN_SLUG, "X") == foreign.replace(FOREIGN_SLUG, "X")


@pytest.mark.parametrize(
    "sql",
    [
        # OR defeats a filter that only has to appear somewhere in the tree.
        f"SELECT DISTINCT patient_id FROM documents WHERE patient_id = '{OWN_UUID}' OR 1=1",
        # A second, unfiltered reference to the same table.
        f"SELECT DISTINCT b.patient_id FROM documents a, documents b "
        f"WHERE a.patient_id = '{OWN_UUID}'",
        f"SELECT b.patient_id FROM documents a JOIN documents b ON 1=1 "
        f"WHERE a.patient_id = '{OWN_UUID}'",
        # Scalar sub-query over the whole table.
        f"SELECT (SELECT group_concat(patient_id) FROM documents) FROM documents "
        f"WHERE patient_id = '{OWN_UUID}'",
        # Sub-query in WHERE.
        f"SELECT id FROM documents WHERE patient_id = '{OWN_UUID}' "
        "AND id IN (SELECT id FROM documents)",
        # Negated equality.
        f"SELECT * FROM documents WHERE NOT (patient_id = '{OWN_UUID}')",
        # Unqualified filter is ambiguous with two tables in the SELECT.
        f"SELECT b.patient_id FROM documents a, documents b WHERE patient_id = '{OWN_UUID}'",
    ],
)
def test_query_db_enumeration_bypasses_rejected(sql):
    assert _validate_query(sql, OWN_UUID) is not None, sql


@pytest.mark.parametrize(
    "sql",
    [
        f"SELECT * FROM documents WHERE (patient_id = '{OWN_UUID}') AND (id > 0)",
        f"SELECT d.id FROM documents d JOIN document_pages p ON p.document_id = d.id "
        f"AND p.patient_id = '{OWN_UUID}' WHERE d.patient_id = '{OWN_UUID}'",
        f"SELECT COUNT(*) FROM (SELECT id FROM documents WHERE patient_id = '{OWN_UUID}')",
        f"SELECT id FROM documents WHERE patient_id = '{OWN_UUID}' AND id IN "
        f"(SELECT document_id FROM document_pages WHERE patient_id = '{OWN_UUID}')",
    ],
)
def test_query_db_properly_scoped_queries_still_pass(sql):
    assert _validate_query(sql, OWN_UUID) is None, sql


async def test_query_db_or_bypass_returns_no_foreign_rows_end_to_end(pdb):
    from oncofiles.tools.db_query import query_db

    for pid, fid in ((OWN_UUID, "file_own_590"), (FOREIGN_UUID, "file_foreign_590")):
        await pdb.insert_document(
            Document(
                file_id=fid,
                filename=f"{fid}.pdf",
                original_filename=f"{fid}.pdf",
                document_date=None,
                institution=None,
                category=DocumentCategory.OTHER,
                description="synthetic",
                mime_type="application/pdf",
                size_bytes=1,
            ),
            patient_id=pid,
        )
    _as(email=OWN_EMAIL, bound=OWN_UUID, method="oauth")
    raw = await query_db(
        _Ctx(pdb),
        f"SELECT DISTINCT patient_id FROM documents WHERE patient_id = '{OWN_UUID}' OR 1=1",
    )
    assert "error" in json.loads(raw)
    _no_foreign(raw)
