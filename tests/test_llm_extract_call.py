"""Shared call + parse path of the local-LLM extractors (#767).

Synthetic data only: invented labels, values and dates (2027+); the local
model is always mocked (httpx MockTransport or a fake ``chat``).
"""

from __future__ import annotations

import functools
import json
import re

import httpx
import pytest

from oncofiles import config
from oncofiles.cloud_guard import local_only
from oncofiles.curated import llm, llm_extract, llm_extract_clinical
from oncofiles.curated import llm_extract_call as xcall

LAB_PAGE = "Odber: 3.5.2027\nS-Faktor Q 4,20 1,0-5,0"
LAB_ANSWER = {
    "facts": [
        {"label_as_printed": "S-Faktor Q", "value": "4,20", "comparator": None,
         "unit_as_printed": None, "ref_range_as_printed": "1,0-5,0", "flag_as_printed": None,
         "date_as_printed": "3.5.2027", "date_role": "sample",
         "evidence": "S-Faktor Q 4,20 1,0-5,0"}
    ]
}  # fmt: skip
SECRETS = ("Faktor", "4,20", "3.5.2027")
CLIN_PAGE, CLIN_ANSWER = llm_extract_clinical.EXAMPLES[1]


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "off")
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "greedy")
    monkeypatch.setattr(config, "LLM_EXTRACT_ANSWER_BUDGET", 4000)
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)


def _client(seen: list, *, content="", reasoning="", finish="stop", usage=None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {"body": json.loads(request.content), "timeout": request.extensions.get("timeout")}
        )
        message = {"content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": usage
                or {"prompt_tokens": 900, "completion_tokens": 120,
                    "completion_tokens_details": {"reasoning_tokens": 0}},
            },
        )  # fmt: skip

    return httpx.Client(transport=httpx.MockTransport(handler))


def _chat(seen, **kw):
    return functools.partial(llm.chat, client=_client(seen, **kw))


# ── the shared helper ───────────────────────────────────────────────────


def _lab(chat, cfg=None):
    with local_only("test"):
        return llm_extract.extract_page(LAB_PAGE, page_number=1, chat=chat, cfg=cfg)


def test_content_answer_is_parsed_with_token_counts(local_llm):
    seen: list = []
    pr = _lab(_chat(seen, content=json.dumps(LAB_ANSWER)))
    assert pr.assessed and len(pr.candidates) == 1 and pr.answer_source == "content"
    diag = pr.diagnostics()
    assert (diag["prompt_tokens"], diag["completion_tokens"], diag["reasoning_tokens"]) == (
        900, 120, 0,
    )  # fmt: skip
    assert diag["finish_reason"] == "stop" and diag["thinking"] == "off"
    assert diag["sampling"] == "greedy"


def test_reasoning_channel_fallback_is_tagged(local_llm):
    seen: list = []
    pr = _lab(_chat(seen, content="", reasoning=json.dumps(LAB_ANSWER)))
    assert pr.assessed and pr.answer_source == "reasoning"
    assert [c["answer_source"] for c in pr.candidates] == ["reasoning"]


def test_finish_length_is_truncated_and_not_assessed(local_llm):
    seen: list = []
    pr = _lab(_chat(seen, content='{"facts": [{"label', finish="length"))
    assert not pr.assessed and pr.truncated and pr.error_kind == "truncated"
    assert pr.candidates == []


def test_no_json_is_a_coded_error(local_llm):
    seen: list = []
    with local_only("test"):
        pr = llm_extract_clinical.extract_page(
            CLIN_PAGE, page_number=1, chat=_chat(seen, content="nothing to report")
        )
    assert not pr.assessed and pr.error_kind == "no_json" and "no_json" in xcall.ERROR_KINDS


def test_both_extractors_use_the_one_shared_path(local_llm, monkeypatch):
    calls = []
    real = xcall.call_and_parse

    def spy(res, **kw):
        calls.append(kw["label"])
        return real(res, **kw)

    monkeypatch.setattr(xcall, "call_and_parse", spy)
    seen: list = []
    _lab(_chat(seen, content=json.dumps(LAB_ANSWER)))
    with local_only("test"):
        llm_extract_clinical.extract_page(
            CLIN_PAGE, page_number=1, chat=_chat(seen, content=json.dumps(CLIN_ANSWER))
        )
    assert calls == ["llm extract", "clinical extract"]
    assert not hasattr(llm_extract_clinical, "_thinking")  # no private copy left
    assert not hasattr(llm_extract, "_thinking")


