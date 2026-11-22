"""Curated classification (#926, step 2): explicit reasoning, model-card
sampling, worked examples, verified metadata in the sidecar, and what the
scorecard (step 1, ``test_926_classify_scorecard``) now sends.

Synthetic data only: the patient is the fixture README's invented one, the
documents are the invented worked examples of ``classify.EXAMPLES`` (dates
2027+). The local model is always mocked.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import classify, extract, indexer, llm, runner
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.drive_ops import Item
from oncofiles.tools import curated as tool

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
README = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
# the fixture contract plus the invented tokens the worked examples assume
CONTRACT = parse_contract(README.replace(
    "**Known doctors:** `Kovacova`, `Hruskova`.",
    "**Known doctors:** `Kovacova`, `Hruskova`, `Lipova`.\n\n"
    "**HospitalName:** `OnkologickaAmbulancia`, `Lucnica`.",
))  # fmt: skip
TODAY = date(2028, 1, 10)  # after every invented example date
URL = "http://llm.test/v1"
# classify.PROMPT_VERSION on main after #965 (template + text cut). The v1
# request must keep it: change it only together with a deliberate v1 change.
MAIN_PROMPT_VERSION = "0b63a86cd73d24c3"


@pytest.fixture(autouse=True)
def _v2(monkeypatch):
    """This module tests the #926 request: CURATED_CLASSIFY_PROMPT=v2 unless a
    test sets v1 (the production default) itself."""
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v2")


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    monkeypatch.setattr(config, "CURATED_CLASSIFY_SAMPLING", "thinking")


@pytest.fixture
def bodies(monkeypatch):
    seen: list[bytes] = []
    real = httpx.Client
    answer = json.dumps(classify.EXAMPLES[0][1], ensure_ascii=False)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content)
        msg = {"content": answer}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    monkeypatch.setattr(httpx, "Client", lambda **_kw: real(transport=httpx.MockTransport(handler)))
    return seen


# ── 1. explicit reasoning + model-card sampling ─────────────────────────


@pytest.mark.parametrize("marker", [False, True])
def test_propose_sends_thinking_on_with_the_thinking_profile(local_llm, bodies, monkeypatch,
                                                             marker):  # fmt: skip
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", marker)
    stats: dict = {}
    raw = classify.propose(CONTRACT, classify.EXAMPLES[0][0], stats=stats)
    assert raw["doc_type"] == "Report"
    body = json.loads(bodies[0])
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert (body["temperature"], body["top_p"], body["top_k"]) == (1.0, 0.95, 20)
    assert "presence_penalty" not in body
    dumped = bodies[0].decode()
    assert "/no_think" not in dumped and llm.REASONING_OFF_MARKER not in dumped
    assert body["messages"][0]["content"] == classify.system_prompt(CONTRACT)
    assert stats["sampling"] == "thinking" and stats["retried"] is False
    assert stats["finish_reason"] == "stop"
    assert "raw_content" not in stats and "reasoning_content" not in stats


def test_greedy_rollback_keeps_thinking_on(local_llm, bodies, monkeypatch):
    monkeypatch.setattr(config, "CURATED_CLASSIFY_SAMPLING", "greedy")
    classify.propose(CONTRACT, classify.EXAMPLES[0][0])
    body = json.loads(bodies[0])
    assert body["temperature"] == 0.0 and "top_p" not in body and "top_k" not in body
    assert body["chat_template_kwargs"] == {"enable_thinking": True}
    assert "/no_think" not in bodies[0].decode()


def test_unknown_sampling_env_falls_back_to_thinking(monkeypatch, caplog):
    monkeypatch.setattr(config, "CURATED_CLASSIFY_SAMPLING", "hot")
    assert classify.sampling_mode() == "thinking"
    assert classify.sampling_mode("greedy") == "greedy"


def test_thinking_true_never_sends_no_think_or_marker(local_llm, bodies, monkeypatch):
    for marker in (False, True):
        llm.chat("SYS", "hi", thinking=True, reasoning_marker=marker)
    for b in bodies:
        assert json.loads(b)["messages"][0]["content"] == "SYS"
        assert "/no_think" not in b.decode() and llm.REASONING_OFF_MARKER not in b.decode()


def test_sampling_profiles_are_shared_with_the_extractors():
    from oncofiles.curated import llm_extract_call

    assert llm.sampling_profile("qwen_thinking") == llm_extract_call.QWEN_THINKING
    assert llm.sampling_profile("qwen_non_thinking") == llm_extract_call.QWEN_NON_THINKING
    assert llm_extract_call.GREEDY == {"temperature": 0.0}
    copy = llm.sampling_profile("qwen_thinking")
    copy["temperature"] = 9
    assert llm.SAMPLING_PROFILES["qwen_thinking"]["temperature"] == 1.0


# ── 2. worked examples ───────────────────────────────────────────────────


def test_prompt_carries_the_examples_and_a_version():
    system = classify.system_prompt(CONTRACT)
    assert 3 <= len(classify.EXAMPLES) <= 5
    for text, answer, _ in classify.EXAMPLES:
        assert text in system and json.dumps(answer, ensure_ascii=False) in system
    assert re.fullmatch(r"[0-9a-f]{16}", classify.PROMPT_VERSION)
    for _text, answer, _ in classify.EXAMPLES:
        if answer["doc_date"]:
            assert answer["doc_date"] >= "2027"  # invented dates only


def _validate(n: int, **kw):
    text, answer, _ = classify.EXAMPLES[n]
    return classify.validate(CONTRACT, answer, text, today=TODAY, **kw)


def test_example_filed_with_contract_tokens():
    cls = _validate(0)
    assert not cls.review_needed, cls.reasons
    assert (cls.fields.date, cls.fields.institution, cls.fields.doctor) == (
        "20270314", "Lucnica", "Lipova")  # fmt: skip


def test_example_new_printed_institution_joins_the_vocabulary():
    cls = _validate(1, auto_vocab=True)
    assert not cls.review_needed, cls.reasons
    assert {(a.label, a.token) for a in cls.additions} == {
        ("Institution", "LaboratoriumBrezina"), ("Doctor", "Javor")}  # fmt: skip
    without = _validate(1)
    assert without.review_needed and any("not in README vocabulary" in r for r in without.reasons)


def test_example_without_printed_date_goes_to_review():
    cls = _validate(2, auto_vocab=True)
    assert cls.review_needed and cls.fields is None
    assert any("no valid clinical date" in r for r in cls.reasons)


def test_example_bundle_goes_to_review():
    cls = _validate(3, auto_vocab=True)
    assert cls.review_needed and any("several documents" in r for r in cls.reasons)


def test_example_month_precision_is_filed_as_month(tmp_path):
    """#919 + #926: a printed month and year without a day → YYYYMM00, the
    sidecar says doc_date_precision: month and keeps the verified evidence."""
    cls = _validate(4, auto_vocab=True)
    assert not cls.review_needed, cls.reasons
    assert cls.fields.date == "20270400"
    text, answer, _ = classify.EXAMPLES[4]
    fields, codes = classify.verify_metadata(answer, text, "2027-04")
    assert codes == [] and fields["date_evidence"] == "Obdobie: 04/2027"


@pytest.mark.parametrize("n", range(len(classify.EXAMPLES)))
def test_every_example_answer_verifies_its_metadata(n):
    text, answer, _ = classify.EXAMPLES[n]
    cls = classify.validate(CONTRACT, answer, text, today=TODAY, auto_vocab=True)
    iso = answer["doc_date"] if cls.fields else None
    fields, codes = classify.verify_metadata(answer, text, iso)
    assert codes == [], codes
    assert fields["diagnoses"] == answer["diagnoses"]


# ── 3. verified metadata in the sidecar (pure verifier: test_926_classify_scorecard) ──


def test_sidecar_stores_only_verified_metadata_and_never_a_review_reason():
    raw = {**classify.EXAMPLES[0][1], "diagnoses": ["C50.4", "C18.7"],
           "doctor": "MUDr. Peter Javor"}  # fmt: skip
    text = classify.EXAMPLES[0][0]
    cls = classify.validate(CONTRACT, raw, text, today=TODAY)
    ex = extract.Extraction(text, "text_layer", 1, "pymupdf")
    item = Item(id="f1", name="scan.pdf", mime="application/pdf", parent="p")
    side = runner.render_sidecar(source_name="scan.pdf", contract=CONTRACT, item=item,
                                 extraction=ex, cls=cls,
                                 now=datetime(2028, 1, 10, tzinfo=UTC))  # fmt: skip
    front, _pages = indexer.parse_sidecar(side)
    assert front["diagnoses"] == ["C50.4"] and front["doctor"] is None
    assert front["metadata_unverified"] == ["unverified_diagnoses", "unverified_doctor"]
    assert front["date_evidence"] == "Vyšetrená dňa: 14.03.2027"
    assert front["summary_source"] == "local_model"
    assert not any("unverified" in r for r in front["review_reasons"])
    assert front["review_needed"] is cls.review_needed
    rec = indexer.IndexRecord(gdrive_id="g1", filename="x.pdf", folder="report",
                              category=indexer.category_for("Report"),
                              mime_type="application/pdf", size_bytes=1, modified="")  # fmt: skip
    indexer._apply_sidecar(rec, front, _pages)
    assert rec.metadata["metadata_unverified"] == ["unverified_diagnoses", "unverified_doctor"]
    assert "C18.7" not in rec.tags and "C50.4" in rec.tags


# ── 0. the switch: v1 (default) is main's request byte for byte ──────────


@pytest.mark.parametrize("env", ["v1", "", "v3"])
def test_v1_request_and_prompt_version_are_mains(local_llm, bodies, monkeypatch, env):
    """With CURATED_CLASSIFY_PROMPT unset (config default v1), empty or unknown,
    ``propose`` sends exactly what main's ``propose`` sent after #965:
    ``llm.chat(SYSTEM_TEMPLATE.format(...), "TEXT:\\n" + text[:cut])`` with the
    chat defaults (no reasoning flag, temperature 0, ``/no_think``), and the
    cache key and PROMPT_VERSION are main's."""
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", env)
    text = classify.EXAMPLES[0][0]
    classify.propose(CONTRACT, text, sampling="thinking")  # sampling ignored under v1
    main_system = classify.SYSTEM_TEMPLATE.format(
        readme=CONTRACT.readme_text, doc_types=", ".join(sorted(CONTRACT.doc_types)))  # fmt: skip
    llm.chat(main_system, f"TEXT:\n{text[: classify.MAX_TEXT_CHARS]}")
    via_propose, via_main = bodies  # exactly two requests
    assert via_propose == via_main
    body = json.loads(bodies[0])
    assert "chat_template_kwargs" not in body and body["temperature"] == 0.0
    assert body["messages"][0]["content"].endswith("\n/no_think")
    assert "WORKED EXAMPLES" not in bodies[0].decode()
    assert classify.prompt_variant() == "v1"
    assert classify.PROMPT_VERSION == MAIN_PROMPT_VERSION
    assert classify.prompt_version() == classify.cache_version() == MAIN_PROMPT_VERSION
    readme_sha = hashlib.sha256(CONTRACT.readme_text.encode("utf-8")).hexdigest()
    main_key = hashlib.sha256(
        f"{MAIN_PROMPT_VERSION}\n{CONTRACT.version}\n{CONTRACT.patient_token}\n"
        f"{readme_sha}\n{text}".encode()
    ).hexdigest()
    assert tool.propose_cache_key(CONTRACT, text) == main_key


