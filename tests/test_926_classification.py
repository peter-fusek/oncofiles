"""Curated classification (#926, step 2): explicit reasoning, model-card
sampling, worked examples, verified metadata in the sidecar, and what the
scorecard (step 1, ``test_926_classify_scorecard``) now sends.

Synthetic data only: the patient is the fixture README's invented one, the
documents are the invented worked examples of ``classify.EXAMPLES`` (dates
2027+). The local model is always mocked.
"""

from __future__ import annotations

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
    status = await _run(seeded, label="after-926", sampling="greedy")
    report = status["report"]
    assert report["settings"]["sampling"] == "greedy"
    assert report["settings"]["thinking"] == "on"
    assert report["settings"]["overrides"] == {"sampling": "greedy"}
    assert report["versions"]["sampling"] == "greedy"
    assert report["versions"]["prompt_version"] == classify.PROMPT_VERSION
    assert SEEN_KWARGS
    assert all(kw == {"thinking": True, "temperature": 0.0} for kw in SEEN_KWARGS)
    # token counts come from propose's stats (counts only, never the answer)
    assert report["totals"]["speed"]["completion_tokens_p50"] is not None
    assert "SECRET" not in json.dumps(report)


async def test_scorecard_refuses_an_unknown_sampling(seeded, scorecard_env):  # noqa: F811
    out = json.loads(await tool.curated_classify_scorecard(_ctx(seeded), [DOC_A], sampling="hot"))
    assert "sampling" in out["error"]
