"""Lock: no unauthenticated HTTP request reaches expensive work (#591 item 2).

Every route the production app serves is enumerated at runtime (the app is
built with the real ``PersistentOAuthProvider``, as in production): the custom
routes in ``server.py``, the MCP endpoint and the MCP OAuth / DCR routes.

1. ``/mcp`` is wrapped by FastMCP's ``RequireAuthMiddleware``.
2. A custom route counts as authenticated only when its handler starts with an
   auth gate (``err = _check_dashboard_auth(request)`` / ``_require_admin`` /
   ``_check_bearer`` followed by ``if err: return err``). Only docstrings,
   imports and a rate-limit check may come before it.
3. Every other route is on ``PUBLIC_ROUTES`` (or ``OAUTH_ROUTES``) with a
   reason. A new ungated route fails the test until it is listed here, and
   every listed entry must still exist (the lists can only shrink).
4. From each public / auth-flow handler a static call graph (AST) follows
   every function it references by name: same module, ``from oncofiles.x
   import y`` (also function-local), ``module.fn`` on an imported oncofiles
   module, and ``self.method``. Nothing reached may enter an LLM / OCR /
   Drive / Gmail / Calendar / cloud-AI / sync module, reference a client or
   job-start function, or start background work (``create_task`` etc.), except
   the allowlisted (route, reference) pairs below.
5. Public handlers that touch the DB at all are allowlisted with what they do,
   so new DB work behind a public route needs a reviewed entry.

Behavioural tests at the end cover the three #591 fixes: ``/oauth/authorize``
needs a caregiver session, ``/api/logout`` never writes a forged token to the
denylist, ``/api/stats`` is cached.

Synthetic values only (example.test addresses, invented slugs).
"""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

# ── what an unauthenticated request must never reach ────────────────────

# Entering any function of these modules is expensive work or an outbound
# Google / AI call.
FORBIDDEN_MODULES = {
    "oncofiles.enhance",  # cloud AI (_get_client)
    "oncofiles.doc_analysis",  # cloud AI
    "oncofiles.ocr",  # vision OCR
    "oncofiles.files_api",  # Anthropic Files API
    "oncofiles.gdrive_client",
    "oncofiles.gdrive_folders",
    "oncofiles.gmail_client",
    "oncofiles.calendar_client",
    "oncofiles.sync",
    "oncofiles.gmail_sync",
    "oncofiles.calendar_sync",
    "oncofiles.split",
    "oncofiles.consolidate",
    "oncofiles.curated_folder",  # Drive listing
    "oncofiles.backfill_ocr",
    "oncofiles.backfill_splits",
    "oncofiles.curated.llm",  # local LLM
    "oncofiles.curated.llm_extract",
    "oncofiles.curated.extract",  # OCR
    "oncofiles.curated.classify",
    "oncofiles.curated.verifier",
    "oncofiles.curated.runner",
    "oncofiles.curated.drive_ops",
    "oncofiles.curated.reocr",
    "oncofiles.curated.reocr_child",
    "oncofiles.curated.reread_verify",
}

# Referencing any of these names (as a name or an attribute) is a client, an
# AI call, a job start or background work.
FORBIDDEN_NAMES = {
    # clients
    "_get_client",
    "FilesClient",
    "GDriveClient",
    "GmailClient",
    "CalendarClient",
    "_create_patient_clients",
    "_get_gdrive",
    "_get_files",
    "dedicated_copy",
    "extract_text_from_image",
    # job starts
    "start_run",
    "execute_run",
    "curated_folder_run",
    "curated_reocr",
    "curated_reread_verify",
    "curated_llm_extract_preview",
    "index_patient",
    "nightly_for_patient",
    "continuous_tick",
    "enhance_documents",
    "sync_from_gdrive",
    "sync_to_gdrive",
    "_run_sync",
    "_run_nightly_ai_pipeline",
    # background work
    "add_job",
    "create_task",
    "ensure_future",
    "to_thread",
    "run_in_executor",
}

AUTH_GATES = {"_check_dashboard_auth", "_require_admin", "_check_bearer"}

