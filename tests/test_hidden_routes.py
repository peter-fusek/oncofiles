"""Pentest 2026-11-27, finding 2: hidden routes cannot be told from an unknown path.

``/ops/mcp`` (#887) answered GET/HEAD/PUT/OPTIONS with ``405`` +
``Allow: POST, DELETE`` and ``/ops/mcp/`` with a 307, from any peer;
``/inbound/brevo`` and ``/inbound/email`` (#950) answered GET with ``405`` +
``Allow: POST`` and their slash form with a 307. An unknown path gets 404, so
the routes were detectable. ``oncofiles/hidden_routes.py`` makes every
method and every slash form answer the unknown path's 404 whenever the gate
would refuse:

* ``/ops/mcp``: a non-loopback peer (forwarding headers never count), for
  every method and slash form; on loopback POST/DELETE still reach OpsGate
  (401 without the operator bearer, the MCP handler with it), any other
  method and the slash form are the 404 too;
* ``/inbound/*``: disabled route, missing or wrong token / signature, for
  every method; a non-POST method and the slash form are the 404 even with a
  valid token. POST keeps its order: enabled check before any body byte.

Every comparison is against a random unknown path (same prefix, so the same
rate-limit class, and one at the root) over the PRODUCTION stack (main()'s
own middleware): status, every header and the body must be identical.

Synthetic tokens and hosts only.
"""

from __future__ import annotations

import secrets

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from oncofiles import inbound_email
from oncofiles.hidden_routes import (
    HIDDEN_PATHS,
    INBOUND_PATHS,
    SLASH_SUFFIX,
    HiddenRoute,
    UnknownPath,
    install_hidden_route_guards,
)
from oncofiles.tool_surface import OPS_MCP_PATH, OpsGate
from tests.test_redirect_location import OPERATOR_TOKEN, asgi_client, production_http_app

METHODS = ["GET", "HEAD", "PUT", "OPTIONS", "POST", "DELETE", "PATCH", "PROPFIND"]
NON_LOOPBACK = ["203.0.113.7", "172.17.0.1", "10.0.0.5", "::ffff:127.0.0.1"]
FORWARDED_AS_LOOPBACK = {
    "X-Forwarded-For": "127.0.0.1",
    "Forwarded": "for=127.0.0.1",
    "X-Real-IP": "127.0.0.1",
}
BREVO_SECRET = "synthetic-brevo-token-pentest"
MIME_SECRET = "synthetic-mime-secret-pentest"


def slash_forms(path: str) -> list[str]:
    return [path + "/", path + "//", path + "/x", path + "/?q=1"]


def _unknown(prefix: str) -> str:
    return f"{prefix}no-such-path-{secrets.token_hex(4)}"


def _shape(r: httpx.Response) -> tuple:
    """Everything an outsider can observe (ASGI: no Date/Server header)."""
    return r.status_code, sorted(r.headers.multi_items()), r.content


async def _same_as_unknown(client, method, path, prefix, headers=None, body=b"{}"):
    """``path`` answers byte for byte what two random unknown paths answer."""
    kwargs = {"headers": headers or {}}
    if method not in ("GET", "HEAD", "OPTIONS"):
        kwargs["content"] = body
    hidden = await client.request(method, path, **kwargs)
    same_prefix = await client.request(method, _unknown(prefix), **kwargs)
    root = await client.request(method, _unknown("/"), **kwargs)
    assert hidden.status_code == 404, (method, path, hidden.status_code, hidden.headers)
    assert _shape(hidden) == _shape(same_prefix), (method, path)
    assert _shape(hidden) == _shape(root), (method, path)
    assert "allow" not in hidden.headers and "location" not in hidden.headers
    assert "www-authenticate" not in hidden.headers


@pytest.fixture
def prod_app(monkeypatch):
    return production_http_app(monkeypatch)


@pytest.fixture
def inbound_disabled(monkeypatch):
    monkeypatch.delenv(inbound_email.MIME_SECRET_ENV, raising=False)
    monkeypatch.delenv(inbound_email.BREVO_TOKEN_ENV, raising=False)


@pytest.fixture
def inbound_enabled(monkeypatch):
    monkeypatch.setenv(inbound_email.MIME_SECRET_ENV, MIME_SECRET)
    monkeypatch.setenv(inbound_email.BREVO_TOKEN_ENV, BREVO_SECRET)


# ── structure ────────────────────────────────────────────────────────────────


