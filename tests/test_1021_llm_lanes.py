"""Local-model arbiter (#1021): interactive first, background capped, fairness.

Real threads around a fake slow model function; no network. Every arbiter
here is a private instance with explicit settings, except the tests that pin
the module-level wiring (``llm.chat``, the holding reader, ``/readiness``)."""

from __future__ import annotations

import json
import threading
import time

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import llm, llm_lanes
from oncofiles.curated.llm_lanes import BACKGROUND, INTERACTIVE, Arbiter, LaneBusyError


def _settings(total=4, cap=3, max_wait=120.0, hold=0.0):
    def get():
        return {
            "slots_total": total,
            "background_max_while_interactive": min(cap, total),
            "background_max_wait_s": max_wait,
            "interactive_hold_s": hold,
        }

    return get


def _until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class Tracker:
    """A fake slow model: records concurrency per lane and the start order."""

    def __init__(self, arb: Arbiter):
        self.arb = arb
        self.lock = threading.Lock()
        self.now = {INTERACTIVE: 0, BACKGROUND: 0}
        self.peak_total = 0
        self.peak_bg_with_interactive = 0
        self.order: list[str] = []

    def call(self, lane: str, name: str, seconds: float = 0.05):
        with self.arb.slot(lane):
            with self.lock:
                self.now[lane] += 1
                self.order.append(name)
                self.peak_total = max(self.peak_total, sum(self.now.values()))
                if self.now[INTERACTIVE]:
                    self.peak_bg_with_interactive = max(
                        self.peak_bg_with_interactive, self.now[BACKGROUND]
                    )
            time.sleep(seconds)
            with self.lock:
                self.now[lane] -= 1


def _start(fn, *args):
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def test_interactive_is_admitted_ahead_of_queued_background():
    arb = Arbiter(settings=_settings(total=1, cap=1))
    first = arb.acquire(BACKGROUND)
    order: list[str] = []

    def take(lane, name):
        with arb.slot(lane):
            order.append(name)

    bg = _start(take, BACKGROUND, "bg")
    assert _until(lambda: arb.status()["waiting"][BACKGROUND] == 1)
    chat = _start(take, INTERACTIVE, "chat")
    assert _until(lambda: arb.interactive_waiting())
    first.release()
    bg.join(3)
    chat.join(3)
    assert order == ["chat", "bg"]


def test_background_is_capped_at_three_while_interactive_is_active():
    arb = Arbiter(settings=_settings(total=4, cap=3))
    tr = Tracker(arb)
    chat = _start(tr.call, INTERACTIVE, "chat", 0.6)
    assert _until(lambda: arb.status()["active"][INTERACTIVE] == 1)
    threads = [_start(tr.call, BACKGROUND, f"bg{i}", 0.1) for i in range(6)]
    assert _until(lambda: arb.status()["active"][BACKGROUND] == 3)
    time.sleep(0.05)
    assert arb.status()["active"][BACKGROUND] == 3  # the fourth slot stays free
    for t in [chat, *threads]:
        t.join(5)
    assert tr.peak_bg_with_interactive == 3
    assert tr.peak_total <= 4


def test_without_interactive_background_uses_every_slot():
    arb = Arbiter(settings=_settings(total=4, cap=3))
    tr = Tracker(arb)
    threads = [_start(tr.call, BACKGROUND, f"bg{i}", 0.2) for i in range(4)]
    assert _until(lambda: arb.status()["active"][BACKGROUND] == 4)
    for t in threads:
        t.join(3)
    assert tr.peak_total == 4


def test_hold_window_keeps_the_cap_between_chat_turns():
    arb = Arbiter(settings=_settings(total=2, cap=1, hold=60.0))
    with arb.slot(INTERACTIVE):
        pass
    assert arb.interactive_active()  # ended, but within the hold
    lease = arb.acquire(BACKGROUND)
    with pytest.raises(LaneBusyError):
        arb.acquire(BACKGROUND, timeout=0.1)  # capped at 1 during the hold
    lease.release()


