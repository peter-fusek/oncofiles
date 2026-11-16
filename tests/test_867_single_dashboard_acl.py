"""#867: one ACL path for every dashboard route that reads a patient.

Before #867, ``/api/patient-context`` and ``/api/patient-budget`` had their own
copy of the patient check (raw value, no slug resolution), ``/api/doc-detail``
loaded a document by id without a patient filter in SQL, the body routes and
``/oauth/authorize`` passed an unknown patient value on
(``resolve_patient_id(raw) or raw``), and ``_require_body_patient_access``
trusted any non-session ``Bearer`` header because ``_check_dashboard_auth``
happened to run first. Each was safe only because of another check.

Now: GET routes go through ``_get_dashboard_patient_id`` (or its refusing
variant ``_dashboard_patient_or_refusal``), body routes through
``_dashboard_body_patient``, and the AST lock below keeps it that way.

Synthetic patients, e-mails (example.test) and invented filenames only.
"""

from __future__ import annotations

import ast
import inspect
import json
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
B_UUID = "00000000-0000-4000-8000-0000000008b7"
C_UUID = "00000000-0000-4000-8000-0000000008c7"  # archived, caregiver B
A_EMAIL = "carer-a@example.test"
B_EMAIL = "carer-b@example.test"
NOBODY_EMAIL = "stranger@example.test"


