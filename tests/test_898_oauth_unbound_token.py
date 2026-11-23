"""#898 — MCP OAuth never mints a token that is not bound to a signed-in account.

Before #898, GET /authorize without a dashboard session passed through and
FastMCP issued a token with no Google email: ``whoami`` showed
``email: null``, ``patients: []`` and nothing said that the fix is to sign in.

Now (fail closed, default):
1. ``MCPAuthorizeEmailCaptureMiddleware`` answers GET /authorize without a
   valid session with a 302 to ``/dashboard?next=<that /authorize URL>``;
   the sign-in page (dashboard.js) returns there after Google sign-in.
2. ``exchange_authorization_code`` refuses a code with no bound email
   (``invalid_grant``) before any token is issued or written.
3. ``whoami`` tells an unbound OAuth connection to sign in and reconnect.
``MCP_OAUTH_ALLOW_UNBOUND=true`` is the single-user opt-out.

All identities, clients and challenges are synthetic.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from mcp.server.auth.provider import AuthorizationParams, TokenError
from mcp.shared.auth import OAuthClientInformationFull

from oncofiles.database import Database
from oncofiles.persistent_oauth import (
    UNBOUND_REFUSAL_MESSAGE,
    PersistentOAuthProvider,
    _email_for_challenge,
    _verified_auth_method,
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
    stash_email_for_challenge,
)

CLIENT_ID = "synthetic-connector-898"
CAREGIVER = "carer-898@example.com"
BEARER = "synthetic-operator-token-898"
SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _default_fail_closed(monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", False)
    for mod in ("oncofiles.config", "oncofiles.server"):
        monkeypatch.setattr(f"{mod}.MCP_BEARER_TOKEN", BEARER)
    _email_for_challenge.clear()
    yield
    _email_for_challenge.clear()


@pytest.fixture
async def odb():
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
async def provider(odb):
    p = PersistentOAuthProvider(db=odb)
    await p.register_client(
        OAuthClientInformationFull(
            client_id=CLIENT_ID,
            client_secret="synthetic-secret",
            redirect_uris=["http://localhost:3000/callback"],
            grant_types=["authorization_code", "refresh_token"],
            token_endpoint_auth_method="client_secret_post",
        )
    )
    return p


async def _code(provider, challenge):
    client = await provider.get_client(CLIENT_ID)
    redirect = await provider.authorize(
        client,
        AuthorizationParams(
            state="s",
            scopes=[],
            code_challenge=challenge,
            code_challenge_method="S256",
            redirect_uri="http://localhost:3000/callback",
            redirect_uri_provided_explicitly=True,
        ),
    )
    code = parse_qs(urlparse(redirect).query)["code"][0]
    return client, await provider.load_authorization_code(client, code)


async def _token_rows(db) -> list[dict]:
    async with db.db.execute("SELECT token_type, user_email FROM mcp_oauth_tokens") as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── code exchange (defense in depth) ─────────────────────────────────


async def test_exchange_without_bound_email_is_refused_and_writes_nothing(odb, provider):
    client, auth_code = await _code(provider, "chal-unbound")
    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_authorization_code(client, auth_code)
    assert exc_info.value.error == "invalid_grant"
    assert exc_info.value.error_description == UNBOUND_REFUSAL_MESSAGE
    assert "sign in" in UNBOUND_REFUSAL_MESSAGE.lower()
    # Nothing issued: no row, no in-memory token, and the code is consumed.
    assert await _token_rows(odb) == []
    assert provider.access_tokens == {}
    assert provider.refresh_tokens == {}
    assert auth_code.code not in provider.auth_codes


async def test_exchange_with_bound_email_issues_a_bound_token(odb, provider):
    stash_email_for_challenge("chal-bound", CAREGIVER)
    client, auth_code = await _code(provider, "chal-bound")
    tok = await provider.exchange_authorization_code(client, auth_code)
    assert tok.access_token and tok.refresh_token
    rows = await _token_rows(odb)
    assert {r["token_type"] for r in rows} == {"access", "refresh"}
    assert {r["user_email"] for r in rows} == {CAREGIVER}


async def test_bound_token_survives_refresh_with_its_email(odb, provider):
    stash_email_for_challenge("chal-refresh", CAREGIVER)
    client, auth_code = await _code(provider, "chal-refresh")
    tok = await provider.exchange_authorization_code(client, auth_code)
    refresh_obj = await provider.load_refresh_token(client, tok.refresh_token)
    tok2 = await provider.exchange_refresh_token(client, refresh_obj, [])
    assert tok2.access_token != tok.access_token
    assert {r["user_email"] for r in await _token_rows(odb)} == {CAREGIVER}


async def test_expired_stash_counts_as_unbound(odb, provider, monkeypatch):
    stash_email_for_challenge("chal-old", CAREGIVER)
    monkeypatch.setattr("oncofiles.persistent_oauth._EMAIL_STASH_TTL_S", -1.0)
    client, auth_code = await _code(provider, "chal-old")
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, auth_code)
    assert await _token_rows(odb) == []


async def test_opt_out_restores_unbound_tokens(odb, provider, monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    client, auth_code = await _code(provider, "chal-optout")
    tok = await provider.exchange_authorization_code(client, auth_code)
    assert tok.access_token
    assert {r["user_email"] for r in await _token_rows(odb)} == {None}


async def test_static_and_patient_bearer_are_unaffected(odb):
    from oncofiles.models import Patient

    pid = "00000000-0000-4000-8000-000000000898"
    await odb.insert_patient(Patient(patient_id=pid, slug="kilo-test", display_name="Kilo Example"))
    patient_token = await odb.create_patient_token(pid, label="t")
    p = PersistentOAuthProvider(db=odb, bearer_token=BEARER)
    assert await p.verify_token(BEARER) is not None
    assert await p.verify_token(patient_token) is not None
    assert _verified_patient_id.get() == pid


def test_opt_out_defaults_to_off(monkeypatch):
    import importlib

    from oncofiles import config

    monkeypatch.delenv("MCP_OAUTH_ALLOW_UNBOUND", raising=False)
    try:
        importlib.reload(config)
        assert config.MCP_OAUTH_ALLOW_UNBOUND is False
    finally:
        importlib.reload(config)


# ── /authorize middleware ────────────────────────────────────────────


AUTH_QS = (
    "response_type=code&client_id=synthetic-connector-898"
    "&redirect_uri=client-callback"
    "&code_challenge=chal-mw&code_challenge_method=S256&state=st-1"
)


async def _run_mw(path="/authorize", method="GET", qs=AUTH_QS, cookie=None):
    from oncofiles.server import MCPAuthorizeEmailCaptureMiddleware

    called = []
    sent = []

    async def app(scope, receive, send):
        called.append(scope["path"])

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    headers = [(b"host", b"oncofiles.example.test")]
    if cookie is not None:
        headers.append((b"cookie", cookie.encode("latin-1")))
    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "query_string": qs.encode("latin-1"),
        "headers": headers,
    }
    await MCPAuthorizeEmailCaptureMiddleware(app)(scope, receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    return called, start


def _location(start) -> str:
    return dict(start["headers"])[b"location"].decode("ascii")


async def test_authorize_without_session_redirects_to_sign_in():
    called, start = await _run_mw()
    assert called == []  # FastMCP never sees it, so no code is minted
    assert start["status"] == 302
    loc = _location(start)
    assert loc.startswith("/dashboard?next=")
    back = unquote(loc.split("next=", 1)[1])
    assert back == "/authorize?" + AUTH_QS
    assert dict(start["headers"])[b"cache-control"] == b"no-store"
    assert _email_for_challenge == {}


async def test_authorize_with_forged_session_redirects():
    called, start = await _run_mw(cookie="oncofiles_session=forged%7C123%7Cabc%7Csig")
    assert called == []
    assert start["status"] == 302
    assert _email_for_challenge == {}


async def test_authorize_with_session_binds_and_passes_through(caplog):
    from urllib.parse import quote

    from oncofiles.server import _make_session_token

    token = _make_session_token(CAREGIVER)
    with caplog.at_level(logging.DEBUG, logger="oncofiles.server"):
        called, start = await _run_mw(cookie=f"other=1; oncofiles_session={quote(token, safe='')}")
    assert called == ["/authorize"]
    assert start is None
    assert _email_for_challenge["chal-mw"][0] == CAREGIVER
    # Logs carry a keyed caller id, never the address.
    assert CAREGIVER not in caplog.text


async def test_bound_flow_end_to_end(odb, provider):
    """Middleware stash with a real session → the exchange issues a bound token."""
    from urllib.parse import quote

    from oncofiles.server import _make_session_token

    cookie = "oncofiles_session=" + quote(_make_session_token(CAREGIVER), safe="")
    qs = AUTH_QS.replace("chal-mw", "chal-e2e")
    called, _ = await _run_mw(qs=qs, cookie=cookie)
    assert called == ["/authorize"]
    client, auth_code = await _code(provider, "chal-e2e")
    await provider.exchange_authorization_code(client, auth_code)
    assert {r["user_email"] for r in await _token_rows(odb)} == {CAREGIVER}


async def test_unbound_flow_end_to_end_writes_no_token(odb, provider):
    """No session: redirected, and even a code minted behind the middleware's
    back (e.g. POST /authorize) is refused at the exchange."""
    called, start = await _run_mw(qs=AUTH_QS.replace("chal-mw", "chal-e2e-2"))
    assert called == [] and start["status"] == 302
    client, auth_code = await _code(provider, "chal-e2e-2")
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, auth_code)
    assert await _token_rows(odb) == []


async def test_opt_out_passes_through_without_session(monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    called, start = await _run_mw()
    assert called == ["/authorize"]
    assert start is None


async def test_overlong_authorize_url_is_refused_not_redirected():
    called, start = await _run_mw(qs=AUTH_QS + "&pad=" + "a" * 5000)
    assert called == []
    assert start["status"] == 400


async def test_non_ascii_query_is_refused():
    called, start = await _run_mw(qs=AUTH_QS + "&x=é")
    assert called == []
    assert start["status"] == 400


@pytest.mark.parametrize(
    ("path", "method"),
    [("/authorize", "POST"), ("/token", "POST"), ("/dashboard", "GET"), ("/mcp", "POST")],
)
async def test_other_requests_pass_through(path, method):
    called, start = await _run_mw(path=path, method=method)
    assert called == [path]
    assert start is None


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("/authorize", True),
        ("/authorize?" + AUTH_QS, True),
        ("/authorizex", False),
        ("/authorize/../dashboard", False),
        ("//evil.example.test/authorize?x=1", False),
        ("https://evil.example.test/authorize?x=1", False),
        ("/authorize?x=1#frag", False),
        ("/authorize?x=1\\y", False),
        ("/authorize?x= 1", False),
        ("/authorize?x=é", False),
        ("/authorize?" + "a" * 5000, False),
        ("", False),
        (None, False),
    ],
)
def test_safe_authorize_next(value, ok):
    from oncofiles.server import _safe_authorize_next

    assert _safe_authorize_next(value) == (value if ok else None)


# ── sign-in page (dashboard.js) ──────────────────────────────────────


def test_dashboard_returns_to_authorize_after_sign_in():
    js = (SRC / "dashboard.js").read_text(encoding="utf-8")
    assert "function safeAuthorizeNext(value)" in js
    assert "AUTHORIZE_NEXT_MAX_LEN = 4096" in js
    assert "params.get('next')" in js
    assert "window.location.replace(back)" in js
    # The state var must be initialised BEFORE init() runs, or its initializer
    # would reset the value init() just set.
    assert js.index("var authorizeNext = ''") < js.index("(function init()")
    # Same limit on both sides.
    from oncofiles.server import AUTHORIZE_NEXT_MAX_LEN

    assert AUTHORIZE_NEXT_MAX_LEN == 4096
    html = (SRC / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="auth-connect-note"' in html


# ── whoami guidance ──────────────────────────────────────────────────


class _Ctx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db, "transport": "streamable-http"}

    def __init__(self, db):
        self.request_context = self._Req(db)


@pytest.fixture
def _identity():
    vars_ = (
        _verified_caller_is_admin,
        _verified_caller_email,
        _verified_patient_id,
        _verified_auth_method,
    )
    toks = [v.set(d) for v, d in zip(vars_, (False, "", "", ""), strict=True)]
    yield
    for v, t in zip(vars_, toks, strict=True):
        v.reset(t)


async def test_whoami_unbound_oauth_says_sign_in_and_reconnect(db, _identity):
    from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
    from oncofiles.patient_middleware import _current_patient_id
    from oncofiles.tools.patient import whoami

    _verified_auth_method.set("oauth")
    _current_patient_id.set(NO_PATIENT_ACCESS_SENTINEL)
    data = json.loads(await whoami(_Ctx(db)))
    assert data["email"] is None
    assert data["patients"] == []
    assert "oncofiles.com/dashboard" in data["guidance"]
    assert "reconnect" in data["guidance"]