# ── allowlists (may only shrink) ─────────────────────────────────────────

# Custom routes without an auth gate: (path, handler) -> reason.
PUBLIC_ROUTES: dict[tuple[str, str], str] = {
    ("/", "landing"): "static landing HTML",
    ("/oncoteam", "oncoteam_page"): "static HTML",
    ("/privacy", "privacy_policy"): "static HTML",
    ("/terms", "terms_of_service"): "static HTML",
    ("/favicon.svg", "favicon_svg"): "static asset",
    ("/og-image.svg", "og_image_svg"): "static asset",
    ("/og-image.png", "og_image_png"): "static asset",
    ("/apple-touch-icon.png", "apple_touch_icon"): "redirect",
    ("/favicon.ico", "favicon_ico"): "redirect",
    ("/dashboard.css", "dashboard_css"): "static asset",
    ("/dashboard.js", "dashboard_js"): "static asset",
    ("/fonts/{name}", "font_asset"): "static asset from a fixed file allowlist",
    ("/demo.css", "demo_css"): "static asset",
    ("/demo.js", "demo_js"): "static asset",
    ("/consent.js", "consent_js"): "static asset",
    ("/assets/pile/{slot}.jpg", "pile_photo"): "static asset from a fixed name map",
    ("/robots.txt", "robots_txt"): "static text",
    ("/sitemap.xml", "sitemap_xml"): "static XML",
    ("/llms.txt", "llms_txt"): "static text + in-memory tool count",
    ("/llms-sk.txt", "llms_sk_txt"): "static text + in-memory tool count",
    ("/onkologia", "onkologia_page"): "static HTML",
    ("/oncology", "oncology_page"): "static HTML",
    ("/eu", "eu_page"): "static HTML",
    ("/sk", "sk_page"): "static HTML",
    ("/manifest.json", "manifest_json"): "static JSON",
    ("/health", "health"): "liveness probe: in-memory counters only, never the DB",
    ("/readiness", "readiness"): "DB ping (5 s timeout); detail only for the operator",
    ("/api/stats", "api_stats"): "public counts, cached 5 min (#591)",
    ("/dashboard", "dashboard"): "static HTML; the dashboard authenticates client-side",
    ("/demo", "demo_dashboard"): "static HTML",
    ("/api/demo-data", "api_demo_data"): "constant JSON, no DB",
    ("/dashboard/config", "dashboard_config"): "returns the public OAuth client id",
    ("/dashboard/verify", "dashboard_verify"): "auth flow: Google tokeninfo, rate limited",
    ("/api/logout", "api_logout"): "auth flow: revokes only a token that verifies (#591)",
    ("/api/newsletter/subscribe", "api_newsletter_subscribe"): "signup, rate limited per IP",
    ("/api/newsletter/check", "api_newsletter_check"): "one lookup by email, rate limited",
    ("/api/share-link/{code}", "api_redeem_share_link"): "one-time code, in memory, rate limited",
    (
        "/oauth/authorize/{service}",
        "oauth_authorize",
    ): "auth flow: needs a session (cookie) + caregiver / admin ACL in the handler (#591)",
    (
        "/oauth/callback",
        "oauth_callback",
    ): "auth flow: v2 state (10 min, single use) bound to principal + patient + service; "
    "overwrite guard and audit row (tests/test_oauth_link_guard.py)",
}

# MCP OAuth (FastMCP / mcp SDK) routes: open by the OAuth / DCR specs.
OAUTH_ROUTES: dict[str, str] = {
    "/.well-known/oauth-authorization-server": "static metadata",
    "/.well-known/oauth-protected-resource/mcp": "static metadata",
    "/authorize": "OAuth authorize: in-memory code; email-capture middleware reads a cookie",
    "/token": "OAuth token exchange: needs a code / refresh token; writes the token pair",
    "/register": "RFC 7591 DCR: open by spec; one client row per call, rate limited by #749",
}

# Unauthenticated (route, reached reference) pairs that are allowed: reason.
REACHABLE_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "/api/newsletter/subscribe",
        "create_task",
    ): "two Resend emails per NEW subscriber (admin lead + welcome), rate limited per IP",
}

