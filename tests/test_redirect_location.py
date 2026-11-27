"""Pentest 2026-11-27, finding 1: no redirect of ours points to ``http://`` on our host.

Behind Traefik ``scope["scheme"]`` is ``http`` (uvicorn trusts no forwarded
header, on purpose), so Starlette's trailing-slash 307 said
``Location: http://oncofiles.com/mcp``. A 307 keeps method and body: an MCP
client configured with ``/mcp/`` would resend its bearer in cleartext.
``RelativeRedirectMiddleware`` (``oncofiles/redirect_location.py``) turns
every 3xx Location to the request's own host over http into a relative
reference and leaves everything else untouched.

Covered here:

* the pure rewrite rule (same host, other host, https, network-path tricks);
* the middleware on a small app (only 3xx, only ``Location``, external
  redirects byte for byte);
* the PRODUCTION stack, built from exactly the middleware ``server.main()``
  hands to ``mcp.run``: the slash redirect of a sample of public routes, a
  lock over the slash form of EVERY served path, handler and middleware
  redirects, an external redirect, and one request through real uvicorn;
* ``main()`` registers the middleware outermost of ours and never widens
  uvicorn's forwarded-header trust.

Synthetic hosts only (``*.test`` / ``*.example``).
"""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from oncofiles.redirect_location import (
    RelativeRedirectMiddleware,
    relative_location,
    request_authority,
)

HOST = "oncofiles.test"
OPERATOR_TOKEN = "synthetic-operator-token-pentest"


# ── production stack helpers (also used by tests/test_hidden_routes.py) ──────


def main_wiring(monkeypatch) -> dict:
    """The keyword arguments ``server.main()`` passes to ``mcp.run`` in HTTP
    mode. Nothing is served: ``run`` is replaced for the test."""
    from oncofiles import server

    captured: dict = {}

    def fake_run(self, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(type(server.mcp), "run", fake_run)
    server.main()
    assert captured, "main() did not call mcp.run"
    return captured


def production_http_app(monkeypatch):
    """The app as production builds it: real ``PersistentOAuthProvider``,
    streamable-http, stateless, and main()'s own middleware list."""
    from fastmcp.server.auth.auth import ClientRegistrationOptions

    from oncofiles import server
    from oncofiles.persistent_oauth import PersistentOAuthProvider

    wiring = main_wiring(monkeypatch)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    provider = PersistentOAuthProvider(
        bearer_token=OPERATOR_TOKEN,
        base_url=f"https://{HOST}",
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    monkeypatch.setattr(server.mcp, "auth", provider)
    return server.mcp.http_app(
        transport="streamable-http", stateless_http=True, middleware=wiring["middleware"]
    )


def asgi_client(app, client_ip="203.0.113.7", host=HOST) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(client_ip, 40123))
    return httpx.AsyncClient(transport=transport, base_url=f"http://{host}")


# ── the rewrite rule ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("location", "authority", "expected"),
    [
        (f"http://{HOST}/mcp", HOST, "/mcp"),
        (f"http://{HOST}/privacy?lang=en#top", HOST, "/privacy?lang=en#top"),
        (f"http://{HOST}", HOST, "/"),
        (f"http://{HOST}/", HOST, "/"),
        (f"HTTP://{HOST.upper()}/Dashboard", HOST, "/Dashboard"),
        ("http://127.0.0.1:8000/demo", "127.0.0.1:8000", "/demo"),
        ("http://[::1]:8000/demo", "[::1]:8000", "/demo"),
        # a network-path reference ("//host") would leave our host: collapsed
        (f"http://{HOST}//evil.example/x", HOST, "/evil.example/x"),
        (f"http://{HOST}/\\evil.example/x", HOST, "/evil.example/x"),
        (f"http://{HOST}/\t/evil.example/x", HOST, "/evil.example/x"),
        (f"http://{HOST}/%2F/evil.example", HOST, "/%2F/evil.example"),
    ],
)
def test_same_host_http_becomes_relative(location, authority, expected):
    assert relative_location(location, authority) == expected


@pytest.mark.parametrize(
    ("location", "authority"),
    [
        # other hosts, also over http, and look-alikes
        ("https://accounts.google.example/o/oauth2/auth?x=1", HOST),
        ("http://partner.example/callback", HOST),
        (f"http://{HOST}.evil.example/x", HOST),
        (f"http://{HOST}@evil.example/x", HOST),
        (f"http://evil.example/{HOST}/x", HOST),
        (f"http://{HOST}:8080/x", HOST),  # another port is another origin
        # https to our own host is already safe
        (f"https://{HOST}/mcp", HOST),
        # relative / network-path / garbage stay as they are
        ("/mcp", HOST),
        (f"//{HOST}/mcp", HOST),
        ("http://[broken/x", HOST),
        # no known authority: never guess
        (f"http://{HOST}/mcp", ""),
    ],
)
def test_everything_else_is_left_alone(location, authority):
    assert relative_location(location, authority) is None


