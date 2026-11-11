"""Google link hardening (#591): bound state, overwrite guard, audit, refresh.

- The ``/oauth/callback`` state (v2) is signed, short-lived and single use. It
  names the patient, the service and the initiating principal. A session
  state only completes in the browser that holds that session.
- A link never replaces another Google account's token unless the initiator
  is the operator, an admin or the patient's caregiver.
- Every link / re-link writes an activity_log row with email hashes only.
- An access-token refresh only touches access_token, token_expiry and updated_at.

Synthetic values only (example.test, invented tokens).
"""

from __future__ import annotations

import ast
import base64
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from oncofiles.models import OAuthToken
from tests.helpers import ERIKA_UUID, TEST_PATIENT_UUID

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

OWNER = "owner@example.test"
CARER = "carer@example.test"
STRANGER = "stranger@example.test"
NEW_ACCOUNT = "other-account@example.test"
DRIVE = "https://www.googleapis.com/auth/drive"


def _id_token(email: str) -> str:
    def b64(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{b64({'alg': 'none'})}.{b64({'email': email, 'email_verified': True})}.sig"


def _tokens(email: str | None, scope: str = DRIVE) -> dict:
    out = {
        "access_token": "synthetic-access-new",
        "refresh_token": "synthetic-refresh-new",
        "expires_in": 3600,
        "scope": f"{scope} openid https://www.googleapis.com/auth/userinfo.email",
    }
    if email:
        out["id_token"] = _id_token(email)
    return out


@pytest.fixture
def http_mode(monkeypatch):
    from oncofiles import oauth, server

    for mod in ("oncofiles.config", "oncofiles.oauth", "oncofiles.server"):
        monkeypatch.setattr(f"{mod}.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr("oncofiles.config.MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr(oauth, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    server._rate_limits.clear()
    oauth._consumed_state_tokens.clear()
    yield server
    server._rate_limits.clear()
    oauth._consumed_state_tokens.clear()


def _request(db, *, state: str, cookies=None, query=None):
    q = {"state": state, "code": "synthetic-code", **(query or {})}
    return SimpleNamespace(
        headers={},
        cookies=cookies or {},
        query_params=q,
        path_params={},
        client=SimpleNamespace(host="203.0.113.7"),
        app=SimpleNamespace(
            state=SimpleNamespace(fastmcp_server=SimpleNamespace(_lifespan_result={"db": db}))
        ),
    )


async def _set_caregiver(db, pid: str, email: str) -> None:
    await db.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?", (email, pid)
    )
    await db.db.commit()


async def _existing_token(db, pid: str = ERIKA_UUID, owner: str = OWNER) -> None:
    await db.upsert_oauth_token(
        OAuthToken(
            patient_id=pid,
            access_token="synthetic-access-old",
            refresh_token="synthetic-refresh-old",
            token_expiry=datetime.now(UTC) + timedelta(hours=1),
            gdrive_folder_id="synthetic-folder-id",
            owner_email=owner,
            granted_scopes=json.dumps([DRIVE]),
        )
    )


async def _link_rows(db, pid: str) -> list[dict]:
    async with db.db.execute(
        "SELECT * FROM activity_log WHERE tool_name = 'google_link' AND patient_id = ? ORDER BY id",
        (pid,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _exchange(monkeypatch, tokens: dict) -> list[str]:
    calls: list[str] = []

    def fake(code):
        calls.append(code)
        return tokens

    monkeypatch.setattr("oncofiles.oauth.exchange_code", fake)
    return calls


# ── 1. refresh never changes owner_email ────────────────────────────────


async def test_refresh_updates_updated_at_but_never_owner_email(db):
    await _existing_token(db)
    await db.db.execute(
        "UPDATE oauth_tokens SET updated_at = '2031-03-17T04:05:06Z' WHERE patient_id = ?",
        (ERIKA_UUID,),
    )
    await db.db.commit()
    expiry = datetime(2027, 6, 1, tzinfo=UTC)
    await db.update_oauth_access_token(ERIKA_UUID, "google", "synthetic-access-refreshed", expiry)

    tok = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert tok.access_token == "synthetic-access-refreshed"
    assert tok.token_expiry == expiry
    assert tok.owner_email == OWNER
    assert tok.refresh_token == "synthetic-refresh-old"
    assert tok.gdrive_folder_id == "synthetic-folder-id"
    assert json.loads(tok.granted_scopes) == [DRIVE]
    async with db.db.execute(
        "SELECT updated_at FROM oauth_tokens WHERE patient_id = ?", (ERIKA_UUID,)
    ) as cur:
        assert (await cur.fetchone())["updated_at"] != "2031-03-17T04:05:06Z"


def test_refresh_paths_use_the_narrow_update():
    """Every function that refreshes an access token persists it with
    update_oauth_access_token, never with the full upsert."""
    offenders, seen = [], 0
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | {
                n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)
            }
            if "refresh_access_token" in names and fn.name != "refresh_access_token":
                seen += 1
                if "upsert_oauth_token" in names or "update_oauth_access_token" not in names:
                    offenders.append(f"{path.name}:{fn.name}")
    assert seen >= 4, seen
    assert not offenders, offenders


# ── 2. state: forged, expired, v1, wrong patient ────────────────────────


def test_forged_and_v1_states_are_refused(http_mode):
    from oncofiles import oauth

    good = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "o"})
    signed, _, _ = good.rpartition(".")
    assert oauth.verify_link_state(f"{signed}.{'0' * 64}") is None  # tampered signature
    assert oauth.verify_link_state("") is None
    assert oauth.verify_link_state(f"{ERIKA_UUID}:{int(time.time())}.{'a' * 32}") is None  # v1
    assert oauth.verify_link_state(good) is not None
    assert oauth.verify_link_state(good) is None  # single use


def test_expired_state_is_refused(http_mode, monkeypatch):
    from oncofiles import oauth

    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "o"})
    now = time.time()
    monkeypatch.setattr(oauth.time, "time", lambda: now + oauth._STATE_MAX_AGE + 5)
    assert oauth.verify_link_state(state) is None