def test_http_400_retries_without_response_format(local_llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_CLINICAL_STRUCTURED_OUTPUT", True)
    formats = []

    def chat(system, user, info=None, **kw):
        formats.append("response_format" in kw)
        if "response_format" in kw:
            raise llm.LocalLLMError("local LLM HTTP 400")
        info["finish_reason"] = "stop"
        return json.dumps(CLIN_ANSWER, ensure_ascii=False)

    with local_only("test"):
        pr = llm_extract_clinical.extract_page(CLIN_PAGE, page_number=1, chat=chat)
    assert formats == [True, False] and pr.structured_output == "rejected" and pr.assessed


# ── sampling profiles in the request payload ────────────────────────────


@pytest.mark.parametrize(
    ("thinking", "sampling", "expected", "absent"),
    [
        ("off", "greedy", {"temperature": 0.0, "max_tokens": 4000},
         ("top_p", "top_k", "presence_penalty")),
        ("off", "qwen", {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 1.5,
                         "max_tokens": 4000}, ()),
        ("on", "qwen", {"temperature": 1.0, "top_p": 0.95, "top_k": 20,
                        "max_tokens": llm.REASONING_BUDGET}, ("presence_penalty",)),
        ("on", "greedy", {"temperature": 0.0, "max_tokens": llm.REASONING_BUDGET},
         ("top_p", "top_k", "presence_penalty")),
    ],
)  # fmt: skip
def test_sampling_profiles_in_the_payload(local_llm, thinking, sampling, expected, absent):
    seen: list = []
    cfg = xcall.settings(thinking, sampling)
    pr = _lab(_chat(seen, content=json.dumps(LAB_ANSWER)), cfg=cfg)
    body = seen[0]["body"]
    assert {k: body[k] for k in expected} == expected
    assert not [k for k in absent if k in body]
    assert body["chat_template_kwargs"] == {"enable_thinking": thinking == "on"}
    assert seen[0]["timeout"]["read"] == 300.0  # the extractor timeout, not 900 s
    assert (pr.thinking, pr.sampling) == (thinking, sampling)


def test_env_selects_the_profile_and_an_unknown_value_stays_greedy(local_llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "qwen")
    assert xcall.settings().params == xcall.QWEN_NON_THINKING
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "creative")
    assert xcall.settings().params == {"temperature": 0.0}
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "on")
    assert xcall.settings("off", "qwen").max_tokens == 4000  # override wins over env


def test_other_callers_keep_the_greedy_payload(local_llm):
    seen: list = []
    llm.chat("sys", "hi", client=_client(seen, content="ok"))
    body = seen[0]["body"]
    assert body["temperature"] == 0.0 and body["max_tokens"] == llm.REASONING_BUDGET
    assert not {"top_p", "top_k", "presence_penalty"} & set(body)
    assert seen[0]["timeout"]["read"] != 300.0  # the client's own timeout


def test_chat_info_and_log_carry_numbers_only(local_llm, caplog):
    caplog.set_level("INFO", logger="oncofiles.curated.llm")
    seen: list = []
    info: dict = {}
    llm.chat("sys", "SECRET PROMPT", client=_client(seen, content="SECRET ANSWER"), info=info,
             temperature=0.7, top_p=0.8, top_k=20, presence_penalty=1.0, thinking=False,
             timeout=300.0, max_tokens=4000)  # fmt: skip
    assert (info["prompt_tokens"], info["completion_tokens"], info["reasoning_tokens"]) == (
        900, 120, 0,
    )  # fmt: skip
    line = next(r.getMessage() for r in caplog.records if "local llm call" in r.getMessage())
    for part in ("temperature=0.7", "top_p=0.8", "top_k=20", "presence_penalty=1.0",
                 "thinking=off", "max_tokens=4000", "timeout=300.0"):  # fmt: skip
        assert part in line, line
    assert "SECRET" not in caplog.text


