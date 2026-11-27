"""Anonymous tool catalogue on the public ``/mcp`` (#1004, maintainer decision).

A client WITHOUT any ``Authorization`` header may call ``initialize``,
``notifications/initialized`` and ``tools/list``: directory checkers (and a
person looking before connecting) see what the server offers. Everything
else keeps FastMCP's ``RequireAuthMiddleware`` and its 401 +
``WWW-Authenticate`` (protected-resource metadata), so OAuth discovery is
unchanged.

What the anonymous path can reach: the MCP handshake and the tool list of
the USER surface (``tool_surface.ToolSurfaceTransform`` hides operator and
unclassified tools from every non-``/ops/mcp`` request). Listing tools makes
no DB, Drive or patient call; the FastMCP middlewares of this app hook only
``on_call_tool``. Stateless HTTP, so nothing is kept between requests.

Fail closed:

* any ``Authorization`` header (also an invalid or expired token) → the
  normal auth path, so a client with a stale token still gets the 401 that
  makes it refresh or sign in again;
* only POST; the body is read up to ``ANON_MAX_BODY_BYTES`` (the rate
  limiter's own ``mcp`` body cap applies before this); a longer body, a body
  that is not JSON, a JSON-RPC response, a non-``2.0`` message, or a batch
  with ANY other method → the normal auth path (401);
* the body is read once and replayed unchanged to whichever app handles it.

Off-switch: runtime env ``MCP_ANON_TOOLS_LIST`` (default on; ``false`` /
``0`` / ``no`` / ``off`` = every anonymous request gets today's 401). The
anonymous ``mcp`` rate-limit buckets (#591) apply to these requests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

ANON_METHODS: frozenset[str] = frozenset({"initialize", "notifications/initialized", "tools/list"})
# A handshake or a tools/list request is a few hundred bytes.
ANON_MAX_BODY_BYTES = 64 * 1024
ENV_SWITCH = "MCP_ANON_TOOLS_LIST"
_OFF = frozenset({"0", "false", "no", "off"})


def anon_enabled() -> bool:
    """Read at call time, so the runtime env (and tests) decide per request."""
    return os.environ.get(ENV_SWITCH, "true").strip().lower() not in _OFF


def has_authorization(scope: Scope) -> bool:
    return any(key.lower() == b"authorization" for key, _ in scope.get("headers") or [])


def anonymous_allowed(body: bytes) -> bool:
    """True only when every JSON-RPC message in ``body`` is a request or
    notification of one of ``ANON_METHODS``."""
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False
    messages = payload if isinstance(payload, list) else [payload]
    if not messages:
        return False
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return False
        if msg.get("method") not in ANON_METHODS:
            return False
    return True


async def _read_bounded(receive: Receive, limit: int) -> tuple[list[Message], bytes, bool]:
    """Read request messages until the body is complete or exceeds ``limit``.

    Returns (messages read, body so far, complete)."""
    messages: list[Message] = []
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        messages.append(message)
        if message["type"] != "http.request":  # disconnect
            return messages, b"".join(chunks), False
        chunk = message.get("body", b"")
        size += len(chunk)
        chunks.append(chunk)
        if size > limit:
            return messages, b"".join(chunks), False
        if not message.get("more_body", False):
            return messages, b"".join(chunks), True


def _replay(messages: list[Message], receive: Receive) -> Callable[[], Awaitable[Message]]:
    """A receive that first returns ``messages``, then the original stream."""
    pending = list(messages)

    async def replay() -> Message:
        if pending:
            return pending.pop(0)
        return await receive()

    return replay


class AnonCatalogGate:
    """ASGI endpoint for ``/mcp``: ``app`` is FastMCP's auth-wrapped handler
    (unchanged path for everything), ``inner`` the bare streamable-http
    handler (only for an anonymous catalogue request)."""

    def __init__(self, app: ASGIApp, inner: ASGIApp):
        self.app = app
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            or has_authorization(scope)
            or not anon_enabled()
        ):
            await self.app(scope, receive, send)
            return
        messages, body, complete = await _read_bounded(receive, ANON_MAX_BODY_BYTES)
        replay = _replay(messages, receive)
        if complete and anonymous_allowed(body):
            await self.inner(scope, replay, send)
            return
        await self.app(scope, replay, send)


def install_anon_catalog(app: Any, mcp_path: str) -> bool:
    """Wrap the ``mcp_path`` route's endpoint (FastMCP's
    ``RequireAuthMiddleware``) in ``AnonCatalogGate``. Call AFTER
    ``tool_surface.install_ops_route`` so ``/ops/mcp`` keeps the plain auth
    wrapper. Returns False when the route is missing or not auth-wrapped
    (then nothing changes: without auth the endpoint is already open)."""
    from fastmcp.server.auth.middleware import RequireAuthMiddleware
    from starlette.routing import Route

    routes = app.router.routes
    for i, route in enumerate(routes):
        if not (isinstance(route, Route) and route.path == mcp_path):
            continue
        endpoint = route.app
        if isinstance(endpoint, AnonCatalogGate):
            return True
        if not isinstance(endpoint, RequireAuthMiddleware):
            return False
        inner = endpoint.app
        routes[i] = Route(
            route.path,
            endpoint=AnonCatalogGate(endpoint, inner),
            methods=sorted(route.methods - {"HEAD"}) if route.methods else None,
            name=route.name,
        )
        return True
    return False