def test_unknown_variant_warns_once(monkeypatch, caplog):
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v9-unknown")
    classify._warned_variants.discard("v9-unknown")
    with caplog.at_level("WARNING"):
        assert classify.prompt_variant() == "v1"
        assert classify.prompt_variant() == "v1"
    assert sum("CURATED_CLASSIFY_PROMPT" in r.message for r in caplog.records) == 1
    assert classify.prompt_variant("v2") == "v2"


def test_v2_prompt_version_and_cache_key_differ(monkeypatch):
    assert classify.prompt_version() == classify.PROMPT_VERSION_V2 != MAIN_PROMPT_VERSION
    key_v2 = tool.propose_cache_key(CONTRACT, "x")
    monkeypatch.setattr(config, "CURATED_CLASSIFY_SAMPLING", "greedy")
    assert tool.propose_cache_key(CONTRACT, "x") != key_v2  # a sampling rollback misses
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v1")
    assert tool.propose_cache_key(CONTRACT, "x") != key_v2


def test_v1_sidecar_is_the_pre_926_front_matter(monkeypatch):
    """Under v1 the verified-metadata block is off: raw classifier fields, no
    date_evidence / metadata_unverified / summary_source keys."""
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v1")
    raw = {**classify.EXAMPLES[0][1], "diagnoses": ["C50.4", "C18.7"],
           "doctor": "MUDr. Peter Javor"}  # fmt: skip
    text = classify.EXAMPLES[0][0]
    cls = classify.validate(CONTRACT, raw, text, today=TODAY)
    ex = extract.Extraction(text, "text_layer", 1, "pymupdf")
    item = Item(id="f1", name="scan.pdf", mime="application/pdf", parent="p")
    side = runner.render_sidecar(source_name="scan.pdf", contract=CONTRACT, item=item,
                                 extraction=ex, cls=cls,
                                 now=datetime(2028, 1, 10, tzinfo=UTC))  # fmt: skip
    front, _ = indexer.parse_sidecar(side)
    assert front["diagnoses"] == ["C50.4", "C18.7"] and front["doctor"] == "MUDr. Peter Javor"
    assert front["hospital_unit"] == "Onkologická ambulancia"
    for key in ("date_evidence", "metadata_unverified", "summary_source"):
        assert key not in front
    keys = list(front)
    assert keys.index("doc_date") < keys.index("institution") < keys.index("doctor")