# ── overrides: validated, write paths refused ───────────────────────────


@pytest.mark.parametrize("kw", [{"thinking": "maybe"}, {"sampling": "hot"}])
def test_invalid_overrides_are_refused(kw):
    with pytest.raises(ValueError):
        xcall.settings(**kw)
    with pytest.raises(ValueError):
        xcall.refuse_overrides_on_write(write=False, **kw)


@pytest.mark.parametrize("kw", [{"thinking": "on"}, {"sampling": "qwen"}])
def test_overrides_are_refused_on_a_write(kw):
    with pytest.raises(ValueError, match="dry run"):
        xcall.refuse_overrides_on_write(write=True, **kw)
    xcall.refuse_overrides_on_write(write=False, **kw)
    xcall.refuse_overrides_on_write(write=True)  # a plain write is fine


# ── report: per-page diagnostics and warnings, never text ───────────────


def test_document_report_has_page_diagnostics_and_warnings_without_text(local_llm):
    from oncofiles.curated.facts_rules import Vocabulary

    vocab = Vocabulary.from_rows([])
    answers = {
        1: {"content": json.dumps(LAB_ANSWER)},
        2: {"content": "", "reasoning": json.dumps(LAB_ANSWER)},
        3: {"content": '{"facts": [', "finish": "length"},
    }
    seen: list = []

    def chat(system, user, **kw):
        header = user.partition(":")[0]  # "Page N"
        reply = answers[int(header.removeprefix("Page "))]
        return llm.chat(system, user, client=_client(seen, **reply), **kw)

    with local_only("test"):
        out = llm_extract.extract_document([(n, LAB_PAGE) for n in answers], vocab=vocab, chat=chat)
    diag = out["page_diagnostics"]
    assert diag["1"]["answer_source"] == "content" and diag["2"]["answer_source"] == "reasoning"
    assert diag["3"]["finish_reason"] == "length" and diag["3"]["error_kind"] == "truncated"
    for d in diag.values():
        assert {"completion_tokens", "reasoning_tokens", "prompt_tokens", "seconds",
                "thinking", "sampling"} <= set(d)  # fmt: skip
    warnings = {w["warning"]: w["pages"] for w in out["warnings"]}
    assert warnings == {"answer_from_reasoning_channel": 1, "truncated": 1}
    dumped = json.dumps({k: out[k] for k in ("page_diagnostics", "warnings")})
    assert not [s for s in SECRETS if s in dumped]


def test_clinical_report_has_page_diagnostics_and_warnings(local_llm):
    from oncofiles.curated.facts_rules import Vocabulary

    seen: list = []
    with local_only("test"):
        out = llm_extract_clinical.extract_document(
            [(1, CLIN_PAGE)], vocab=Vocabulary.from_rows([]),
            chat=_chat(seen, content="", reasoning=json.dumps(CLIN_ANSWER, ensure_ascii=False)),
        )  # fmt: skip
    assert out["warnings"] == [{"warning": "answer_from_reasoning_channel", "pages": 1}]
    page = out["page_diagnostics"]["1"]
    assert page["answer_source"] == "reasoning" and page["completion_tokens"] == 120
    assert "answer" not in page  # the clinical report never carries an answer shape
    dumped = json.dumps(out["page_diagnostics"], ensure_ascii=False)
    assert "ložisko" not in dumped and "23 x 18" not in dumped


def test_a_clean_run_has_no_warnings():
    pr = xcall.PageResult(page_number=1, answer_source="content", finish_reason="stop")
    assert xcall.warnings([pr]) == []
    assert xcall.merge_warnings([{"warnings": []}, {}]) == []
    assert xcall.merge_warnings(
        [{"warnings": [{"warning": "truncated", "pages": 1}]},
         {"warnings": [{"warning": "truncated", "pages": 2}]}]
    ) == [{"warning": "truncated", "pages": 3}]  # fmt: skip


# ── #770: Qwen3.8 model-card profiles; the inert prefill is gone ─────────