@pytest.fixture
async def acl_db(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("acl867-a", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "acl867-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email, is_active) "
        "VALUES (?, ?, ?, ?, 0)",
        (C_UUID, "acl867-c", "Cyril Synthetic", B_EMAIL),
    )
    await db.db.commit()
    await db.insert_document(
        make_doc(
            file_id="acl867-a-1",
            filename="20270311_A_SynthClinic_Labs_AclDetailA.pdf",
            document_date=date(2027, 3, 11),
        ),
        patient_id=A_UUID,
    )
    await db.insert_document(
        make_doc(
            file_id="acl867-b-1",
            filename="20270422_B_SynthClinic_Labs_AclDetailB.pdf",
            document_date=date(2027, 4, 22),
        ),
        patient_id=B_UUID,
    )
    return db


def _request(
    db: Database,
    patient_id: str | None = None,
    *,
    body: dict | None = None,
    auth: str = "Bearer session:synthetic",
) -> MagicMock:
    request = MagicMock()
    request.query_params = {"patient_id": patient_id} if patient_id is not None else {}
    request.path_params = {}
    request.headers = {"authorization": auth}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    request.json = AsyncMock(return_value=body or {})
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


async def _doc_id(db: Database, file_id: str) -> int:
    async with db.db.execute("SELECT id FROM documents WHERE file_id = ?", (file_id,)) as cur:
        return (await cur.fetchone())[0]


# ── /api/patient-context and /api/patient-budget ─────────────────────────


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
async def test_caregiver_reads_own_patient_by_slug(acl_db, route):
    """Slugs now resolve (before #867 these routes used the raw value)."""
    with _caller(B_EMAIL):
        resp = await getattr(server, route)(_request(acl_db, "acl867-b"))
    assert resp.status_code == 200
    if route == "api_patient_context":
        assert json.loads(resp.body)["patient_id"] == B_UUID


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
@pytest.mark.parametrize("requested", [B_UUID, "acl867-b", "no-such-patient"])
async def test_foreign_or_unknown_patient_is_404_not_own_fallback(acl_db, route, requested):
    with _caller(A_EMAIL):
        resp = await getattr(server, route)(_request(acl_db, requested))
    assert resp.status_code == 404
    assert json.loads(resp.body) == {"error": "not_found"}
    assert A_UUID not in resp.body.decode()


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
async def test_no_patient_named_uses_own_first_patient(acl_db, route):
    with _caller(A_EMAIL):
        resp = await getattr(server, route)(_request(acl_db))
    assert resp.status_code == 200


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
async def test_stranger_without_patient_gets_400(acl_db, route):
    with _caller(NOBODY_EMAIL):
        resp = await getattr(server, route)(_request(acl_db))
    assert resp.status_code == 400


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
async def test_admin_and_bearer_may_read_named_patient(acl_db, route):
    with _caller(NOBODY_EMAIL, admin=True):
        resp = await getattr(server, route)(_request(acl_db, "acl867-b"))
    assert resp.status_code == 200
    with _caller(None, bearer=True):
        resp = await getattr(server, route)(_request(acl_db, B_UUID, auth="Bearer op"))
    assert resp.status_code == 200


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
async def test_acl_db_error_refuses(acl_db, route):
    """Fail closed (#531) — the not-found answer; a typed 503 result is a follow-up."""
    with (
        _caller(A_EMAIL),
        patch.object(acl_db, "list_patients", AsyncMock(side_effect=RuntimeError("db"))),
    ):
        resp = await getattr(server, route)(_request(acl_db, B_UUID))
    assert resp.status_code == 404


async def test_named_patient_refusal_variant_never_falls_back(acl_db):
    with _caller(A_EMAIL):
        assert (
            await server._get_dashboard_patient_id(_request(acl_db, B_UUID), fallback_to_own=False)
            == NO_PATIENT_ACCESS_SENTINEL
        )
        # The default keeps the existing contract for list routes.
        assert await server._get_dashboard_patient_id(_request(acl_db, B_UUID)) == A_UUID


# ── /api/doc-detail: SQL-scoped ──────────────────────────────────────────


async def test_doc_detail_own_document(acl_db):
    request = _request(acl_db, "acl867-a")
    request.path_params = {"doc_id": str(await _doc_id(acl_db, "acl867-a-1"))}
    with _caller(A_EMAIL):
        resp = await server.api_doc_detail(request)
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["filename"] == "20270311_A_SynthClinic_Labs_AclDetailA.pdf"
    assert body["document_date"] == "2027-03-11"
    assert body["category"] == "labs"


@pytest.mark.parametrize("requested", [None, "acl867-b", B_UUID])
async def test_doc_detail_foreign_document_is_404(acl_db, requested):
    request = _request(acl_db, requested)
    request.path_params = {"doc_id": str(await _doc_id(acl_db, "acl867-b-1"))}
    with _caller(A_EMAIL):
        resp = await server.api_doc_detail(request)
    assert resp.status_code == 404
    assert "AclDetailB" not in resp.body.decode()


async def test_doc_detail_soft_deleted_is_404(acl_db):
    doc_id = await _doc_id(acl_db, "acl867-a-1")
    await acl_db.db.execute(
        "UPDATE documents SET deleted_at = '2027-05-01T00:00:00' WHERE id = ?", (doc_id,)
    )
    await acl_db.db.commit()
    request = _request(acl_db, "acl867-a")
    request.path_params = {"doc_id": str(doc_id)}
    with _caller(A_EMAIL):
        resp = await server.api_doc_detail(request)
    assert resp.status_code == 404


async def test_doc_detail_admin_reads_named_patient(acl_db):
    request = _request(acl_db, "acl867-b")
    request.path_params = {"doc_id": str(await _doc_id(acl_db, "acl867-b-1"))}
    with _caller(NOBODY_EMAIL, admin=True):
        resp = await server.api_doc_detail(request)
    assert resp.status_code == 200


def test_doc_detail_uses_scoped_get_document():
    src = inspect.getsource(server.api_doc_detail)
    assert "get_document(doc_id, patient_id=patient_id)" in src
    assert "FROM documents WHERE id = ?" not in src


# ── Body routes: unknown value refused at resolution ─────────────────────


async def test_body_helper_unknown_patient_is_404_before_acl(acl_db):
    acl = AsyncMock(return_value=None)
    with _caller(None, bearer=True), patch.object(server, "_require_body_patient_access", acl):
        pid, err = await server._dashboard_body_patient(
            _request(acl_db, auth="Bearer op"), acl_db, "no-such-patient"
        )
    assert pid is None and err is not None and err.status_code == 404
    acl.assert_not_called()


async def test_body_helper_resolves_slug_and_archived_uuid(acl_db):
    with _caller(B_EMAIL):
        assert await server._dashboard_body_patient(_request(acl_db), acl_db, "acl867-b") == (
            B_UUID,
            None,
        )
        # Archived: by exact UUID as before #867; its slug still does not resolve.
        assert await server._dashboard_body_patient(_request(acl_db), acl_db, C_UUID) == (
            C_UUID,
            None,
        )
        pid, err = await server._dashboard_body_patient(_request(acl_db), acl_db, "acl867-c")
    assert pid is None and err.status_code == 404


async def test_body_helper_foreign_patient_is_not_found(acl_db):
    with _caller(A_EMAIL):
        pid, err = await server._dashboard_body_patient(_request(acl_db), acl_db, "acl867-b")
    assert pid is None and err.status_code == 404


async def test_patient_tokens_unknown_patient_mints_nothing(acl_db):
    create = AsyncMock()
    with (
        _caller(None, bearer=True),
        patch.object(acl_db, "create_patient_token", create),
        patch.object(server, "_check_rate_limit", return_value=None),
    ):
        resp = await server.api_create_patient_token(
            _request(acl_db, body={"patient_id": "no-such-patient"}, auth="Bearer op")
        )
    assert resp.status_code == 404
    create.assert_not_called()


async def test_require_body_access_checks_auth_itself(acl_db):
    """No longer relies on `_check_dashboard_auth` having run first."""
    # A non-session Bearer that is NOT the operator token.
    with _caller(None):
        err = await server._require_body_patient_access(
            _request(acl_db, auth="Bearer wrong-token"), acl_db, A_UUID
        )
    assert err is not None and err.status_code == 401
    # No identity at all.
    with _caller(None):
        err = await server._require_body_patient_access(_request(acl_db, auth=""), acl_db, A_UUID)
    assert err is not None and err.status_code == 401


async def test_oauth_authorize_unknown_patient_is_404(acl_db, monkeypatch):
    monkeypatch.setattr(server, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    request = _request(acl_db, "no-such-patient", auth="Bearer op")
    request.path_params = {"service": "drive"}
    with (
        _caller(None, bearer=True),
        patch.object(server, "_check_rate_limit", return_value=None),
        patch.object(acl_db, "get_patient", AsyncMock(wraps=acl_db.get_patient)) as get_patient,
    ):
        resp = await server.oauth_authorize(request)
    assert resp.status_code == 404
    # The raw value is looked up once during resolution, never passed on.
    assert get_patient.await_count <= 1


# ── Lock: one ACL path for every dashboard route ─────────────────────────

# The single helpers. A route that reads a patient must call one of them.
SINGLE_HELPERS = {
    "_get_dashboard_patient_id",
    "_dashboard_patient_or_refusal",
    "_dashboard_body_patient",
}

# Routes that read a patient without the dashboard helpers, each with its reason.
# This list may only shrink.
ALLOWLIST = {
    # Lists the caller's own patients (caregiver match per row), no target patient.
    "api_list_patients",
    # Creates a new patient; the caller becomes its caregiver.
    "api_create_patient",
    # Path-param write with a stricter rule: caregiver only, admins do NOT
    # bypass. Unifying it would widen admin writes (separate decision).
    "api_update_patient",
    # Opened in a new tab: its session is the cookie, not the Bearer header the
    # dashboard helpers read. It resolves via `_resolve_body_patient_id`
    # (unknown → 404) and runs its own caregiver/admin/operator check.
    "oauth_authorize",
    # The patient comes from the HMAC-signed state minted by oauth_authorize,
    # not from the request; the caregiver check decides re-link privilege (#591).
    "oauth_callback",
    # Operator-only readiness probe.
    "readiness",
}


def _custom_routes() -> list[ast.AsyncFunctionDef]:
    tree = ast.parse(inspect.getsource(server))
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "custom_route"
            ):
                routes.append(node)
    return routes


def _called_names(fn: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _reads_patient(fn: ast.AST) -> bool:
    for node in ast.walk(fn):
        # body.get("patient_id") / query_params.get("patient_id")
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in ("patient_id", "patient_slug")
        ):
            return True
        # path_params["patient_id"]
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == "patient_id"
        ):
            return True
    return False