def test_fairness_timeout_lets_starved_background_through():
    arb = Arbiter(settings=_settings(total=1, cap=1, max_wait=0.2))
    first = arb.acquire(BACKGROUND)
    order: list[str] = []

    def take(lane, name):
        with arb.slot(lane):
            order.append(name)
            time.sleep(0.02)

    bg = _start(take, BACKGROUND, "bg")
    assert _until(lambda: arb.status()["waiting"][BACKGROUND] == 1)
    chat = _start(take, INTERACTIVE, "chat")
    assert _until(lambda: arb.interactive_waiting())
    time.sleep(0.3)  # bg has now waited longer than the fairness limit
    first.release()
    bg.join(3)
    chat.join(3)
    assert order == ["bg", "chat"]
    assert arb.status()["fairness_grants"] == 1


def test_fairness_never_breaks_the_background_cap():
    arb = Arbiter(settings=_settings(total=4, cap=3, max_wait=0.0))
    leases = [arb.acquire(BACKGROUND) for _ in range(3)]
    chat = arb.acquire(INTERACTIVE)
    with pytest.raises(LaneBusyError):
        arb.acquire(BACKGROUND, timeout=0.1)
    for lease in [*leases, chat]:
        lease.release()


def test_background_never_takes_the_slot_a_chat_waits_for():
    arb = Arbiter(settings=_settings(total=2, cap=2))
    a = arb.acquire(BACKGROUND)
    b = arb.acquire(BACKGROUND)
    got: list[str] = []
    chat = _start(lambda: (arb.acquire(INTERACTIVE), got.append("chat")))
    assert _until(lambda: arb.interactive_waiting())
    bg = _start(lambda: (arb.acquire(BACKGROUND, timeout=2), got.append("bg")))
    assert _until(lambda: arb.status()["waiting"][BACKGROUND] == 1)
    a.release()
    chat.join(3)
    assert got == ["chat"]  # the freed slot went to the chat
    b.release()
    bg.join(3)
    assert got == ["chat", "bg"]


def test_nothing_double_books_the_budget_under_load():
    arb = Arbiter(settings=_settings(total=4, cap=3, max_wait=0.05))
    tr = Tracker(arb)
    threads = []
    for i in range(30):
        lane = INTERACTIVE if i % 4 == 0 else BACKGROUND
        threads.append(_start(tr.call, lane, f"{lane}{i}", 0.01 + (i % 3) * 0.01))
    for t in threads:
        t.join(10)
    st = arb.status()
    assert tr.peak_total <= 4
    assert st["active"] == {INTERACTIVE: 0, BACKGROUND: 0}
    assert st["waiting"] == {INTERACTIVE: 0, BACKGROUND: 0}
    assert st["admitted"][INTERACTIVE] + st["admitted"][BACKGROUND] == 30


def test_multi_slot_lease_for_a_child_process_counts_every_slot():
    arb = Arbiter(settings=_settings(total=4, cap=3))
    child = arb.acquire(BACKGROUND, slots=2)
    assert arb.status()["active"][BACKGROUND] == 2
    other = arb.acquire(BACKGROUND, slots=2)
    with pytest.raises(LaneBusyError):
        arb.acquire(INTERACTIVE, timeout=0.1)
    child.release()
    child.release()  # idempotent
    assert arb.status()["active"][BACKGROUND] == 2
    other.release()


def test_timeout_raises_and_leaves_no_waiter_behind():
    arb = Arbiter(settings=_settings(total=1, cap=1))
    lease = arb.acquire(INTERACTIVE)
    with pytest.raises(LaneBusyError):
        arb.acquire(INTERACTIVE, timeout=0.05)
    with pytest.raises(LaneBusyError):
        arb.acquire(BACKGROUND, timeout=0.05)
    st = arb.status()
    assert st["waiting"] == {INTERACTIVE: 0, BACKGROUND: 0}
    lease.release()


def test_unknown_lane_is_refused():
    with pytest.raises(ValueError):
        Arbiter(settings=_settings()).acquire("urgent")


def test_yield_to_interactive_checkpoint():
    arb = Arbiter(settings=_settings(total=1, cap=1))
    assert arb.yield_to_interactive(1.0) < 0.05  # nobody waits: at once
    lease = arb.acquire(BACKGROUND)
    chat = _start(lambda: arb.acquire(INTERACTIVE).release())
    assert _until(lambda: arb.interactive_waiting())
    _start(lambda: (time.sleep(0.15), lease.release()))
    waited = arb.yield_to_interactive(2.0)
    chat.join(3)
    assert 0.1 <= waited < 2.0
    # bounded: never waits longer than asked
    lease2 = arb.acquire(BACKGROUND)
    chat2 = _start(lambda: arb.acquire(INTERACTIVE).release())
    assert _until(lambda: arb.interactive_waiting())
    assert arb.yield_to_interactive(0.1) < 0.5
    lease2.release()
    chat2.join(3)