def test_qwen_profiles_follow_the_qwen38_model_card():
    assert xcall.QWEN_NON_THINKING == {"temperature": 0.7, "top_p": 0.8, "top_k": 20,
                                       "presence_penalty": 1.5}  # fmt: skip
    assert xcall.QWEN_THINKING == {"temperature": 1.0, "top_p": 0.95, "top_k": 20}
    assert xcall.GREEDY == {"temperature": 0.0}


def test_prefill_is_removed_everywhere(local_llm):
    """The Qwen3.8 chat template renders a trailing assistant message as a
    finished turn, so a prefill never acted as a continuation (#770). Nothing
    in src may send one again; the request always ends with the user turn."""
    import inspect
    from pathlib import Path

    assert "prefill_assistant" not in inspect.signature(llm.chat).parameters
    assert not hasattr(xcall, "THINK_PREFILL") and not hasattr(config, "LLM_EXTRACT_PREFILL")
    src = Path(llm.__file__).resolve().parents[1]
    users = sorted(
        p.relative_to(src).as_posix()
        for p in src.rglob("*.py")
        if re.search(r"prefill_assistant|THINK_PREFILL|LLM_EXTRACT_PREFILL|prefill=",
                     p.read_text(encoding="utf-8"))
    )  # fmt: skip
    assert users == []
    cfg = xcall.settings("off", "qwen")
    assert "prefill" not in cfg.report()
    seen: list = []
    pr = _lab(_chat(seen, content=json.dumps(LAB_ANSWER)), cfg=cfg)
    assert [m["role"] for m in seen[0]["body"]["messages"]] == ["system", "user"]
    assert "prefill" not in pr.diagnostics()


@pytest.mark.parametrize("fn", ["settings", "validate_overrides"])
def test_a_prefill_override_is_an_unknown_argument(fn):
    with pytest.raises(TypeError, match="prefill"):
        getattr(xcall, fn)(prefill="on")
    with pytest.raises(TypeError, match="prefill"):
        xcall.refuse_overrides_on_write(write=False, prefill="off")


@pytest.mark.parametrize(
    "tool", ["curated_llm_extract_preview", "curated_clinical_extract_preview",
             "curated_clinical_extract"],
)  # fmt: skip
def test_mcp_tools_reject_prefill_as_an_unknown_parameter(tool):
    """A client that still sends ``prefill`` gets a clean validation error
    from FastMCP before the tool runs: no job starts, nothing is written."""
    import asyncio

    from fastmcp.exceptions import ValidationError

    from oncofiles.server import mcp

    registered = {t.name: t for t in asyncio.run(mcp.list_tools())}[tool]
    props = registered.parameters["properties"]
    assert "prefill" not in props and {"timeout_s", "reasoning_marker"} <= set(props)
    # argument validation only (no server lifespan, no DB): rejected before the body runs
    with pytest.raises(ValidationError, match="prefill"):
        asyncio.run(registered.run({"document_ids": [1], "prefill": "on"}))


@pytest.mark.parametrize("timeout_s", [30, 120, 900])
def test_timeout_override_reaches_the_request(local_llm, timeout_s):
    seen: list = []
    cfg = xcall.settings(timeout_s=timeout_s)
    assert cfg.timeout_s == float(timeout_s) and cfg.report()["timeout_s"] == float(timeout_s)
    _lab(_chat(seen, content=json.dumps(LAB_ANSWER)), cfg=cfg)
    assert seen[0]["timeout"]["read"] == float(timeout_s)


@pytest.mark.parametrize("bad", [29, 901, 0, -5, 60.0, "300", True])
def test_timeout_override_bounds(bad):
    with pytest.raises(ValueError, match="timeout_s"):
        xcall.settings(timeout_s=bad)
    with pytest.raises(ValueError, match="timeout_s"):
        xcall.refuse_overrides_on_write(write=False, timeout_s=bad)


@pytest.mark.parametrize("kw", [{"timeout_s": 300}, {"reasoning_marker": "on"}])
def test_timeout_and_marker_are_refused_on_a_write(kw):
    with pytest.raises(ValueError, match="dry run"):
        xcall.refuse_overrides_on_write(write=True, **kw)
    xcall.refuse_overrides_on_write(write=False, **kw)