def test_authority_is_the_one_starlette_builds():
    with_host = {
        "type": "http",
        "scheme": "http",
        "path": "/x",
        "headers": [(b"host", b"oncofiles.test:8080")],
        "server": ("10.0.0.5", 8000),
    }
    assert request_authority(with_host) == "oncofiles.test:8080"
    without_host = {**with_host, "headers": []}
    assert request_authority(without_host) == "10.0.0.5:8000"
    assert request_authority({"type": "http", "headers": []}) == ""


# ── the middleware on a small app ────────────────────────────────────────────


def _small_app():
    async def to(request):
        return RedirectResponse(
            request.query_params["u"], status_code=int(request.query_params["s"])
        )

    async def slash_target(request):
        return PlainTextResponse("ok")

    async def created(request):
        return Response(status_code=201, headers={"Location": f"http://{HOST}/thing/1"})

    app = Starlette(
        routes=[
            Route("/to", to),
            Route("/target", slash_target),
            Route("/created", created, methods=["POST"]),
        ]
    )
    return RelativeRedirectMiddleware(app)


async def test_router_slash_redirect_is_relative():
    async with asgi_client(_small_app()) as c:
        r = await c.get("/target/?q=1")
    assert r.status_code == 307
    assert r.headers["location"] == "/target?q=1"


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_every_3xx_to_our_host_is_relative(status):
    async with asgi_client(_small_app()) as c:
        r = await c.get("/to", params={"u": f"http://{HOST}/landing?a=b", "s": status})
    assert r.status_code == status
    assert r.headers["location"] == "/landing?a=b"


@pytest.mark.parametrize(
    "target",
    [
        "https://accounts.google.example/auth?redirect_uri=http://oncofiles.test/cb&state=s",
        "http://partner.example/callback?code=c",
        f"https://{HOST}/already-https",
        "/already-relative",
    ],
)
async def test_external_and_safe_redirects_are_untouched(target):
    async with asgi_client(_small_app()) as c:
        r = await c.get("/to", params={"u": target, "s": 302})
    assert r.status_code == 302
    assert r.headers["location"] == RedirectResponse(target).headers["location"]


async def test_non_redirect_location_is_untouched():
    """Only redirects: a 201's Location is not a navigation target."""
    async with asgi_client(_small_app()) as c:
        r = await c.post("/created")
    assert r.status_code == 201
    assert r.headers["location"] == f"http://{HOST}/thing/1"


# ── the production stack ─────────────────────────────────────────────────────


@pytest.fixture
def prod_app(monkeypatch):
    return production_http_app(monkeypatch)


SLASH_SAMPLE = [
    ("/mcp/", "/mcp"),
    ("/dashboard/", "/dashboard"),
    ("/demo/", "/demo"),
    ("/privacy/", "/privacy"),
    ("/sk/privacy/", "/sk/privacy"),
    ("/movement-atlas/", "/movement-atlas"),
    ("/terms/?lang=en", "/terms?lang=en"),
    ("/mcp//", "/mcp"),
]


@pytest.mark.parametrize(("path", "expected"), SLASH_SAMPLE)
@pytest.mark.parametrize("method", ["GET", "HEAD", "POST", "DELETE"])
@pytest.mark.parametrize("host", [HOST, "oncofiles.test:8443"])
async def test_production_slash_redirects_are_relative(prod_app, path, expected, method, host):
    async with asgi_client(prod_app, host=host) as c:
        r = await c.request(method, path, headers={"Authorization": "Bearer synthetic-mcp-token"})
    assert r.status_code == 307, (method, path, r.status_code)
    assert r.headers["location"] == expected


async def test_production_slash_redirect_never_follows_a_spoofed_host(prod_app):
    """A Host header is client input: the relative form never names it."""
    async with asgi_client(prod_app, host="evil.example") as c:
        r = await c.get("/privacy/")
    assert r.status_code == 307 and r.headers["location"] == "/privacy"


def _static_paths(app) -> list[str]:
    paths = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if path and path != "/" and "{" not in path:
            paths.append(path)
    return sorted(set(paths))