# Public handlers that touch the DB: (path, handler) -> what they do.
DB_ALLOWLIST: dict[tuple[str, str], str] = {
    (
        "/readiness",
        "readiness",
    ): "db.ping(2 s); 24 h prompt_log SUM for the operator only",
    ("/api/stats", "api_stats"): "1 + N(patients) COUNTs, at most once per 5 min (cache)",
    ("/api/logout", "api_logout"): "one revocation row, only for a token that verifies",
    ("/api/newsletter/subscribe", "api_newsletter_subscribe"): "INSERT OR IGNORE one row",
    ("/api/newsletter/check", "api_newsletter_check"): "one SELECT by email",
    ("/oauth/authorize/{service}", "oauth_authorize"): "slug + patient lookup after the session",
    (
        "/oauth/callback",
        "oauth_callback",
    ): "patient + token lookup; guarded upsert + activity row after a bound state",
}

# ── static analysis ──────────────────────────────────────────────────────


def _module_name(rel: str) -> str:
    parts = rel[: -len(".py")].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["oncofiles", *parts])


class _Module:
    def __init__(self, rel: str, tree: ast.Module) -> None:
        self.rel = rel
        self.tree = tree
        self.funcs: dict[str, list[ast.AST]] = defaultdict(list)
        self.imports: dict[str, tuple[str, str]] = {}  # alias -> (module, name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.funcs[node.name].append(node)
            elif (
                isinstance(node, ast.ImportFrom)
                and node.level == 0
                and (node.module or "").startswith("oncofiles")
            ):
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = (node.module, alias.name)


def _load_modules(sources: dict[str, str] | None = None) -> dict[str, _Module]:
    if sources is None:
        sources = {
            p.relative_to(SRC).as_posix(): p.read_text(encoding="utf-8") for p in SRC.rglob("*.py")
        }
    return {_module_name(rel): _Module(rel, ast.parse(text)) for rel, text in sources.items()}


_DB_BASES = {"db", "db_inst", "db_check", "_db"}


def _touches_db(node: ast.AST) -> bool:
    """``db.x`` / ``self._db.x`` / ``ctx["db"].x`` — any use of the Database."""
    if not isinstance(node, ast.Attribute):
        return False
    base = node.value
    if isinstance(base, ast.Name):
        return base.id in _DB_BASES
    if isinstance(base, ast.Attribute):
        return base.attr in _DB_BASES
    if isinstance(base, ast.Subscript) and isinstance(base.slice, ast.Constant):
        return base.slice.value == "db"
    return False


def _reach(
    modules: dict[str, _Module], roots: list[tuple[str, str]]
) -> tuple[set[tuple[str, str, str]], bool]:
    """Walk from (module, function) roots. Returns (offenders, touches_db).

    An offender is (file, function, reference): a forbidden name, or
    ``module:<name>`` when a forbidden module's function is entered.
    """
    todo = list(roots)
    seen: set[tuple[str, str]] = set()
    offenders: set[tuple[str, str, str]] = set()
    db = False
    while todo:
        mod_name, fn_name = todo.pop()
        if (mod_name, fn_name) in seen:
            continue
        seen.add((mod_name, fn_name))
        mod = modules.get(mod_name)
        if mod is None:
            continue
        if mod_name in FORBIDDEN_MODULES and fn_name in mod.funcs:
            offenders.add((mod.rel, fn_name, f"module:{mod_name}"))
            continue
        if fn_name not in mod.funcs:
            if fn_name in mod.imports:  # re-export
                todo.append(mod.imports[fn_name])
            continue
        for fn in mod.funcs[fn_name]:
            for node in ast.walk(fn):
                ref = None
                if isinstance(node, ast.Name):
                    ref = node.id
                    if ref in mod.funcs:
                        todo.append((mod_name, ref))
                    elif ref in mod.imports:
                        todo.append(mod.imports[ref])
                elif isinstance(node, ast.Attribute):
                    ref = node.attr
                    db = db or _touches_db(node)
                    base = node.value
                    if isinstance(base, ast.Name) and base.id == "self":
                        todo.append((mod_name, ref))
                    elif isinstance(base, ast.Name) and base.id in mod.imports:
                        src_mod, src_name = mod.imports[base.id]
                        sub = f"{src_mod}.{src_name}"
                        if sub in modules:  # `from oncofiles import x` + x.fn
                            todo.append((sub, ref))
                    elif (
                        isinstance(base, ast.Call)
                        and isinstance(base.func, ast.Name)
                        and base.func.id == "super"
                    ):
                        todo.append((mod_name, ref))
                if ref in FORBIDDEN_NAMES:
                    offenders.add((mod.rel, fn_name, ref))
    return offenders, db


