"""Tests for the #591 rate-limit middleware (synthetic credentials only)."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import httpx
import pytest

from oncofiles import ratelimit
from oncofiles.ratelimit import (
    API,
    AUTH,
    EXEMPT,
    MCP,
    PUBLIC,
    ClassLimits,
    Limit,
    RateLimitConfig,
    RateLimitMiddleware,
    classify,
    principal_for,
)

STATIC = "static-test-bearer-synthetic"
SESSION_TOKEN = "carer@example.test|exp|tidsynthetic|sigsynthetic"
SESSION_COOKIE = quote("carer@example.test|exp2|tidother|sigother", safe="")
OFF = Limit(per_minute=0, burst=0)
TRICKLE = Limit(per_minute=1, burst=1)
SMALL_BODY = 100
TINY_BODY = 1


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def echo_app(scope, receive, send):
    """Minimal ASGI app: drains the body and returns 200 with its length."""
    total = 0
    while True:
        msg = await receive()
        total += len(msg.get("body") or b"")
        if not msg.get("more_body"):
            break
    body = json.dumps({"ok": True, "len": total}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _cfg(**overrides) -> RateLimitConfig:
    classes = {
        MCP: ClassLimits(Limit(60, 3), Limit(6000, 1000), 1000),
        AUTH: ClassLimits(Limit(60, 2), Limit(6000, 1000), 100),
        API: ClassLimits(Limit(60, 4), Limit(6000, 1000), 500),
        PUBLIC: ClassLimits(Limit(60, 5), Limit(6000, 1000), 100),
    }
    classes.update(overrides.pop("classes", {}))
    return RateLimitConfig(classes=classes, static_bearer=STATIC, **overrides)


def _client(app, ip="203.0.113.7") -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(ip, 1234))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _mw(app=echo_app, clock=None, **cfg) -> RateLimitMiddleware:
    return RateLimitMiddleware(app, config=_cfg(**cfg), clock=clock or FakeClock())


# ── classification + principals ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/health", EXEMPT),
        ("/mcp", MCP),
        ("/mcp/", MCP),
        ("/authorize", AUTH),
        ("/token", AUTH),
        ("/register", AUTH),
        ("/oauth/callback", AUTH),
        ("/dashboard/verify", AUTH),
        ("/api/documents", API),
        ("/dashboard/config", API),
        ("/", PUBLIC),
        ("/dashboard", PUBLIC),
        ("/readiness", PUBLIC),
        ("/healthz", PUBLIC),
    ],
)
def test_classify(path, expected):
    assert classify(path) == expected


def _scope(headers=(), ip="198.51.100.1"):
    return {
        "type": "http",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "client": (ip, 1),
    }


def test_principal_kinds():
    cfg = _cfg()
    assert principal_for(_scope([("authorization", f"Bearer {STATIC}")]), MCP, cfg) == "static"
    assert principal_for(_scope([("authorization", "Bearer onco_x")]), MCP, cfg).startswith(
        "patient:"
    )
    assert principal_for(_scope([("authorization", "Bearer opaque")]), MCP, cfg).startswith(
        "oauth:"
    )
    sess = principal_for(_scope([("authorization", f"Bearer session:{SESSION_TOKEN}")]), API, cfg)
    cookie = principal_for(
        _scope([("cookie", f"theme=dark; oncofiles_session={SESSION_COOKIE}")]), API, cfg
    )
    assert sess.startswith("session:") and sess == cookie  # same user, same bucket
    assert principal_for(_scope(), PUBLIC, cfg) == "ip:198.51.100.1"
    # auth endpoints are always IP-keyed, even with a credential
    assert principal_for(_scope([("authorization", f"Bearer {STATIC}")]), AUTH, cfg).startswith(
        "ip:"
    )


def test_forwarded_for_only_when_trusted():
    s = _scope([("x-forwarded-for", "192.0.2.9, 10.0.0.1")])
    assert principal_for(s, AUTH, _cfg()) == "ip:198.51.100.1"
    assert principal_for(s, AUTH, _cfg(trust_forwarded=True)) == "ip:192.0.2.9"


# ── each bucket + 429 ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path,method,burst,json_body",
    [
        ("/mcp", "POST", 3, True),
        ("/token", "POST", 2, True),
        ("/api/documents", "GET", 4, True),
        ("/privacy", "GET", 5, False),
    ],
)
async def test_each_bucket_429_with_retry_after(path, method, burst, json_body):
    app = _mw()
    async with _client(app) as c:
        for _ in range(burst):
            r = await c.request(method, path, headers={"authorization": f"Bearer {STATIC}"})
            assert r.status_code == 200
        r = await c.request(method, path, headers={"authorization": f"Bearer {STATIC}"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1
    if json_body:
        assert r.headers["content-type"] == "application/json"
        assert r.json()["error"] == "rate limit exceeded"
        assert r.json()["retry_after"] == int(r.headers["retry-after"])
    else:
        assert r.headers["content-type"].startswith("text/plain")


async def test_buckets_are_separate_per_class():
    app = _mw()
    async with _client(app) as c:
        for _ in range(5):
            await c.get("/privacy")
        assert (await c.get("/privacy")).status_code == 429
        assert (await c.get("/api/stats")).status_code == 200
        assert (await c.post("/mcp")).status_code == 200


async def test_bucket_refills_over_time():
    clock = FakeClock()
    app = _mw(clock=clock)
    async with _client(app) as c:
        for _ in range(3):
            await c.post("/mcp")
        r = await c.post("/mcp")
        assert r.status_code == 429
        assert r.headers["retry-after"] == "1"  # 60/min → 1 token per second
        clock.t += 1.0
        assert (await c.post("/mcp")).status_code == 200


async def test_global_bucket_caps_all_principals():
    app = _mw(classes={MCP: ClassLimits(Limit(60, 100), Limit(60, 3), 1000)})
    async with _client(app) as c:
        for i in range(3):
            r = await c.post("/mcp", headers={"authorization": f"Bearer onco_user{i}"})
            assert r.status_code == 200
        r = await c.post("/mcp", headers={"authorization": "Bearer onco_user9"})
    assert r.status_code == 429


async def test_zero_rate_disables_bucket():
    app = _mw(classes={PUBLIC: ClassLimits(OFF, OFF, SMALL_BODY)})
    async with _client(app) as c:
        for _ in range(50):
            assert (await c.get("/")).status_code == 200


# ── per-principal isolation ───────────────────────────────────────────────────


async def test_one_principal_burst_does_not_block_another():
    app = _mw()
    async with _client(app) as c:
        for _ in range(3):
            await c.post("/mcp", headers={"authorization": "Bearer onco_alice"})
        r = await c.post("/mcp", headers={"authorization": "Bearer onco_alice"})
        assert r.status_code == 429
        r = await c.post("/mcp", headers={"authorization": "Bearer onco_bob"})
        assert r.status_code == 200
        r = await c.post("/mcp", headers={"authorization": f"Bearer {STATIC}"})
        assert r.status_code == 200


async def test_unauthenticated_ips_are_isolated():
    app = _mw()
    async with _client(app, ip="198.51.100.1") as a, _client(app, ip="198.51.100.2") as b:
        for _ in range(2):
            await a.post("/register")
        assert (await a.post("/register")).status_code == 429
        assert (await b.post("/register")).status_code == 200


def test_principal_map_is_bounded():
    mw = _mw(max_principals=5)
    for i in range(50):
        mw.check(MCP, f"oauth:{i}")
    assert len(mw._principal) == 5


# ── /health never limited ─────────────────────────────────────────────────────


async def test_health_never_limited_or_size_checked():
    app = _mw(classes={PUBLIC: ClassLimits(TRICKLE, TRICKLE, TINY_BODY)})
    async with _client(app) as c:
        # exhaust everything else first
        await c.get("/")
        assert (await c.get("/")).status_code == 429
        for _ in range(200):
            assert (await c.get("/health")).status_code == 200
        r = await c.post("/health", content=b"x" * 10_000)
        assert r.status_code == 200


async def test_options_preflight_not_limited():
    app = _mw()
    async with _client(app) as c:
        for _ in range(20):
            assert (await c.options("/mcp")).status_code == 200


# ── 413 body size ─────────────────────────────────────────────────────────────


async def test_413_from_content_length_before_reading():
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True
        await echo_app(scope, receive, send)

    mw = _mw(app=app)
    async with _client(mw) as c:
        r = await c.post("/mcp", content=b"x" * 1001)
    assert r.status_code == 413
    assert r.json()["error"] == "request body too large"
    assert called is False


async def test_body_limit_per_class():
    mw = _mw()
    async with _client(mw) as c:
        assert (await c.post("/mcp", content=b"x" * 900)).status_code == 200
        assert (await c.post("/token", content=b"x" * 101)).status_code == 413
        assert (await c.post("/api/bug-report", content=b"x" * 400)).status_code == 200


async def test_413_for_chunked_body_without_content_length():
    mw = _mw()

    async def chunks():
        for _ in range(5):
            yield b"x" * 300

    async with _client(mw) as c:
        r = await c.post("/mcp", content=chunks())
    assert r.status_code == 413


async def test_413_when_app_swallows_the_guard_exception():
    async def app(scope, receive, send):
        try:
            while (await receive()).get("more_body"):
                pass
        except Exception:
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": b"bad"})
            return

    mw = _mw(app=app)

    async def chunks():
        for _ in range(5):
            yield b"x" * 300

    async with _client(mw) as c:
        r = await c.post("/mcp", content=chunks())
    assert r.status_code == 413


def test_default_mcp_body_limit_fits_base64_uploads():
    assert ratelimit.DEFAULTS[MCP].max_body_bytes == 10 * 1024 * 1024
    assert ratelimit.DEFAULTS[AUTH].max_body_bytes <= 64 * 1024


# ── concurrency cap ───────────────────────────────────────────────────────────


async def test_mcp_concurrency_cap_per_principal():
    gate = asyncio.Event()
    entered = 0

    async def slow_app(scope, receive, send):
        nonlocal entered
        entered += 1
        await gate.wait()
        await echo_app(scope, receive, send)

    mw = _mw(
        app=slow_app,
        mcp_concurrency=2,
        classes={MCP: ClassLimits(Limit(6000, 100), Limit(6000, 100), 1000)},
    )
    alice = {"authorization": "Bearer onco_alice"}
    async with _client(mw) as c:
        first = asyncio.create_task(c.post("/mcp", headers=alice))
        second = asyncio.create_task(c.post("/mcp", headers=alice))
        while entered < 2:
            await asyncio.sleep(0)
        r3 = await c.post("/mcp", headers=alice)
        assert r3.status_code == 429
        assert r3.headers["retry-after"] == "1"
        assert r3.json()["error"] == "too many concurrent requests"
        # another principal is unaffected
        other = asyncio.create_task(c.post("/mcp", headers={"authorization": "Bearer onco_bob"}))
        while entered < 3:
            await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(first, second, other)
    assert [r.status_code for r in results] == [200, 200, 200]
    assert mw._inflight == {}  # slots released


async def test_concurrency_slot_released_on_app_error():
    async def boom(scope, receive, send):
        raise RuntimeError("synthetic failure")

    mw = _mw(app=boom, mcp_concurrency=1)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
        "client": ("198.51.100.1", 1),
    }

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(_):
        pass

    with pytest.raises(RuntimeError):
        await mw(scope, receive, send)
    assert mw._inflight == {}


# ── disabled mode + env config ────────────────────────────────────────────────


async def test_disabled_mode_is_pass_through():
    mw = _mw(enabled=False)
    async with _client(mw) as c:
        for _ in range(30):
            assert (await c.post("/token")).status_code == 200
        assert (await c.post("/token", content=b"x" * 5000)).status_code == 200


def test_from_env_defaults(monkeypatch):
    for k in list(__import__("os").environ):
        if k.startswith("RATE_LIMIT_"):
            monkeypatch.delenv(k)
    cfg = RateLimitConfig.from_env()
    assert cfg.enabled is True
    assert cfg.classes == ratelimit.DEFAULTS
    assert cfg.mcp_concurrency == ratelimit.DEFAULT_MCP_CONCURRENCY
    assert cfg.trust_forwarded is False


def test_from_env_overrides_and_kill_switch(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "false")
    monkeypatch.setenv("RATE_LIMIT_MCP_PER_MIN", "10")
    monkeypatch.setenv("RATE_LIMIT_AUTH_MAX_BODY_BYTES", "2048")
    monkeypatch.setenv("RATE_LIMIT_MCP_CONCURRENCY", "7")
    monkeypatch.setenv("RATE_LIMIT_API_BURST", "not-a-number")
    cfg = RateLimitConfig.from_env()
    assert cfg.enabled is False
    assert cfg.classes[MCP].principal.per_minute == 10
    assert cfg.classes[AUTH].max_body_bytes == 2048
    assert cfg.mcp_concurrency == 7
    assert cfg.classes[API].principal.burst == ratelimit.DEFAULTS[API].principal.burst


def test_registered_in_server_main():
    import inspect

    from oncofiles import server

    src = inspect.getsource(server.main)
    assert "Middleware(RateLimitMiddleware)" in src
