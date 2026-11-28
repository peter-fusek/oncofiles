"""#1027: the public /health carries no operational telemetry.

* memory numbers, semaphores, uptime and the curated probe heartbeat moved to
  the operator /readiness (the public /readiness never shows them);
* over the memory ceiling /health still answers 200 with a code only;
* infra contract: scripts grep the exact compact strings
  ``"commit":"<sha7>"`` and ``"curated_run_active":false`` in the raw body;
* every documented /health surface stays.

Synthetic values only.
"""

from __future__ import annotations

import inspect
import json

import pytest

from oncofiles import memory_ceiling
from oncofiles.tools import curated
from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

REMOVED = {
    "memory_rss_mb",
    "memory",
    "semaphores",
    "uptime_s",
    "curated_last_probe_age_s",
    "curated_loop_suspected",
}
SHA = "abc1234def5678"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    import oncofiles.server as server

    memory_ceiling._reset_for_tests()
    monkeypatch.setenv("ONCOFILES_GIT_SHA", SHA)
    monkeypatch.setattr(server, "get_rss_mb", lambda: 100.0)
    monkeypatch.setattr(curated, "run_active", lambda: False)
    yield
    memory_ceiling._reset_for_tests()


async def _health():
    import oncofiles.server as server

    request, _db = _request(operator=False)
    return await server.health(request)


async def test_health_raw_body_keeps_the_infra_contract_strings():
    resp = await _health()
    raw = resp.body.decode()
    assert resp.status_code == 200
    assert '"commit":"abc1234"' in raw
    assert '"curated_run_active":false' in raw
    assert '": ' not in raw  # compact JSON, no space after a colon


async def test_health_reports_an_active_run_compactly(monkeypatch):
    monkeypatch.setattr(curated, "run_active", lambda: True)
    raw = (await _health()).body.decode()
    assert '"curated_run_active":true' in raw


async def test_health_has_no_operational_telemetry():
    body = json.loads((await _health()).body)
    assert not REMOVED & set(body), REMOVED & set(body)
    assert "memory_state" not in body  # only while over the ceiling
    for key in ("status", "version", "commit", "curated_run_active", "cloud_ai", "onboarding"):
        assert key in body, key
    assert body["status"] == "ok" and isinstance(body["curated_run_active"], bool)


async def test_health_keeps_the_documented_counts_and_switches(monkeypatch):
    import oncofiles.server as server

    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", False)
    monkeypatch.setattr(server, "_invalid_grant_tokens", {("p-synthetic", "gmail")})
    monkeypatch.setitem(server._hard_auth_failures, ("p-synthetic", "drive"), {"reason": "x"})
    monkeypatch.setitem(server._folder_404_counts, "p-synthetic", server._FOLDER_404_THRESHOLD)
    monkeypatch.setitem(server._sync_staleness, "stale_count", 2)
    body = json.loads((await _health()).body)
    assert body["google_integration"] == "disabled"
    assert body["needs_reauth_count"] == 1
    assert body["hard_auth_failed_count"] == 1
    assert body["folder_404_suspended_count"] == 1
    assert body["stale_sync_count"] == 2


async def test_health_over_the_ceiling_is_200_with_a_code_only(monkeypatch):
    monkeypatch.setattr(memory_ceiling, "check", lambda rss, sem=None: "degraded")
    resp = await _health()
    body = json.loads(resp.body)
    assert resp.status_code == 200
    assert body["memory_state"] == "degraded" and body["status"] == "ok"
    assert not REMOVED & set(body)


def test_health_makes_no_db_call_and_reads_no_removed_helper():
    from oncofiles.server import health

    src = inspect.getsource(health)
    assert "await db." not in src and "_lifespan_result" not in src
    for name in ("get_rss_trend", "get_semaphore_status", "heartbeat(", "started_at"):
        assert name not in src, name


async def test_operator_readiness_carries_the_moved_keys(monkeypatch):
    import oncofiles.server as server

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    request, _db = _request(operator=True)
    body = json.loads((await server.readiness(request)).body)
    assert set(body) >= REMOVED, REMOVED - set(body)
    assert body["memory_rss_mb"] == 100.0
    mem = body["memory"]
    for key in ("state", "ceiling_mb", "last_resort_mb", "current_mb", "peak_mb"):
        assert key in mem, key
    assert "growth_rate_mb_per_hour" in mem
    assert set(body["semaphores"]) == {"query", "express", "background"}
    assert body["uptime_s"] is None  # the synthetic lifespan has no started_at
    assert isinstance(body["curated_loop_suspected"], bool)


async def test_public_readiness_carries_none_of_them(monkeypatch):
    import oncofiles.server as server

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    request, _db = _request(operator=False)
    body = json.loads((await server.readiness(request)).body)
    assert set(body) == {"status", "version", "db"}