def _is_gate_call(node: ast.expr, gates: set[str]) -> bool:
    """``GATE(...)``, or ``GATE(...) or other(...)``: the gate is the FIRST
    operand of the ``or``, so it runs first and short-circuits on failure
    (e.g. ``_require_admin(request) or _cross_origin(request)``, #573)."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in gates


def _is_gate_pair(stmt: ast.stmt, nxt: ast.stmt | None, gates: set[str]) -> bool:
    """``x = GATE(request)`` directly followed by ``if x: return ...``."""
    if not (
        isinstance(stmt, ast.Assign)
        and len(stmt.targets) == 1
        and isinstance(stmt.targets[0], ast.Name)
        and _is_gate_call(stmt.value, gates)
    ):
        return False
    var = stmt.targets[0].id
    return (
        isinstance(nxt, ast.If)
        and any(isinstance(n, ast.Name) and n.id == var for n in ast.walk(nxt.test))
        and isinstance(nxt.body[0], ast.Return)
    )


def _is_gated(fn: ast.AST) -> bool:
    """The handler's first real statement pair is an auth gate.

    Only a docstring, imports and a ``_check_rate_limit`` pair may precede it,
    so no work happens before the caller is authenticated.
    """
    body = fn.body
    i = 0
    while i < len(body):
        stmt = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else None
        if _is_gate_pair(stmt, nxt, AUTH_GATES):
            return True
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            or isinstance(stmt, (ast.Import, ast.ImportFrom))
        ):
            i += 1
        elif _is_gate_pair(stmt, nxt, {"_check_rate_limit"}):
            i += 2
        else:
            return False
    return False


def _custom_route_handlers(mod: _Module) -> dict[tuple[str, str], ast.AST]:
    """(path, handler) for every ``@<x>.custom_route("<path>", ...)``."""
    found: dict[tuple[str, str], ast.AST] = {}
    for node in mod.tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "custom_route"
            ):
                path = dec.args[0].value if dec.args else dec.keywords[0].value.value
                found[(path, node.name)] = node
    return found


def _ungated_routes(modules: dict[str, _Module], mod_name: str) -> dict[tuple[str, str], ast.AST]:
    handlers = _custom_route_handlers(modules[mod_name])
    return {key: fn for key, fn in handlers.items() if not _is_gated(fn)}


def _route_offenders(
    modules: dict[str, _Module], mod_name: str = "oncofiles.server"
) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """((path, reference) offenders, (path, handler) routes that touch the DB)."""
    offenders: set[tuple[str, str]] = set()
    db_routes: set[tuple[str, str]] = set()
    for path, handler in _ungated_routes(modules, mod_name):
        found, db = _reach(modules, [(mod_name, handler)])
        offenders |= {(path, ref) for _, _, ref in found}
        if db:
            db_routes.add((path, handler))
    return offenders, db_routes


# ── runtime route enumeration ────────────────────────────────────────────


@pytest.fixture(scope="module")
def prod_routes():
    """Routes of the app built like production (PersistentOAuthProvider)."""
    from fastmcp.server.auth.auth import ClientRegistrationOptions

    from oncofiles import server
    from oncofiles.persistent_oauth import PersistentOAuthProvider

    saved = server.mcp.auth
    server.mcp.auth = PersistentOAuthProvider(
        bearer_token="synthetic-operator-token",
        base_url="https://example.test",
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    try:
        app = server.mcp.http_app(transport="streamable-http", stateless_http=True)
    finally:
        server.mcp.auth = saved
    return list(app.routes)


def test_mcp_endpoint_requires_auth(prod_routes):
    from fastmcp.server.auth.middleware import RequireAuthMiddleware

    mcp_routes = [r for r in prod_routes if getattr(r, "path", None) == "/mcp"]
    assert mcp_routes, "no /mcp route"
    for r in mcp_routes:
        assert isinstance(r.endpoint, RequireAuthMiddleware), type(r.endpoint)


def test_every_route_is_classified(prod_routes):
    """Each served route is authenticated (gated handler), public (listed) or OAuth."""
    from oncofiles import server

    modules = _load_modules()
    handlers = _custom_route_handlers(modules["oncofiles.server"])
    ungated = _ungated_routes(modules, "oncofiles.server")
    unclassified = []
    for r in prod_routes:
        path = getattr(r, "path", None)
        endpoint = getattr(r, "endpoint", None)
        if path == "/mcp":
            continue
        if getattr(endpoint, "__module__", None) == server.__name__:
            key = (path, endpoint.__name__)
            assert key in handlers, f"route {key} not found by the AST scan"
            if key in ungated and key not in PUBLIC_ROUTES:
                unclassified.append(key)
        elif path not in OAUTH_ROUTES:
            unclassified.append((path, type(endpoint).__name__))
    assert not unclassified, (
        "route without an auth gate and not reviewed (#591): add an auth gate, or an "
        f"entry with a reason to PUBLIC_ROUTES / OAUTH_ROUTES: {unclassified}"
    )


def test_ast_scan_matches_runtime_routes(prod_routes):
    """Guard the guard: the AST sees exactly the custom routes the app serves."""
    from oncofiles import server

    served = {
        (r.path, r.endpoint.__name__)
        for r in prod_routes
        if getattr(getattr(r, "endpoint", None), "__module__", None) == server.__name__
    }
    assert served == set(_custom_route_handlers(_load_modules()["oncofiles.server"]))


def test_public_routes_no_stale_entries():
    ungated = set(_ungated_routes(_load_modules(), "oncofiles.server"))
    stale = set(PUBLIC_ROUTES) - ungated
    assert not stale, f"no longer public (or gone), remove from PUBLIC_ROUTES: {sorted(stale)}"


def test_oauth_routes_no_stale_entries(prod_routes):
    served = {getattr(r, "path", None) for r in prod_routes}
    stale = set(OAUTH_ROUTES) - served
    assert not stale, f"OAuth route no longer served, remove it: {sorted(stale)}"


def test_known_gated_routes_are_detected():
    """Guard the guard: real authenticated routes are seen as gated."""
    handlers = _custom_route_handlers(_load_modules()["oncofiles.server"])
    for key in [
        ("/api/sync-trigger", "api_sync_trigger"),
        ("/api/enhance-trigger", "api_enhance_trigger"),
        ("/api/admin/curated/run", "api_admin_curated_run"),
        ("/api/gdrive-folders", "api_gdrive_folders"),
        ("/metrics", "metrics"),
        ("/status", "status"),
    ]:
        assert _is_gated(handlers[key]), key


def test_public_routes_reach_no_expensive_work():
    offenders, _ = _route_offenders(_load_modules())
    unexpected = sorted(offenders - set(REACHABLE_ALLOWLIST))
    assert not unexpected, (
        "an unauthenticated route reaches LLM / OCR / Drive / Gmail / Calendar / cloud "
        f"AI, a job start or background work (#591): {unexpected}"
    )


def test_reachable_allowlist_no_stale_entries():
    offenders, _ = _route_offenders(_load_modules())
    stale = set(REACHABLE_ALLOWLIST) - offenders
    assert not stale, f"no longer reachable, remove from REACHABLE_ALLOWLIST: {sorted(stale)}"


def test_public_db_work_is_reviewed():
    _, db_routes = _route_offenders(_load_modules())
    unexpected = sorted(db_routes - set(DB_ALLOWLIST))
    assert not unexpected, f"public route touches the DB without a reviewed entry: {unexpected}"
    stale = set(DB_ALLOWLIST) - db_routes
    assert not stale, f"no longer touches the DB, remove from DB_ALLOWLIST: {sorted(stale)}"


def test_sync_and_enhance_triggers_are_expensive():
    """Guard the guard: the walker does see the work behind real triggers."""
    modules = _load_modules()
    found, db = _reach(modules, [("oncofiles.server", "api_sync_trigger")])
    refs = {ref for _, _, ref in found}
    assert "_create_patient_clients" in refs and "module:oncofiles.sync" in refs, refs
    assert db
    found, _ = _reach(modules, [("oncofiles.server", "api_admin_curated_run")])
    assert found, "curated run trigger must look expensive"


# OAuth provider methods an unauthenticated caller can drive: DCR, the OAuth
# endpoints and verify_token (any bearer sent to /mcp).
OAUTH_PROVIDER_ENTRY_POINTS = [
    "verify_token",
    "register_client",
    "exchange_authorization_code",
    "exchange_refresh_token",
    "revoke_token",
]


def test_oauth_provider_reaches_no_expensive_work():
    modules = _load_modules()
    roots = [("oncofiles.persistent_oauth", fn) for fn in OAUTH_PROVIDER_ENTRY_POINTS]
    for _, fn in roots:
        assert fn in modules["oncofiles.persistent_oauth"].funcs, fn
    offenders, _ = _reach(modules, roots)
    assert not offenders, f"MCP OAuth path reaches expensive work: {sorted(offenders)}"


# ── the lock catches synthetic regressions ──────────────────────────────

_SYNTH_HELPERS = """
from oncofiles.gdrive_client import GDriveClient