def test_lock_found_the_routes():
    names = {r.name for r in _custom_routes()}
    assert {"api_patient_context", "api_patient_budget", "api_doc_detail"} <= names
    assert {"api_create_patient_token", "oauth_authorize"} <= names


def test_every_patient_reading_route_uses_the_single_helper():
    offenders = []
    for route in _custom_routes():
        if route.name in ALLOWLIST or not _reads_patient(route):
            continue
        called = _called_names(route)
        if "_require_admin" in called:
            # Admin-only operator routes (`_require_admin` + `_admin_patient_id`).
            continue
        if not called & SINGLE_HELPERS:
            offenders.append(route.name)
    assert not offenders, f"routes reading a patient outside the single ACL path: {offenders}"


def test_no_route_reimplements_the_caregiver_check():
    offenders = [
        r.name
        for r in _custom_routes()
        if r.name not in ALLOWLIST and "_email_matches_caregiver" in _called_names(r)
    ]
    assert not offenders, offenders


def test_no_unknown_patient_value_is_passed_on():
    """`resolve_patient_id(raw) or raw` is gone from server.py."""
    tree = ast.parse(inspect.getsource(server))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        first = node.values[0]
        if isinstance(first, ast.Await):
            first = first.value
        if (
            isinstance(first, ast.Call)
            and isinstance(first.func, ast.Attribute)
            and first.func.attr == "resolve_patient_id"
        ):
            pytest.fail(f"`resolve_patient_id(...) or ...` at server.py line {node.lineno}")


def test_body_helper_runs_auth_check_itself():
    for fn in (server._require_body_patient_access, server._dashboard_body_patient):
        assert "_check_dashboard_auth" in _called_names(ast.parse(inspect.getsource(fn)))


# ── No existence oracle (#590): unknown and foreign answer byte-identically ──

UNKNOWN_SLUG = "acl867-nobody"
UNKNOWN_UUID = "00000000-0000-4000-8000-0000000008ff"
PAIRS = [(UNKNOWN_SLUG, "acl867-b"), (UNKNOWN_UUID, B_UUID)]


