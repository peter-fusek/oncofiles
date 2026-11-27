"""Same-host redirects never point to ``http://`` (external pentest 2026-11-27).

Behind Traefik the app sees ``scope["scheme"] == "http"``: uvicorn trusts no
forwarded header (``FORWARDED_ALLOW_IPS`` stays at its default ``127.0.0.1``
on purpose, so a forwarded header can never become the #887 OpsGate peer).
Every absolute URL Starlette builds from the scope is therefore
``http://oncofiles.com/…``. The router's trailing-slash redirect
(``redirect_slashes``: ``/mcp/`` → ``/mcp``, ``/dashboard/`` → ``/dashboard``)
is a 307, which keeps method and body: an MCP client configured with
``/mcp/`` would have resent its bearer and JSON-RPC body in cleartext to
port 80.

``RelativeRedirectMiddleware`` rewrites the ``Location`` of every 3xx
response that points to the request's OWN authority over plain ``http``
into a relative reference (``/path?query#fragment``, valid per RFC 9110
§10.2.2), so the client keeps the scheme it used: https in production,
http in local development. Left byte for byte: absolute redirects to any
other host (Google OAuth, an OAuth client's ``redirect_uri``, …), https
Locations, relative ones and every non-3xx response.

The request's authority is computed exactly as Starlette computes it for
``URL(scope=…)`` (the Host header, else the server address), so whatever
Starlette or a handler builds from the scope is recognised. A path that
would start with ``//`` or ``/\\`` (also with tabs or newlines between, which
browsers drop) would be a network-path reference to another host, so its
leading slashes are collapsed to one.

Registered first in ``server.main()``'s middleware list (the outermost of
ours), so redirects of the router, of our routes, of FastMCP's OAuth routes
and of our other middlewares all pass through it. Neither
``scope["scheme"]`` nor ``scope["client"]`` is touched. Lock tests:
``tests/test_redirect_location.py``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.datastructures import URL
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Stripped from the start of a rewritten path before one "/" is put back:
# slashes, backslashes and every ASCII control / space character (browsers
# remove tab and newline anywhere in a URL, so "/\t/evil.example" would be
# "//evil.example").
_LEADING_JUNK = "/\\" + "".join(chr(c) for c in range(0x21))


def request_authority(scope: Scope) -> str:
    """The netloc Starlette puts into ``URL(scope=scope)`` (Host header, else
    the server address); empty when neither is known."""
    try:
        return URL(scope=scope).netloc
    except Exception:  # a scope without a path or with an odd scheme
        return ""


def relative_location(location: str, authority: str) -> str | None:
    """The relative form of ``location`` when it is ``http://<authority>/…``,
    else None (leave the header as it is)."""
    if not authority:
        return None
    try:
        parts = urlsplit(location)
    except ValueError:
        return None
    if parts.scheme.lower() != "http" or parts.netloc.lower() != authority.lower():
        return None
    out = "/" + parts.path.lstrip(_LEADING_JUNK)
    if parts.query:
        out += "?" + parts.query
    if parts.fragment:
        out += "#" + parts.fragment
    return out


class RelativeRedirectMiddleware:
    """Pure ASGI: make a 3xx ``Location`` to our own host over http relative."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_relative(message: Message) -> None:
            if message["type"] == "http.response.start" and 300 <= message.get("status", 0) < 400:
                headers = list(message.get("headers") or [])
                changed = False
                for i, (name, value) in enumerate(headers):
                    if name.lower() != b"location":
                        continue
                    relative = relative_location(value.decode("latin-1"), request_authority(scope))
                    if relative is not None:
                        headers[i] = (name, relative.encode("latin-1"))
                        changed = True
                if changed:
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_relative)
