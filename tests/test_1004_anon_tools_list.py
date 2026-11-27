"""#1004: anonymous tools/list on the public /mcp (user surface).

Over real HTTP (uvicorn on 127.0.0.1, the production app built by
``mcp.http_app`` with the real ``PersistentOAuthProvider``):

* without any Authorization header, ``tools/list`` answers and the list is
  exactly the user surface (no operator tool);
* anonymous ``initialize`` (and ``notifications/initialized``) stay 401: a
  200 there made Claude.ai pre-select "No sign-in" for a new connector;
* ``tools/call`` and any other method, a batch mixing in one, a body that is
  not JSON or too long → 401 with ``WWW-Authenticate`` (as before);
* an invalid token is NOT treated as anonymous (401, so clients re-auth);
* ``MCP_ANON_TOOLS_LIST=false`` → 401 for everything anonymous;
* authenticated calls and /ops/mcp are unchanged.

Synthetic token only.
"""

from __future__ import annotations

import json

import httpx
import pytest

from oncofiles import mcp_anon
from oncofiles.tool_surface import OPERATOR_TOOLS, OPS_MCP_PATH, USER_TOOLS
from tests.test_tool_surface import OPERATOR_TOKEN, _client, live_server  # noqa: F401

ACCEPT = {"Accept": "application/json, text/event-stream"}
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "anon-test", "version": "0"},
    },
}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
CALL = {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "whoami"}}


def _payloads(text: str) -> list[dict]:
    """JSON body or the data lines of an SSE answer."""
    text = text.strip()
    if text.startswith("{") or text.startswith("["):
        data = json.loads(text)
        return data if isinstance(data, list) else [data]
    return [json.loads(ln[5:]) for ln in text.splitlines() if ln.startswith("data:")]


async def _post(url, body, headers=None, raw=None):
    async with httpx.AsyncClient() as http:
        return await http.post(
            url,
            content=raw if raw is not None else json.dumps(body),
            headers={"Content-Type": "application/json", **ACCEPT, **(headers or {})},
        )


# ── unit: the method check ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body, ok",
    [
        (INIT, False),
        (LIST, True),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, False),
        ([INIT, LIST], False),
        ([LIST, LIST], True),
        (CALL, False),
        ([LIST, CALL], False),
        ([], False),
        ({"jsonrpc": "2.0", "method": "resources/list", "id": 4}, False),
        ({"jsonrpc": "2.0", "method": "prompts/list", "id": 5}, False),
        ({"jsonrpc": "1.0", "method": "tools/list", "id": 6}, False),
        ({"jsonrpc": "2.0", "id": 7, "result": {}}, False),
        ("tools/list", False),
        ([LIST, "x"], False),
    ],
)
def test_anonymous_allowed(body, ok):
    assert mcp_anon.anonymous_allowed(json.dumps(body).encode()) is ok


def test_not_json_is_refused():
    assert not mcp_anon.anonymous_allowed(b"{nope")
    assert not mcp_anon.anonymous_allowed(b"\xff\xfe")


@pytest.mark.parametrize("value", ["false", "0", "no", "OFF", " False "])
def test_switch_off_values(monkeypatch, value):
    monkeypatch.setenv(mcp_anon.ENV_SWITCH, value)
    assert not mcp_anon.anon_enabled()


def test_switch_default_on(monkeypatch):
    monkeypatch.delenv(mcp_anon.ENV_SWITCH, raising=False)
    assert mcp_anon.anon_enabled()


async def test_body_is_replayed_byte_for_byte_to_the_auth_path():
    """A refused (too long, chunked) body reaches the auth-wrapped app whole."""
    seen = {}

    async def auth_app(scope, receive, send):
        chunks = []
        while True:
            m = await receive()
            chunks.append(m.get("body", b""))
            if not m.get("more_body"):
                break
        seen["auth"] = b"".join(chunks)

    async def inner(scope, receive, send):  # pragma: no cover - must not run
        seen["inner"] = True

    gate = mcp_anon.AnonCatalogGate(auth_app, inner)
    big = b"x" * (mcp_anon.ANON_MAX_BODY_BYTES + 10)
    parts = [big[:40000], big[40000:]]
    msgs = [
        {"type": "http.request", "body": parts[0], "more_body": True},
        {"type": "http.request", "body": parts[1], "more_body": False},
    ]

    async def receive():
        return msgs.pop(0)

    await gate({"type": "http", "method": "POST", "headers": []}, receive, None)
    assert seen == {"auth": big}


# ── live server ──────────────────────────────────────────────────────────────


async def test_anonymous_tools_list_without_initialize(live_server):  # noqa: F811
    r = await _post(f"{live_server}/mcp", LIST)
    assert r.status_code == 200, r.text
    [listed] = _payloads(r.text)
    names = {t["name"] for t in listed["result"]["tools"]}
    assert names == set(USER_TOOLS)
    assert names & OPERATOR_TOOLS == set()


@pytest.mark.parametrize(
    "body",
    [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"}, [INIT, LIST]],
    ids=["initialize", "initialized-notification", "init-batch"],
)
async def test_anonymous_handshake_stays_401(live_server, body):  # noqa: F811
    """Clients decide on sign-in from ``initialize``: it must answer 401."""
    r = await _post(f"{live_server}/mcp", body)
    assert r.status_code == 401, r.text
    assert "bearer" in r.headers.get("www-authenticate", "").lower()


@pytest.mark.parametrize(
    "body, raw",
    [
        (CALL, None),
        ([LIST, CALL], None),
        ({"jsonrpc": "2.0", "id": 4, "method": "resources/list", "params": {}}, None),
        (None, b"{not json"),
        (None, json.dumps(LIST).encode() + b" " * (mcp_anon.ANON_MAX_BODY_BYTES + 1)),
    ],
    ids=["tools-call", "mixed-batch", "resources", "not-json", "too-long"],
)
async def test_anything_else_anonymous_is_401(live_server, body, raw):  # noqa: F811
    r = await _post(f"{live_server}/mcp", body, raw=raw)
    assert r.status_code == 401, r.text
    assert "bearer" in r.headers.get("www-authenticate", "").lower()


async def test_invalid_token_is_not_anonymous(live_server):  # noqa: F811
    r = await _post(f"{live_server}/mcp", LIST, headers={"Authorization": "Bearer wrong-token"})
    assert r.status_code == 401
    assert "bearer" in r.headers.get("www-authenticate", "").lower()


async def test_switch_off_restores_401(live_server, monkeypatch):  # noqa: F811
    monkeypatch.setenv(mcp_anon.ENV_SWITCH, "false")
    for body in (INIT, LIST):
        r = await _post(f"{live_server}/mcp", body)
        assert r.status_code == 401
        assert "bearer" in r.headers.get("www-authenticate", "").lower()


async def test_authenticated_paths_unchanged(live_server):  # noqa: F811
    async with _client(f"{live_server}/mcp", OPERATOR_TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
        ok = await c.call_tool("whoami", {}, raise_on_error=False)
    assert names == set(USER_TOOLS)
    assert not ok.is_error


async def test_ops_endpoint_stays_closed_anonymously(live_server):  # noqa: F811
    r = await _post(f"{live_server}{OPS_MCP_PATH}", LIST)
    assert r.status_code == 401
    async with _client(f"{live_server}{OPS_MCP_PATH}", OPERATOR_TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == set(USER_TOOLS | OPERATOR_TOOLS)