def test_state_for_patient_a_cannot_be_rewritten_to_b(http_mode):
    from oncofiles import oauth

    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "o"})
    signed, _, sig = state.rpartition(".")
    body = signed.removeprefix("v2.")
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    payload["pid"] = TEST_PATIENT_UUID
    forged_body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert oauth.verify_link_state(f"v2.{forged_body}.{sig}") is None


async def test_callback_refuses_forged_state_without_write(http_mode, db, monkeypatch):
    calls = _exchange(monkeypatch, _tokens(OWNER))
    resp = await http_mode.oauth_callback(_request(db, state="v2.e30." + "0" * 64))
    assert resp.status_code == 400
    assert calls == []  # no code exchange
    assert await db.get_oauth_token(patient_id=ERIKA_UUID) is None
    assert await _link_rows(db, ERIKA_UUID) == []


async def test_callback_links_only_the_patient_in_the_state(http_mode, db, monkeypatch):
    from oncofiles import oauth
    from oncofiles.models import Patient

    await db.insert_patient(
        Patient(patient_id=TEST_PATIENT_UUID, slug="bob-test", display_name="Bob Test")
    )
    assert await db.get_patient(TEST_PATIENT_UUID) is not None
    _exchange(monkeypatch, _tokens(OWNER))
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "o"})
    resp = await http_mode.oauth_callback(
        _request(db, state=state, query={"patient_id": TEST_PATIENT_UUID})
    )
    assert resp.status_code == 200
    assert await db.get_oauth_token(patient_id=ERIKA_UUID) is not None
    assert await db.get_oauth_token(patient_id=TEST_PATIENT_UUID) is None