def test_v2_propose_retries_with_the_same_reasoning_request(monkeypatch):
    calls = []

    def fake_chat(system, user, **k):
        calls.append({key: v for key, v in k.items() if key != "info"})
        if len(calls) == 1:
            raise llm.LocalLLMAnswerError("local LLM answer truncated: reasoning budget")
        return json.dumps(classify.EXAMPLES[0][1])

    monkeypatch.setattr(llm, "chat", fake_chat)
    classify.propose(CONTRACT, classify.EXAMPLES[0][0])
    first, retry = calls
    assert "max_tokens" not in first and retry["max_tokens"] == 2 * llm.REASONING_BUDGET
    assert first["thinking"] is True and retry["thinking"] is True
    assert first["temperature"] == retry["temperature"] == 1.0


# ── 4. the scorecard measures the step-2 request ─────────────────────────
# (fixtures and data of the step-1 scorecard tests)
from tests.test_926_classify_scorecard import (  # noqa: E402
    DOC_A,
    SEEN_KWARGS,
    _ctx,
    _run,
    scorecard_env,  # noqa: F401  (fixture)
    seeded,  # noqa: F401  (fixture)
)


async def test_scorecard_sends_the_step2_request_and_echoes_the_override(
    seeded,  # noqa: F811
    scorecard_env,  # noqa: F811
):
    monkeypatch_env = config.CURATED_CLASSIFY_PROMPT
    config.CURATED_CLASSIFY_PROMPT = "v1"  # the env is the default; the job overrides it
    try:
        status = await _run(seeded, label="after-926", prompt_variant="v2", sampling="greedy")
    finally:
        config.CURATED_CLASSIFY_PROMPT = monkeypatch_env
    report = status["report"]
    assert report["settings"]["prompt_variant"] == "v2"
    assert report["settings"]["sampling"] == "greedy"
    assert report["settings"]["thinking"] == "on"
    assert report["settings"]["overrides"] == {"prompt_variant": "v2", "sampling": "greedy"}
    assert report["versions"]["sampling"] == "greedy"
    assert report["versions"]["prompt_variant"] == "v2"
    assert report["versions"]["prompt_version"] == classify.PROMPT_VERSION_V2
    assert SEEN_KWARGS
    assert all(kw == {"thinking": True, "temperature": 0.0} for kw in SEEN_KWARGS)
    # token counts come from propose's stats (counts only, never the answer)
    assert report["totals"]["speed"]["completion_tokens_p50"] is not None
    assert "SECRET" not in json.dumps(report)


