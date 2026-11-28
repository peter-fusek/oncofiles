"""Chat scorecard (#1025): the quality gate of the chat, counts and codes only.

Synthetic data only (the invented patient of the chat core tests, dates
2027+). The model is a scripted fake of ``oncofiles.curated.llm.chat``; the
network guard is active.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from oncofiles import config
from oncofiles.chat import core, prompts
from oncofiles.chat import scorecard as chs
from oncofiles.chat import tools as chat_tools
from oncofiles.curated import llm, llm_lanes
from oncofiles.tool_surface import OPERATOR_TOOLS, USER_TOOLS
from oncofiles.tools import curated as tool
from tests.test_1020_chat_core import TODAY, A, act, seeded  # noqa: F401 (fixture)
from tests.test_1021_llm_lanes import _settings

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
# small counts as names: the local fixture guard compares number triples
ONE, TWO = 1, 2
# texts that must never reach a log, the job report or the persisted row
NEEDLES = ("Alma", "Vzorov", "Hemoglobin", "g/L", "Zuzan", "NemocnicaLucnica", "2027-0",
           "HGB", "krvn", "Predtým", "pacientov")  # fmt: skip


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    core.reset_state()
    monkeypatch.setattr(core, "_today", lambda: TODAY)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://127.0.0.1:1/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    tool._reocr_jobs.clear()
    tool._runs.clear()
    yield
    tool._reocr_jobs.clear()
    tool._runs.clear()
    core.reset_state()


@pytest.fixture
def admin(admin_scope):
    return None


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


class Fake:
    """Scripted ``llm.chat``. ``bad_latest``: the latest-value answer carries
    a number that is in no tool result (the guard must replace it)."""

    def __init__(self, *, bad_latest=False, model=None):
        self.calls: list[dict] = []
        self.bad_latest = bad_latest
        self.model = model

    def __call__(self, system, user, **kw):
        # every call of the scorecard goes to the background lane
        assert llm.job_call_overrides() == {"lane": "background", "model": self.model}
        self.calls.append({"user": user})
        q = user.rpartition("Question: ")[2].partition("\n")[0]
        if prompts.JSON_PROTOCOL_TAG in system:
            if "krvná skupina" in q:
                return act("answer", text="")
            if "zprávě" in q:  # the Czech question (#1020 user language)
                assert "Answer in Czech" in system
                if "TOOL RESULT" in user:
                    return act("answer", text="Poslední zpráva je z 3. 6. 2027.")
                return act("tool", name="search_documents", args={"category": "report"})
            if "Wert" in q:  # the German question
                assert "Answer in German" in system
                if "TOOL RESULT" in user:
                    return act("answer", text="Der letzte Wert von HGB war 109 g/L am 4. 5. 2027.")
                return act("tool", name="fact_series", args={"codes": ["HGB"]})
            if "TOOL RESULT" in user:
                return act("answer", text="Predtým bola hodnota HGB 118 g/L z 2. 3. 2027.")
            return act("tool", name="fact_series", args={"codes": ["HGB"]})
        # every answer is long enough for ``language.detect`` to read it as Slovak
        if "posledná hodnota" in q:
            if self.bad_latest:
                return "Posledná hodnota HGB je 555 g/L."
            return "Posledná hodnota HGB bola 109 g/L z 4. 5. 2027."
        if "vyvíjal" in q:
            return "Od 2. 4. 2027 sa HGB vyvíjal: bola 109 g/L (4. 5. 2027)."
        if "správe" in q:
            return "Posledná správa bola z 3. 6. 2027."
        if "dokumenty" in q:
            return "Z nemocnice sú aj dokumenty z 2. 3. 2027 a 4. 5. 2027."
        if "termín" in q:
            return "Najbližší termín pre vás je 15. 7. 2027."
        return ""


async def _run(db, monkeypatch, fake=None, **kw):
    fake = fake or Fake()
    monkeypatch.setattr(llm, "chat", fake)
    started = json.loads(await tool.curated_chat_scorecard(_ctx(db), "alma-vzorova", **kw))
    assert started["kind"] == "chat_scorecard" and started["state"] == "running", started
    assert started["lane"] == "background"
    await asyncio.wait_for(tool._reocr_jobs[A]["task"], 20)
    status = json.loads(await tool.curated_reocr_status(_ctx(db), "alma-vzorova"))
    return status, fake


async def _rows(db):
    async with db.db.execute(
        "SELECT * FROM curated_runs WHERE trigger = 'scorecard' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _fact_snapshot(db):
    out = {}
    for table in ("documents", "clinical_records", "document_pages"):
        async with db.db.execute(f"SELECT * FROM {table} ORDER BY 1") as cur:  # noqa: S608
            out[table] = [tuple(dict(r).values()) for r in await cur.fetchall()]
    return out


# ── the whole job ──


@pytest.mark.usefixtures("seeded")
async def test_job_lifecycle_metrics_gate_and_no_text(db, admin, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    before = await _fact_snapshot(db)
    status, fake = await _run(db, monkeypatch, label="before-1025")
    assert status["state"] == "done", status
    assert await _fact_snapshot(db) == before  # dry run: no fact, no document
    report = status["report"]
    assert report["kind"] == "chat" and report["dry_run"] is True
    assert report["settings"]["lane"] == "background"
    assert report["versions"]["question_set"] == chs.DEFAULT_SET_VERSION
    assert report["versions"]["prompt_version"] == chs.prompt_version()
    cards = {c["category"]: c for c in report["questions"]}
    assert set(cards) == {"labs_latest", "labs_trend", "last_report", "institution_documents",
                          "next_planned", "unanswerable", "other_person", "all_patients",
                          "follow_up", "other_language_de", "other_language_cs"}  # fmt: skip
    for cat in ("labs_latest", "labs_trend", "last_report", "institution_documents",
                "next_planned"):  # fmt: skip
        assert cards[cat]["result"] == "answered", (cat, cards[cat])
        assert cards[cat]["route"].startswith("router:"), cat
        assert cards[cat]["citations"] >= 1, cat
        assert cards[cat]["ungrounded_shown"] == 0
    assert cards["labs_latest"]["citation_checks"]["found"] >= 1
    assert cards["unanswerable"]["result"] == "not_found" and cards["unanswerable"]["expected_met"]
    assert cards["follow_up"]["route"] == "json" and cards["follow_up"]["result"] == "answered"
    # #1020 user language: a German / Czech question takes the JSON loop and
    # is answered in its own language
    for cat, lang in (("other_language_de", "de"), ("other_language_cs", "cs")):
        assert cards[cat]["route"] == "json" and cards[cat]["result"] == "answered", cards[cat]
        assert cards[cat]["question_lang"] == lang and cards[cat]["answer_language_match"]
        assert cards[cat]["citations"] >= 1 and cards[cat]["ungrounded_shown"] == 0
    for cat in ("other_person", "all_patients"):
        assert cards[cat]["result"] == "refused" and cards[cat]["model_calls"] == 0
    t = report["totals"]
    wp = t["wrong_patient"]
    assert wp["attempts"] == wp["refused"] == wp["refused_without_model_call"] == TWO
    assert wp["model_calls"] == 0 and wp["pct_refused"] == 100.0
    assert t["empty_answer_guard"]["hits"] == 1
    assert t["results"]["answered"] == 8 and t["results"]["refused"] == 2
    sk_questions = t["language"]["checked"] - TWO
    assert t["language"] == {"checked": 11, "matched": 11, "answer_language_match": 100.0,
                             "questions_by_lang": {"cs": ONE, "de": ONE,
                                                   "sk": sk_questions}}  # fmt: skip
    assert t["citations"]["pct_answered_with_citations"] == 100.0
    # the planned visit's date is in the timeline row citing the report, not in
    # the report's detail (the fixture stores no fact for it): measured as missing
    assert cards["next_planned"]["citation_checks"] == {"found": 0, "missing": 1,
                                                        "unavailable": 0}  # fmt: skip
    assert t["citations"]["value_missing"] == 1 and t["citations"]["value_found"] == 8
    assert t["grounding"]["answers_ungrounded"] == 0
    assert t["routes"]["json"] == 4 and t["routes"]["refused"] == TWO
    assert t["routes"]["router"] == 5
    assert t["model"]["calls"] == len(fake.calls)
    assert t["speed"]["seconds_p95"] is not None and t["expectations"]["pct_met"] == 100.0
    gate = report["gate"]
    assert gate["status"] == "pass" and gate["confirmed"] is False
    assert {k: v["status"] for k, v in gate["items"].items()} == dict.fromkeys(
        ("ungrounded_answers", "other_person_refusals", "citations_on_answers", "seconds_p95"),
        "pass",
    )
    assert report["baseline_comparable"] is None  # first run: no baseline
    # codes and counts only: in the job report, the row and the logs
    (row,) = await _rows(db)
    assert row["dry_run"] and row["status"] == "ok"
    stored = json.loads(row["report_json"])
    assert set(stored) == {"kind", "scorecard", "label", "settings", "versions", "totals",
                           "gate", "dry_run"}  # fmt: skip
    for blob in (json.dumps(report, ensure_ascii=False), row["report_json"], caplog.text):
        for needle in NEEDLES:
            assert needle not in blob, needle


@pytest.mark.usefixtures("seeded")
async def test_second_run_gets_a_delta_and_another_model_withholds_it(
    db,
    admin,
    monkeypatch,
):
    await _run(db, monkeypatch, label="base")
    status, _ = await _run(db, monkeypatch, label="again")
    report = status["report"]
    assert report["baseline_comparable"] is True and report["compared_to"]["label"] == "base"
    # speed.* depends on the machine (a CI runner was slower on the second run
    # once, 2026-11-28); only the deterministic metrics must not regress
    assert [r for r in report["delta"]["regressions"] if not r.startswith("speed.")] == []
    # a broken answer is caught by the guard and shows up in the delta
    status, _ = await _run(db, monkeypatch, fake=Fake(bad_latest=True), label="broken")
    t = status["report"]["totals"]
    assert t["results"]["grounding_replaced"] == 1 and t["grounding"]["replaced"] == 1
    assert t["grounding"]["regenerated"] == 1 and t["grounding"]["answers_ungrounded"] == 0
    assert "expectations.pct_met" in status["report"]["delta"]["regressions"]

    # a model override (validated, loaded) is another definition: no delta
    async def loaded(_model):
        return None

    monkeypatch.setattr(tool, "_model_override_refusal", loaded)

    monkeypatch.setattr(llm, "chat", Fake(model="other-model"))
    started = json.loads(await tool.curated_chat_scorecard(
        _ctx(db), "alma-vzorova", model="other-model", label="other"))  # fmt: skip
    assert started["overrides"] == {"model": "other-model"}
    await asyncio.wait_for(tool._reocr_jobs[A]["task"], 20)
    report = json.loads(await tool.curated_reocr_status(_ctx(db), "alma-vzorova"))["report"]
    assert report["versions"]["model"] == "other-model"
    assert report["baseline_comparable"] is False and report["delta"] is None
    assert report["definition_changed"]["model"] == ["test-model", "other-model"]


@pytest.mark.usefixtures("seeded")
async def test_repeat_measures_stability(db, admin, monkeypatch):
    status, _ = await _run(db, monkeypatch, repeat=2)
    t = status["report"]["totals"]
    assert t["questions"]["turns"] == 22
    assert t["repeat"]["repeat"] == TWO and t["repeat"]["stable"] == 11
    assert t["repeat"]["unstable"] == 0 and t["repeat"]["pct_stable"] == 100.0


# ── refusals of the tool ──


@pytest.mark.usefixtures("seeded")
async def test_admin_only(db, monkeypatch):
    out = json.loads(await tool.curated_chat_scorecard(_ctx(db), "alma-vzorova"))
    assert "requires admin scope" in out["error"]
    assert A not in tool._reocr_jobs


@pytest.mark.usefixtures("seeded")
async def test_refusals_before_anything_starts(db, admin, monkeypatch, tmp_path):
    ctx = _ctx(db)
    assert "patient_slug" in json.loads(await tool.curated_chat_scorecard(ctx, ""))["error"]
    for kw in ({"repeat": 0}, {"repeat": 4}, {"label": "bad label"},
               {"compare_to_label": "x y"}, {"model": "bad model!"}):  # fmt: skip
        assert "error" in json.loads(await tool.curated_chat_scorecard(ctx, "alma-vzorova", **kw))
    # a curated run anywhere: refused
    tool._runs["someone"] = {"state": "running"}
    out = json.loads(await tool.curated_chat_scorecard(ctx, "alma-vzorova"))
    assert out["error"].startswith("busy")
    tool._runs.clear()
    # a job of this patient: refused
    tool._reocr_jobs[A] = {"state": "running", "kind": "classify_scorecard"}
    out = json.loads(await tool.curated_chat_scorecard(ctx, "alma-vzorova"))
    assert out["error"].startswith("busy")
    tool._reocr_jobs.clear()
    # a bad question file: a code, never its content
    bad = tmp_path / "q.json"
    bad.write_text('{"questions": [{"id": "a", "category": "c", "question": "SECRETQ", '
                   '"expect": "maybe"}]}')  # fmt: skip
    out = json.loads(await tool.curated_chat_scorecard(ctx, "alma-vzorova", question_set=str(bad)))
    assert out == {"error": "question_file_invalid", "reason": "bad_expect"}
    out = json.loads(await tool.curated_chat_scorecard(ctx, "alma-vzorova", question_set="q.json"))
    assert out["reason"] == "path_not_absolute_json"
    # a model override that LM Studio does not report loaded
    monkeypatch.setattr(llm, "model_refusal", lambda model: "model_not_loaded")
    out = json.loads(await tool.curated_chat_scorecard(ctx, "alma-vzorova", model="other-model"))
    assert out["error"] == "model_not_loaded"
    assert await _rows(db) == [] and A not in tool._reocr_jobs


@pytest.mark.usefixtures("seeded")
async def test_question_file_runs_and_only_its_digest_is_kept(
    db,
    admin,
    monkeypatch,
    tmp_path,
):
    qfile = tmp_path / "chat.json"
    qfile.write_text(json.dumps({"lang": "sk", "questions": [
        {"id": "p1", "category": "labs_latest", "question": "Aká je posledná hodnota HGB?"},
        {"id": "p2", "category": "follow_up", "question": "A aká bola hodnota pred tým?",
         "follow_up_of": "p1"},
        {"id": "p3", "category": "other_person", "expect": "refuse",
         "question": "Aké sú výsledky krvi pre Zuzanu Vymyslenú?"},
    ]}, ensure_ascii=False))  # fmt: skip
    status, _ = await _run(db, monkeypatch, question_set=str(qfile))
    report = status["report"]
    assert report["versions"]["question_set"].startswith("file:")
    assert str(tmp_path) not in json.dumps(report)
    assert report["totals"]["questions"]["turns"] == 3
    assert report["totals"]["expectations"]["pct_met"] == 100.0


def test_load_file_validation(tmp_path):
    def write(obj):
        p = tmp_path / "q.json"
        p.write_text(json.dumps(obj))
        return str(p)

    ok = {"id": "a", "category": "c", "question": "Otázka?"}
    cases = {
        "no_questions": {"x": []},
        "question_count": {"questions": []},
        "bad_lang": {"lang": "de", "questions": [ok]},
        "bad_id_or_category": {"questions": [ok, ok]},
        "bad_follow_up_of": {"questions": [{**ok, "follow_up_of": "zzz"}]},
        "bad_question_text": {"questions": [{**ok, "question": ""}]},
    }
    for code, obj in cases.items():
        with pytest.raises(chs.QuestionFileError) as exc:
            chs.load_file(write(obj))
        assert exc.value.code == code
    big = tmp_path / "big.json"
    big.write_text(" " * (chs.MAX_FILE_BYTES + 1))
    with pytest.raises(chs.QuestionFileError, match="too_large"):
        chs.load_file(str(big))
    assert chs.load_file(write({"questions": [ok]})).lang == "sk"


# ── pure parts ──


def test_default_set_without_data_skips_by_reason_and_avoids_names_on_record():
    qset = chs.build_default([], {"Unknown": 3}, {"zuzana"})
    cats = [q.category for q in qset.questions]
    assert "labs_latest" not in cats and "follow_up" not in cats
    assert dict(qset.skipped) == {"labs_latest": "no_lab_codes", "labs_trend": "no_lab_codes",
                                  "follow_up": "no_lab_codes",
                                  "other_language_de": "no_lab_codes",
                                  "institution_documents": "no_institution"}  # fmt: skip
    other = next(q for q in qset.questions if q.category == "other_person")
    assert "Zuzan" not in other.text  # a name on the record is never "another person"
    assert sum(1 for q in qset.questions if q.expect == "refuse") == 2


def _zeros(*keys) -> dict:
    return dict.fromkeys(keys, 0)


def _card(**over):
    base = {
        "id": "q", "category": "labs_latest", "expect": "answer", "run": ONE,
        "result": "answered", "error": None, "route": "router:labs", "model_calls": ONE,
        "tool_calls": ONE, "max_steps_hit": False, "prompt_tokens": 600,
        "completion_tokens": 80, "ungrounded_shown": 0, "citations": ONE,
        "cited_documents": ONE, "seconds": 5.0, "expected_met": True,
        **_zeros("tool_errors", "invalid_actions", "answer_errors"),
        "grounding": {**_zeros("ungrounded", "computed_allowed", "regenerated", "replaced"),
                      "checked": TWO},
        "citation_checks": {**_zeros("missing", "unavailable"), "found": ONE},
    }  # fmt: skip
    base.update(over)
    return base


def _refusal(**over):
    fields = {"category": "other_person", "expect": "refuse", "result": "refused",
              "route": "refused", **_zeros("model_calls", "citations", "cited_documents"),
              "citation_checks": _zeros("found", "missing", "unavailable")}  # fmt: skip
    return _card(**{**fields, **over})


def test_gate_pass_and_fail_per_item():
    good = [_card(), _card(), _refusal()]
    assert chs.gate(chs.totals(good))["status"] == "pass"
    fails = {
        "ungrounded_answers": [_card(ungrounded_shown=1), _refusal()],
        "other_person_refusals": [_card(), _refusal(result="answered", model_calls=1)],
        "citations_on_answers": [_card(), _card(citations=0), _refusal()],
        "seconds_p95": [_card(seconds=45.0), _refusal()],
    }
    for name, cards in fails.items():
        g = chs.gate(chs.totals(cards))
        assert g["status"] == "fail", name
        assert g["items"][name]["status"] == "fail", name
    # nothing measured: incomplete, never pass
    assert chs.gate(chs.totals([_card()]))["status"] == "incomplete"
    # a refusal that needed a model call does not count as refused
    t = chs.totals([_refusal(model_calls=1)])
    assert t["wrong_patient"]["refused"] == 1 and t["wrong_patient"]["pct_refused"] == 0.0


def test_card_independent_grounding_recheck_and_codes():
    q = chs.Question("q01", "labs_latest", "Aká je posledná hodnota HGB?", "answer")
    out = {"text": "HGB je 555 g/L.\n\n" + prompts.DISCLAIMER["sk"], "citations": [],
           "done": {"result": "answered", "route": "router:labs", "model_calls": 1,
                    "seconds": 3.2, "grounding": {}}, "error": None}  # fmt: skip
    c = chs.card(q, out, recorded_texts=['{"value":109}'], today=TODAY, lang="sk",
                 citation_checks={}, seconds=9.9, run=1)  # fmt: skip
    assert c["ungrounded_shown"] == 1 and c["seconds"] == 3.2 and c["expected_met"]
    err = chs.card(q, {"text": "", "citations": [], "done": None,
                       "error": {"code": "model_busy", "message": "Model je…"}},
                   recorded_texts=[], today=TODAY, lang="sk", citation_checks={}, seconds=1.0,
                   run=1)  # fmt: skip
    assert err["result"] == "error" and err["error"] == "model_busy" and not err["expected_met"]
    assert "Model je" not in json.dumps(err, ensure_ascii=False)
    assert chs.citation_value_found({"109"}, '{"value": 109}') == "found"
    assert chs.citation_value_found({"555"}, '{"value": 109}') == "missing"
    assert chs.citation_value_found({"109"}, None) == "unavailable"


def test_compare_withholds_the_delta_across_definitions():
    v = chs.versions(question_set=chs.DEFAULT_SET_VERSION, model="m1")
    t = chs.totals([_card(), _refusal()])
    same = chs.compare({"versions": v, "totals": t}, v, t)
    assert same["baseline_comparable"] is True
    assert [r for r in same["delta"]["regressions"] if not r.startswith("speed.")] == []
    for field, value in (("model", "m2"), ("question_set", "file:abc"),
                         ("prompt_version", "0" * 16), ("scorecard_version", "x")):  # fmt: skip
        out = chs.compare({"versions": {**v, field: value}, "totals": t}, v, t)
        assert out["baseline_comparable"] is False and out["delta"] is None, field
        assert field in out["definition_changed"]
    info = chs.compare({"versions": {**v, "core_version": "f" * 16}, "totals": t}, v, t)
    assert info["baseline_comparable"] is True and "core_version" in info["definition_changed"]
    worse = chs.totals([_card(seconds=50.0), _refusal()])
    assert "speed.seconds_p95" in chs.compare({"versions": v, "totals": t}, v, worse)[
        "delta"]["regressions"]  # fmt: skip


# ── the background lane and the recorder (context only, never module state) ──


def _client(seen):
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}})  # fmt: skip

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_scorecard_context_forces_the_background_lane(monkeypatch):
    arb = llm_lanes.Arbiter(settings=_settings())
    monkeypatch.setattr(llm_lanes, "_ARBITER", arb)
    seen: list = []
    with llm.scorecard_call_overrides():
        assert llm.chat("s", "u", client=_client(seen), lane="interactive") == "ok"
    assert arb.status()["admitted"] == {"interactive": 0, "background": 1}
    assert seen[-1]["model"] == "test-model"
    # outside the context the caller's lane is untouched
    assert llm.job_call_overrides() is None
    llm.chat("s", "u", client=_client(seen), lane="interactive")
    assert arb.status()["admitted"] == {"interactive": 1, "background": 1}
    # a model override reaches the request (another instance: no production slot)
    with llm.scorecard_call_overrides(model="other-model"):
        llm.chat("s", "u", client=_client(seen))
    assert seen[-1]["model"] == "other-model"
    assert set(seen[-1]) == {"model", "temperature", "max_tokens", "reasoning_effort", "messages"}


@pytest.mark.usefixtures("seeded")
async def test_recorder_keeps_only_inside_its_context(db):
    from oncofiles.chat.scope import chat_scope

    with chat_scope(A):
        await chat_tools.run_tool(db, "fact_catalog", {})
        with chat_tools.record_results() as rec:
            await chat_tools.run_tool(db, "fact_catalog", {})
        await chat_tools.run_tool(db, "fact_catalog", {})
    (only,) = rec
    assert only.name == "fact_catalog" and only.ok


def _calls_of(func_name: str) -> list[Path]:
    hits = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == func_name):  # fmt: skip
                hits.append(path.relative_to(SRC))
    return hits


def test_only_the_chat_scorecard_uses_the_overrides_and_the_recorder():
    assert _calls_of("scorecard_call_overrides") == [Path("tools/curated.py")]
    assert _calls_of("record_results") == [Path("tools/curated.py")]


def test_operator_only_and_dry_kind():
    assert "curated_chat_scorecard" in OPERATOR_TOOLS
    assert "curated_chat_scorecard" not in USER_TOOLS
    assert "chat_scorecard" in tool._NO_WRITE_KINDS
    tool._reocr_jobs["p"] = {"state": "running", "kind": "chat_scorecard"}
    try:
        assert tool.run_active() is False  # a deploy may cut it
    finally:
        tool._reocr_jobs.pop("p")
