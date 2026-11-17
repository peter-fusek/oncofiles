"""#531: the dashboard patient ACL fails CLOSED.

`_get_dashboard_patient_id` used to return the requested/resolved patient id
when the authorization lookup raised (`resolve_patient_id`, `list_patients`,
caregiver match), and the raw value when nothing matched (which let an
archived patient's UUID skip the ACL entirely). Every such branch must now
return `NO_PATIENT_ACCESS_SENTINEL`, so the downstream `WHERE patient_id = ?`
matches zero rows of the target patient.

Synthetic patients, e-mails (example.test) and invented filenames only.
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap
from contextlib import contextmanager
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import server
from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
from oncofiles.database import Database
from tests.helpers import ERIKA_UUID, make_doc

A_UUID = ERIKA_UUID
B_UUID = "00000000-0000-4000-8000-0000000005b1"
C_UUID = "00000000-0000-4000-8000-0000000005c1"  # archived, caregiver B
A_EMAIL = "carer-a@example.test"
B_EMAIL = "carer-b@example.test"
NOBODY_EMAIL = "stranger@example.test"


@pytest.fixture
async def acl_db(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("acl-a", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "acl-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email, is_active) "
        "VALUES (?, ?, ?, ?, 0)",
        (C_UUID, "acl-c", "Cyril Synthetic", B_EMAIL),
    )
    await db.db.commit()
    await db.insert_document(
        make_doc(
            file_id="acl-a-1",
            filename="20270311_A_SynthClinic_Labs_AclPatientA.pdf",
            document_date=date(2027, 3, 11),
        ),
        patient_id=A_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="acl-b-1",
            filename="20270422_B_SynthClinic_Labs_AclPatientB.pdf",
            document_date=date(2027, 4, 22),
        ),
        patient_id=B_UUID,
    )
    return db


def _request(db: Database, patient_id: str | None = None) -> MagicMock:
    request = MagicMock()
    request.query_params = {"patient_id": patient_id} if patient_id is not None else {}
    request.path_params = {}
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    return request


@contextmanager
def _caller(email: str | None, *, bearer: bool = False, admin: bool = False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=None if bearer else unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


# ── Healthy DB: the ACL itself ───────────────────────────────────────────


async def test_caregiver_gets_own_patient(acl_db):
    with _caller(B_EMAIL):
        assert await server._get_dashboard_patient_id(_request(acl_db, "acl-b")) == B_UUID


async def test_foreign_patient_falls_back_to_own(acl_db):
    with _caller(A_EMAIL):
        assert await server._get_dashboard_patient_id(_request(acl_db, B_UUID)) == A_UUID


async def test_stranger_gets_sentinel(acl_db):
    with _caller(NOBODY_EMAIL):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, "acl-b"))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_admin_may_read_any_patient(acl_db):
    with _caller(NOBODY_EMAIL, admin=True):
        assert await server._get_dashboard_patient_id(_request(acl_db, "acl-b")) == B_UUID


async def test_bearer_bypasses_scoping(acl_db):
    with _caller(None, bearer=True):
        assert await server._get_dashboard_patient_id(_request(acl_db, "acl-b")) == B_UUID


async def test_unknown_patient_is_sentinel_not_raw(acl_db):
    unknown = "00000000-0000-4000-8000-0000000005ff"
    # #867/#590: unknown behaves like foreign — own patient, never the raw value.
    with _caller(A_EMAIL):
        assert await server._get_dashboard_patient_id(_request(acl_db, unknown)) == A_UUID
    with _caller(NOBODY_EMAIL):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, unknown))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_archived_patient_goes_through_acl(acl_db):
    """An archived UUID used to be returned raw, with no caregiver check."""
    with _caller(A_EMAIL):
        assert await server._get_dashboard_patient_id(_request(acl_db, C_UUID)) == A_UUID
    with _caller(NOBODY_EMAIL):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, C_UUID))
            == NO_PATIENT_ACCESS_SENTINEL
        )
    # The archived patient's own caregiver still reaches it.
    with _caller(B_EMAIL):
        assert await server._get_dashboard_patient_id(_request(acl_db, C_UUID)) == C_UUID


async def test_no_identity_is_sentinel(acl_db):
    """Neither bearer nor session: never a default patient."""
    with _caller(None):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, "acl-b"))
            == NO_PATIENT_ACCESS_SENTINEL
        )
        assert (
            await server._get_dashboard_patient_id(_request(acl_db)) == NO_PATIENT_ACCESS_SENTINEL
        )


# ── Error branches: every one returns the sentinel ──────────────────────


async def test_resolve_patient_id_raises(acl_db):
    with (
        _caller(A_EMAIL),
        patch.object(acl_db, "resolve_patient_id", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, B_UUID))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_archived_fallback_lookup_raises(acl_db):
    calls = AsyncMock(side_effect=[None, RuntimeError("db")])
    with _caller(A_EMAIL), patch.object(acl_db, "resolve_patient_id", calls):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, C_UUID))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_list_patients_raises(acl_db):
    with (
        _caller(A_EMAIL),
        patch.object(acl_db, "list_patients", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, B_UUID))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_caregiver_match_raises(acl_db):
    with (
        _caller(B_EMAIL),
        patch.object(server, "_email_matches_caregiver", side_effect=RuntimeError("boom")),
    ):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, B_UUID))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_admin_check_raises(acl_db):
    with (
        _caller(A_EMAIL),
        patch.object(server, "_is_admin_email", side_effect=RuntimeError("boom")),
    ):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, B_UUID))
            == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_default_patient_lookup_raises(acl_db):
    with (
        _caller(A_EMAIL),
        patch.object(acl_db, "list_patients", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db)) == NO_PATIENT_ACCESS_SENTINEL
        )


async def test_acl_logs_hashed_caller_not_email(acl_db, caplog):
    caplog.set_level("WARNING", logger="oncofiles.server")
    with (
        _caller(A_EMAIL),
        patch.object(acl_db, "list_patients", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        await server._get_dashboard_patient_id(_request(acl_db, B_UUID))
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "dashboard patient ACL" in text
    assert A_EMAIL not in text
    assert B_UUID not in text


# ── Endpoints: zero rows of the target patient on an ACL error ──────────


async def test_api_manifest_returns_no_target_rows_when_acl_errors(acl_db):
    with (
        _caller(A_EMAIL),
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(acl_db, "list_patients", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        resp = await server.api_manifest(_request(acl_db, B_UUID))
    body = json.loads(resp.body)
    assert body.get("documents", []) == []
    assert "acl-b-1" not in resp.body.decode()


async def test_api_documents_returns_no_target_rows_when_acl_errors(acl_db):
    with (
        _caller(A_EMAIL),
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(acl_db, "list_patients", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        resp = await server.api_documents(_request(acl_db, B_UUID))
    assert "AclPatientB" not in resp.body.decode()


async def test_api_doc_detail_hides_target_document_when_acl_errors(acl_db):
    async with acl_db.db.execute("SELECT id FROM documents WHERE file_id = 'acl-b-1'") as cur:
        doc_id = (await cur.fetchone())[0]
    request = _request(acl_db, B_UUID)
    request.path_params = {"doc_id": str(doc_id)}
    with (
        _caller(A_EMAIL),
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch.object(acl_db, "resolve_patient_id", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        resp = await server.api_doc_detail(request)
    assert resp.status_code == 404
    assert "AclPatientB" not in resp.body.decode()


# ── Lock tests (AST) ─────────────────────────────────────────────────────


def _function_ast(fn) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(fn)))


def test_every_except_in_dashboard_helper_returns_sentinel():
    """No `except` block may return or assign the requested/resolved pid.

    Each handler must end in `return NO_PATIENT_ACCESS_SENTINEL`, and nothing
    inside it may return another value or (re)bind a variable.
    """
    tree = _function_ast(server._get_dashboard_patient_id)
    handlers = [n for n in ast.walk(tree) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "expected the helper to guard its DB calls"
    for h in handlers:
        last = h.body[-1]
        assert isinstance(last, ast.Return), f"except at line {h.lineno} does not return"
        for node in ast.walk(h):
            if isinstance(node, ast.Return):
                assert isinstance(node.value, ast.Name), f"line {node.lineno}"
                assert node.value.id == "NO_PATIENT_ACCESS_SENTINEL", f"line {node.lineno}"
            if isinstance(node, ast.Assign | ast.AugAssign | ast.AnnAssign | ast.NamedExpr):
                pytest.fail(f"except at line {h.lineno} assigns a variable")


def test_dashboard_helper_never_returns_raw_query_value():
    tree = _function_ast(server._get_dashboard_patient_id)
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
            assert node.value.id != "raw", f"`return raw` at line {node.lineno}"


def test_mcp_resolver_swallows_no_exception():
    """`_resolve_patient_id` has no try/except: a DB error propagates, never grants."""
    from oncofiles.tools._helpers import _resolve_patient_id

    tree = _function_ast(_resolve_patient_id)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Try | ast.TryStar)]


async def test_mcp_resolver_db_error_does_not_grant(acl_db):
    from oncofiles.tools._helpers import _resolve_patient_id

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": acl_db}
    with (
        patch.object(acl_db, "get_patient_by_slug", AsyncMock(side_effect=RuntimeError("db"))),
        pytest.raises(RuntimeError),
    ):
        await _resolve_patient_id("acl-b", ctx)