def list_inbox(creds):
    return GDriveClient(creds).list_folder("x")

async def kick_off(pid):
    import asyncio
    asyncio.create_task(pid)
"""

_SYNTH_SERVER = """
from oncofiles.synth_helpers import list_inbox, kick_off

@mcp.custom_route("/api/new-public", methods=["GET"])
async def new_public(request):
    return list_inbox(None)

@mcp.custom_route("/api/new-gated", methods=["POST"])
async def new_gated(request):
    \"\"\"Doc.\"\"\"
    err = _check_dashboard_auth(request)
    if err:
        return err
    await kick_off("p")

@mcp.custom_route("/api/late-gate", methods=["POST"])
async def late_gate(request):
    db = request.app.state.fastmcp_server._lifespan_result["db"]
    await db.count_documents(patient_id="p")
    err = _check_dashboard_auth(request)
    if err:
        return err
"""


def _synthetic() -> dict[str, _Module]:
    return _load_modules({"synth_helpers.py": _SYNTH_HELPERS, "synth_server.py": _SYNTH_SERVER})


def test_synthetic_ungated_route_is_found():
    ungated = set(_ungated_routes(_synthetic(), "oncofiles.synth_server"))
    assert ungated == {("/api/new-public", "new_public"), ("/api/late-gate", "late_gate")}


def test_synthetic_public_drive_call_fails_the_lock():
    offenders, db_routes = _route_offenders(_synthetic(), "oncofiles.synth_server")
    assert ("/api/new-public", "GDriveClient") in offenders
    assert ("/api/late-gate", "late_gate") in db_routes
    # The gated route's background task is not an unauthenticated offender.
    assert not any(path == "/api/new-gated" for path, _ in offenders)


# ── behaviour of the #591 fixes ─────────────────────────────────────────


class _FakeRequest:
    def __init__(self, *, headers=None, cookies=None, query=None, path=None, db=None):
        from types import SimpleNamespace

        self.headers = headers or {}
        self.cookies = cookies or {}
        self.query_params = query or {}
        self.path_params = path or {}
        self.client = SimpleNamespace(host="203.0.113.7")
        self.app = SimpleNamespace(
            state=SimpleNamespace(
                fastmcp_server=SimpleNamespace(_lifespan_result={"db": db} if db else {})
            )
        )


@pytest.fixture
def http_mode(monkeypatch):
    from oncofiles import server

    monkeypatch.setattr(server, "MCP_TRANSPORT", "streamable-http")
    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr("oncofiles.oauth.MCP_BEARER_TOKEN", "synthetic-operator-token")
    monkeypatch.setattr("oncofiles.oauth.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr(server, "GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    monkeypatch.setattr("oncofiles.config.GOOGLE_OAUTH_CLIENT_ID", "synthetic-client.example.test")
    server._rate_limits.clear()
    yield server
    server._rate_limits.clear()


async def test_oauth_authorize_rejects_anonymous_caller(http_mode, db):
    server = http_mode
    req = _FakeRequest(query={"patient_id": "jana"}, path={"service": "drive"}, db=db)
    resp = await server.oauth_authorize(req)
    assert resp.status_code == 401


async def test_oauth_authorize_rejects_other_caregiver(http_mode, db):
    from tests.helpers import ERIKA_UUID

    server = http_mode
    token = server._make_session_token("stranger@example.test")
    req = _FakeRequest(
        cookies={"oncofiles_session": token},
        query={"patient_id": ERIKA_UUID},
        path={"service": "drive"},
        db=db,
    )
    resp = await server.oauth_authorize(req)
    assert resp.status_code == 403


async def test_oauth_authorize_allows_caregiver_cookie(http_mode, db):
    from tests.helpers import ERIKA_UUID

    server = http_mode
    await db.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?",
        ("carer@example.test", ERIKA_UUID),
    )
    await db.db.commit()
    token = server._make_session_token("carer@example.test")
    req = _FakeRequest(
        cookies={"oncofiles_session": token},
        query={"patient_id": ERIKA_UUID},
        path={"service": "drive"},
        db=db,
    )
    resp = await server.oauth_authorize(req)
    assert resp.status_code in (302, 307), resp.status_code
    assert "accounts.google.com" in resp.headers["location"]


async def test_logout_never_revokes_a_forged_token(http_mode, db):
    from oncofiles import session_revocation

    server = http_mode
    forged = "anyone@example.test|9999999999|00112233445566ff|" + "0" * 32
    req = _FakeRequest(headers={"authorization": f"Bearer session:{forged}"}, db=db)
    resp = await server.api_logout(req)
    assert resp.status_code == 200
    assert not session_revocation.is_revoked("00112233445566ff")
    async with db.db.execute("SELECT COUNT(*) AS n FROM session_revocations") as cur:
        assert (await cur.fetchone())["n"] == 0


async def test_logout_revokes_a_valid_token(http_mode, db):
    server = http_mode
    token = server._make_session_token("carer@example.test")
    assert server._verify_session_token(token) == "carer@example.test"
    req = _FakeRequest(headers={"authorization": f"Bearer session:{token}"}, db=db)
    resp = await server.api_logout(req)
    assert resp.status_code == 200
    assert server._verify_session_token(token) is None


async def test_api_stats_is_cached(monkeypatch):
    from oncofiles import server

    calls = {"n": 0}

    class _CountingDb:
        async def list_patients(self, active_only=True):
            calls["n"] += 1
            return []

        async def count_documents(self, patient_id=None):
            return 0

    monkeypatch.setattr(server, "_api_stats_cache", {"at": 0.0, "documents": None})
    req = _FakeRequest(db=_CountingDb())
    for _ in range(5):
        resp = await server.api_stats(req)
        assert resp.status_code == 200
    assert calls["n"] == 1


def test_gate_must_be_first_operand_of_or():
    """``GATE(r) or other(r)`` is gated; ``other(r) or GATE(r)`` is not, since
    ``other`` would run before the caller is authenticated."""

    def gated(src: str) -> bool:
        return _is_gated(ast.parse(src).body[0])

    first = "async def h(request):\n    err = _require_admin(request) or _x(request)\n"
    second = "async def h(request):\n    err = _x(request) or _require_admin(request)\n"
    tail = "    if err:\n        return err\n    return 1\n"
    assert gated(first + tail)
    assert not gated(second + tail)