def _assert_identical(r1, r2):
    """Same status, same body bytes, same headers (content-type included)."""
    assert r1.status_code == r2.status_code == 404
    assert r1.body == r2.body
    assert r1.headers.get("content-type") == r2.headers.get("content-type")
    assert sorted(r1.raw_headers) == sorted(r2.raw_headers)


@pytest.mark.parametrize("route", ["api_patient_context", "api_patient_budget"])
@pytest.mark.parametrize(("unknown", "foreign"), PAIRS)
async def test_oracle_context_and_budget(acl_db, route, unknown, foreign):
    with _caller(A_EMAIL):
        r_unknown = await getattr(server, route)(_request(acl_db, unknown))
        r_foreign = await getattr(server, route)(_request(acl_db, foreign))
    _assert_identical(r_unknown, r_foreign)


@pytest.mark.parametrize(("unknown", "foreign"), PAIRS)
async def test_oracle_doc_detail_named_patient(acl_db, unknown, foreign):
    """The caller's OWN document under an unknown vs a foreign ?patient_id=."""
    own_doc = str(await _doc_id(acl_db, "acl867-a-1"))
    responses = []
    for requested in (unknown, foreign):
        request = _request(acl_db, requested)
        request.path_params = {"doc_id": own_doc}
        with _caller(A_EMAIL):
            responses.append(await server.api_doc_detail(request))
    _assert_identical(*responses)


async def test_oracle_doc_detail_document_id(acl_db):
    """A foreign document id vs an id that does not exist."""
    foreign_doc = await _doc_id(acl_db, "acl867-b-1")
    responses = []
    for doc_id in (foreign_doc, 999_999):
        request = _request(acl_db, "acl867-a")
        request.path_params = {"doc_id": str(doc_id)}
        with _caller(A_EMAIL):
            responses.append(await server.api_doc_detail(request))
    _assert_identical(*responses)


async def test_oracle_body_helper(acl_db):
    with _caller(A_EMAIL):
        _, r_unknown = await server._dashboard_body_patient(_request(acl_db), acl_db, UNKNOWN_SLUG)
        _, r_foreign = await server._dashboard_body_patient(_request(acl_db), acl_db, "acl867-b")
    _assert_identical(r_unknown, r_foreign)


@pytest.mark.parametrize(
    "route",
    [
        "api_create_patient_token",
        "api_create_share_link",
        "api_sync_trigger",
        "api_enhance_trigger",
        "api_gdrive_set_folder",
    ],
)
@pytest.mark.parametrize(("unknown", "foreign"), PAIRS)
async def test_oracle_body_routes(acl_db, monkeypatch, route, unknown, foreign):
    # Let the #852 gates through so the patient check itself is compared.
    monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", "open")
    responses = []
    for requested in (unknown, foreign):
        body = {"patient_id": requested, "folder_id": "synthetic-folder-867"}
        with (
            _caller(A_EMAIL),
            patch.object(server, "_check_rate_limit", return_value=None),
            patch.object(server, "_dashboard_legacy_refusal", return_value=None),
            patch.object(server, "_dashboard_onboarding_refusal", return_value=None),
        ):
            responses.append(await getattr(server, route)(_request(acl_db, body=body)))
    _assert_identical(*responses)
    assert json.loads(responses[0].body) == {"error": "not_found"}


def _oauth_request(db, token: str, requested: str):
    from types import SimpleNamespace

    return SimpleNamespace(
        headers={},
        cookies={"oncofiles_session": token},
        query_params={"patient_id": requested},
        path_params={"service": "drive"},
        client=SimpleNamespace(host="203.0.113.7"),
        app=SimpleNamespace(
            state=SimpleNamespace(fastmcp_server=SimpleNamespace(_lifespan_result={"db": db}))
        ),
    )


@pytest.mark.parametrize(("unknown", "foreign"), PAIRS)
async def test_oracle_oauth_authorize(acl_db, monkeypatch, unknown, foreign):
    """A non-admin dashboard session (cookie) starting a Google link."""
    for mod in ("oncofiles.config", "oncofiles.oauth", "oncofiles.server"):
        monkeypatch.setattr(f"{mod}.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr("oncofiles.config.MCP_TRANSPORT", "streamable-http")
    for mod in ("oncofiles.server", "oncofiles.oauth", "oncofiles.config"):
        monkeypatch.setattr(f"{mod}.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", "open")
    token = server._make_session_token(A_EMAIL)
    responses = []
    with (
        patch.object(server, "_check_rate_limit", return_value=None),
        patch.object(server, "_is_admin_email", return_value=False),
    ):
        for requested in (unknown, foreign):
            responses.append(await server.oauth_authorize(_oauth_request(acl_db, token, requested)))
        own = await server.oauth_authorize(_oauth_request(acl_db, token, "acl867-a"))
    _assert_identical(*responses)
    # Sanity: the caller's own patient still starts the link.
    assert own.status_code in (302, 307)