def test_every_hidden_path_is_guarded_in_the_production_app(prod_app):
    routes = list(prod_app.routes)
    for path in HIDDEN_PATHS:
        serving = [r for r in routes if getattr(r, "path", None) == path]
        assert len(serving) == 1 and isinstance(serving[0], HiddenRoute), path
        slash = [r for r in routes if getattr(r, "path", None) == path + SLASH_SUFFIX]
        assert len(slash) == 1 and isinstance(slash[0].endpoint, UnknownPath), path
        assert slash[0].methods is None  # every method
        # nothing else lives under the hidden path (the catch-all would shadow it)
        under = [
            r.path
            for r in routes
            if getattr(r, "path", "").startswith(path + "/") and r is not slash[0]
        ]
        assert under == [], under
    ops = next(r for r in routes if getattr(r, "path", None) == OPS_MCP_PATH)
    assert isinstance(ops.endpoint, OpsGate) and ops.methods == {"POST", "DELETE"}
    from oncofiles import server

    for path in INBOUND_PATHS:
        route = next(r for r in routes if getattr(r, "path", None) == path)
        assert route.methods == {"POST"}
        assert route.endpoint.__module__ == server.__name__


def test_install_is_idempotent_and_refuses_a_shadowed_route():
    async def post(request):
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/hidden", post, methods=["POST"])])
    assert install_hidden_route_guards(app, ["/hidden", "/absent"]) == ["/hidden"]
    before = list(app.routes)
    assert install_hidden_route_guards(app, ["/hidden"]) == ["/hidden"]
    assert list(app.routes) == before

    two = Starlette(
        routes=[Route("/x", post, methods=["POST"]), Route("/x", post, methods=["GET"])]
    )
    with pytest.raises(ValueError, match="served by 2 routes"):
        install_hidden_route_guards(two, ["/x"])


async def test_guard_the_guard_plain_route_leaks_and_the_guard_fixes_it():
    """Without the guard, the router itself reveals a POST-only route (405 +
    Allow, 307 for the slash form); with it, both are the unknown 404."""

    async def post(request):
        return PlainTextResponse("ok")

    plain = Starlette(routes=[Route("/hidden", post, methods=["POST"])])
    async with asgi_client(plain) as c:
        assert (await c.get("/hidden")).status_code == 405
        assert (await c.get("/hidden/")).status_code == 307
    install_hidden_route_guards(plain, ["/hidden"])
    async with asgi_client(plain) as c:
        for method in METHODS:
            if method == "POST":
                continue
            await _same_as_unknown(c, method, "/hidden", "/")
        for path in slash_forms("/hidden"):
            await _same_as_unknown(c, "POST", path, "/")
        assert (await c.post("/hidden")).text == "ok"


# ── /ops/mcp ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("client_ip", NON_LOOPBACK)
@pytest.mark.parametrize("method", METHODS)
async def test_ops_every_method_from_outside_is_the_unknown_404(prod_app, client_ip, method):
    auth = {"Authorization": f"Bearer {OPERATOR_TOKEN}", **FORWARDED_AS_LOOPBACK}
    async with asgi_client(prod_app, client_ip=client_ip) as c:
        await _same_as_unknown(c, method, OPS_MCP_PATH, "/ops/", headers=auth)


@pytest.mark.parametrize("client_ip", ["203.0.113.7", "127.0.0.1", "::1"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "DELETE", "OPTIONS"])
async def test_ops_slash_forms_are_the_unknown_404_from_any_peer(prod_app, client_ip, method):
    auth = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
    async with asgi_client(prod_app, client_ip=client_ip) as c:
        for path in slash_forms(OPS_MCP_PATH):
            await _same_as_unknown(c, method, path, "/ops/", headers=auth)


@pytest.mark.parametrize("client_ip", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "OPTIONS", "PATCH"])
async def test_ops_other_methods_on_loopback_are_the_unknown_404(prod_app, client_ip, method):
    auth = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
    async with asgi_client(prod_app, client_ip=client_ip) as c:
        await _same_as_unknown(c, method, OPS_MCP_PATH, "/ops/", headers=auth)


