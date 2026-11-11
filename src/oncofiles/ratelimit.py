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

Each class has a **per-principal** token bucket and two global buckets: the
**trusted** global bucket for callers whose credential is known to be valid,
and a smaller **anonymous** global bucket for everyone else (no credential,
or a credential not yet seen to be valid). An anonymous flood therefore can
never exhaust the bucket authenticated users draw from.

Principals are derived without any DB lookup, so limiting stays O(1) and runs
before any auth or DB work:

* ``static``  — the operator ``MCP_BEARER_TOKEN`` (constant-time compare). Trusted.
* ``session`` — a dashboard session (``Bearer session:…`` or the
  ``oncofiles_session`` cookie). Its HMAC is verified with the injected
  ``session_verifier`` (no DB); only a **valid** session is keyed by the hash
  of its email (all of a user's tabs share one bucket) and trusted. An
  invalid or unverifiable session is keyed by the hash of the *whole* token,
  so a forged token carrying a victim's email never touches the victim's
  bucket.
* ``patient`` / ``oauth`` — an ``onco_*`` patient token / any other bearer,
  keyed by the hash of the whole token. These can only be verified against
  the DB, so they start untrusted; once a ``/mcp`` response for that exact
  token comes back 2xx (``/mcp`` always requires auth), the principal is
  remembered as trusted (LRU-bounded set).
* ``ip``      — no credential: the client address. Never trusted.

The ``auth`` class is always keyed by client IP. ``X-Forwarded-For`` is
honoured **only** when the direct peer is in ``TRUSTED_PROXIES`` (CIDRs,
**empty by default** — infra sets Traefik's exact address). Entries are
walked right to left, skipping trusted proxies; if every hop is a trusted
proxy the direct peer is used, never the client-controlled left-most entry.
This is only sound when the app port is reachable solely through Traefik and
Traefik overwrites (does not append to) a client-sent ``X-Forwarded-For``.
With ``TRUSTED_PROXIES`` empty, all clients may share one address, so the
per-IP auth limit is raised to the auth global limit instead of acting as a
hidden second global bucket.

Logging: credentials, emails and tokens are never logged. Session tokens are
checked structurally first and then with a *silent* verifier, so forged
sessions cannot flood the logs. Rejections (429/413) are logged at most once
per minute per (status, class, principal kind) with a suppressed count, and
request paths are stripped of control characters and truncated.

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
import ipaddress
import json
import logging
import math
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from urllib.parse import unquote

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


Network = ipaddress.IPv4Network | ipaddress.IPv6Network


def parse_networks(raw: str) -> tuple[Network, ...]:
    """Parse a comma-separated CIDR list; invalid entries are logged and skipped."""
    nets = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("ratelimit: ignoring invalid TRUSTED_PROXIES entry %r", part)
    return tuple(nets)


DEFAULT_TRUSTED_PROXIES = ""


@dataclass(frozen=True)
class Limit:
    """Token-bucket parameters: sustained ``per_minute`` with ``burst`` capacity.
    ``per_minute == 0`` disables that bucket."""

    per_minute: float
    burst: float


@dataclass(frozen=True)
class ClassLimits:
    principal: Limit
    global_: Limit  # trusted callers
    anon_global: Limit  # credential-less / not-yet-verified callers
    max_body_bytes: int  # 0 = unlimited


# Defaults — sized for a handful of caregivers on one Mac mini. A Claude.ai
# conversation fires tool calls in short bursts; the dashboard loads ~20 JSON
# endpoints sequentially on open; an OAuth handshake is ~5 auth requests.
# Auth and public traffic is anonymous by nature, so their anonymous global
# equals their global.
DEFAULTS: dict[str, ClassLimits] = {
    MCP: ClassLimits(Limit(120, 60), Limit(600, 200), Limit(60, 30), 10 * 1024 * 1024),
    AUTH: ClassLimits(Limit(30, 20), Limit(120, 60), Limit(120, 60), 64 * 1024),
    API: ClassLimits(Limit(300, 120), Limit(1200, 400), Limit(120, 60), 1024 * 1024),
    PUBLIC: ClassLimits(Limit(300, 150), Limit(1200, 400), Limit(1200, 400), 64 * 1024),
}
DEFAULT_MCP_CONCURRENCY = 4
LOG_INTERVAL_S = 60.0
DEFAULT_MAX_PRINCIPALS = 10_000


@dataclass(frozen=True)
class RateLimitConfig:
    enabled: bool = True
    classes: dict[str, ClassLimits] = field(default_factory=lambda: dict(DEFAULTS))
    mcp_concurrency: int = DEFAULT_MCP_CONCURRENCY  # 0 = unlimited
    trusted_proxies: tuple[Network, ...] = field(
        default_factory=lambda: parse_networks(DEFAULT_TRUSTED_PROXIES)
    )
    max_principals: int = DEFAULT_MAX_PRINCIPALS
    static_bearer: str = ""

    @classmethod
    def from_env(cls, static_bearer: str = "") -> RateLimitConfig:
        """Read env vars. Per class ``X`` in MCP/AUTH/API/PUBLIC:
        ``RATE_LIMIT_X_PER_MIN``, ``RATE_LIMIT_X_BURST``,
        ``RATE_LIMIT_X_GLOBAL_PER_MIN``, ``RATE_LIMIT_X_GLOBAL_BURST``,
        ``RATE_LIMIT_X_ANON_PER_MIN``, ``RATE_LIMIT_X_ANON_BURST``,
        ``RATE_LIMIT_X_MAX_BODY_BYTES``. Plus ``RATE_LIMIT_ENABLED``,
        ``RATE_LIMIT_MCP_CONCURRENCY``, ``RATE_LIMIT_MAX_PRINCIPALS`` and
        ``TRUSTED_PROXIES`` (CIDR list; set it to an empty string to trust none)."""
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
                anon_global=Limit(
                    _env_num(p + "ANON_PER_MIN", d.anon_global.per_minute),
                    _env_num(p + "ANON_BURST", d.anon_global.burst),
                ),
                max_body_bytes=int(_env_num(p + "MAX_BODY_BYTES", d.max_body_bytes)),
            )
        return cls(
            enabled=_env_bool("RATE_LIMIT_ENABLED", True),
            classes=classes,
            mcp_concurrency=int(_env_num("RATE_LIMIT_MCP_CONCURRENCY", DEFAULT_MCP_CONCURRENCY)),
            trusted_proxies=parse_networks(
                os.environ.get("TRUSTED_PROXIES", DEFAULT_TRUSTED_PROXIES)
            ),
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


def safe_for_log(value, limit: int = 120) -> str:
    """Strip control/non-printable characters and truncate (log-injection guard)."""
    text = str(value)
    return "".join(ch if ch.isprintable() else "?" for ch in text[:limit])


def _h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:24]


def _header(scope, name: bytes) -> str:
    for k, v in scope.get("headers") or ():
        if k.lower() == name:
            return v.decode("latin-1", "replace")
    return ""


def _in_networks(ip: str, nets: tuple[Network, ...]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def client_ip(scope, trusted_proxies: tuple[Network, ...] = ()) -> str:
    """Client address. ``X-Forwarded-For`` is used only when the direct peer is
    a trusted proxy; then the right-most entry that is not itself a trusted
    proxy wins (left-most entries are client-controlled)."""
    client = scope.get("client")
    peer = client[0] if client else "unknown"
    if not trusted_proxies or not _in_networks(peer, trusted_proxies):
        return peer
    xff = _header(scope, b"x-forwarded-for")
    if not xff:
        return peer
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ipaddress.ip_address(hop)
        except ValueError:
            return peer  # malformed chain — don't trust any of it
        if not _in_networks(hop, trusted_proxies):
            return hop
    # Every hop is a trusted proxy: the left-most entry is client-controlled,
    # so fall back to the direct peer.
    return peer


@dataclass(frozen=True)
class Principal:
    key: str
    trusted: bool  # credential verified without the DB (static bearer / valid session)
    learnable: bool = False  # token-hash principal that may be learned as valid


SessionVerifier = Callable[[str], "str | None"]


_HEX = frozenset("0123456789abcdef")
MAX_SESSION_TOKEN_LEN = 512


def looks_like_session_token(token: str) -> bool:
    """Cheap structural check of ``email|expiry|tid|sig`` (#510) before any
    HMAC work: bounded length, printable ASCII, 4 parts, numeric expiry,
    32 lowercase-hex signature. Legacy 3-part tokens fail (they were
    invalidated by the #502 key rotation) and are simply treated as unverified."""
    if not token or len(token) > MAX_SESSION_TOKEN_LEN or not token.isascii():
        return False
    if not token.isprintable():
        return False
    parts = token.rsplit("|", 3)
    if len(parts) != 4:
        return False
    email, expiry, tid, sig = parts
    return bool(email) and expiry.isdigit() and bool(tid) and len(sig) == 32 and set(sig) <= _HEX


def _session_principal(token: str, verifier: SessionVerifier | None) -> Principal:
    email = None
    if verifier is not None and looks_like_session_token(token):
        try:
            email = verifier(token)  # must be silent and side-effect free
        except Exception:
            email = None  # never log: the token is attacker-controlled
    if email:
        return Principal("session:" + _h(email.strip().lower()), trusted=True)
    # Unverified: key on the whole token, never on the (forgeable) email field.
    return Principal("session-unverified:" + _h(token), trusted=False)


def principal_for(
    scope,
    route_class: str,
    cfg: RateLimitConfig,
    session_verifier: SessionVerifier | None = None,
) -> Principal:
    """Return the bucket principal for this request (no DB)."""
    ip = client_ip(scope, cfg.trusted_proxies)
    if route_class == AUTH:
        return Principal("ip:" + ip, trusted=False)
    auth = _header(scope, b"authorization").strip()
    if auth[:7].lower() == "bearer ":
        token = auth[7:].strip()
        if token.startswith("session:"):
            return _session_principal(token.removeprefix("session:"), session_verifier)
        if cfg.static_bearer and hmac.compare_digest(
            token.encode("utf-8", "replace"), cfg.static_bearer.encode("utf-8", "replace")
        ):
            return Principal("static", trusted=True)
        if token.startswith("onco_"):
            return Principal("patient:" + _h(token), trusted=False, learnable=True)
        if token:
            return Principal("oauth:" + _h(token), trusted=False, learnable=True)
    cookie = _header(scope, b"cookie")
    if cookie:
        for pair in cookie.split(";"):
            name, sep, value = pair.strip().partition("=")
            if sep and name == "oncofiles_session" and value.strip():
                return _session_principal(unquote(value.strip()), session_verifier)
    return Principal("ip:" + ip, trusted=False)


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
        session_verifier: SessionVerifier | None = None,
    ) -> None:
        self.app = app
        if config is None:
            from oncofiles import config as _app_config

            config = RateLimitConfig.from_env(static_bearer=_app_config.MCP_BEARER_TOKEN or "")
        self.config = config
        self.clock = clock
        self.session_verifier = session_verifier
        limits = dict(config.classes)
        if not config.trusted_proxies:
            # No trusted proxy → every client may share one address; a strict
            # per-IP auth bucket would silently be a second global bucket.
            a = limits[AUTH]
            raised = Limit(
                max(a.principal.per_minute, a.global_.per_minute),
                max(a.principal.burst, a.global_.burst),
            )
            limits[AUTH] = replace(a, principal=raised)
        self.limits = limits
        self._global: dict[tuple[str, bool], TokenBucket] = {}
        self._principal: OrderedDict[tuple[str, str], TokenBucket] = OrderedDict()
        self._known_good: OrderedDict[str, None] = OrderedDict()
        self._inflight: dict[str, int] = {}
        self._log_state: dict[tuple, list[float]] = {}  # key -> [last_logged, suppressed]
        if config.enabled:
            logger.info(
                "ratelimit enabled: %s; mcp concurrency=%d; trusted proxies=%s",
                ", ".join(
                    f"{n}={c.principal.per_minute:g}/min(b{c.principal.burst:g}) "
                    f"global={c.global_.per_minute:g}/min anon={c.anon_global.per_minute:g}/min "
                    f"body<={c.max_body_bytes}"
                    for n, c in limits.items()
                ),
                config.mcp_concurrency,
                ",".join(str(n) for n in config.trusted_proxies) or "none",
            )
        else:
            logger.warning("ratelimit DISABLED (RATE_LIMIT_ENABLED=false)")

    # -- logging --

    def _log_reject(self, key: tuple, fmt: str, *args) -> None:
        """Log a rejection at most once per LOG_INTERVAL_S per key, reporting
        how many were suppressed in between (no log flooding)."""
        now = self.clock()
        st = self._log_state.get(key)
        if st is not None and now - st[0] < LOG_INTERVAL_S:
            st[1] += 1
            return
        suppressed = int(st[1]) if st is not None else 0
        self._log_state[key] = [now, 0]
        logger.warning(fmt + " (suppressed %d similar)", *args, suppressed)

    # -- buckets --

    def _global_bucket(self, route_class: str, trusted: bool, now: float) -> TokenBucket | None:
        c = self.limits[route_class]
        limit = c.global_ if trusted else c.anon_global
        if limit.per_minute <= 0:
            return None
        k = (route_class, trusted)
        b = self._global.get(k)
        if b is None:
            b = self._global[k] = TokenBucket(limit, now)
        return b

    def _principal_bucket(self, route_class: str, key: str, now: float) -> TokenBucket | None:
        limit = self.limits[route_class].principal
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

    def is_trusted(self, principal: Principal) -> bool:
        if principal.trusted:
            return True
        if principal.learnable and principal.key in self._known_good:
            self._known_good.move_to_end(principal.key)
            return True
        return False

    def mark_good(self, key: str) -> None:
        self._known_good[key] = None
        self._known_good.move_to_end(key)
        while len(self._known_good) > self.config.max_principals:
            self._known_good.popitem(last=False)

    def check(self, route_class: str, key: str, trusted: bool = False) -> float:
        """Consume one token from the principal bucket and the matching global
        bucket. Returns 0 if allowed, else the Retry-After seconds. Nothing is
        consumed when the request is rejected."""
        now = self.clock()
        buckets = [
            b
            for b in (
                self._principal_bucket(route_class, key, now),
                self._global_bucket(route_class, trusted, now),
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

        max_body = self.limits[route_class].max_body_bytes

        # 1. Body size from Content-Length — before reading anything.
        if max_body > 0:
            cl = _header(scope, b"content-length")
            if cl:
                try:
                    declared = int(cl)
                except ValueError:
                    declared = -1
                if declared > max_body:
                    self._log_reject(
                        (413, route_class),
                        "ratelimit: 413 %s %s content-length=%d > %d",
                        route_class,
                        safe_for_log(path),
                        declared,
                        max_body,
                    )
                    await self._reject(send, 413, "request body too large", route_class)
                    return

        # 2. Token buckets.
        principal = principal_for(scope, route_class, self.config, self.session_verifier)
        key = principal.key
        trusted = self.is_trusted(principal)
        wait = self.check(route_class, key, trusted)
        if wait > 0:
            retry_after = max(1, math.ceil(wait))
            kind = key.split(":", 1)[0]
            self._log_reject(
                (429, route_class, kind, trusted),
                "ratelimit: 429 %s %s principal=%s trusted=%s retry_after=%ds",
                route_class,
                safe_for_log(path),
                kind,
                trusted,
                retry_after,
            )
            await self._reject(send, 429, "rate limit exceeded", route_class, retry_after)
            return

        # 3. Concurrency slot for MCP tool calls.
        slot_key = None
        if route_class == MCP and scope.get("method") == "POST" and self.config.mcp_concurrency > 0:
            if self._inflight.get(key, 0) >= self.config.mcp_concurrency:
                kind = key.split(":", 1)[0]
                self._log_reject(
                    ("concurrency", kind),
                    "ratelimit: 429 mcp concurrency cap (%d) principal=%s",
                    self.config.mcp_concurrency,
                    kind,
                )
                await self._reject(send, 429, "too many concurrent requests", route_class, 1)
                return
            self._inflight[key] = self._inflight.get(key, 0) + 1
            slot_key = key

        # 4. Learn token-hash principals from successful /mcp responses
        #    (/mcp always requires a valid bearer, so 2xx proves the token).
        out_send = send
        if route_class == MCP and principal.learnable and not trusted:

            async def out_send(message):
                if message.get("type") == "http.response.start" and (
                    200 <= int(message.get("status", 0)) < 300
                ):
                    self.mark_good(key)
                await send(message)

        try:
            await self._call_with_body_guard(scope, receive, out_send, route_class, max_body)
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
            self._log_reject(
                (413, route_class),
                "ratelimit: 413 %s %s streamed body > %d",
                route_class,
                safe_for_log(scope.get("path")),
                max_body,
            )
            await self._reject(send, 413, "request body too large", route_class)
