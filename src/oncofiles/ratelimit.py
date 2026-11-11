"""Application-level rate limiting, concurrency caps and body-size limits (#591).

Pure ASGI middleware, in memory, stdlib only. Registered once in
``server.main()``; everything else lives here so the server module stays
untouched.

Model
-----
Every HTTP request is put into one *route class*:

``mcp``     ``/mcp`` (MCP streamable-http — tool calls, uploads)
``auth``    OAuth / login endpoints (``/authorize``, ``/token``, ``/register``,
            ``/revoke``, ``/oauth/*``, ``/dashboard/verify``) — stricter, IP-keyed
``api``     dashboard JSON API (``/api/*``, ``/dashboard/config``)
``public``  everything else (landing pages, static assets, ``/readiness``)
``exempt``  ``/health`` — never limited, never size-checked (Coolify's
            healthcheck and CI's smoke test poll it)

Each class has two token buckets: a **global** one shared by every caller of
that class and a **per-principal** one. The principal is derived from the
credential the request carries — never from a DB lookup, so limiting stays
O(1) and runs before any auth or DB work:

* ``static``  — the operator ``MCP_BEARER_TOKEN`` (constant-time compare)
* ``patient`` — an ``onco_*`` patient bearer token (keyed by its hash)
* ``session`` — a dashboard session (``Bearer session:…`` or the
  ``oncofiles_session`` cookie), keyed by the hash of the session email
  field so all of a user's tabs share one bucket
* ``oauth``   — any other bearer (MCP OAuth access token), keyed by its hash
* ``ip``      — no credential: the client address

Credentials are not *verified* here (that would need the DB). A caller who
invents a fresh fake token per request gets a fresh per-principal bucket, but
still drains the class's global bucket and is rejected by the real auth
layer with a cheap 401. The per-principal map is LRU-bounded so such a
caller cannot grow memory without bound.

The ``auth`` class is always keyed by IP. Behind the current host port
forwarding every client shares one address (#591), so the per-IP auth bucket
effectively acts as a second global limit until real client IPs are
available; set ``RATE_LIMIT_TRUST_FORWARDED=true`` once the proxy sends a
trustworthy ``X-Forwarded-For``.

``/mcp`` POSTs additionally take a per-principal **concurrency** slot for the
duration of the request (one stateless tool call = one POST).

Rejections: ``429`` + ``Retry-After`` (JSON body on ``mcp``/``api``/``auth``,
plain text on public pages) and ``413`` for bodies over the class limit —
rejected from ``Content-Length`` before the body is read, or while streaming
a chunked body as soon as the running total crosses the limit.

Kill switch: ``RATE_LIMIT_ENABLED=false`` turns the middleware into a
pass-through.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

EXEMPT = "exempt"
MCP = "mcp"
AUTH = "auth"
API = "api"
PUBLIC = "public"
CLASSES = (MCP, AUTH, API, PUBLIC)

_EXEMPT_PATHS = frozenset({"/health"})
_AUTH_PATHS = frozenset({"/authorize", "/token", "/register", "/revoke", "/dashboard/verify"})
_AUTH_PREFIXES = ("/oauth/",)
_API_PATHS = frozenset({"/dashboard/config"})
_API_PREFIXES = ("/api/",)
_JSON_CLASSES = frozenset({MCP, API, AUTH})


def classify(path: str) -> str:
    """Map a request path to its route class."""
    if path in _EXEMPT_PATHS:
        return EXEMPT
    if path == "/mcp" or path.startswith("/mcp/"):
        return MCP
    if path in _AUTH_PATHS or path.startswith(_AUTH_PREFIXES):
        return AUTH
    if path in _API_PATHS or path.startswith(_API_PREFIXES):
        return API
    return PUBLIC


# ── Config ───────────────────────────────────────────────────────────────────


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _env_num(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ratelimit: ignoring non-numeric %s=%r", name, raw)
        return default
    return value if value >= 0 else default


@dataclass(frozen=True)
class Limit:
    """Token-bucket parameters: sustained ``per_minute`` with ``burst`` capacity.
    ``per_minute == 0`` disables that bucket."""

    per_minute: float
    burst: float


@dataclass(frozen=True)
class ClassLimits:
    principal: Limit
    global_: Limit
    max_body_bytes: int  # 0 = unlimited


# Defaults — sized for a handful of caregivers on one Mac mini. A Claude.ai
# conversation fires tool calls in short bursts; the dashboard loads ~20 JSON
# endpoints sequentially on open; an OAuth handshake is ~5 auth requests.
DEFAULTS: dict[str, ClassLimits] = {
    MCP: ClassLimits(Limit(120, 60), Limit(600, 200), 10 * 1024 * 1024),
    AUTH: ClassLimits(Limit(30, 20), Limit(120, 60), 64 * 1024),
    API: ClassLimits(Limit(300, 120), Limit(1200, 400), 1024 * 1024),
    PUBLIC: ClassLimits(Limit(300, 150), Limit(1200, 400), 64 * 1024),
}
DEFAULT_MCP_CONCURRENCY = 4
DEFAULT_MAX_PRINCIPALS = 10_000


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool = True
    classes: dict[str, ClassLimits] = field(default_factory=lambda: dict(DEFAULTS))
    mcp_concurrency: int = DEFAULT_MCP_CONCURRENCY  # 0 = unlimited
    trust_forwarded: bool = False
    max_principals: int = DEFAULT_MAX_PRINCIPALS
    static_bearer: str = ""

    @classmethod
    def from_env(cls, static_bearer: str = "") -> RateLimitConfig:
        """Read ``RATE_LIMIT_*`` env vars. Per class ``X`` in MCP/AUTH/API/PUBLIC:
        ``RATE_LIMIT_X_PER_MIN``, ``RATE_LIMIT_X_BURST``,
        ``RATE_LIMIT_X_GLOBAL_PER_MIN``, ``RATE_LIMIT_X_GLOBAL_BURST``,
        ``RATE_LIMIT_X_MAX_BODY_BYTES``."""
        classes = {}
        for name, d in DEFAULTS.items():
            p = f"RATE_LIMIT_{name.upper()}_"
            classes[name] = ClassLimits(
                principal=Limit(
                    _env_num(p + "PER_MIN", d.principal.per_minute),
                    _env_num(p + "BURST", d.principal.burst),
                ),
                global_=Limit(
                    _env_num(p + "GLOBAL_PER_MIN", d.global_.per_minute),
                    _env_num(p + "GLOBAL_BURST", d.global_.burst),
                ),
                max_body_bytes=int(_env_num(p + "MAX_BODY_BYTES", d.max_body_bytes)),
            )
        return cls(
            enabled=_env_bool("RATE_LIMIT_ENABLED", True),
            classes=classes,
            mcp_concurrency=int(_env_num("RATE_LIMIT_MCP_CONCURRENCY", DEFAULT_MCP_CONCURRENCY)),
            trust_forwarded=_env_bool("RATE_LIMIT_TRUST_FORWARDED", False),
            max_principals=int(_env_num("RATE_LIMIT_MAX_PRINCIPALS", DEFAULT_MAX_PRINCIPALS))
            or DEFAULT_MAX_PRINCIPALS,
            static_bearer=static_bearer,
        )


# ── Token bucket ─────────────────────────────────────────────────────────────


class TokenBucket:
    """Classic token bucket. Single-threaded (asyncio) use only."""

    __slots__ = ("rate", "capacity", "tokens", "updated")

    def __init__(self, limit: Limit, now: float) -> None:
        self.rate = limit.per_minute / 60.0  # tokens per second
        self.capacity = max(limit.burst, 1.0)
        self.tokens = self.capacity
        self.updated = now

    def _refill(self, now: float) -> None:
        if now > self.updated:
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now

    def peek(self, now: float) -> float:
        """Seconds until one token is available (0 if available now)."""
        self._refill(now)
        if self.tokens >= 1.0:
            return 0.0
        if self.rate <= 0:
            return 60.0
        return (1.0 - self.tokens) / self.rate

    def take(self) -> None:
        self.tokens -= 1.0


# ── Principal extraction ────────────────────────────────────────────────────


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or ():
        if k.lower() == name:
            return v.decode("latin-1", "replace")
    return ""


def client_ip(scope, trust_forwarded: bool = False) -> str:
    if trust_forwarded:
        xff = _header(scope, b"x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip() or "unknown"
    client = scope.get("client")
    return client[0] if client else "unknown"


def _session_key(token: str) -> str:
    # Session token format (#510): ``email|expiry|tid|sig``. Key on the email
    # field so a user's concurrent tabs/sessions share one bucket.
    email = token.split("|", 1)[0].strip().lower()
    return "session:" + _h(email or token)


def principal_for(scope, route_class: str, cfg: RateLimitConfig) -> str:
    """Return the bucket key for the caller of this request (no DB, no verification)."""
    ip = client_ip(scope, cfg.trust_forwarded)
    if route_class == AUTH:
        return "ip:" + ip
    auth = _header(scope, b"authorization").strip()
    if auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
        if token.startswith("session:"):
            return _session_key(token.removeprefix("session:"))
        if cfg.static_bearer and hmac.compare_digest(
            token.encode("utf-8", "replace"), cfg.static_bearer.encode("utf-8", "replace")
        ):
            return "static"
        if token.startswith("onco_"):
            return "patient:" + _h(token)
        if token:
            return "oauth:" + _h(token)
    cookie = _header(scope, b"cookie")
    if cookie:
        from urllib.parse import unquote

        for pair in cookie.split(";"):
            name, sep, value = pair.strip().partition("=")
            if sep and name == "oncofiles_session" and value.strip():
                return _session_key(unquote(value.strip()))
    return "ip:" + ip


# ── Middleware ───────────────────────────────────────────────────────────────


class _BodyTooLargeError(Exception):
    pass


class RateLimitMiddleware:
    """ASGI middleware enforcing #591 limits. See module docstring."""

    def __init__(
        self,
        app,
        config: RateLimitConfig | None = None,
        clock=time.monotonic,
    ) -> None:
        self.app = app
        if config is None:
            from oncofiles import config as _app_config

            config = RateLimitConfig.from_env(static_bearer=_app_config.MCP_BEARER_TOKEN or "")
        self.config = config
        self.clock = clock
        self._global: dict[str, TokenBucket] = {}
        self._principal: OrderedDict[tuple[str, str], TokenBucket] = OrderedDict()
        self._inflight: dict[str, int] = {}
        if config.enabled:
            logger.info(
                "ratelimit enabled: %s; mcp concurrency=%d",
                ", ".join(
                    f"{n}={c.principal.per_minute:g}/min(b{c.principal.burst:g}) "
                    f"global={c.global_.per_minute:g}/min body<={c.max_body_bytes}"
                    for n, c in config.classes.items()
                ),
                config.mcp_concurrency,
            )
        else:
            logger.warning("ratelimit DISABLED (RATE_LIMIT_ENABLED=false)")

    # -- buckets --

    def _global_bucket(self, route_class: str, now: float) -> TokenBucket | None:
        limit = self.config.classes[route_class].global_
        if limit.per_minute <= 0:
            return None
        b = self._global.get(route_class)
        if b is None:
            b = self._global[route_class] = TokenBucket(limit, now)
        return b

    def _principal_bucket(self, route_class: str, key: str, now: float) -> TokenBucket | None:
        limit = self.config.classes[route_class].principal
        if limit.per_minute <= 0:
            return None
        k = (route_class, key)
        b = self._principal.get(k)
        if b is None:
            b = self._principal[k] = TokenBucket(limit, now)
            while len(self._principal) > self.config.max_principals:
                self._principal.popitem(last=False)
        else:
            self._principal.move_to_end(k)
        return b

    def check(self, route_class: str, key: str) -> float:
        """Consume one token from both buckets. Returns 0 if allowed, else the
        Retry-After seconds. Nothing is consumed when the request is rejected."""
        now = self.clock()
        buckets = [
            b
            for b in (
                self._principal_bucket(route_class, key, now),
                self._global_bucket(route_class, now),
            )
            if b is not None
        ]
        wait = max((b.peek(now) for b in buckets), default=0.0)
        if wait > 0:
            return wait
        for b in buckets:
            b.take()
        return 0.0

    # -- responses --

    @staticmethod
    async def _reject(send, status: int, message: str, route_class: str, retry_after=None):
        headers = []
        if route_class in _JSON_CLASSES:
            payload = {"error": message}
            if retry_after is not None:
                payload["retry_after"] = retry_after
            body = json.dumps(payload).encode()
            headers.append((b"content-type", b"application/json"))
        else:
            body = message.encode()
            headers.append((b"content-type", b"text/plain; charset=utf-8"))
        if retry_after is not None:
            headers.append((b"retry-after", str(retry_after).encode()))
        headers.append((b"content-length", str(len(body)).encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    # -- ASGI --

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.config.enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path") or "/"
        route_class = classify(path)
        if route_class == EXEMPT or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        limits = self.config.classes[route_class]
        max_body = limits.max_body_bytes

        # 1. Body size from Content-Length — before reading anything.
        if max_body > 0:
            cl = _header(scope, b"content-length")
            if cl:
                try:
                    declared = int(cl)
                except ValueError:
                    declared = -1
                if declared > max_body:
                    logger.warning(
                        "ratelimit: 413 %s %s content-length=%d > %d",
                        route_class,
                        path,
                        declared,
                        max_body,
                    )
                    await self._reject(send, 413, "request body too large", route_class)
                    return

        # 2. Token buckets.
        key = principal_for(scope, route_class, self.config)
        wait = self.check(route_class, key)
        if wait > 0:
            retry_after = max(1, math.ceil(wait))
            logger.warning(
                "ratelimit: 429 %s %s principal=%s retry_after=%ds",
                route_class,
                path,
                key.split(":", 1)[0],
                retry_after,
            )
            await self._reject(send, 429, "rate limit exceeded", route_class, retry_after)
            return

        # 3. Concurrency slot for MCP tool calls.
        slot_key = None
        if route_class == MCP and scope.get("method") == "POST" and self.config.mcp_concurrency > 0:
            if self._inflight.get(key, 0) >= self.config.mcp_concurrency:
                logger.warning(
                    "ratelimit: 429 mcp concurrency cap (%d) principal=%s",
                    self.config.mcp_concurrency,
                    key.split(":", 1)[0],
                )
                await self._reject(send, 429, "too many concurrent requests", route_class, 1)
                return
            self._inflight[key] = self._inflight.get(key, 0) + 1
            slot_key = key

        try:
            await self._call_with_body_guard(scope, receive, send, route_class, max_body)
        finally:
            if slot_key is not None:
                n = self._inflight.get(slot_key, 1) - 1
                if n <= 0:
                    self._inflight.pop(slot_key, None)
                else:
                    self._inflight[slot_key] = n

    async def _call_with_body_guard(self, scope, receive, send, route_class, max_body):
        if max_body <= 0:
            await self.app(scope, receive, send)
            return

        state = {"seen": 0, "exceeded": False, "started": False}

        async def guarded_receive():
            message = await receive()
            if message.get("type") == "http.request":
                state["seen"] += len(message.get("body") or b"")
                if state["seen"] > max_body:
                    state["exceeded"] = True
                    raise _BodyTooLargeError()
            return message

        async def guarded_send(message):
            if state["exceeded"] and not state["started"]:
                return  # swallow the app's own error response; we send 413
            if message.get("type") == "http.response.start":
                state["started"] = True
            await send(message)

        try:
            await self.app(scope, guarded_receive, guarded_send)
        except _BodyTooLargeError:
            pass
        except Exception:
            if not state["exceeded"]:
                raise
        if state["exceeded"] and not state["started"]:
            logger.warning(
                "ratelimit: 413 %s %s streamed body > %d", route_class, scope.get("path"), max_body
            )
            await self._reject(send, 413, "request body too large", route_class)