async def test_session_state_needs_the_same_session(http_mode, db, monkeypatch):
    from oncofiles import oauth

    server = http_mode
    await _set_caregiver(db, ERIKA_UUID, CARER)
    _exchange(monkeypatch, _tokens(CARER))
    mine = server._make_session_token(CARER)
    other = server._make_session_token(CARER)  # same user, different session
    tid = server._parse_session_token(mine)[2]

    def state():
        return oauth._make_state_token(
            ERIKA_UUID, service="drive", principal={"k": "s", "tid": tid}
        )

    for cookies in ({}, {"oncofiles_session": other}):
        resp = await server.oauth_callback(_request(db, state=state(), cookies=cookies))
        assert resp.status_code == 403
    assert await db.get_oauth_token(patient_id=ERIKA_UUID) is None

    resp = await server.oauth_callback(
        _request(db, state=state(), cookies={"oncofiles_session": mine})
    )
    assert resp.status_code == 200
    assert (await db.get_oauth_token(patient_id=ERIKA_UUID)).access_token == "synthetic-access-new"


async def test_authorize_state_binds_session_patient_and_service(http_mode, db):
    from urllib.parse import parse_qs, urlparse

    from oncofiles import oauth

    server = http_mode
    await _set_caregiver(db, ERIKA_UUID, CARER)
    token = server._make_session_token(CARER)
    req = SimpleNamespace(
        headers={},
        cookies={"oncofiles_session": token},
        query_params={"patient_id": ERIKA_UUID},
        path_params={"service": "gmail"},
        client=SimpleNamespace(host="203.0.113.7"),
        app=SimpleNamespace(
            state=SimpleNamespace(fastmcp_server=SimpleNamespace(_lifespan_result={"db": db}))
        ),
    )
    resp = await server.oauth_authorize(req)
    state = parse_qs(urlparse(resp.headers["location"]).query)["state"][0]
    payload = oauth.verify_link_state(state)
    assert payload["pid"] == ERIKA_UUID
    assert payload["svc"] == "gmail"
    assert payload["pr"] == {"k": "s", "tid": server._parse_session_token(token)[2]}
    assert CARER not in state  # no plaintext email in the URL


async def test_callback_refuses_when_service_scope_not_granted(http_mode, db, monkeypatch):
    from oncofiles import oauth

    _exchange(monkeypatch, _tokens(OWNER, scope=DRIVE))  # gmail unticked
    state = oauth._make_state_token(ERIKA_UUID, service="gmail", principal={"k": "o"})
    resp = await http_mode.oauth_callback(_request(db, state=state))
    assert resp.status_code == 403
    assert await db.get_oauth_token(patient_id=ERIKA_UUID) is None
    assert (await _link_rows(db, ERIKA_UUID))[-1]["status"] == "error"


# ── 3. overwrite guard + audit ──────────────────────────────────────────


async def test_relink_with_other_account_by_non_caregiver_is_refused(http_mode, db, monkeypatch):
    from oncofiles import oauth
    from oncofiles.google_link import email_hash

    await _set_caregiver(db, ERIKA_UUID, CARER)
    await _existing_token(db)
    _exchange(monkeypatch, _tokens(NEW_ACCOUNT))
    principal = {"k": "m", "adm": False, "eh": email_hash(STRANGER)}
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal=principal)
    resp = await http_mode.oauth_callback(_request(db, state=state))
    assert resp.status_code == 403

    tok = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert tok.access_token == "synthetic-access-old"
    assert tok.refresh_token == "synthetic-refresh-old"
    rows = await _link_rows(db, ERIKA_UUID)
    assert len(rows) == 1 and rows[0]["status"] == "error"
    detail = json.loads(rows[0]["input_summary"])
    assert detail["event"] == "relink" and detail["privileged"] is False
    assert detail["new_email_hash"] == email_hash(NEW_ACCOUNT)
    assert detail["old_email_hash"] == email_hash(OWNER)


