"""Admin zone: Google OAuth users against the unverified-app cap of 100.

``GET /api/admin/oauth-users`` counts the distinct Google accounts that ever
completed a Drive/Gmail/Calendar link (keyed hashes in the append-only
``google_link`` audit rows) plus stored tokens of patients without such a row
(one each). Admin-only, counts only, never an e-mail.

Synthetic values only (example.test addresses, invented tokens and ids).
"""

from __future__ import annotations

import base64
import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import server
from oncofiles.google_link import store_google_link
from oncofiles.models import OAuthToken

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
DRIVE = "https://www.googleapis.com/auth/drive"

PID_A = "a1111111-0000-4000-8000-000000000001"
PID_B = "a1111111-0000-4000-8000-000000000002"
PID_C = "a1111111-0000-4000-8000-000000000003"
PID_D = "a1111111-0000-4000-8000-000000000004"
ALL_PIDS = [PID_A, PID_B, PID_C, PID_D]

ACCOUNT_X = "count-x@example.test"
ACCOUNT_Y = "count-y@example.test"
ACCOUNT_Z = "count-z@example.test"
STRANGER = "count-stranger@example.test"
CARER = "count-carer@example.test"
EMAILS = (ACCOUNT_X, ACCOUNT_Y, ACCOUNT_Z, STRANGER, CARER)


@pytest.fixture(autouse=True)
def _hash_key(monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.delenv("AUDIT_EMAIL_HASH_SECRET", raising=False)


@pytest.fixture
async def patients(db):
    for pid in ALL_PIDS:
        await db.db.execute(
            "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
            "VALUES (?, ?, ?, ?)",
            (pid, "count-" + pid[-4:], "Synthetic " + pid[-4:], CARER),
        )
    await db.db.commit()
    return db


def _id_token(email: str) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'email': email, 'email_verified': True})}.sig"


def _tokens(email: str) -> dict:
    return {
        "access_token": "synthetic-access",
        "refresh_token": "synthetic-refresh",
        "expires_in": 3600,
        "scope": f"{DRIVE} openid https://www.googleapis.com/auth/userinfo.email",
        "id_token": _id_token(email),
    }


async def _link(db, pid: str, email: str, *, privileged: bool = False) -> bool:
    from oncofiles.google_link import email_hash

    stored, _ = await store_google_link(
        db,
        patient_id=pid,
        tokens=_tokens(email),
        service="drive",
        principal_kind="s",
        privileged=privileged,
        allowed_account_hashes={email_hash(a) for a in (ACCOUNT_X, ACCOUNT_Y, ACCOUNT_Z)},
        source="oauth_callback",
    )
    return stored


async def _seed(db) -> None:
    assert await _link(db, PID_A, ACCOUNT_X)
    assert await _link(db, PID_B, ACCOUNT_X)  # same Google account, second patient
    assert await _link(db, PID_C, ACCOUNT_Y)
    assert not await _link(db, PID_C, STRANGER)  # refused: never counted
    assert await _link(db, PID_A, ACCOUNT_Z, privileged=True)  # re-link: X stays counted
    # A token stored before the #591 audit existed: account unknown, counts once.
    await db.upsert_oauth_token(
        OAuthToken(patient_id=PID_D, access_token="synthetic-a", refresh_token="synthetic-r")
    )


# ── the count ────────────────────────────────────────────────────────────


async def test_empty_database_counts_zero(db):
    assert await db.count_google_oauth_accounts() == {"accounts": 0, "unaudited_links": 0}


async def test_counts_distinct_accounts_ever_linked_plus_unaudited_tokens(patients):
    db = patients
    await _seed(db)
    # X (two patients, later replaced on PID_A), Y, Z; the stranger was refused.
    assert await db.count_google_oauth_accounts() == {"accounts": 3, "unaudited_links": 1}


async def test_unhashed_audit_row_leaves_the_token_unaudited(patients):
    db = patients
    from oncofiles.models import ActivityLogEntry

    await db.upsert_oauth_token(
        OAuthToken(patient_id=PID_A, access_token="synthetic-a", refresh_token="synthetic-r")
    )
    # A stored link written while no hash key existed: no account identity.
    await db.insert_activity_log(
        ActivityLogEntry(
            patient_id=PID_A,
            session_id="google-link",
            agent_id="oauth_callback",
            tool_name="google_link",
            input_summary=json.dumps({"event": "link", "new_email_hash": None}),
            output_summary="stored",
            status="ok",
        )
    )
    assert await db.count_google_oauth_accounts() == {"accounts": 0, "unaudited_links": 1}


# ── the route ────────────────────────────────────────────────────────────


def _request(db) -> MagicMock:
    request = MagicMock()
    request.query_params = {}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic"}
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


async def test_route_serves_admin_counts_only(patients, caplog):
    db = patients
    caplog.set_level(logging.DEBUG)
    await _seed(db)
    with _caller("count-admin@example.test", admin=True):
        resp = await server.api_admin_oauth_users(_request(db))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["google_oauth_users"] == 4
    assert body["cap"] == 100
    assert body["basis"] == server.GOOGLE_OAUTH_BASIS
    assert body["accounts"] == 3 and body["unaudited_links"] == 1
    raw = resp.body.decode()
    assert "@" not in raw
    for email in EMAILS:
        assert email not in raw
        assert email not in caplog.text


async def test_route_serves_the_operator_bearer(patients):
    with _caller(None, bearer=True):
        resp = await server.api_admin_oauth_users(_request(patients))
    assert resp.status_code == 200
    assert json.loads(resp.body)["google_oauth_users"] == 0


async def test_route_refuses_a_caregiver(patients):
    db = patients
    count = patch.object(
        type(db), "count_google_oauth_accounts", side_effect=AssertionError("no count")
    )
    with _caller(CARER), count:
        resp = await server.api_admin_oauth_users(_request(db))
    assert resp.status_code == 403
    assert json.loads(resp.body) == {"error": "admin only"}


async def test_route_requires_sign_in(patients):
    with _caller(None):
        resp = await server.api_admin_oauth_users(_request(patients))
    assert resp.status_code == 401


# ── the dashboard ────────────────────────────────────────────────────────


def _js_function(name: str) -> str:
    """Source of one top-level dashboard function, up to its closing brace."""
    js = (SRC / "dashboard.js").read_text()
    start = js.index(f"function {name}(")
    depth = 0
    for pos in range(js.index("{", start), len(js)):
        if js[pos] == "{":
            depth += 1
        elif js[pos] == "}":
            depth -= 1
            if depth == 0:
                return js[start : pos + 1]
    raise AssertionError(name)


def test_dashboard_loads_the_line_only_when_its_admin_panel_opens():
    page = server._load_dashboard_html()
    assert 'id="admin-body-oauth" hidden' in page
    assert 'data-action="toggleAdminPanel|oauth|@this"' in page
    assert "'/api/admin/oauth-users'" in _js_function("loadAdminPanel")
    assert "oauth-users" not in _js_function("refresh")
    demo = server._load_demo_dashboard_html()
    assert "admin-body-oauth" not in demo


def test_dashboard_warns_by_shape_and_text_from_80():
    js = (SRC / "dashboard.js").read_text()
    assert re.search(r"var OAUTH_USERS_WARN = 80;", js)
    render = _js_function("renderOauthUsers")
    assert "n >= OAUTH_USERS_WARN" in render
    assert "'attention' : 'ready'" in render  # diamond vs. disc
    assert "Google OAuth users: " in render and "Používatelia Google OAuth: " in render
    assert "warning:" in render and "pozor:" in render
    assert "innerHTML" not in render