async def test_lock_no_slash_redirect_of_any_served_path_is_http(prod_app, monkeypatch):
    """Every served path without parameters, in its slash forms: an ordinary
    path is a 307 to its relative self, a hidden one (#887 / #950) the unknown
    path's 404 (tests/test_hidden_routes.py). Never ``http://``."""
    from oncofiles.hidden_routes import HIDDEN_PATHS

    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")  # many requests, one client
    paths = _static_paths(prod_app)
    assert {"/mcp", "/dashboard", "/privacy", "/ops/mcp", "/inbound/brevo"} <= set(paths)
    async with asgi_client(prod_app) as c:
        for path in paths:
            for suffix in ("/", "//"):
                for method in ("GET", "POST"):
                    r = await c.request(method, path + suffix)
                    location = r.headers.get("location", "")
                    assert not location.lower().startswith("http://"), (path, location)
                    if path in HIDDEN_PATHS:
                        assert r.status_code == 404 and not location, (path, suffix, method)
                    else:
                        assert (r.status_code, location) == (307, path), (path, suffix, method)


@pytest.mark.parametrize(
    ("path", "expected_location"),
    [("/favicon.ico", "/favicon.svg"), ("/apple-touch-icon.png", "/favicon.svg")],
)
async def test_handler_redirects_stay_relative(prod_app, path, expected_location):
    async with asgi_client(prod_app) as c:
        r = await c.get(path)
    assert r.status_code == 301 and r.headers["location"] == expected_location


async def test_authorize_sign_in_redirect_is_relative(prod_app):
    """The #898 consent middleware's 302 to the sign-in page (a middleware
    inside ours) stays a relative reference."""
    params = {
        "client_id": "synthetic-client",
        "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
        "response_type": "code",
        "code_challenge": "A" * 43,
        "code_challenge_method": "S256",
        "state": "synthetic-state",
    }
    async with asgi_client(prod_app) as c:
        r = await c.get("/authorize", params=params)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("/dashboard?next=")
    assert not location.lower().startswith("http")


async def test_production_external_redirect_is_untouched(prod_app):
    """An absolute redirect to another host (Google OAuth, a client's
    redirect_uri) passes the production stack byte for byte, including an
    http URL of ours inside its query."""
    google = (
        "https://accounts.google.example/auth?client_id=synthetic"
        "&redirect_uri=http://oncofiles.test/oauth/callback&prompt=consent"
    )
    partner = "http://partner.example/cb?code=synthetic"

    async def go(request):
        return RedirectResponse(google if request.query_params.get("to") == "g" else partner)

    prod_app.router.routes.append(Route("/__synthetic-redirect", go))
    async with asgi_client(prod_app) as c:
        g = await c.get("/__synthetic-redirect", params={"to": "g"})
        p = await c.get("/__synthetic-redirect", params={"to": "p"})
    assert g.status_code == p.status_code == 307
    assert g.headers["location"] == RedirectResponse(google).headers["location"]
    assert p.headers["location"] == RedirectResponse(partner).headers["location"]


async def test_real_uvicorn_slash_redirect_is_relative(prod_app):
    """Through real uvicorn on loopback, as Traefik reaches the app: scheme
    http, Host with a port. The Location is relative, so an https client stays
    on https."""
    import uvicorn

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(prod_app, lifespan="off", ws="none", log_level="warning")
    srv = uvicorn.Server(config)
    task = asyncio.create_task(srv.serve(sockets=[sock]))
    try:
        for _ in range(500):
            if srv.started:
                break
            await asyncio.sleep(0.01)
        assert srv.started, "test server did not start"
        async with httpx.AsyncClient() as http:
            r = await http.post(
                f"http://127.0.0.1:{port}/mcp/",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Authorization": "Bearer synthetic-mcp-token"},
            )
            d = await http.get(f"http://127.0.0.1:{port}/dashboard/")
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
    assert (r.status_code, r.headers["location"]) == (307, "/mcp")
    assert (d.status_code, d.headers["location"]) == (307, "/dashboard")


# ── wiring in server.main() ──────────────────────────────────────────────────


def test_main_registers_the_middleware_outermost(monkeypatch):
    wiring = main_wiring(monkeypatch)
    classes = [m.cls for m in wiring["middleware"]]
    assert classes[0] is RelativeRedirectMiddleware
    assert classes.count(RelativeRedirectMiddleware) == 1


def test_main_never_widens_forwarded_header_trust(monkeypatch):
    """The fix must not come from trusting X-Forwarded-*: that would let a
    forwarded header become the #887 OpsGate peer. uvicorn keeps its default
    FORWARDED_ALLOW_IPS (127.0.0.1)."""
    wiring = main_wiring(monkeypatch)
    uv = {k.lower() for k in wiring.get("uvicorn_config") or {}}
    assert "forwarded_allow_ips" not in uv and "proxy_headers" not in uv
    from oncofiles import redirect_location

    src = Path(redirect_location.__file__).read_text(encoding="utf-8")
    code = src.split('"""', 2)[2]  # skip the module docstring
    for needle in ("x-forwarded", "forwarded", 'scope["client"]', "scope['client']"):
        assert needle not in code.lower(), needle
    assert 'scope["scheme"] =' not in code
