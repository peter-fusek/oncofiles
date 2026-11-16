"""Server-side onboarding gate (#852).

Lock tests (AST): every dashboard route and every MCP tool that creates a
patient, starts a Google link, sets a Drive folder or triggers the legacy Drive
sync / rename / reconcile / enhance path calls the gate. The tools are
discovered from the running server (``mcp.list_tools()``), by name and by what
their body references, so a new entry point fails here until it is gated or
listed in ``EXEMPT_TOOLS`` with a reason.

Behaviour tests: closed / invite / open x admin / invited / not invited, legacy
triggers admin-only in every mode, an existing caregiver keeps working, an
unknown mode fails closed, /health and /api/onboarding-status never expose the
invite list.

Synthetic values only: example.test addresses, invented slugs, years 2027+.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import re
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from oncofiles import onboarding_gate
from oncofiles.models import OAuthToken
from tests.helpers import ERIKA_UUID

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

ADMIN = "admin@example.test"
INVITED = "Invited.Carer@example.test"
STRANGER = "stranger@example.test"
CARER = "carer@example.test"

# ── lock: discovery ──────────────────────────────────────────────────────

# Referencing any of these from a route/tool body means it creates a patient,
# starts or completes a Google link, stores a folder, or runs a legacy Drive
# write / sync / enhance path.
SENSITIVE_REFS = {
    "insert_patient",
    "get_auth_url",
    "get_auth_url_for_scopes",
    "_make_state_token",
    "store_google_link",
    "update_oauth_folder",
    "connect_folder",  # #848: folder writes go through drive_folder_link.connect_folder
    "sync",
    "_sync",
    "sync_from_gdrive",
    "_sync_from_gdrive",
    "sync_to_gdrive",
    "_sync_to_gdrive",
    "enhance_documents",
    "_enhance_documents",
    "create_folder",
    "rename_file",
    "move_file",
    "trash_file",
    "grant_access_recursive",
    "_rename_to_standard",
    "ensure_folder_structure",
}

# Tool names that look like an onboarding / legacy entry point.
SENSITIVE_NAME = re.compile(
    r"(sync|reconcile|rename|setup_gdrive|set_folder|auth_url|auth_callback|auth_enable"
    r"|fix_permissions|repair_gdrive|consolidate_external|create_patient)"
)

GATE_CALLS = {
    "mcp_onboarding_refusal",
    "mcp_legacy_refusal",
    "mcp_google_connect_refusal",
}

# Matched by the discovery above but deliberately not gated. May only shrink.
EXEMPT_TOOLS = {
    "gdrive_sync_status": "read-only status of the last legacy sync",
    "upload_document": "existing caregivers' upload; with cloud AI off it writes into the "
    "patient's already-connected Drive (curated folders are write-blocked). Connecting "
    "Drive is gated; making new folders curated-only is #853",
}

# The entry points this PR gates. Discovery must find each of them, so a
# rename cannot silently drop one out of the lock.
EXPECTED_ONBOARDING_TOOLS = {
    "gdrive_auth_url",
    "gdrive_auth_callback",
    "gdrive_set_folder",
    "gmail_auth_enable",
    "calendar_auth_enable",
}
EXPECTED_LEGACY_TOOLS = {
    "gdrive_sync",
    "sync_from_gdrive",
    "sync_to_gdrive",
    "setup_gdrive",
    "gdrive_fix_permissions",
    "reconcile_gdrive",
    "repair_gdrive_folder_hygiene",
    "consolidate_external_gdrive_files",
    "rename_documents_to_standard",
    "enhance_documents",
    "detect_and_clone_vaccinations",
}

ROUTE_GATES = {
    "_dashboard_onboarding_refusal",
    "_dashboard_legacy_refusal",
    "onboarding_allowed",
    "onboarding_allowed_for_hash",
}
EXPECTED_ROUTES = {
    "api_create_patient": "_dashboard_onboarding_refusal",
    "api_gdrive_set_folder": "_dashboard_onboarding_refusal",
    "oauth_authorize": "onboarding_allowed",
    "oauth_callback": "onboarding_allowed",
    "api_sync_trigger": "_dashboard_legacy_refusal",
    "api_enhance_trigger": "_dashboard_legacy_refusal",
}


def _refs(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add(n.asname or n.name.split(".")[-1])
    return out


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _sensitive_tools() -> dict[str, ast.AST]:
    from oncofiles.server import mcp

    found: dict[str, ast.AST] = {}
    for tool in asyncio.run(mcp.list_tools()):
        fn = inspect.unwrap(tool.fn)
        node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        if _refs(node) & SENSITIVE_REFS or SENSITIVE_NAME.search(tool.name):
            found[tool.name] = node
    return found


def _custom_routes() -> dict[str, ast.AST]:
    tree = ast.parse((SRC / "server.py").read_text())
    routes = {}
    for node in tree.body:
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "custom_route"
            ):
                routes[node.name] = node
    return routes


def test_every_sensitive_tool_calls_the_gate():
    tools = _sensitive_tools()
    ungated = sorted(
        name
        for name, node in tools.items()
        if name not in EXEMPT_TOOLS and not (_calls(node) & GATE_CALLS)
    )
    assert not ungated, (
        f"MCP tools that create a patient, link Google, set a folder or run a legacy "
        f"Drive path without the #852 onboarding gate: {ungated}. Call "
        "onboarding_gate.mcp_onboarding_refusal / mcp_google_connect_refusal / "
        "mcp_legacy_refusal first, or add a reasoned EXEMPT_TOOLS entry."
    )


def test_expected_tools_are_discovered_and_gated_with_the_right_kind():
    tools = _sensitive_tools()
    missing = (EXPECTED_ONBOARDING_TOOLS | EXPECTED_LEGACY_TOOLS) - set(tools)
    assert not missing, f"discovery lost gated tools: {sorted(missing)}"
    for name in EXPECTED_LEGACY_TOOLS:
        assert "mcp_legacy_refusal" in _calls(tools[name]), name
    for name in EXPECTED_ONBOARDING_TOOLS:
        assert _calls(tools[name]) & {"mcp_onboarding_refusal", "mcp_google_connect_refusal"}, name


def test_exempt_tools_still_exist_and_are_few():
    tools = _sensitive_tools()
    stale = set(EXEMPT_TOOLS) - set(tools)
    assert not stale, f"remove stale EXEMPT_TOOLS entries: {sorted(stale)}"
    assert len(EXEMPT_TOOLS) <= 2, "EXEMPT_TOOLS may only shrink"


def test_every_sensitive_route_calls_the_gate():
    routes = _custom_routes()
    sensitive = {name for name, node in routes.items() if _refs(node) & SENSITIVE_REFS}
    assert set(EXPECTED_ROUTES) <= sensitive, set(EXPECTED_ROUTES) - sensitive
    ungated = sorted(name for name in sensitive if not (_calls(routes[name]) & ROUTE_GATES))
    assert not ungated, (
        f"dashboard routes that create a patient, link Google, set a folder or run "
        f"a legacy sync without the #852 gate: {ungated}"
    )
    for name, gate in EXPECTED_ROUTES.items():
        assert gate in _calls(routes[name]), (name, gate)


def test_route_gate_runs_before_any_db_or_drive_work():
    """The gate sits right after the auth check (only a rate limit may precede it)."""
    routes = _custom_routes()
    for name, gate in EXPECTED_ROUTES.items():
        if gate == "onboarding_allowed":
            continue  # OAuth routes need the patient first (existing link = re-consent)
        body = routes[name].body
        stmts = [
            s for s in body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
        ]
        first_calls = [_calls(s) for s in stmts[:4]]
        assert any(gate in c for c in first_calls), (name, first_calls)


# ── unit: modes ──────────────────────────────────────────────────────────


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr("oncofiles.config.ONBOARDING_INVITE_EMAILS", frozenset({INVITED.lower()}))
    monkeypatch.setattr(onboarding_gate, "_last_log", {})
    monkeypatch.setattr(onboarding_gate, "_suppressed", {})

    def set_mode(mode: str) -> None:
        monkeypatch.setattr("oncofiles.config.ONBOARDING_MODE", mode)

    return set_mode


@pytest.mark.parametrize(
    ("mode", "email", "is_admin", "allowed"),
    [
        ("closed", STRANGER, False, False),
        ("closed", INVITED, False, False),
        ("closed", ADMIN, True, True),
        ("invite", STRANGER, False, False),
        ("invite", INVITED, False, True),
        ("invite", INVITED.upper(), False, True),
        ("invite", None, False, False),
        ("invite", ADMIN, True, True),
        ("open", STRANGER, False, True),
        ("open", None, False, True),
        ("open", ADMIN, True, True),
    ],
)
def test_mode_matrix(gate, mode, email, is_admin, allowed):
    gate(mode)
    assert onboarding_gate.onboarding_allowed(email, is_admin=is_admin) is allowed


@pytest.mark.parametrize("mode", ["closed", "invite", "open"])
def test_legacy_is_admin_only_in_every_mode(gate, mode):
    gate(mode)
    assert onboarding_gate.legacy_allowed(is_admin=True) is True
    assert onboarding_gate.legacy_allowed(is_admin=False) is False


@pytest.mark.parametrize("raw", ["", "OPEN-ISH", "opened", "yes", "  "])
def test_unknown_mode_fails_closed(gate, raw, caplog):
    gate(raw)
    onboarding_gate._warned_modes.clear()
    with caplog.at_level(logging.WARNING, logger="oncofiles.onboarding_gate"):
        assert onboarding_gate.onboarding_mode() == "closed"
    assert onboarding_gate.onboarding_allowed(STRANGER, is_admin=False) is False
    assert any("treating it as 'closed'" in r.getMessage() for r in caplog.records)


def test_mode_is_case_insensitive(gate):
    gate(" Invite ")
    assert onboarding_gate.onboarding_mode() == "invite"


def test_default_is_closed():
    """Without ONBOARDING_MODE in the environment the gate is closed."""
    import subprocess
    import sys

    code = (
        "import os; os.environ.pop('ONBOARDING_MODE', None); "
        "from oncofiles import onboarding_gate; print(onboarding_gate.onboarding_mode())"
    )
    env = {k: v for k, v in __import__("os").environ.items() if k != "ONBOARDING_MODE"}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=True
    )
    assert out.stdout.strip().splitlines()[-1] == "closed"


def test_refusal_log_has_no_email_and_is_throttled(gate, caplog):
    gate("closed")
    with caplog.at_level(logging.INFO, logger="oncofiles.onboarding_gate"):
        for _ in range(3):
            onboarding_gate.log_refusal("create_patient", STRANGER, surface="dashboard", code="x")
    lines = [r.getMessage() for r in caplog.records if "refused" in r.getMessage()]
    assert len(lines) == 1
    assert "example.test" not in lines[0] and "stranger" not in lines[0]


def test_status_never_contains_the_invite_list(gate):
    gate("invite")
    status = onboarding_gate.status_for(STRANGER, is_admin=False)
    assert status == {
        "mode": "invite",
        "allowed": False,
        "legacy_sync_allowed": False,
        "waitlist_url": "/#join",
    }
    assert "example.test" not in json.dumps(status)


# ── dashboard behaviour ──────────────────────────────────────────────────


class _Req:
    def __init__(
        self, *, email=None, bearer=None, body=None, query=None, path=None, db=None, cookie=False
    ):
        from oncofiles import server

        self.headers = {}
        self.cookies = {}
        if bearer:
            self.headers["authorization"] = f"Bearer {bearer}"
        elif email:
            token = server._make_session_token(email)
            if cookie:
                self.cookies["oncofiles_session"] = token
            else:
                self.headers["authorization"] = f"Bearer session:{token}"
        self._body = body or {}
        self.query_params = query or {}
        self.path_params = path or {}
        self.client = SimpleNamespace(host="203.0.113.9")
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                fastmcp_server=SimpleNamespace(
                    _lifespan_result={"db": db, "files": MagicMock()} if db else {}
                )
            )
        )

    async def json(self):
        return self._body


@pytest.fixture
def http(monkeypatch, gate):
    from oncofiles import oauth, server

    for mod in ("oncofiles.config", "oncofiles.oauth", "oncofiles.server"):
        monkeypatch.setattr(f"{mod}.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr("oncofiles.config.MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "DASHBOARD_ADMIN_EMAILS", [ADMIN])
    monkeypatch.setattr(server, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr(oauth, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr(server, "_notify_admin_onboarding_event", _no_notify)
    server._rate_limits.clear()
    yield server
    server._rate_limits.clear()


async def _no_notify(**_kw):
    return None


def _body(resp) -> dict:
    return json.loads(resp.body)


def _assert_onboarding_closed(resp):
    assert resp.status_code == 403, resp.body
    body = _body(resp)
    assert body["error"] == "onboarding_closed"
    assert body["message_sk"] and body["message_en"]
    assert body["waitlist_url"] == "/#join"


async def _set_caregiver(db, email: str, pid: str = ERIKA_UUID) -> None:
    await db.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?", (email, pid)
    )
    await db.db.commit()


async def _link_google(db, pid: str = ERIKA_UUID) -> None:
    await db.upsert_oauth_token(
        OAuthToken(
            patient_id=pid,
            access_token="synthetic-access",
            refresh_token="synthetic-refresh",
            token_expiry=datetime.now(UTC) + timedelta(hours=1),
            gdrive_folder_id="synthetic-folder-id",
            granted_scopes=json.dumps(["https://www.googleapis.com/auth/drive"]),
        )
    )


@pytest.mark.parametrize(
    ("mode", "caller", "expected"),
    [
        ("closed", STRANGER, 403),
        ("closed", INVITED, 403),
        ("closed", ADMIN, 201),
        ("closed", "bearer", 201),
        ("invite", STRANGER, 403),
        ("invite", INVITED, 201),
        ("invite", ADMIN, 201),
        ("open", STRANGER, 201),
    ],
)
async def test_create_patient(http, gate, db, mode, caller, expected, monkeypatch):
    gate(mode)
    slug = "synthetic-" + re.sub(r"[^a-z0-9]+", "-", f"{mode}-{caller}".lower())[:40]
    body = {"patient_id": slug, "display_name": "Synthetic Person"}
    if caller == "bearer":
        req = _Req(bearer="synthetic-operator-token", body=body, db=db)
    else:
        req = _Req(email=caller, body=body, db=db)
    resp = await http.api_create_patient(req)
    if expected == 403:
        _assert_onboarding_closed(resp)
        assert await db.get_patient_by_slug(slug) is None
    else:
        assert resp.status_code == 201, resp.body


@pytest.mark.parametrize("mode", ["closed", "invite"])
async def test_set_folder_refused_before_any_drive_call(http, gate, db, mode):
    gate(mode)
    await _set_caregiver(db, STRANGER)
    await _link_google(db)
    req = _Req(
        email=STRANGER, body={"patient_id": ERIKA_UUID, "folder_id": "synthetic-folder-2027"}, db=db
    )
    _assert_onboarding_closed(await http.api_gdrive_set_folder(req))


async def test_set_folder_invited_passes_the_gate(http, gate, db):
    gate("invite")
    await _set_caregiver(db, INVITED.lower())
    req = _Req(
        email=INVITED, body={"patient_id": ERIKA_UUID, "folder_id": "synthetic-folder-2027"}, db=db
    )
    resp = await http.api_gdrive_set_folder(req)
    # Past the gate: no Google link yet, so the handler's own 400.
    assert resp.status_code == 400
    assert "No OAuth token" in _body(resp)["error"]


@pytest.mark.parametrize("mode", ["closed", "invite", "open"])
@pytest.mark.parametrize("route", ["api_sync_trigger", "api_enhance_trigger"])
async def test_legacy_triggers_refuse_non_admin_in_every_mode(http, gate, db, mode, route):
    gate(mode)
    await _set_caregiver(db, INVITED.lower())
    req = _Req(email=INVITED, body={"patient_id": ERIKA_UUID}, db=db)
    resp = await getattr(http, route)(req)
    assert resp.status_code == 403
    body = _body(resp)
    assert body["error"] == "legacy_sync_admin_only"
    assert body["message_sk"] and body["message_en"]


async def test_sync_trigger_admin_passes_the_gate(http, gate, db):
    gate("closed")
    req = _Req(email=ADMIN, body={"patient_id": ERIKA_UUID}, db=db)
    resp = await http.api_sync_trigger(req)
    # Past the gate: no Google link, so the handler's own 400.
    assert resp.status_code == 400
    assert "Connect Google Drive" in _body(resp)["error"]


async def test_oauth_authorize_first_link_is_gated(http, gate, db):
    gate("closed")
    await _set_caregiver(db, CARER)
    req = _Req(
        email=CARER, cookie=True, query={"patient_id": ERIKA_UUID}, path={"service": "drive"}, db=db
    )
    _assert_onboarding_closed(await http.oauth_authorize(req))


async def test_oauth_authorize_existing_link_may_reconsent(http, gate, db):
    """An existing caregiver whose patient is already linked keeps working."""
    gate("closed")
    await _set_caregiver(db, CARER)
    await _link_google(db)
    req = _Req(
        email=CARER, cookie=True, query={"patient_id": ERIKA_UUID}, path={"service": "drive"}, db=db
    )
    resp = await http.oauth_authorize(req)
    assert resp.status_code in (302, 307), resp.body
    assert "accounts.google.com" in resp.headers["location"]


@pytest.mark.parametrize(("mode", "email"), [("invite", INVITED), ("open", CARER)])
async def test_oauth_authorize_admitted_callers(http, gate, db, mode, email):
    gate(mode)
    await _set_caregiver(db, email.lower())
    req = _Req(
        email=email, cookie=True, query={"patient_id": ERIKA_UUID}, path={"service": "drive"}, db=db
    )
    resp = await http.oauth_authorize(req)
    assert resp.status_code in (302, 307), resp.body


async def test_existing_caregiver_reads_still_work_when_closed(http, gate, db):
    gate("closed")
    await _set_caregiver(db, CARER)
    req = _Req(email=CARER, db=db)
    resp = await http.api_list_patients(req)
    assert resp.status_code == 200
    assert ERIKA_UUID in resp.body.decode()


@pytest.mark.parametrize(
    ("mode", "email", "allowed"),
    [
        ("closed", STRANGER, False),
        ("invite", INVITED, True),
        ("invite", STRANGER, False),
        ("closed", ADMIN, True),
    ],
)
async def test_onboarding_status_endpoint(http, gate, mode, email, allowed):
    gate(mode)
    resp = await http.api_onboarding_status(_Req(email=email))
    assert resp.status_code == 200
    body = _body(resp)
    assert body["mode"] == mode
    assert body["allowed"] is allowed
    assert set(body) == {"mode", "allowed", "legacy_sync_allowed", "waitlist_url"}
    assert "invited" not in resp.body.decode().lower()


async def test_onboarding_status_requires_auth(http, gate):
    resp = await http.api_onboarding_status(_Req())
    assert resp.status_code == 401


async def test_health_reports_mode_without_the_list(http, gate, monkeypatch):
    gate("invite")
    monkeypatch.setattr(http, "get_rss_mb", lambda: 100.0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    resp = await http.health(request)
    body = _body(resp)
    assert body["onboarding"] == "invite"
    assert "example.test" not in resp.body.decode()


# ── MCP behaviour ────────────────────────────────────────────────────────


def _ctx(db) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {
        "db": db,
        "files": MagicMock(),
        "gdrive": None,
        "oauth_folder_id": "",
    }
    return ctx


@pytest.fixture
def mcp_caller(monkeypatch, gate):
    from oncofiles.persistent_oauth import _verified_caller_email, _verified_caller_is_admin

    monkeypatch.setattr("oncofiles.config.MCP_TRANSPORT", "streamable-http")

    # pytest-asyncio runs the test in its own context, so the values set here
    # never leak into other tests (and cannot be reset from the fixture).
    def as_caller(email: str = "", admin: bool = False) -> None:
        _verified_caller_email.set(email)
        _verified_caller_is_admin.set(admin)

    return as_caller


def _legacy_calls():
    from oncofiles.tools import enhance_tools, gdrive, hygiene, naming

    return {
        "gdrive_sync": lambda c: gdrive.gdrive_sync(c),
        "sync_from_gdrive": lambda c: gdrive.sync_from_gdrive(c),
        "sync_to_gdrive": lambda c: gdrive.sync_to_gdrive(c),
        "setup_gdrive": lambda c: gdrive.setup_gdrive(c, root_folder_id="synthetic-root-2027"),
        "gdrive_fix_permissions": lambda c: gdrive.gdrive_fix_permissions(c),
        "reconcile_gdrive": lambda c: hygiene.reconcile_gdrive(c),
        "repair_gdrive_folder_hygiene": lambda c: hygiene.repair_gdrive_folder_hygiene(c),
        "consolidate_external_gdrive_files": lambda c: hygiene.consolidate_external_gdrive_files(c),
        "rename_documents_to_standard": lambda c: naming.rename_documents_to_standard(c),
        "enhance_documents": lambda c: enhance_tools.enhance_documents(c),
        "detect_and_clone_vaccinations": lambda c: enhance_tools.detect_and_clone_vaccinations(c),
    }


def test_legacy_call_table_covers_every_expected_tool():
    assert set(_legacy_calls()) == EXPECTED_LEGACY_TOOLS


@pytest.mark.parametrize("mode", ["closed", "invite", "open"])
async def test_mcp_legacy_tools_refuse_non_admin_in_every_mode(mcp_caller, gate, db, mode):
    gate(mode)
    mcp_caller(email=INVITED, admin=False)
    for name, call in _legacy_calls().items():
        result = json.loads(await call(_ctx(db)))
        assert result.get("error") == "legacy_sync_admin_only", (name, result)
        assert result["message_en"] and result["message_sk"]


async def test_mcp_legacy_tools_admin_passes_the_gate(mcp_caller, gate, db):
    gate("closed")
    mcp_caller(email=ADMIN, admin=True)
    for name, call in _legacy_calls().items():
        try:
            raw = await call(_ctx(db))
        except Exception:
            continue  # past the gate; the mock context has no Drive
        assert "legacy_sync_admin_only" not in str(raw), name


async def test_mcp_stdio_is_the_operator(gate, db, monkeypatch):
    gate("closed")
    monkeypatch.setattr("oncofiles.config.MCP_TRANSPORT", "stdio")
    from oncofiles.tools import gdrive

    result = json.loads(await gdrive.gdrive_set_folder(_ctx(db), folder_id="synthetic-folder-2027"))
    assert result.get("error") != "onboarding_closed"


@pytest.mark.parametrize(
    ("mode", "email", "admin", "refused"),
    [
        ("closed", STRANGER, False, True),
        ("closed", "", False, True),  # patient bearer, no e-mail
        ("closed", ADMIN, True, False),
        ("invite", INVITED, False, False),
        ("invite", STRANGER, False, True),
        ("open", STRANGER, False, False),
    ],
)
async def test_mcp_set_folder(mcp_caller, gate, db, mode, email, admin, refused):
    from oncofiles.tools import gdrive

    gate(mode)
    mcp_caller(email=email, admin=admin)
    result = json.loads(await gdrive.gdrive_set_folder(_ctx(db), folder_id="synthetic-folder-2027"))
    if refused:
        assert result["error"] == "onboarding_closed"
        assert result["waitlist_url"] == "/#join"
    else:
        assert result.get("error") != "onboarding_closed"


async def test_mcp_auth_url_first_link_gated_existing_link_allowed(
    mcp_caller, gate, db, monkeypatch
):
    from oncofiles.tools import gdrive

    monkeypatch.setattr(gdrive, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.oauth.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.oauth.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-operator-token")
    gate("closed")
    mcp_caller(email=CARER, admin=False)
    result = json.loads(await gdrive.gdrive_auth_url(_ctx(db)))
    assert result["error"] == "onboarding_closed"

    refused = json.loads(await gdrive.gdrive_auth_callback(_ctx(db), code="synthetic-code"))
    assert refused["error"] == "onboarding_closed"

    await _link_google(db)
    result = json.loads(await gdrive.gdrive_auth_url(_ctx(db)))
    assert "auth_url" in result


@pytest.mark.parametrize("tool", ["gmail_auth_enable", "calendar_auth_enable"])
async def test_mcp_gmail_calendar_first_link_gated(mcp_caller, gate, db, monkeypatch, tool):
    from oncofiles.tools import integrations

    monkeypatch.setattr("oncofiles.config.GMAIL_CALENDAR_ENABLED", True)
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    gate("closed")
    mcp_caller(email="", admin=False)
    result = json.loads(await getattr(integrations, tool)(_ctx(db)))
    assert result.get("error") == "onboarding_closed", result


# ── /oauth/callback re-checks the gate ───────────────────────────────────


def _session_state(server, email: str) -> tuple[str, str]:
    from oncofiles import oauth

    token = server._make_session_token(email)
    tid = server._parse_session_token(token)[2]
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "s", "tid": tid})
    return token, state


class _ExchangeCalledError(Exception):
    pass


def _no_exchange(code):
    raise _ExchangeCalledError(code)


def _assert_past_gate(resp):
    """The handler reached the code exchange (stubbed to fail): the gate let it through."""
    assert resp.status_code == 500, resp.body
    assert "Token exchange failed" in _body(resp)["error"]


async def test_callback_refuses_a_first_link_once_closed(http, gate, db, monkeypatch):
    """A state minted while open cannot complete after a rollback to closed."""
    from oncofiles import oauth

    monkeypatch.setattr(oauth, "exchange_code", _no_exchange)
    await _set_caregiver(db, CARER)
    gate("open")
    token, state = _session_state(http, CARER)
    gate("closed")
    req = _Req(query={"state": state, "code": "synthetic-code"}, db=db)
    req.cookies["oncofiles_session"] = token
    _assert_onboarding_closed(await http.oauth_callback(req))


async def test_callback_existing_link_may_reconsent(http, gate, db, monkeypatch):
    from oncofiles import oauth

    monkeypatch.setattr(oauth, "exchange_code", _no_exchange)
    await _set_caregiver(db, CARER)
    await _link_google(db)
    gate("closed")
    token, state = _session_state(http, CARER)
    req = _Req(query={"state": state, "code": "synthetic-code"}, db=db)
    req.cookies["oncofiles_session"] = token
    _assert_past_gate(await http.oauth_callback(req))


@pytest.mark.parametrize(("email", "admitted"), [(INVITED, True), (STRANGER, False)])
async def test_callback_mcp_state_checks_invites_by_hash(
    http, gate, db, monkeypatch, email, admitted
):
    from oncofiles import oauth
    from oncofiles.google_link import email_hash

    monkeypatch.setattr(oauth, "exchange_code", _no_exchange)
    gate("invite")
    principal = {"k": "m", "adm": False, "eh": email_hash(email)}
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal=principal)
    req = _Req(query={"state": state, "code": "synthetic-code"}, db=db)
    if admitted:
        _assert_past_gate(await http.oauth_callback(req))
    else:
        _assert_onboarding_closed(await http.oauth_callback(req))
