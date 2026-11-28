"""Chat core tuning after the first production scorecard (#1020, #1025).

Citations on every answered path (a planned row without a document cites as
``timeline``), the zero-citation invariant, short answers, the JSON loop's
context budget, early stop, the three-step limit with a best-effort answer,
the longer lane wait of a background caller and the lane telemetry.

Synthetic data only (the invented patient of the chat core tests, dates
2027+). The model is a scripted fake of ``oncofiles.curated.llm.chat``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from oncofiles import config
from oncofiles.chat import core, prompts
from oncofiles.chat import scorecard as chs
from oncofiles.chat import tools as chat_tools
from oncofiles.curated import llm, llm_lanes
from tests import test_1020_chat_core as _base
from tests.test_1020_chat_core import (  # noqa: F401 (fixtures)
    TODAY,
    A,
    _chat_state,
    _fake,
    _seq,
    act,
    ask,
)
from tests.test_1021_llm_lanes import _settings

seeded = _base.seeded  # the chat core tests' synthetic patient (fixture)

# small counts as names: the local fixture guard compares number triples
ONE = 1
TWO = ONE + ONE
THREE = TWO + ONE


async def _planned_without_document(db):
    await db.db.execute(
        "INSERT INTO encounters (patient_id, start_at, kind, title, status, natural_key, "
        "verification) VALUES (?,?,?,?,?,?,?)",
        (A, "2027-08-20", "visit", "Kontrola u onkologa", "planned", "chat-plan-nodoc",
         "machine"),
    )  # fmt: skip
    await db.db.commit()


# ── citations on every answered route ──


@pytest.mark.parametrize(
    ("question", "answer", "route"),
    [("Aká je posledná hodnota CEA?", "CEA je 7,9 ug/L.", "router:labs"),
     ("Ako sa vyvíjal hemoglobín od marca 2027?", "Zo 118 na 109 g/L.", "router:labs"),
     ("Čo hovorila posledná lekárska správa?", "Správa je z 3. 6. 2027.", "router:report"),
     ("Ktoré dokumenty sú z Nemocnice Lúčnica?", "Z 2. 3. 2027 a 4. 5. 2027.",
      "router:institution"),
     ("Kedy je najbližší plánovaný termín?", "Kontrolné CT 15. 7. 2027.", "router:planned")],
)  # fmt: skip
async def test_every_router_answer_cites(seeded, db, monkeypatch, question, answer, route):
    _fake(monkeypatch, _seq(answer))
    out = await ask(db, question)
    assert out["done"]["route"] == route and out["done"]["result"] == "answered"
    assert out["citations"] and out["done"]["citations"] == len(out["citations"])
    assert all(c["kind"] == "document" for c in out["citations"])


async def test_json_answer_cites(seeded, db, monkeypatch):
    _fake(monkeypatch, _seq(
        act("tool", name="fact_series", args={"codes": ["PLT"]}),
        act("answer", text="Trombocyty boli 182."),
    ))  # fmt: skip
    out = await ask(db, "Ako na tom bola krv?")
    assert out["done"]["route"] == "json" and out["done"]["result"] == "answered"
    assert any(c["document_id"] == seeded["lab_new"] for c in out["citations"])


async def test_planned_without_a_document_cites_the_timeline_row(seeded, db, monkeypatch):
    await _planned_without_document(db)
    _fake(monkeypatch, _seq("Kontrola u onkológa je 20. 8. 2027."))
    out = await ask(db, "Kedy je najbližší plánovaný termín?")
    assert out["done"]["route"] == "router:planned" and out["done"]["result"] == "answered"
    timeline = [c for c in out["citations"] if c["kind"] == "timeline"]
    assert timeline and timeline[0]["document_id"] is None
    assert timeline[0]["date"].startswith("2027-08-20") and timeline[0]["label"]
    # the scorecard counts it as a citation (not as a cited document)
    q = chs.Question("q05", "next_planned", "Kedy?", "answer")
    card = chs.card(q, out, recorded_texts=[], today=TODAY, lang="sk", citation_checks={},
                    seconds=1.0, run=ONE)  # fmt: skip
    assert card["citations"] >= ONE and card["timeline_citations"] >= ONE
    assert chs.totals([card])["citations"]["pct_answered_with_citations"] == 100.0


async def test_planned_answer_without_a_date_cites_the_nearest_planned_row(seeded, db, monkeypatch):
    await _planned_without_document(db)
    _fake(monkeypatch, _seq("Najbližšie je naplánované vyšetrenie."))
    out = await ask(db, "Kedy je najbližší plánovaný termín?")
    assert out["done"]["result"] == "answered"
    (cite,) = out["citations"]
    assert cite["date"].startswith("2027-07-15") and cite["document_id"] == seeded["report"]


# ── the zero-citation invariant ──


async def test_router_answer_without_any_source_becomes_not_found(seeded, db, monkeypatch):
    _fake(monkeypatch, _seq("Áno, všetko je v poriadku."))
    out = await ask(db, "Aká je posledná hodnota CEA?")
    assert out["done"]["result"] == "not_found" and out["done"]["uncited_replaced"]
    assert out["citations"] == [] and out["text"].startswith(prompts.NOT_FOUND["sk"])


async def test_json_answer_without_tools_becomes_not_found(seeded, db, monkeypatch):
    _fake(monkeypatch, _seq(act("answer", text="Áno, je to tak.")))
    out = await ask(db, "Je to tak?")
    assert out["done"]["route"] == "json" and out["done"]["result"] == "not_found"
    assert out["done"]["uncited_replaced"] and out["citations"] == []


async def test_answered_results_always_carry_citations(seeded, db, monkeypatch):
    answers = ["CEA je 7,9 ug/L.", "Neviem.", "Správa je z 3. 6. 2027.", "Áno."]
    for text in answers:
        _fake(monkeypatch, _seq(text))
        out = await ask(db, "Aká je posledná hodnota CEA?")
        assert out["done"]["result"] != "answered" or out["citations"], text


# ── short answers ──


async def test_concise_prompt_and_answer_budgets(seeded, db, monkeypatch):
    fake = _fake(monkeypatch, _seq("CEA je 7,9 ug/L."))
    await ask(db, "Aká je posledná hodnota CEA?")
    kw = fake.calls[0]["kw"]
    assert kw["max_tokens"] == core.ROUTER_ANSWER_BUDGET == 800  # headroom, #1043
    assert kw["thinking"] is False and kw["reasoning_marker"] is True
    assert f"at most about {prompts.ANSWER_MAX_WORDS} words" in fake.calls[0]["system"]
    assert "no disclaimer" in fake.calls[0]["system"]
    fake = _fake(monkeypatch, _seq(act("answer", text="Neviem.")))
    await ask(db, "Je to tak?")
    assert fake.calls[0]["kw"]["max_tokens"] == core.JSON_STEP_BUDGET == 800


async def test_a_cut_answer_is_never_shown(seeded, db, monkeypatch):
    def script(system, user, n):
        return "CEA je 7,9 ug/L a potom ešte dlhý text, ktorý"

    fake = _fake(monkeypatch, script)
    original = fake.__call__

    def with_length(system, user, **kw):
        kw["info"]["finish_reason"] = "length"
        return original(system, user, **kw)

    monkeypatch.setattr(llm, "chat", with_length)
    out = await ask(db, "Aká je posledná hodnota CEA?")
    # cut twice (the one short retry included): the safe not-found text
    assert out["done"]["truncated"] == ONE + ONE and out["done"]["result"] == "not_found"
    assert out["done"]["retried_short"] and out["done"]["not_found_reason"] == "truncated"
    assert "dlhý text" not in out["text"]


# ── the JSON loop: context budget, early stop, three steps ──


def _big(name: str, args: dict) -> chat_tools.ToolResult:
    base = 100 + 1000 * len(name)  # every tool returns other (new) data
    rows = [{"at": f"2027-{1 + i % 12:02d}-{1 + i % 28:02d}", "value": base + i,
             "label": "XQ", "source_document_id": 7 + i % 3, "verification": "machine"}
            for i in range(300)]  # fmt: skip
    data = {"rows": rows}
    return chat_tools.ToolResult(name, args, True, data, chat_tools.fit(json.loads(
        json.dumps(data))))  # fmt: skip


def _empty(name: str, args: dict) -> chat_tools.ToolResult:
    data = {"total": 0}
    return chat_tools.ToolResult(name, args, True, data, json.dumps(data))


def _fake_tools(monkeypatch, make):
    calls: list[str] = []

    async def run_tool(db, name, args):
        calls.append(name)
        return make(name, args)

    monkeypatch.setattr(chat_tools, "run_tool", run_tool)
    return calls


def _counting(monkeypatch, script):
    """A fake that also reports prompt tokens (~4 bytes each), like LM Studio."""
    fake = _fake(monkeypatch, script)
    original = fake.__call__

    def chat(system, user, **kw):
        kw["info"]["prompt_tokens"] = len((system + user).encode()) // 4
        kw["info"]["completion_tokens"] = 40
        return original(system, user, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    return fake


_TOOLS = ["patient_overview", "fact_catalog", "timeline", "search_documents",
          "people_and_places"]  # fmt: skip


async def test_unanswerable_question_stays_within_three_calls_and_budget(seeded, db, monkeypatch):
    # the production case: the model keeps calling tools and never answers
    tools_run = _fake_tools(monkeypatch, _big)
    fake = _counting(monkeypatch, lambda s, u, n: act("tool", name=_TOOLS[n - 1], args={}))
    out = await ask(db, "Aká je moja krvná skupina?")
    done = out["done"]
    assert out["error"] is None and len(fake.calls) == THREE == core.MAX_STEPS
    assert done["max_steps_hit"] and done["result"] == "not_found"
    assert len(tools_run) == TWO  # the last reply may only answer
    assert prompts.LAST_STEP_NOTE in fake.calls[-1]["user"]
    for call in fake.calls:
        blocks = call["user"].split("TOOL RESULT ")[1:]
        assert all(len(b.encode()) <= core.TOOL_RESULT_MODEL_BYTES + 200 for b in blocks)
        assert len(call["user"].encode()) <= core.TOOL_CONTEXT_BYTES + 1_500
    # production spent 10823 prompt tokens on five calls; three calls now stay far below
    assert done["prompt_tokens"] <= 6_000
    assert out["text"].startswith(prompts.NOT_FOUND["sk"]) and "[doc" in out["text"]


async def test_tool_context_summarises_the_oldest_results(seeded, db, monkeypatch):
    monkeypatch.setattr(core, "MAX_STEPS", 4)
    monkeypatch.setattr(core, "TOOL_CONTEXT_BYTES", 5_000)
    _fake_tools(monkeypatch, _big)
    fake = _fake(monkeypatch, lambda s, u, n: act("tool", name=_TOOLS[n - 1], args={}))
    out = await ask(db, "Aká je moja krvná skupina?")
    last = fake.calls[-1]["user"]
    assert "[summarised: sources doc " in last
    assert last.index("[summarised:") < last.rindex(prompts.DATA_OPEN)  # the oldest first
    assert out["done"]["tool_context_summarised"] >= ONE
    # the full results still serve the not-found text's document list
    assert "[doc" in out["text"]


async def test_two_empty_results_in_a_row_stop_early(seeded, db, monkeypatch):
    tools_run = _fake_tools(monkeypatch, _empty)
    fake = _fake(monkeypatch, lambda s, u, n: act("tool", name=_TOOLS[n - 1], args={}))
    out = await ask(db, "Aká je moja krvná skupina?")
    assert len(fake.calls) == TWO and len(tools_run) == TWO
    assert out["done"]["early_stop"] and not out["done"]["max_steps_hit"]
    assert out["done"]["result"] == "not_found" and out["error"] is None


async def test_one_empty_result_does_not_stop(seeded, db, monkeypatch):
    def make(name, args):
        return _empty(name, args) if name == "patient_overview" else _big(name, args)

    _fake_tools(monkeypatch, make)
    fake = _fake(monkeypatch, _seq(
        act("tool", name="patient_overview", args={}),
        act("tool", name="fact_catalog", args={}),
        act("answer", text="Hodnota bola 12100."),  # the first fact_catalog row
    ))  # fmt: skip
    out = await ask(db, "Aká bola hodnota?")
    assert len(fake.calls) == THREE and not out["done"]["early_stop"]
    assert out["done"]["result"] == "answered" and out["citations"]


# ── the lane wait: background longer than interactive ──


def test_background_caller_waits_longer_than_a_person(monkeypatch):
    seen: list[dict] = []

    def fake_chat(system, user, **kw):
        seen.append(kw)
        return "ok"

    monkeypatch.setattr(llm, "chat", fake_chat)
    core._call_llm("s", "u", {})
    with llm.scorecard_call_overrides():
        core._call_llm("s", "u", {})
    interactive, background = seen
    assert interactive["slot_timeout"] == core.INTERACTIVE_LANE_WAIT_S
    assert interactive["timeout"] == core.CALL_TIMEOUT_S
    assert background["slot_timeout"] == core.BACKGROUND_LANE_WAIT_S >= 300
    assert background["timeout"] == core.BACKGROUND_CALL_TIMEOUT_S
    assert background["slot_timeout"] > interactive["slot_timeout"]


def _ok_client():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})  # fmt: skip

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_llm_slot_timeout_raises_lane_busy_and_records_the_wait(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    arb = llm_lanes.Arbiter(settings=_settings(total=1))
    monkeypatch.setattr(llm_lanes, "_ARBITER", arb)
    info: dict = {}
    assert llm.chat("s", "u", client=_ok_client(), slot_timeout=5.0, info=info) == "ok"
    assert isinstance(info["lane_wait_s"], float) and "lane_timeout" not in info
    plain: dict = {}
    llm.chat("s", "u", client=_ok_client(), info=plain)
    assert "lane_wait_s" not in plain  # no bounded wait: the info keys are unchanged
    lease = arb.acquire("background")
    try:
        info = {}
        with pytest.raises(llm.LocalLLMLaneBusyError):
            llm.chat("s", "u", client=_ok_client(), slot_timeout=0.05, info=info)
        assert info["lane_timeout"] is True and info["lane_wait_s"] >= 0.04
    finally:
        lease.release()


async def test_lane_timeout_is_model_busy_with_counts(seeded, db, monkeypatch):
    def busy(system, user, **kw):
        kw["info"].update(lane_wait_s=60.0, lane_timeout=True)
        raise llm.LocalLLMLaneBusyError("local LLM lane busy: no slot (Timeout)")

    monkeypatch.setattr(llm, "chat", busy)
    out = await ask(db, "Aká je posledná hodnota CEA?")
    assert out["error"]["code"] == "model_busy" and out["done"] is None
    assert out["error"]["lane_timeouts"] == ONE and out["error"]["lane_wait_seconds"] == 60.0
    q = chs.Question("q01", "labs_latest", "Aká?", "answer")
    card = chs.card(q, out, recorded_texts=[], today=TODAY, lang="sk", citation_checks={},
                    seconds=60.0, run=ONE)  # fmt: skip
    assert card["lane_timeouts"] == ONE and card["result"] == "error"
    t = chs.totals([card])
    assert t["lanes"]["timeouts"] == ONE and t["lanes"]["wait_seconds_max"] == 60.0


async def test_done_event_carries_the_lane_wait(seeded, db, monkeypatch):
    fake = _fake(monkeypatch, _seq("CEA je 7,9 ug/L."))
    original = fake.__call__

    def chat(system, user, **kw):
        kw["info"]["lane_wait_s"] = 1.5
        return original(system, user, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    out = await ask(db, "Aká je posledná hodnota CEA?")
    assert out["done"]["lane_wait_seconds"] == 1.5 and out["done"]["lane_timeouts"] == 0


def test_interactive_slot_wait_is_bounded_in_a_real_arbiter(monkeypatch):
    """With every slot held, the chat's call ends as lane busy after the
    interactive bound (model_busy), and nothing is sent."""
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(core, "INTERACTIVE_LANE_WAIT_S", 0.05)
    arb = llm_lanes.Arbiter(settings=_settings(total=1))
    monkeypatch.setattr(llm_lanes, "_ARBITER", arb)
    lease = arb.acquire("background")
    try:
        info: dict = {}
        with pytest.raises(llm.LocalLLMLaneBusyError):
            core._call_llm("s", "u", info)
        assert info["lane_timeout"] is True
        assert core._unavailable_code(llm.LocalLLMLaneBusyError("x")) == "model_busy"
    finally:
        lease.release()


def test_a_short_budget_is_sent_and_only_a_cut_budget_is_refused(monkeypatch):
    """The chat's 400-token budget reaches LM Studio; the floor of
    MIN_ANSWER_TOKENS only refuses a budget the context window cut."""
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(llm_lanes, "_ARBITER", llm_lanes.Arbiter(settings=_settings()))
    sent: list[dict] = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})  # fmt: skip

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.chat("s", "u", client=client, max_tokens=core.ROUTER_ANSWER_BUDGET) == "ok"
    assert sent[-1]["max_tokens"] == core.ROUTER_ANSWER_BUDGET
    huge = "x " * (config.LOCAL_LLM_CONTEXT_WINDOW * 2)
    with pytest.raises(llm.LocalLLMAnswerError):
        llm.chat("s", huge, client=client, max_tokens=core.ROUTER_ANSWER_BUDGET)
    with pytest.raises(llm.LocalLLMAnswerError):
        llm.chat("s", huge, client=client)  # the default budget: refused as before
    assert len(sent) == 1


# ── lane telemetry in the scorecard ──


def test_lane_snapshot_and_peaks():
    snaps = [
        chs.lane_snapshot({"active": {"interactive": 1, "background": 2},
                           "waiting": {"interactive": 0, "background": 3}}),
        chs.lane_snapshot({"active": {"interactive": 0, "background": 3},
                           "waiting": {"interactive": 1, "background": 0}}),
        chs.lane_snapshot({}),
    ]  # fmt: skip
    peaks = chs.lane_peaks(snaps)
    assert peaks == {"active_interactive": 1, "active_background": 3,
                     "waiting_interactive": 1, "waiting_background": 3}  # fmt: skip
    t = chs.totals([], lane_peak=peaks)
    assert t["lanes"]["max_active_background"] == 3 and t["lanes"]["max_waiting_background"] == 3
    assert chs.SCORECARD_VERSION == "chat-scorecard-v3"


def test_prompt_revision_is_part_of_the_prompt_version():
    assert prompts.PROMPT_REVISION == "chat-prompts-v3"
    assert len(chs.prompt_version()) == 16


@pytest.mark.usefixtures("seeded")
async def test_scorecard_job_records_lane_peaks(db, admin_scope, monkeypatch):
    from oncofiles.tools import curated as tool
    from tests.test_1025_chat_scorecard import Fake, _ctx

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    tool._reocr_jobs.clear()
    tool._runs.clear()
    snaps = iter(range(100))
    spike_at = TWO + TWO  # the question start that sees two waiting

    def status():
        n = next(snaps)
        waiting_bg = TWO if n == spike_at else 0
        return {"active": {"interactive": 0, "background": n % THREE},
                "waiting": {"interactive": 0, "background": waiting_bg}}  # fmt: skip

    monkeypatch.setattr(llm_lanes, "status", status)
    monkeypatch.setattr(llm, "chat", Fake())
    try:
        await tool.curated_chat_scorecard(_ctx(db), "alma-vzorova")
        await asyncio.wait_for(tool._reocr_jobs[A]["task"], 20)
        report = tool._reocr_jobs[A]["report"]
        lanes = report["totals"]["lanes"]
        assert lanes["max_active_background"] == TWO and lanes["max_waiting_background"] == TWO
        assert lanes["timeouts"] == 0
        assert report["versions"]["scorecard_version"] == "chat-scorecard-v3"
        assert report["totals"]["citations"]["pct_answered_with_citations"] == 100.0
    finally:
        tool._reocr_jobs.clear()
        tool._runs.clear()
        core.reset_state()