async def test_caregiver_can_relink_another_account(http_mode, db, monkeypatch):
    from oncofiles import oauth

    server = http_mode
    await _set_caregiver(db, ERIKA_UUID, CARER)
    await _existing_token(db)
    _exchange(monkeypatch, _tokens(NEW_ACCOUNT))
    session = server._make_session_token(CARER)
    tid = server._parse_session_token(session)[2]
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "s", "tid": tid})
    resp = await server.oauth_callback(
        _request(db, state=state, cookies={"oncofiles_session": session})
    )
    assert resp.status_code == 200

    tok = await db.get_oauth_token(patient_id=ERIKA_UUID)
    assert tok.access_token == "synthetic-access-new"
    assert tok.gdrive_folder_id == "synthetic-folder-id"
    assert tok.owner_email == OWNER  # the folder owner is not changed by a link
    rows = await _link_rows(db, ERIKA_UUID)
    assert rows[-1]["status"] == "ok"
    assert json.loads(rows[-1]["input_summary"])["privileged"] is True


async def test_mcp_caregiver_hash_is_privileged(http_mode, db, monkeypatch):
    from oncofiles import oauth
    from oncofiles.google_link import email_hash

    await _set_caregiver(db, ERIKA_UUID, f"someone@example.test, {CARER}")
    await _existing_token(db)
    _exchange(monkeypatch, _tokens(NEW_ACCOUNT))
    principal = {"k": "m", "adm": False, "eh": email_hash(CARER.upper())}
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal=principal)
    resp = await http_mode.oauth_callback(_request(db, state=state))
    assert resp.status_code == 200


async def test_same_account_relink_needs_no_privilege(http_mode, db, monkeypatch):
    from oncofiles import oauth

    await _existing_token(db)
    _exchange(monkeypatch, _tokens(OWNER))
    principal = {"k": "m", "adm": False, "eh": ""}
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal=principal)
    resp = await http_mode.oauth_callback(_request(db, state=state))
    assert resp.status_code == 200
    detail = json.loads((await _link_rows(db, ERIKA_UUID))[-1]["input_summary"])
    assert detail["same_account"] is True


async def test_unknown_new_account_cannot_replace_without_privilege(http_mode, db, monkeypatch):
    from oncofiles import oauth

    await _existing_token(db)
    _exchange(monkeypatch, _tokens(None))  # no ID token
    state = oauth._make_state_token(
        ERIKA_UUID, service="drive", principal={"k": "m", "adm": False, "eh": ""}
    )
    resp = await http_mode.oauth_callback(_request(db, state=state))
    assert resp.status_code == 403
    assert (await db.get_oauth_token(patient_id=ERIKA_UUID)).access_token == "synthetic-access-old"


async def test_first_link_writes_activity_row_without_plaintext_email(http_mode, db, monkeypatch):
    from oncofiles import oauth

    _exchange(monkeypatch, _tokens(NEW_ACCOUNT))
    state = oauth._make_state_token(ERIKA_UUID, service="drive", principal={"k": "o"})
    resp = await http_mode.oauth_callback(_request(db, state=state))
    assert resp.status_code == 200
    rows = await _link_rows(db, ERIKA_UUID)
    assert len(rows) == 1 and rows[0]["status"] == "ok"
    assert json.loads(rows[0]["input_summary"])["event"] == "link"
    blob = json.dumps(rows[0])
    assert "@" not in blob


async def test_mcp_auth_callback_tool_uses_the_guard(http_mode, db, monkeypatch):
    from oncofiles.persistent_oauth import _verified_caller_email, _verified_caller_is_admin
    from oncofiles.tools import gdrive

    monkeypatch.setattr(gdrive, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr(gdrive, "_get_db", lambda ctx: db)
    await _set_caregiver(db, ERIKA_UUID, CARER)
    await _existing_token(db)
    _exchange(monkeypatch, _tokens(NEW_ACCOUNT))
    t1 = _verified_caller_is_admin.set(False)
    t2 = _verified_caller_email.set(STRANGER)
    try:
        out = json.loads(await gdrive.gdrive_auth_callback(None, "synthetic-code"))
    finally:
        _verified_caller_is_admin.reset(t1)
        _verified_caller_email.reset(t2)
    assert "error" in out
    assert (await db.get_oauth_token(patient_id=ERIKA_UUID)).access_token == "synthetic-access-old"
    assert (await _link_rows(db, ERIKA_UUID))[-1]["agent_id"] == "gdrive_auth_callback"
