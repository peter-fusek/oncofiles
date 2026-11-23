"""#898 — MCP OAuth binds a signed-in account only after explicit consent, and
never mints an unbound token.

Before #898, GET /authorize without a dashboard session passed through and
FastMCP issued a token with no account (``whoami``: ``email: null``,
``patients: []``). The security review of the first fix added two exploit
paths that are locked here too:

* **Code phishing**: anyone can DCR-register a client with their own
  redirect_uri and send a signed-in victim an /authorize link. Now DCR
  refuses an off-list redirect host, /authorize refuses an off-list client
  even if it was registered earlier, and nothing is bound without the
  consent page's POST carrying a session-bound, single-use nonce.
* **Parser differential**: the account used to be stashed by the
  middleware's own parse of ``code_challenge``. Now duplicate single params
  are refused, the binding is recorded inside ``provider.authorize`` from
  FastMCP's own parsed params against the exact
  (client_id, code_challenge, redirect_uri), and the exchange re-checks it.

Plus: unbound refresh tokens are refused (not rotated); bound ones keep
refreshing. ``MCP_OAUTH_ALLOW_UNBOUND=true`` is the single-user opt-out.

All identities, clients, hosts and challenges are synthetic.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlparse

import pytest
from mcp.server.auth.provider import (
    AuthorizationParams,
    AuthorizeError,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull

from oncofiles.database import Database
from oncofiles.persistent_oauth import (
    UNBOUND_REFUSAL_MESSAGE,
    ConsentedBinding,
    PersistentOAuthProvider,
    _binding_for_code,
    _verified_auth_method,
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
    consented_binding,
    redirect_uri_allowed,
    token_digest,
)

CLIENT_ID = "synthetic-connector-898"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
CAREGIVER = "carer-898@example.com"
BEARER = "synthetic-operator-token-898"
SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _default_fail_closed(monkeypatch):
    from oncofiles import server

    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", False)
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_EXTRA_REDIRECT_URIS", ())
    for mod in ("oncofiles.config", "oncofiles.server"):
        monkeypatch.setattr(f"{mod}.MCP_BEARER_TOKEN", BEARER)
    _binding_for_code.clear()
    server._consumed_consent_nonces.clear()
    yield
    _binding_for_code.clear()
    server._consumed_consent_nonces.clear()


@pytest.fixture
async def odb():
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


def _client_info(client_id=CLIENT_ID, redirect=REDIRECT, name="Synthetic Connector"):
    return OAuthClientInformationFull(
        client_id=client_id,
        client_secret="synthetic-secret",
        client_name=name,
        redirect_uris=[redirect],
        grant_types=["authorization_code", "refresh_token"],
        token_endpoint_auth_method="client_secret_post",
    )


@pytest.fixture
async def provider(odb, monkeypatch):
    p = PersistentOAuthProvider(db=odb)
    await p.register_client(_client_info())
    monkeypatch.setattr("oncofiles.server.auth", p)
    return p


def _params(challenge, redirect=REDIRECT):
    return AuthorizationParams(
        state="s",
        scopes=[],
        code_challenge=challenge,
        code_challenge_method="S256",
        redirect_uri=redirect,
        redirect_uri_provided_explicitly=True,
    )


async def _code(provider, challenge, *, bind_as=None, client_id=CLIENT_ID, redirect=REDIRECT):
    client = await provider.get_client(client_id)
    if bind_as:
        with consented_binding(bind_as, client_id, challenge, redirect):
            loc = await provider.authorize(client, _params(challenge, redirect))
    else:
        loc = await provider.authorize(client, _params(challenge, redirect))
    code = parse_qs(urlparse(loc).query)["code"][0]
    return client, await provider.load_authorization_code(client, code)


async def _token_rows(db) -> list[dict]:
    async with db.db.execute(
        "SELECT token, token_type, user_email FROM mcp_oauth_tokens ORDER BY token"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


# ── redirect URI allowlist (exact URIs, loopback open) ───────────────

GPT_ACTION = "https://chatgpt.com/aip/g-synthetic898/oauth/callback"


@pytest.mark.parametrize(
    ("uri", "ok"),
    [
        ("https://claude.ai/api/mcp/auth_callback", True),
        ("https://CLAUDE.ai:443/api/mcp/auth_callback", True),
        ("https://claude.com/api/mcp/auth_callback", True),
        ("https://chatgpt.com/connector_platform_oauth_redirect", True),
        ("https://chat.openai.com/connector_platform_oauth_redirect", True),
        ("http://localhost:3000/callback", True),
        ("http://127.0.0.1:53682/any/path", True),
        ("http://[::1]:8765/cb", True),
        # Shared vendor hosts: only the exact connector callback.
        (GPT_ACTION, False),
        ("https://chat.openai.com/aip/g-synthetic898/oauth/callback", False),
        ("https://chatgpt.com/connector_platform_oauth_redirect/extra", False),
        ("https://chatgpt.com/connector_platform_oauth_redirect?x=1", False),
        ("https://claude.ai/api/mcp/auth_callback#frag", False),
        ("https://claude.ai/other", False),
        ("https://mcp.claude.ai/api/mcp/auth_callback", False),  # no subdomains
        ("https://claude.ai:8443/api/mcp/auth_callback", False),
        ("http://claude.ai/api/mcp/auth_callback", False),  # https only
        ("https://claude.ai.evil.example.test/api/mcp/auth_callback", False),
        ("https://attacker.example.test/cb", False),
        ("https://user:pw@claude.ai/api/mcp/auth_callback", False),
        ("http://user@localhost/cb", False),
        ("javascript:alert(1)", False),
        ("", False),
    ],
)
def test_redirect_uri_allowlist(uri, ok):
    assert redirect_uri_allowed(uri) is ok


def test_extra_uris_env_extends_the_list_exactly(monkeypatch):
    monkeypatch.setattr(
        "oncofiles.config.MCP_OAUTH_EXTRA_REDIRECT_URIS",
        ("https://connector.example.test/oauth/cb",),
    )
    assert redirect_uri_allowed("https://connector.example.test/oauth/cb")
    assert not redirect_uri_allowed("https://connector.example.test/oauth/cb2")
    assert not redirect_uri_allowed("https://connector.example.test/other")


async def test_gpt_action_callback_is_refused_at_register_and_authorize(provider):
    with pytest.raises(RegistrationError):
        await provider.register_client(_client_info("gpt-898", GPT_ACTION, name="My GPT"))
    provider.clients["gpt-898"] = _client_info("gpt-898", GPT_ACTION, name="My GPT")
    client = await provider.get_client("gpt-898")
    with (
        consented_binding(CAREGIVER, "gpt-898", "chal-gpt", GPT_ACTION),
        pytest.raises(AuthorizeError),
    ):
        await provider.authorize(client, _params("chal-gpt", GPT_ACTION))
    app = _FastMCPLike(provider)
    start, body = await _call(app, qs=_qs("chal-gpt", "gpt-898", GPT_ACTION), cookie=_cookie())
    assert start["status"] == 400 and b"redirect_uri_not_allowed" in body
    assert provider.auth_codes == {} and app.calls == []


# ── client_name anti-impersonation ───────────────────────────────────

CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
GPT_CB = "https://chatgpt.com/connector_platform_oauth_redirect"
LOCAL_CB = "http://localhost:3000/callback"


@pytest.mark.parametrize(
    ("name", "redirect", "ok"),
    [
        ("Claude", CLAUDE_CB, True),
        ("Anthropic Claude", CLAUDE_CB, True),
        ("ChatGPT", GPT_CB, True),
        ("OpenAI connector", GPT_CB, True),
        ("My tool", LOCAL_CB, True),
        (None, LOCAL_CB, True),
        ("Claude Desktop helper", LOCAL_CB, False),
        ("C-l-a-u-d-e", LOCAL_CB, False),
        ("ChatGPT", CLAUDE_CB, False),
        ("Claude", GPT_CB, False),
        ("Official OpenAI", LOCAL_CB, False),
        ("Oncofiles", CLAUDE_CB, False),
        ("oncofiles official", LOCAL_CB, False),
    ],
)
async def test_reserved_client_names(odb, name, redirect, ok):
    from oncofiles.persistent_oauth import client_name_refusal

    assert (client_name_refusal(name, [redirect]) == "") is ok
    p = PersistentOAuthProvider(db=odb)
    if ok:
        await p.register_client(_client_info("n-898", redirect, name=name))
        assert await p.get_client("n-898") is not None
    else:
        with pytest.raises(RegistrationError) as exc_info:
            await p.register_client(_client_info("n-898", redirect, name=name))
        assert exc_info.value.error == "invalid_client_metadata"
        assert await p.get_client("n-898") is None


async def test_reserved_name_on_an_old_client_is_refused_at_authorize(provider):
    provider.clients["fake-claude"] = _client_info("fake-claude", LOCAL_CB, name="Claude")
    client = await provider.get_client("fake-claude")
    with (
        consented_binding(CAREGIVER, "fake-claude", "chal-n", LOCAL_CB),
        pytest.raises(AuthorizeError),
    ):
        await provider.authorize(client, _params("chal-n", LOCAL_CB))
    app = _FastMCPLike(provider)
    start, body = await _call(app, qs=_qs("chal-n", "fake-claude", LOCAL_CB), cookie=_cookie())
    assert start["status"] == 400 and b"client_name_reserved" in body


# ── DCR /register ────────────────────────────────────────────────────


async def test_register_refuses_foreign_redirect_host_and_logs_host_only(odb, caplog):
    p = PersistentOAuthProvider(db=odb)
    foreign = "https://attacker.example.test/steal?secret-path-898"
    with (
        caplog.at_level(logging.INFO, logger="oncofiles.persistent_oauth"),
        pytest.raises(RegistrationError) as exc_info,
    ):
        await p.register_client(_client_info("evil-898", foreign))
    assert exc_info.value.error == "invalid_redirect_uri"
    assert await p.get_client("evil-898") is None
    async with odb.db.execute("SELECT COUNT(*) AS n FROM mcp_oauth_clients") as cur:
        assert (await cur.fetchone())["n"] == 0
    assert "attacker.example.test" in caplog.text
    assert "secret-path-898" not in caplog.text
    assert "synthetic-secret" not in caplog.text


async def test_register_endpoint_returns_a_clean_oauth_error(odb, monkeypatch):
    """Through the real DCR handler: 400 + RFC 7591 error body."""
    from mcp.server.auth.handlers.register import RegistrationHandler
    from mcp.server.auth.settings import ClientRegistrationOptions
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    p = PersistentOAuthProvider(db=odb)
    handler = RegistrationHandler(p, options=ClientRegistrationOptions(enabled=True))
    app = Starlette(routes=[Route("/register", handler.handle, methods=["POST"])])
    with TestClient(app) as tc:
        bad = tc.post(
            "/register",
            json={"redirect_uris": ["https://attacker.example.test/cb"], "client_name": "x"},
        )
        good = tc.post(
            "/register",
            json={"redirect_uris": [REDIRECT], "client_name": "Synthetic"},
        )
    assert bad.status_code == 400
    assert bad.json()["error"] == "invalid_redirect_uri"
    assert good.status_code == 201


# ── provider.authorize: no code without consent ──────────────────────


async def test_authorize_without_consent_mints_no_code(provider):
    client = await provider.get_client(CLIENT_ID)
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, _params("chal-noconsent"))
    assert provider.auth_codes == {}


async def test_authorize_consent_for_another_tuple_mints_no_code(provider):
    client = await provider.get_client(CLIENT_ID)
    with (
        consented_binding(CAREGIVER, CLIENT_ID, "chal-other", REDIRECT),
        pytest.raises(AuthorizeError),
    ):
        await provider.authorize(client, _params("chal-actual"))
    with (
        consented_binding(CAREGIVER, "another-client", "chal-actual", REDIRECT),
        pytest.raises(AuthorizeError),
    ):
        await provider.authorize(client, _params("chal-actual"))
    assert provider.auth_codes == {} and _binding_for_code == {}


async def test_previously_registered_offlist_client_can_never_authorize(provider, monkeypatch):
    """A client registered before the allowlist (e.g. a crawler) is refused
    even with a consent binding and even with the unbound opt-out."""
    foreign = "https://attacker.example.test/cb"
    provider.clients["legacy-evil"] = _client_info("legacy-evil", foreign)
    client = await provider.get_client("legacy-evil")
    with (
        consented_binding(CAREGIVER, "legacy-evil", "chal-l", foreign),
        pytest.raises(AuthorizeError),
    ):
        await provider.authorize(client, _params("chal-l", foreign))
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    with pytest.raises(AuthorizeError):
        await provider.authorize(client, _params("chal-l", foreign))
    assert provider.auth_codes == {}


# ── code exchange ────────────────────────────────────────────────────


async def test_consented_exchange_issues_a_bound_token(odb, provider):
    client, auth_code = await _code(provider, "chal-bound", bind_as=CAREGIVER)
    tok = await provider.exchange_authorization_code(client, auth_code)
    assert tok.access_token and tok.refresh_token
    rows = await _token_rows(odb)
    assert {r["token_type"] for r in rows} == {"access", "refresh"}
    assert {r["user_email"] for r in rows} == {CAREGIVER}


async def test_exchange_without_binding_is_refused_and_writes_nothing(odb, provider, monkeypatch):
    # A code can only exist without a binding under the opt-out; switch it
    # off again before the exchange.
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    client, auth_code = await _code(provider, "chal-unbound")
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", False)
    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_authorization_code(client, auth_code)
    assert exc_info.value.error == "invalid_grant"
    assert exc_info.value.error_description == UNBOUND_REFUSAL_MESSAGE
    assert await _token_rows(odb) == []
    assert provider.access_tokens == {} and provider.refresh_tokens == {}
    assert auth_code.code not in provider.auth_codes


@pytest.mark.parametrize("field", ["client_id", "code_challenge", "redirect_uri"])
async def test_exchange_refuses_a_code_whose_tuple_does_not_match_the_binding(odb, provider, field):
    client, auth_code = await _code(provider, "chal-tuple", bind_as=CAREGIVER)
    binding, created = _binding_for_code[auth_code.code]
    values = {
        "email": binding.email,
        "client_id": binding.client_id,
        "code_challenge": binding.code_challenge,
        "redirect_uri": binding.redirect_uri,
    }
    values[field] = {
        "client_id": "other-client",
        "code_challenge": "chal-forged",
        "redirect_uri": "https://claude.ai/other",
    }[field]
    _binding_for_code[auth_code.code] = (ConsentedBinding(**values), created)
    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_authorization_code(client, auth_code)
    assert exc_info.value.error == "invalid_grant"
    assert await _token_rows(odb) == []


async def test_expired_binding_counts_as_unbound(odb, provider, monkeypatch):
    client, auth_code = await _code(provider, "chal-old", bind_as=CAREGIVER)
    monkeypatch.setattr("oncofiles.persistent_oauth._EMAIL_STASH_TTL_S", -1.0)
    with pytest.raises(TokenError):
        await provider.exchange_authorization_code(client, auth_code)
    assert await _token_rows(odb) == []


async def test_opt_out_restores_unbound_tokens(odb, provider, monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    client, auth_code = await _code(provider, "chal-optout")
    tok = await provider.exchange_authorization_code(client, auth_code)
    assert tok.access_token
    assert {r["user_email"] for r in await _token_rows(odb)} == {None}


def test_opt_out_defaults_to_off(monkeypatch):
    import importlib

    from oncofiles import config

    monkeypatch.delenv("MCP_OAUTH_ALLOW_UNBOUND", raising=False)
    try:
        importlib.reload(config)
        assert config.MCP_OAUTH_ALLOW_UNBOUND is False
    finally:
        importlib.reload(config)


# ── refresh ──────────────────────────────────────────────────────────


async def test_preexisting_bound_claude_refresh_token_still_refreshes(odb, provider):
    """A bound connector from before this change keeps working after a
    restart: restore from the DB, refresh, the account stays bound."""
    client, auth_code = await _code(provider, "chal-pre", bind_as=CAREGIVER)
    old = await provider.exchange_authorization_code(client, auth_code)

    restarted = PersistentOAuthProvider(db=odb)
    await restarted.restore_from_db()
    client2 = await restarted.get_client(CLIENT_ID)
    refresh_obj = await restarted.load_refresh_token(client2, old.refresh_token)
    assert refresh_obj is not None
    new = await restarted.exchange_refresh_token(client2, refresh_obj, [])
    assert new.access_token != old.access_token
    rows = await _token_rows(odb)
    assert {r["user_email"] for r in rows} == {CAREGIVER}
    assert token_digest(old.refresh_token) not in {r["token"] for r in rows}


async def test_unbound_refresh_token_is_refused_and_not_rotated(odb, provider, monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    client, auth_code = await _code(provider, "chal-legacy")
    legacy = await provider.exchange_authorization_code(client, auth_code)
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", False)
    before = await _token_rows(odb)

    refresh_obj = await provider.load_refresh_token(client, legacy.refresh_token)
    with pytest.raises(TokenError) as exc_info:
        await provider.exchange_refresh_token(client, refresh_obj, [])
    assert exc_info.value.error == "invalid_grant"
    assert exc_info.value.error_description == UNBOUND_REFUSAL_MESSAGE
    assert await _token_rows(odb) == before  # not rotated, nothing new
    assert token_digest(legacy.refresh_token) in provider.refresh_tokens


async def test_static_and_patient_bearer_are_unaffected(odb):
    from oncofiles.models import Patient

    pid = "00000000-0000-4000-8000-000000000898"
    await odb.insert_patient(Patient(patient_id=pid, slug="kilo-test", display_name="Kilo Example"))
    patient_token = await odb.create_patient_token(pid, label="t")
    p = PersistentOAuthProvider(db=odb, bearer_token=BEARER)
    assert await p.verify_token(BEARER) is not None
    assert await p.verify_token(patient_token) is not None
    assert _verified_patient_id.get() == pid


# ── /authorize middleware + consent page ─────────────────────────────


def _qs(challenge="chal-mw", client_id=CLIENT_ID, redirect=REDIRECT, extra=()):
    pairs = [
        ("response_type", "code"),
        ("client_id", client_id),
        ("redirect_uri", redirect),
        ("code_challenge", challenge),
        ("code_challenge_method", "S256"),
        ("state", "st-1"),
        *extra,
    ]
    return urlencode(pairs)


def _cookie(account=CAREGIVER) -> str:
    from oncofiles.server import _make_session_token

    return "oncofiles_session=" + quote(_make_session_token(account), safe="")


class _FastMCPLike:
    """Stands in for FastMCP's AuthorizationHandler behind the middleware:
    parses its own params and calls provider.authorize, like production."""

    def __init__(self, provider):
        self.provider = provider
        self.calls: list[str] = []
        self.codes: list[str] = []

    async def __call__(self, scope, receive, send):
        self.calls.append(f"{scope['method']} {scope['path']}")
        if scope["path"] != "/authorize" or scope["method"] != "GET":
            return
        q = dict(parse_qsl(scope["query_string"].decode("ascii")))
        client = await self.provider.get_client(q["client_id"])
        try:
            loc = await self.provider.authorize(
                client, _params(q["code_challenge"], q["redirect_uri"])
            )
        except AuthorizeError:
            return
        self.codes.append(parse_qs(urlparse(loc).query)["code"][0])


async def _call(app, *, path="/authorize", method="GET", qs="", cookie=None, body=b""):
    from oncofiles.server import MCPAuthorizeEmailCaptureMiddleware

    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(msg):
        sent.append(msg)

    headers = [(b"host", b"oncofiles.example.test")]
    if cookie:
        headers.append((b"cookie", cookie.encode("latin-1")))
    if body:
        headers.append((b"content-type", b"application/x-www-form-urlencoded"))
    scope = {
        "type": "http",
        "path": path,
        "method": method,
        "query_string": qs.encode("latin-1"),
        "headers": headers,
    }
    await MCPAuthorizeEmailCaptureMiddleware(app)(scope, receive, send)
    start = next((m for m in sent if m["type"] == "http.response.start"), None)
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return start, payload


def _form(page: bytes) -> dict[str, str]:
    html = page.decode("utf-8")
    return {
        name: unescape(value)
        for name, value in re.findall(r'name="(q|nonce)" value="([^"]*)"', html)
    }


async def _consent(app, page, cookie, **override):
    form = {**_form(page), **override}
    body = urlencode(form).encode("ascii")
    return await _call(app, path="/authorize/consent", method="POST", cookie=cookie, body=body)


async def test_no_session_redirects_to_sign_in(provider):
    app = _FastMCPLike(provider)
    start, _ = await _call(app, qs=_qs())
    assert app.calls == []
    assert start["status"] == 302
    loc = dict(start["headers"])[b"location"].decode("ascii")
    assert loc.startswith("/dashboard?next=")
    assert unquote(loc.split("next=", 1)[1]) == "/authorize?" + _qs()


async def test_forged_session_redirects(provider):
    app = _FastMCPLike(provider)
    start, _ = await _call(app, qs=_qs(), cookie="oncofiles_session=forged-session-token")
    assert start["status"] == 302 and app.calls == []


async def test_signed_in_get_shows_consent_page_and_binds_nothing(provider):
    app = _FastMCPLike(provider)
    start, page = await _call(app, qs=_qs(), cookie=_cookie())
    assert start["status"] == 200
    headers = dict(start["headers"])
    assert headers[b"x-frame-options"] == b"DENY"
    assert b"frame-ancestors 'none'" in headers[b"content-security-policy"]
    html = page.decode("utf-8")
    assert "Synthetic Connector" in html
    assert "as supplied by the app" in html
    assert REDIRECT in html  # the full callback URI for a web client
    assert CAREGIVER in html
    assert 'action="/authorize/consent"' in html
    # GET never reaches FastMCP and never binds.
    assert app.calls == [] and _binding_for_code == {} and provider.auth_codes == {}


async def test_consent_post_completes_a_bound_flow(odb, provider, caplog):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    _, page = await _call(app, qs=_qs("chal-ok"), cookie=cookie)
    with caplog.at_level(logging.DEBUG):
        start, _ = await _consent(app, page, cookie)
    assert app.calls == ["GET /authorize"]
    assert len(app.codes) == 1
    client = await provider.get_client(CLIENT_ID)
    auth_code = await provider.load_authorization_code(client, app.codes[0])
    await provider.exchange_authorization_code(client, auth_code)
    assert {r["user_email"] for r in await _token_rows(odb)} == {CAREGIVER}
    assert CAREGIVER not in caplog.text
    assert "/api/mcp/auth_callback" not in caplog.text


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"nonce": ""}, b"nonce_malformed"),
        ({"nonce": "1.2.3"}, b"nonce_invalid"),
        ({"nonce": "garbage"}, b"nonce_malformed"),
    ],
)
async def test_consent_post_with_bad_nonce_is_refused(provider, override, code):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    _, page = await _call(app, qs=_qs(), cookie=cookie)
    start, body = await _consent(app, page, cookie, **override)
    assert start["status"] == 403 and code in body
    assert app.calls == [] and provider.auth_codes == {}


async def test_consent_post_without_nonce_is_refused(provider):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    _, page = await _call(app, qs=_qs(), cookie=cookie)
    body = urlencode({"q": _form(page)["q"]}).encode("ascii")
    start, payload = await _call(
        app, path="/authorize/consent", method="POST", cookie=cookie, body=body
    )
    assert start["status"] == 400 and b"form_invalid" in payload
    assert app.calls == []


async def test_consent_nonce_is_single_use(provider):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    _, page = await _call(app, qs=_qs("chal-replay"), cookie=cookie)
    first, _ = await _consent(app, page, cookie)
    second, body = await _consent(app, page, cookie)
    assert second["status"] == 403 and b"nonce_replayed" in body
    assert app.calls == ["GET /authorize"]


async def test_expired_consent_nonce_is_refused(provider, monkeypatch):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    monkeypatch.setattr("oncofiles.server.CONSENT_NONCE_TTL_S", -5)
    _, page = await _call(app, qs=_qs(), cookie=cookie)
    start, body = await _consent(app, page, cookie)
    assert start["status"] == 403 and b"nonce_expired" in body
    assert app.calls == []


async def test_nonce_is_bound_to_the_session_and_the_flow(provider):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    _, page = await _call(app, qs=_qs("chal-a"), cookie=cookie)
    # Another session of the same account (different tid): refused.
    start, body = await _consent(app, page, _cookie())
    assert start["status"] == 403 and b"nonce_invalid" in body
    # Same session, the nonce moved onto another flow (challenge): refused.
    start, body = await _consent(app, page, cookie, q=_qs("chal-b"))
    assert start["status"] == 403 and b"nonce_invalid" in body
    # No session at all: refused.
    start, body = await _consent(app, page, None)
    assert start["status"] == 403 and b"no_session" in body
    assert app.calls == [] and provider.auth_codes == {}


@pytest.mark.parametrize(
    "param", ["client_id", "code_challenge", "redirect_uri", "response_type", "state"]
)
async def test_duplicate_params_are_refused(provider, param):
    app = _FastMCPLike(provider)
    second = {"client_id": "other", "code_challenge": "chal-2", "redirect_uri": REDIRECT}
    qs = _qs(extra=((param, second.get(param, "x")),))
    for cookie in (None, _cookie()):
        start, body = await _call(app, qs=qs, cookie=cookie)
        assert start["status"] == 400 and b"duplicate_param" in body
    assert app.calls == []


async def test_duplicate_params_in_consent_q_are_refused(provider):
    app = _FastMCPLike(provider)
    cookie = _cookie()
    _, page = await _call(app, qs=_qs("chal-d"), cookie=cookie)
    forged = _qs("chal-d", extra=(("code_challenge", "chal-attacker"),))
    start, body = await _consent(app, page, cookie, q=forged)
    assert start["status"] == 400 and b"duplicate_param" in body
    assert app.calls == []


async def test_missing_redirect_uri_is_refused(provider):
    app = _FastMCPLike(provider)
    qs = urlencode([("response_type", "code"), ("client_id", CLIENT_ID), ("code_challenge", "c")])
    start, body = await _call(app, qs=qs, cookie=_cookie())
    assert start["status"] == 400 and b"missing_param" in body


async def test_phishing_with_a_foreign_redirect_host_is_refused(provider):
    """Attacker client with its own redirect host, victim signed in."""
    foreign = "https://attacker.example.test/cb"
    with pytest.raises(RegistrationError):
        await provider.register_client(_client_info("evil", foreign))
    # Registered before the allowlist existed: still refused at /authorize.
    provider.clients["evil"] = _client_info("evil", foreign)
    app = _FastMCPLike(provider)
    start, body = await _call(app, qs=_qs("chal-ph", "evil", foreign), cookie=_cookie())
    assert start["status"] == 400 and b"redirect_uri_not_allowed" in body
    # An attacker-made consent POST for it (no valid nonce) is refused too.
    q = _qs("chal-ph", "evil", foreign)
    start, _ = await _call(
        app,
        path="/authorize/consent",
        method="POST",
        cookie=_cookie(),
        body=urlencode({"q": q, "nonce": "1.2.3"}).encode("ascii"),
    )
    assert start["status"] in (400, 403)
    assert app.calls == [] and provider.auth_codes == {} and _binding_for_code == {}


async def test_redirect_uri_not_registered_for_the_client_is_refused(provider):
    app = _FastMCPLike(provider)
    start, body = await _call(
        app, qs=_qs(redirect="https://claude.ai/somewhere-else"), cookie=_cookie()
    )
    assert start["status"] == 400 and b"redirect_uri_not_registered" in body


async def test_unknown_client_is_refused(provider):
    app = _FastMCPLike(provider)
    start, body = await _call(app, qs=_qs(client_id="nobody"), cookie=_cookie())
    assert start["status"] == 400 and b"unknown_client" in body


async def test_post_authorize_is_blocked(provider):
    app = _FastMCPLike(provider)
    start, _ = await _call(app, method="POST", qs=_qs(), cookie=_cookie())
    assert start["status"] == 405 and app.calls == []


async def test_opt_out_passes_sessionless_requests_through(provider, monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_OAUTH_ALLOW_UNBOUND", True)
    app = _FastMCPLike(provider)
    start, _ = await _call(app, qs=_qs("chal-oo"))
    assert start is None and app.calls == ["GET /authorize"]
    start, _ = await _call(app, method="POST", qs=_qs())
    assert start is None


async def test_overlong_or_non_ascii_query_is_refused(provider):
    app = _FastMCPLike(provider)
    for qs in (_qs() + "&pad=" + "a" * 5000, _qs() + "&x=é"):
        start, _ = await _call(app, qs=qs)
        assert start["status"] == 400
    assert app.calls == []


@pytest.mark.parametrize(
    ("path", "method"), [("/token", "POST"), ("/dashboard", "GET"), ("/mcp", "POST")]
)
async def test_other_requests_pass_through(provider, path, method):
    app = _FastMCPLike(provider)
    start, _ = await _call(app, path=path, method=method)
    assert app.calls == [f"{method} {path}"] and start is None


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("/authorize", True),
        ("/authorize?" + _qs(), True),
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
    assert js.index("var authorizeNext = ''") < js.index("(function init()")
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


# ── HEAD, rate limiting, bounded bookkeeping ─────────────────────────


@pytest.mark.parametrize("path", ["/authorize", "/authorize/consent"])
@pytest.mark.parametrize("cookie", [False, True])
async def test_head_and_other_methods_never_reach_fastmcp(provider, path, cookie):
    app = _FastMCPLike(provider)
    for method in ("HEAD", "PUT", "DELETE"):
        start, _ = await _call(
            app, path=path, method=method, qs=_qs(), cookie=_cookie() if cookie else None
        )
        assert start["status"] == 405
    assert app.calls == [] and provider.auth_codes == {}


def test_consent_paths_are_in_the_auth_rate_limit_class():
    from oncofiles.ratelimit import AUTH, classify

    assert classify("/authorize") == AUTH
    assert classify("/authorize/consent") == AUTH


def test_consent_middleware_sits_inside_the_rate_limiter():
    """main(): the limiter must run before the consent middleware's early
    returns (consent page, refusals), which still run before FastMCP."""
    src = (SRC / "server.py").read_text(encoding="utf-8")
    main_src = src[src.index("def main() -> None:") :]
    rl = main_src.index("Middleware(RateLimitMiddleware")
    consent = main_src.index("Middleware(MCPAuthorizeEmailCaptureMiddleware)")
    assert rl < consent
    assert main_src.count("Middleware(MCPAuthorizeEmailCaptureMiddleware)") == 1


def _limited(app, clock_value=1000.0):
    from oncofiles import ratelimit
    from oncofiles.ratelimit import AUTH, ClassLimits, Limit, RateLimitConfig, RateLimitMiddleware
    from oncofiles.server import MCPAuthorizeEmailCaptureMiddleware

    classes = dict(ratelimit.DEFAULTS)
    trickle = Limit(per_minute=1, burst=2)
    classes[AUTH] = ClassLimits(trickle, trickle, trickle, 8192)
    cfg = RateLimitConfig(classes=classes, trusted_proxies=(), static_bearer="")
    return RateLimitMiddleware(
        MCPAuthorizeEmailCaptureMiddleware(app),
        config=cfg,
        clock=lambda: clock_value,
        session_verifier=lambda token: None,
    )


async def test_rate_limit_applies_to_consent_get_and_post(provider):
    import httpx

    app = _FastMCPLike(provider)
    stack = _limited(app)
    transport = httpx.ASGITransport(app=stack, client=("203.0.113.9", 1234))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        headers = {"cookie": _cookie()}
        codes = [
            (await http.get("/authorize?" + _qs(), headers=headers)).status_code for _ in range(4)
        ]
        assert codes[:2] == [200, 200]
        assert 429 in codes[2:]
        post = await http.post(
            "/authorize/consent",
            content=urlencode({"q": _qs(), "nonce": "1.2.3"}),
            headers={**headers, "content-type": "application/x-www-form-urlencoded"},
        )
        assert post.status_code == 429
    assert app.calls == []


def test_binding_map_is_capped_oldest_first(monkeypatch):
    from oncofiles import persistent_oauth as po

    monkeypatch.setattr(po, "BINDING_CAP", 3)
    b = ConsentedBinding(CAREGIVER, CLIENT_ID, "c", REDIRECT)
    keys = ["code-a", "code-b", "code-c", "code-d", "code-e"]
    for key in keys:
        po._record_binding(key, b)
    assert list(po._binding_for_code) == keys[2:]
    assert po.pop_binding_for_code(keys[0]) is None
    assert po.pop_binding_for_code(keys[-1]) == b


def test_binding_prune_stops_at_the_first_live_entry(monkeypatch):
    """Structure-level: pruning pops expired entries from the front and does
    not iterate the live remainder."""
    from oncofiles import persistent_oauth as po

    b = ConsentedBinding(CAREGIVER, CLIENT_ID, "c", REDIRECT)
    old = po.time.monotonic() - po._EMAIL_STASH_TTL_S - 10
    stale = ("stale-a", "stale-b")
    for key in stale:
        po._binding_for_code[key] = (b, old)
    live_count = 50
    for i in range(live_count):
        po._binding_for_code[f"live-{i}"] = (b, po.time.monotonic())

    seen = []

    class Spy(type(po._binding_for_code)):
        def items(self):
            for kv in super().items():
                seen.append(kv[0])
                yield kv

    spy = Spy(po._binding_for_code)
    monkeypatch.setattr(po, "_binding_for_code", spy)
    po._prune_bindings()
    assert not any(key in spy for key in stale)
    assert len(spy) == live_count
    assert len(seen) <= len(stale) + 1  # the stale ones + the first live one


def test_consumed_nonce_map_is_capped_and_refuses_at_the_cap(monkeypatch):
    """At the cap new consents are refused (fail closed): evicting a live
    consumed nonce would make it replayable."""
    from oncofiles import server

    monkeypatch.setattr(server, "CONSENT_NONCE_CAP", 2)
    nonces = [_mk_nonce(f"chal-{i}") for i in range(3)]
    assert _chk(nonces[0], "chal-0") == ""
    assert _chk(nonces[1], "chal-1") == ""
    assert _chk(nonces[2], "chal-2") == "consent_busy"
    assert _chk(nonces[0], "chal-0") == "nonce_replayed"
    assert len(server._consumed_consent_nonces) == 2


def test_consumed_nonce_prune_is_front_only():
    from oncofiles import server

    now = int(server.time.time())
    server._consumed_consent_nonces["old-a"] = now - 10
    server._consumed_consent_nonces["old-b"] = now - 5
    for i in range(20):
        server._consumed_consent_nonces[f"live-{i}"] = now + 100
    server._prune_consent_nonces(now)
    assert list(server._consumed_consent_nonces)[:1] == ["live-0"]
    assert len(server._consumed_consent_nonces) == 20


def _mk_nonce(challenge):
    from oncofiles.server import _make_consent_nonce

    return _make_consent_nonce(CAREGIVER, "tid898", CLIENT_ID, challenge, REDIRECT)


def _chk(nonce, challenge):
    from oncofiles.server import _check_consent_nonce

    return _check_consent_nonce(nonce, CAREGIVER, "tid898", CLIENT_ID, challenge, REDIRECT)