@pytest.mark.parametrize("client_ip", ["127.0.0.1", "::1"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_ops_loopback_post_delete_still_reach_the_gate(prod_app, client_ip, method):
    """Unchanged: on loopback the gate answers 401 without the operator bearer."""
    async with asgi_client(prod_app, client_ip=client_ip) as c:
        r = await c.request(method, OPS_MCP_PATH, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == 'Bearer realm="oncofiles-ops"'


async def test_ops_cors_preflight_is_the_same_as_for_an_unknown_path(prod_app):
    preflight = {
        "Origin": "https://claude.ai",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization",
    }
    async with asgi_client(prod_app) as c:
        hidden = await c.options(OPS_MCP_PATH, headers=preflight)
        unknown = await c.options(_unknown("/ops/"), headers=preflight)
    assert _shape(hidden) == _shape(unknown)


# ── /inbound/* ───────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("inbound_disabled")
@pytest.mark.parametrize("path", INBOUND_PATHS)
@pytest.mark.parametrize("method", METHODS)
async def test_disabled_inbound_every_method_is_the_unknown_404(prod_app, path, method):
    async with asgi_client(prod_app) as c:
        await _same_as_unknown(c, method, path, "/inbound/")


@pytest.mark.usefixtures("inbound_enabled")
@pytest.mark.parametrize("token", [None, "", "wrong", MIME_SECRET])
@pytest.mark.parametrize("method", METHODS)
async def test_brevo_without_a_valid_token_is_the_unknown_404(prod_app, token, method):
    headers = {} if token is None else {inbound_email.BREVO_TOKEN_HEADER: token}
    async with asgi_client(prod_app) as c:
        await _same_as_unknown(c, method, "/inbound/brevo", "/inbound/", headers=headers)


@pytest.mark.usefixtures("inbound_enabled")
@pytest.mark.parametrize("method", METHODS)
async def test_mime_without_a_valid_signature_is_the_unknown_404(prod_app, method):
    headers = {inbound_email.SIGNATURE_HEADER: "sha256=" + "0" * 64}
    async with asgi_client(prod_app) as c:
        await _same_as_unknown(c, method, "/inbound/email", "/inbound/", headers=headers)


@pytest.mark.usefixtures("inbound_enabled")
@pytest.mark.parametrize("method", [m for m in METHODS if m != "POST"])
async def test_valid_token_never_opens_another_method(prod_app, method):
    headers = {inbound_email.BREVO_TOKEN_HEADER: BREVO_SECRET}
    async with asgi_client(prod_app) as c:
        await _same_as_unknown(c, method, "/inbound/brevo", "/inbound/", headers=headers)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("path", INBOUND_PATHS)
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "PUT", "OPTIONS"])
async def test_inbound_slash_forms_are_the_unknown_404(
    prod_app, monkeypatch, enabled, path, method
):
    if enabled:
        monkeypatch.setenv(inbound_email.MIME_SECRET_ENV, MIME_SECRET)
        monkeypatch.setenv(inbound_email.BREVO_TOKEN_ENV, BREVO_SECRET)
    else:
        monkeypatch.delenv(inbound_email.MIME_SECRET_ENV, raising=False)
        monkeypatch.delenv(inbound_email.BREVO_TOKEN_ENV, raising=False)
    headers = {inbound_email.BREVO_TOKEN_HEADER: BREVO_SECRET}
    async with asgi_client(prod_app) as c:
        for form in slash_forms(path):
            await _same_as_unknown(c, method, form, "/inbound/", headers=headers)


@pytest.mark.usefixtures("inbound_enabled")
async def test_brevo_post_with_a_valid_token_still_reaches_the_handler(prod_app, monkeypatch):
    """The gate admitted it: without INBOUND_JOBS_SECRET the handler answers
    503 + Retry-After (#952), never the hidden-route 404."""
    monkeypatch.delenv("INBOUND_JOBS_SECRET", raising=False)
    async with asgi_client(prod_app) as c:
        r = await c.post(
            "/inbound/brevo",
            content=b'{"items": []}',
            headers={inbound_email.BREVO_TOKEN_HEADER: BREVO_SECRET},
        )
    assert r.status_code == 503 and r.headers["retry-after"] == "30"


@pytest.mark.usefixtures("inbound_disabled")
@pytest.mark.parametrize("path", INBOUND_PATHS)
async def test_disabled_inbound_post_reads_no_body(prod_app, path):
    """Order unchanged for POST: the enabled check runs before any body byte."""
    read = {"n": 0}

    async def receive():
        read["n"] += 1
        return {"type": "http.request", "body": b"x" * 64, "more_body": False}

    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"oncofiles.test"), (b"content-length", str(10**12).encode())],
        "client": ("203.0.113.7", 40123),
        "server": ("oncofiles.test", 80),
    }
    await prod_app(scope, receive, send)
    assert sent[0]["status"] == 404 and read["n"] == 0
