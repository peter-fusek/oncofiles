"""Hidden routes answer like an unknown path, for every method and the slash
form (external pentest 2026-11-27).

The operator surface ``/ops/mcp`` (#887) and the inbound mail webhooks
``/inbound/brevo`` / ``/inbound/email`` (#950) promise that an outsider cannot
tell them from a path that does not exist: their gates (OpsGate's loopback
check; the adapter switch, then the token or signature of a webhook) answer
the app's plain 404. Starlette's router gave them away before any gate ran:

* a method the route does not list (GET, HEAD, PUT, OPTIONS, …) got
  ``405 Method Not Allowed`` + ``Allow: POST, DELETE`` / ``Allow: POST``;
* the trailing-slash form (``/ops/mcp/``, ``/inbound/brevo/``) got the
  ``redirect_slashes`` 307 to the real path, where an unknown path gets 404.

``install_hidden_route_guards(app)`` (called from
``server._OncofilesMCP.http_app``, after the ops route and the anonymous
catalogue are installed) therefore:

1. replaces each hidden route by a ``HiddenRoute``: same path, endpoint,
   name and allowed methods, but it matches EVERY method, so the router
   never answers 405 for it. A method outside the allowed set gets the
   unknown-path 404 (``HTTPException(404)``, the very exception the router's
   ``not_found`` raises, so the app's one 404 handling answers both); an
   allowed method reaches the route's gate exactly as before;
2. puts ``<path>/{rest:path}`` in front of all routes, answering that same
   404 for every method, so no slash form (``/ops/mcp/``, ``/ops/mcp//``,
   ``/inbound/email/x``) is ever redirected. The slash forms were never
   endpoints: the ops CLI and Brevo call the exact path.

Nothing here reads a forwarding header, the client address or a body; the
gates are unchanged (``tool_surface.OpsGate``, the webhook check order in
``server``). The rate limiter classifies by prefix, so a hidden path and an
unknown path under the same prefix share their buckets too. Lock tests:
``tests/test_hidden_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.exceptions import HTTPException
from starlette.routing import Match, Route
from starlette.types import Receive, Scope, Send

from oncofiles.tool_surface import OPS_MCP_PATH

INBOUND_PATHS: tuple[str, ...] = ("/inbound/brevo", "/inbound/email")
HIDDEN_PATHS: tuple[str, ...] = (OPS_MCP_PATH, *INBOUND_PATHS)
SLASH_SUFFIX = "/{rest:path}"


def unknown_path() -> HTTPException:
    """What the router raises for a path no route matches (``Router.not_found``
    inside a Starlette app); the app's exception middleware renders it."""
    return HTTPException(status_code=404)


class UnknownPath:
    """ASGI app that answers exactly like a path no route matches.

    A class (not a function) so ``Route`` treats it as an ASGI app and lets
    every method through (a function endpoint would default to GET only)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        raise unknown_path()


class HiddenRoute(Route):
    """A ``Route`` that never answers 405: every method matches, and one
    outside ``methods`` gets the unknown-path 404 instead of ``Allow``."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if match is Match.PARTIAL:
            return Match.FULL, child_scope
        return match, child_scope

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self.methods and scope["method"] not in self.methods:
            raise unknown_path()
        await self.app(scope, receive, send)

    @classmethod
    def of(cls, route: Route) -> HiddenRoute:
        return cls(
            route.path,
            endpoint=route.endpoint,
            methods=route.methods,
            name=route.name,
            include_in_schema=route.include_in_schema,
        )


def install_hidden_route_guards(app, paths: Iterable[str] = HIDDEN_PATHS) -> list[str]:
    """Hide ``paths`` in a Starlette app (see the module docstring).

    Returns the paths that were found and guarded (``/ops/mcp`` is missing on
    the SSE transport). Raises ``ValueError`` when more than one route serves
    a hidden path: a ``HiddenRoute`` matches every method and would shadow the
    other one. Idempotent."""
    routes = app.router.routes
    guarded: list[str] = []
    for path in paths:
        same = [i for i, r in enumerate(routes) if isinstance(r, Route) and r.path == path]
        if not same:
            continue
        if len(same) > 1:
            raise ValueError(f"hidden path {path} is served by {len(same)} routes")
        route = routes[same[0]]
        if not isinstance(route, HiddenRoute):
            routes[same[0]] = HiddenRoute.of(route)
        slash = path + SLASH_SUFFIX
        if not any(getattr(r, "path", None) == slash for r in routes):
            routes.insert(0, Route(slash, endpoint=UnknownPath(), methods=None))
        guarded.append(path)
    return guarded