async def test_scorecard_refuses_an_unknown_sampling(seeded, scorecard_env):  # noqa: F811
    out = json.loads(await tool.curated_classify_scorecard(_ctx(seeded), [DOC_A], sampling="hot"))
    assert "sampling" in out["error"]


async def test_scorecard_refuses_sampling_under_v1_and_an_unknown_variant(
    seeded,  # noqa: F811
    scorecard_env,  # noqa: F811
    monkeypatch,
):
    ctx = _ctx(seeded)
    out = json.loads(await tool.curated_classify_scorecard(
        ctx, [DOC_A], prompt_variant="v1", sampling="greedy"))  # fmt: skip
    assert "v2" in out["error"]
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v1")
    out = json.loads(await tool.curated_classify_scorecard(ctx, [DOC_A], sampling="greedy"))
    assert "v2" in out["error"]
    out = json.loads(await tool.curated_classify_scorecard(ctx, [DOC_A], prompt_variant="v3"))
    assert "prompt_variant" in out["error"]


async def test_before_v1_after_v2_on_the_same_code_shows_the_definition_change(
    seeded,  # noqa: F811
    scorecard_env,  # noqa: F811
    monkeypatch,
):
    """Infra's measurement: before = v1, after = v2, same deployed code."""
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v1")
    before = (await _run(seeded, label="before-926", prompt_variant="v1"))["report"]
    assert before["versions"]["prompt_variant"] == "v1"
    assert before["versions"]["prompt_version"] == MAIN_PROMPT_VERSION
    assert before["settings"]["sampling"] == "legacy"
    assert all(kw == {} for kw in SEEN_KWARGS)
    SEEN_KWARGS.clear()
    after = (await _run(seeded, label="after-926", compare_to_label="before-926",
                        prompt_variant="v2"))["report"]  # fmt: skip
    assert all(kw.get("thinking") is True for kw in SEEN_KWARGS)
    assert after["baseline_comparable"] is True and after["delta"] is not None
    changed = after["definition_changed"]
    assert changed["prompt_variant"] == ["v1", "v2"]
    assert changed["prompt_version"] == [MAIN_PROMPT_VERSION, classify.PROMPT_VERSION_V2]
    assert changed["sampling"] == ["legacy", "thinking"]