def test_status_is_counts_and_seconds_only():
    arb = Arbiter(settings=_settings())
    with arb.slot(INTERACTIVE):
        pass
    with arb.slot(BACKGROUND):
        pass
    st = arb.status()
    assert st["interactive_turns_last_hour"] == 1
    assert st["wait_s"][INTERACTIVE]["n"] == 1
    assert st["wait_s"][BACKGROUND]["p95"] is not None

    def leaves(v):
        if isinstance(v, dict):
            for x in v.values():
                yield from leaves(x)
        else:
            yield v

    assert all(v is None or isinstance(v, int | float | bool) for v in leaves(st))


def test_env_defaults(monkeypatch):
    for name in (
        "LLM_SLOTS_TOTAL",
        "LLM_BACKGROUND_MAX_WHILE_INTERACTIVE",
        "LLM_BACKGROUND_MAX_WAIT_S",
        "LLM_INTERACTIVE_HOLD_S",
    ):
        monkeypatch.delenv(name, raising=False)
    assert llm_lanes._settings() == {
        "slots_total": 4,
        "background_max_while_interactive": 3,
        "background_max_wait_s": 120.0,
        "interactive_hold_s": 120.0,
    }
    monkeypatch.setenv("LLM_SLOTS_TOTAL", "nonsense")
    assert llm_lanes._settings()["slots_total"] == 4


# -- wiring -------------------------------------------------------------------


@pytest.fixture
def fresh_arbiter(monkeypatch):
    arb = Arbiter(settings=_settings(total=4, cap=3))
    monkeypatch.setattr(llm_lanes, "_ARBITER", arb)
    return arb


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")


def _client(seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_chat_default_lane_is_background_and_body_unchanged(fresh_arbiter, local_llm):
    seen: list = []
    assert llm.chat("sys", "hello", client=_client(seen)) == "ok"
    assert fresh_arbiter.status()["admitted"] == {INTERACTIVE: 0, BACKGROUND: 1}
    assert "lane" not in seen[0]
    assert set(seen[0]) == {"model", "temperature", "max_tokens", "reasoning_effort", "messages"}
    assert fresh_arbiter.status()["active"] == {INTERACTIVE: 0, BACKGROUND: 0}


def test_chat_interactive_lane(fresh_arbiter, local_llm):
    seen: list = []
    assert llm.chat("sys", "hello", client=_client(seen), lane="interactive") == "ok"
    assert fresh_arbiter.status()["admitted"][INTERACTIVE] == 1
    with pytest.raises(ValueError):
        llm.chat("sys", "hello", client=_client(seen), lane="urgent")
    assert len(seen) == 1  # nothing sent for the refused lane


def test_model_override_bypasses_the_production_budget(fresh_arbiter, local_llm):
    """A per-job ``model`` override (scorecard, bench) runs on another LM Studio
    instance with its own slots: it must not take or wait for a production slot."""
    seen: list = []
    held = [fresh_arbiter.acquire(BACKGROUND) for _ in range(4)]  # production budget full
    try:
        assert llm.chat("sys", "hello", client=_client(seen), model="other-test-model") == "ok"
    finally:
        for lease in held:
            lease.release()
    assert seen[0]["model"] == "other-test-model"
    assert fresh_arbiter.status()["admitted"] == {INTERACTIVE: 0, BACKGROUND: 4}


def test_chat_releases_the_slot_on_transport_error(fresh_arbiter, local_llm):
    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(llm.LocalLLMTransportError):
        llm.chat("sys", "hello", client=httpx.Client(transport=httpx.MockTransport(boom)))
    assert fresh_arbiter.status()["active"] == {INTERACTIVE: 0, BACKGROUND: 0}


def test_holding_reader_waits_while_a_chat_is_active(fresh_arbiter):
    from oncofiles import holding_extract

    lease = fresh_arbiter.acquire(INTERACTIVE)
    try:
        assert holding_extract.lm_busy(now_mono=0.0) == "interactive_chat"
    finally:
        lease.release()
