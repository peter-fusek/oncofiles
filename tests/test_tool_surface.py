"""#887: user tools on /mcp, operator tools only on /ops/mcp, ops CLI.

* every registered tool is classified exactly once (new tools must choose);
* every deprecated tool is operator-only;
* over real HTTP (uvicorn on 127.0.0.1, the production app built by
  ``mcp.http_app``): ``/mcp`` lists no operator tool and refuses to call one;
  ``/ops/mcp`` without / with a wrong / with a non-operator bearer → 401;
  with the operator bearer → the full list;
* the CLI (``python -m oncofiles.ops``) calls a harmless read tool, reports
  401 on a wrong token, and talks to the SAME process: a job started through
  it occupies the in-memory job registry, so a second start is refused busy.

Synthetic token and data only.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import sys
from contextlib import asynccontextmanager

import httpx
import pytest

from oncofiles.tool_surface import OPERATOR_TOOLS, OPS_MCP_PATH, USER_TOOLS
from tests.conftest import ERIKA_UUID

OPERATOR_TOKEN = "synthetic-operator-token-887"


@pytest.fixture(scope="module")
def registered():
    from oncofiles.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


# ── classification lock ──────────────────────────────────────────────────────


def test_sets_are_disjoint():
    assert not USER_TOOLS & OPERATOR_TOOLS


def test_every_tool_is_classified(registered):
    """A new tool must be put in USER_TOOLS or OPERATOR_TOOLS
    (oncofiles/tool_surface.py). Unclassified tools are hidden on /mcp."""
    missing = sorted(set(registered) - USER_TOOLS - OPERATOR_TOOLS)
    assert missing == [], f"classify these tools in tool_surface.py: {missing}"


def test_no_stale_names(registered):
    stale = sorted((USER_TOOLS | OPERATOR_TOOLS) - set(registered))
    assert stale == [], f"no longer registered, remove from tool_surface.py: {stale}"


def test_deprecated_tools_are_operator_only(registered):
    deprecated = {
        n for n, t in registered.items() if (t.description or "").lstrip().startswith("[DEPRECATED")
    }
    assert deprecated, "expected deprecated tools to exist"
    assert sorted(deprecated - OPERATOR_TOOLS) == []


def test_in_process_listing_is_the_full_surface(registered):
    """No HTTP request (stdio, in-process): nothing is hidden."""
    assert set(registered) == USER_TOOLS | OPERATOR_TOOLS


def test_instructions_do_not_point_users_at_operator_tools():
    from oncofiles.server import _MCP_INSTRUCTIONS

    assert "system_health" not in _MCP_INSTRUCTIONS
    assert "suggest `curated_index`" not in _MCP_INSTRUCTIONS


# ── live server on 127.0.0.1 ─────────────────────────────────────────────────


@pytest.fixture
async def live_server(db, monkeypatch):
    """The production app (``mcp.http_app``) served by uvicorn on loopback,
    with a synthetic operator bearer and a minimal lifespan around the test DB."""
    import uvicorn
    from fastmcp.server.auth.auth import ClientRegistrationOptions

    from oncofiles import server
    from oncofiles.persistent_oauth import PersistentOAuthProvider

    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    provider = PersistentOAuthProvider(
        bearer_token=OPERATOR_TOKEN,
        base_url="http://127.0.0.1",
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    provider.set_db(db)

    @asynccontextmanager
    async def test_lifespan(_server):
        yield {"db": db, "files": None, "gdrive": None, "gmail": None, "calendar": None}

    monkeypatch.setattr(server.mcp, "auth", provider)
    monkeypatch.setattr(server.mcp, "_lifespan", test_lifespan)
    app = server.mcp.http_app(transport="streamable-http", stateless_http=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, lifespan="on", log_level="warning"))
    task = asyncio.create_task(srv.serve(sockets=[sock]))
    for _ in range(500):
        if srv.started:
            break
        await asyncio.sleep(0.01)
    assert srv.started, "test server did not start"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        srv.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()


@asynccontextmanager
async def _client(url: str, token: str):
    from fastmcp import Client
    from fastmcp.client.transports import StreamableHttpTransport

    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport, timeout=30) as client:
        yield client


async def test_public_mcp_lists_no_operator_tool(live_server):
    async with _client(f"{live_server}/mcp", OPERATOR_TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == set(USER_TOOLS)
    assert names & OPERATOR_TOOLS == set()


async def test_public_mcp_refuses_operator_tool_call(live_server):
    async with _client(f"{live_server}/mcp", OPERATOR_TOKEN) as c:
        result = await c.call_tool("query_db", {"sql": "SELECT 1"}, raise_on_error=False)
        assert result.is_error
        text = " ".join(getattr(x, "text", "") for x in result.content).lower()
        assert "unknown tool" in text or "not found" in text
        # a user tool still works on the same endpoint
        ok = await c.call_tool("whoami", {}, raise_on_error=False)
        assert not ok.is_error


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token"},
        {"Authorization": "Bearer onco_synthetic_patient_token"},
        {"Authorization": f"Basic {OPERATOR_TOKEN}"},
    ],
    ids=["missing", "wrong", "patient-token", "not-bearer"],
)
async def test_ops_endpoint_requires_operator_bearer(live_server, headers):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    async with httpx.AsyncClient() as http:
        r = await http.post(
            f"{live_server}{OPS_MCP_PATH}",
            json=body,
            headers={**headers, "Accept": "application/json, text/event-stream"},
        )
    assert r.status_code == 401
    assert OPERATOR_TOKEN not in r.text


async def test_ops_endpoint_lists_everything_with_operator_bearer(live_server):
    async with _client(f"{live_server}{OPS_MCP_PATH}", OPERATOR_TOKEN) as c:
        names = {t.name for t in await c.list_tools()}
    assert names == set(USER_TOOLS | OPERATOR_TOOLS)


# ── loopback only ────────────────────────────────────────────────────────────


@pytest.fixture
def prod_app(monkeypatch):
    """The production app (no lifespan needed: the gate answers first)."""
    from fastmcp.server.auth.auth import ClientRegistrationOptions

    from oncofiles import server
    from oncofiles.persistent_oauth import PersistentOAuthProvider

    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    provider = PersistentOAuthProvider(
        bearer_token=OPERATOR_TOKEN,
        base_url="http://127.0.0.1",
        client_registration_options=ClientRegistrationOptions(enabled=True),
    )
    monkeypatch.setattr(server.mcp, "auth", provider)
    return server.mcp.http_app(transport="streamable-http", stateless_http=True)


_LIST_BODY = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
_ACCEPT = {"Accept": "application/json, text/event-stream"}


async def _post_as(app, client_ip, path, headers):
    transport = httpx.ASGITransport(app=app, client=(client_ip, 40123))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        return await http.post(path, json=_LIST_BODY, headers={**_ACCEPT, **headers})


@pytest.mark.parametrize("client_ip", ["172.17.0.1", "10.0.0.5", "203.0.113.7", "::ffff:127.0.0.1"])
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"X-Forwarded-For": "127.0.0.1"},
        {"Forwarded": "for=127.0.0.1"},
        {"X-Real-IP": "127.0.0.1"},
        dict.fromkeys(("X-Forwarded-For", "X-Real-IP", "Forwarded"), "localhost"),
    ],
    ids=["plain", "xff", "forwarded", "x-real-ip", "all-three-localhost"],
)
async def test_ops_endpoint_is_404_for_non_loopback_even_with_valid_token(
    prod_app, client_ip, extra
):
    """Valid operator token from a non-loopback peer → the same 404 as an
    unknown path; forwarding headers never make a peer loopback."""
    auth = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
    r = await _post_as(prod_app, client_ip, OPS_MCP_PATH, {**auth, **extra})
    unknown = await _post_as(prod_app, client_ip, "/no-such-path-887", auth)
    assert r.status_code == 404
    assert (r.status_code, r.text, r.headers.get("content-type")) == (
        unknown.status_code,
        unknown.text,
        unknown.headers.get("content-type"),
    )
    assert "www-authenticate" not in r.headers


async def test_ops_endpoint_without_client_is_404(prod_app):
    from oncofiles.tool_surface import OpsGate

    gate = next(r.endpoint for r in prod_app.routes if getattr(r, "path", None) == OPS_MCP_PATH)
    assert isinstance(gate, OpsGate)
    from starlette.exceptions import HTTPException

    scope = {"type": "http", "method": "POST", "path": OPS_MCP_PATH, "headers": []}
    with pytest.raises(HTTPException) as exc:
        await gate(scope, None, None)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("client_ip", ["127.0.0.1", "::1"])
async def test_ops_endpoint_loopback_bad_token_is_401(prod_app, client_ip):
    r = await _post_as(prod_app, client_ip, OPS_MCP_PATH, {"Authorization": "Bearer wrong"})
    assert r.status_code == 401


# ── CLI ──────────────────────────────────────────────────────────────────────


async def _cli(argv, url, monkeypatch, token=OPERATOR_TOKEN):
    from oncofiles import ops

    monkeypatch.setenv("MCP_BEARER_TOKEN", token)
    monkeypatch.setenv("ONCOFILES_OPS_URL", f"{url}{OPS_MCP_PATH}")
    out, err = io.StringIO(), io.StringIO()
    code = await ops.run(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


async def test_cli_runs_a_read_tool(live_server, monkeypatch):
    code, out, err = await _cli(
        ["curated_reocr_status", "--patient", "q1b"], live_server, monkeypatch
    )
    assert code == 0, err
    assert json.loads(out)["state"] == "none"
    assert OPERATOR_TOKEN not in out + err


async def test_cli_list_marks_operator_tools(live_server, monkeypatch):
    code, out, _ = await _cli(["list"], live_server, monkeypatch)
    assert code == 0
    rows = dict(line.split("\t") for line in out.strip().splitlines())
    assert rows["curated_reocr"] == "operator"
    assert rows["search_documents"] == "user"


async def test_cli_wrong_token_is_401(live_server, monkeypatch):
    code, out, err = await _cli(["list"], live_server, monkeypatch, token="wrong-token")
    assert code == 2
    assert "401" in err
    assert "wrong-token" not in out + err


async def test_cli_without_token_refuses(monkeypatch):
    from oncofiles import ops

    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    err = io.StringIO()
    assert await ops.run(["list"], out=io.StringIO(), err=err) == 2
    assert "MCP_BEARER_TOKEN" in err.getvalue()


async def test_cli_shares_the_server_job_registry(live_server, monkeypatch):
    """A job started through /ops lives in the server's in-memory registry:
    a second start for the same patient is refused as busy."""
    from oncofiles.tools import curated

    release = asyncio.Event()

    async def fake_targets(db, pid, document_ids, *, only_with_facts=True, excluded=None):
        return [
            {
                "document_id": 1,
                "filename": "20270105_Synthetic_Lab_Labs_Panel.pdf",
                "category": "labs",
                "reasons": ["synthetic"],
                "unverified_facts": 0,
            }
        ]

    async def fake_job(db, pid, docs, **kwargs):
        await release.wait()

    monkeypatch.setattr(curated, "_reocr_targets", fake_targets)
    monkeypatch.setattr(curated, "_reocr_job", fake_job)
    try:
        code, out, err = await _cli(
            ["curated_reocr", "--patient", "q1b", "--json", '{"dry_run": true}'],
            live_server,
            monkeypatch,
        )
        assert code == 0, err
        assert json.loads(out)["state"] == "running"
        assert curated._reocr_jobs[ERIKA_UUID]["state"] == "running"

        code, out, err = await _cli(
            ["curated_reocr", "--patient", "q1b", "--json", '{"dry_run": true}'],
            live_server,
            monkeypatch,
        )
        assert code == 0, err
        assert json.loads(out)["error"].startswith("busy")

        status_argv = ["--status", "reocr", "--patient", "q1b"]
        code, out, _ = await _cli(status_argv, live_server, monkeypatch)
        assert code == 0 and json.loads(out)["state"] == "running"
    finally:
        release.set()
        job = curated._reocr_jobs.pop(ERIKA_UUID, None) or {}
        task = job.get("task")
        if task is not None:
            await asyncio.wait_for(task, 5)


async def test_cli_module_runs_as_subprocess(live_server):
    """``python -m oncofiles.ops`` end to end, as infra runs it in the container."""
    env = {
        **os.environ,
        "MCP_BEARER_TOKEN": OPERATOR_TOKEN,
        "ONCOFILES_OPS_URL": f"{live_server}{OPS_MCP_PATH}",
    }
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "oncofiles.ops",
        "list",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, err.decode()[-500:]
    assert "curated_reocr\toperator" in out.decode()
    assert OPERATOR_TOKEN.encode() not in out + err
