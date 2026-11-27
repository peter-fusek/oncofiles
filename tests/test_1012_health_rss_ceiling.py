"""#1012: the /health hard RSS ceiling follows the container limit.

* levels: env, else a share of the cgroup limit, else the old default; the
  last resort the same way (or derived from the ceiling);
* order soft < ceiling < last resort < limit, a broken env combination logs
  one warning and is used as given;
* between ceiling and last resort /health exits only when idle; while busy
  (unknown = busy) it answers 200 and operator /readiness reads degraded;
* above the last resort it always exits, one line with the reason and
  whether work ran; SIGTERM once, never sys.exit;
* /health stays free of DB calls.

Synthetic only: fake cgroup files under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import signal

import pytest

from oncofiles import memory_ceiling, memory_check_loop
from oncofiles.tools import curated
from tests.test_999_memory_check_loop import idle, kills  # noqa: F401 - fixtures

C = memory_ceiling
MIB = 1024**2
GIB = 1024**3
LIMIT = 2 * GIB
LIMIT_MB = 2048
CEIL = int(LIMIT_MB * C.CEILING_SHARE)
LAST = int(LIMIT_MB * C.LAST_RESORT_SHARE)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(C.CEILING_ENV, raising=False)
    monkeypatch.delenv(C.LAST_RESORT_ENV, raising=False)
    C._reset_for_tests()
    yield
    C._reset_for_tests()


def _cgroup(tmp_path, text):
    f = tmp_path / "memory.max"
    f.write_text(text)
    return f


def _resolve(tmp_path, text, soft=None, environ=None):
    return C.resolve(
        soft, environ or {}, unified=_cgroup(tmp_path, text), legacy=tmp_path / "missing"
    )


# ── levels ───────────────────────────────────────────────────────────────


def test_levels_from_the_cgroup_limit(tmp_path):
    lv = _resolve(tmp_path, str(LIMIT))
    assert (lv["ceiling_mb"], lv["ceiling_source"]) == (CEIL, "cgroup")
    assert (lv["last_resort_mb"], lv["last_resort_source"]) == (LAST, "cgroup")
    assert lv["limit_mb"] == LIMIT_MB


def test_no_limit_keeps_the_old_ceiling(tmp_path):
    lv = _resolve(tmp_path, "max")
    assert (lv["ceiling_mb"], lv["ceiling_source"]) == (C.DEFAULT_CEILING_MB, "default")
    assert lv["last_resort_source"] == "derived"
    assert lv["last_resort_mb"] > lv["ceiling_mb"]
    assert lv["limit_mb"] is None


def test_env_overrides_both(tmp_path):
    env = {C.CEILING_ENV: "1500", C.LAST_RESORT_ENV: "1900"}
    lv = _resolve(tmp_path, str(LIMIT), environ=env)
    assert (lv["ceiling_mb"], lv["ceiling_source"]) == (1500, "env")
    assert (lv["last_resort_mb"], lv["last_resort_source"]) == (1900, "env")


def test_bad_env_value_is_ignored(tmp_path, caplog):
    lv = _resolve(tmp_path, str(LIMIT), environ={C.CEILING_ENV: "lots"})
    assert lv["ceiling_source"] == "cgroup"
    assert "not a positive integer" in caplog.text


# ── order ────────────────────────────────────────────────────────────────


def test_default_order_holds_with_the_memory_check_threshold(tmp_path):
    soft, _ = memory_check_loop.resolve_threshold(
        {}, unified=_cgroup(tmp_path, str(LIMIT)), legacy=tmp_path / "missing"
    )
    lv = C.levels(LIMIT, {})
    assert soft < lv["ceiling_mb"] < lv["last_resort_mb"] < lv["limit_mb"]
    assert C.ordering_problems(soft, lv) == []


def test_order_holds_without_a_limit():
    soft = memory_check_loop.DEFAULT_THRESHOLD_MB
    lv = C.levels(None, {})
    assert soft < lv["ceiling_mb"] < lv["last_resort_mb"]
    assert C.ordering_problems(soft, lv) == []


@pytest.mark.parametrize(
    "env,soft,problem",
    [
        ({C.CEILING_ENV: "1000"}, 1228, "soft_not_below_ceiling"),
        ({C.CEILING_ENV: "1950", C.LAST_RESORT_ENV: "1900"}, 1228, "ceiling_not_below_last_resort"),
        ({C.LAST_RESORT_ENV: "4096"}, 1228, "last_resort_not_below_limit"),
    ],
)
def test_broken_env_order_warns_once_and_is_used_as_given(tmp_path, caplog, env, soft, problem):
    caplog.set_level(logging.WARNING, logger="oncofiles.memory_ceiling")
    lv = _resolve(tmp_path, str(LIMIT), soft=soft, environ=env)
    lines = [r.getMessage() for r in caplog.records if "level order broken" in r.getMessage()]
    assert len(lines) == 1 and problem in lines[0]
    for name, value in env.items():  # never swapped
        key = "ceiling_mb" if name == C.CEILING_ENV else "last_resort_mb"
        assert lv[key] == int(value)


# ── the check /health runs ───────────────────────────────────────────────


def _levels_of(tmp_path):
    _resolve(tmp_path, str(LIMIT), soft=1228)


def test_under_the_ceiling_is_ok(tmp_path, kills, idle):  # noqa: F811
    _levels_of(tmp_path)
    assert C.check(CEIL - 10, asyncio.Semaphore(1)) == "ok"
    assert kills == [] and C.status()["state"] == "ok"


def test_over_the_ceiling_while_idle_exits_gracefully(tmp_path, kills, idle, caplog):  # noqa: F811
    _levels_of(tmp_path)
    assert C.check(CEIL + 10, asyncio.Semaphore(1)) == "exit_idle"
    assert [s for _p, s in kills] == [signal.SIGTERM]
    assert "reason=ceiling_idle" in caplog.text and "work_running=False" in caplog.text


def test_over_the_ceiling_while_busy_stays_alive_and_degraded(
    tmp_path,
    kills,  # noqa: F811
    idle,  # noqa: F811
    monkeypatch,
):
    _levels_of(tmp_path)
    monkeypatch.setattr(curated, "run_active", lambda: True)
    assert C.check(CEIL + 10, asyncio.Semaphore(1)) == "degraded"
    assert C.check(CEIL + 20, asyncio.Semaphore(1)) == "degraded"
    assert kills == []
    st = C.status()
    assert st["state"] == "degraded" and st["since"] and st["deferred_reason"] == "curated_work"
    # work ends, still over the ceiling: exits now
    monkeypatch.setattr(curated, "run_active", lambda: False)
    assert C.check(CEIL + 20, asyncio.Semaphore(1)) == "exit_idle"
    assert len(kills) == 1


def test_unknown_state_counts_as_busy(tmp_path, kills, idle, monkeypatch):  # noqa: F811
    _levels_of(tmp_path)
    monkeypatch.setattr(curated, "run_active", lambda: 1 / 0)
    assert C.check(CEIL + 10, asyncio.Semaphore(1)) == "degraded"
    assert C.status()["deferred_reason"] == "state_unknown" and kills == []


def test_back_under_the_ceiling_clears_degraded(tmp_path, kills, idle, monkeypatch):  # noqa: F811
    _levels_of(tmp_path)
    monkeypatch.setattr(curated, "run_active", lambda: True)
    C.check(CEIL + 10, asyncio.Semaphore(1))
    assert C.check(CEIL - 10, asyncio.Semaphore(1)) == "ok"
    st = C.status()
    assert st["state"] == "ok" and st["since"] is None and kills == []


def test_last_resort_exits_even_while_busy(tmp_path, kills, idle, monkeypatch, caplog):  # noqa: F811
    _levels_of(tmp_path)
    monkeypatch.setattr(curated, "run_active", lambda: True)
    assert C.check(LAST + 10, asyncio.Semaphore(1)) == "exit_last_resort"
    assert [s for _p, s in kills] == [signal.SIGTERM]
    assert "reason=last_resort" in caplog.text
    assert "work_running=True" in caplog.text and "busy=curated_work" in caplog.text


def test_sigterm_is_sent_once(tmp_path, kills, idle):  # noqa: F811
    _levels_of(tmp_path)
    C.check(LAST + 10, asyncio.Semaphore(1))
    assert C.check(LAST + 10, asyncio.Semaphore(1)) == "exit_sent"
    assert len(kills) == 1 and C.status()["exit_sent"] is True


def test_before_the_loop_resolves_env_and_defaults_apply(kills, idle):  # noqa: F811
    st = C.status()
    assert st["ceiling_mb"] == C.DEFAULT_CEILING_MB and st["limit_mb"] is None


# ── /health and /readiness ───────────────────────────────────────────────


def test_health_has_no_db_call_and_no_sys_exit():
    from oncofiles.server import health

    src = inspect.getsource(health)
    assert "sys.exit(" not in src and "await db." not in src
    assert "memory_ceiling.check(" in src
    assert "sys.exit(" not in inspect.getsource(memory_ceiling)


async def test_health_answers_200_while_degraded(tmp_path, kills, idle, monkeypatch):  # noqa: F811
    import oncofiles.server as server

    _levels_of(tmp_path)
    monkeypatch.setattr(curated, "run_active", lambda: True)
    monkeypatch.setattr(server, "get_rss_mb", lambda: float(CEIL + 50))
    from tests.test_one_scheduler_per_process import _request

    request, _ = _request(operator=False)
    resp = await server.health(request)
    assert resp.status_code == 200 and kills == []
    assert C.status()["state"] == "degraded"


async def test_readiness_shows_the_memory_state(tmp_path, kills, idle, monkeypatch):  # noqa: F811
    import oncofiles.server as server
    from tests.test_one_scheduler_per_process import OPERATOR_TOKEN, _request

    monkeypatch.setattr(server, "MCP_BEARER_TOKEN", OPERATOR_TOKEN)
    _levels_of(tmp_path)
    monkeypatch.setattr(curated, "run_active", lambda: True)
    C.check(CEIL + 10, asyncio.Semaphore(1))
    request, _ = _request(operator=True)
    mem = json.loads((await server.readiness(request)).body)["memory"]
    assert mem["state"] == "degraded" and mem["since"]
    assert mem["ceiling_mb"] == CEIL and mem["ceiling_source"] == "cgroup"
    assert mem["last_resort_mb"] == LAST
