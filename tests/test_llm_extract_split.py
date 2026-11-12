"""Page split after a truncated answer (#773), in the shared call path.

Synthetic data only: invented labels, values and dates (2027+); the local
model is always mocked (httpx MockTransport behind ``llm.chat``).
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.cloud_guard import local_only
from oncofiles.curated import llm, llm_extract, llm_extract_clinical
from oncofiles.curated import llm_extract_call as xcall
from oncofiles.curated.facts_rules import Vocabulary

LAB_LINES = [
    "Odber: 3.5.2027",
    "S-Faktor Q 4,20 1,0-5,0",
    "S-Faktor R 2,10 1,0-3,0",
    "",
    "KRVNY OBRAZ",
    "B-Faktor S 7,70 4,0-9,0",
    "B-Faktor T 1,30 1,0-2,0",
]
LAB_PAGE = "\n".join(LAB_LINES)
SECRETS = ("Faktor", "4,20", "7,70", "3.5.2027")


def _lab_item(label: str, value: str, ref: str) -> dict:
    return {"label_as_printed": label, "value": value, "comparator": None,
            "unit_as_printed": None, "ref_range_as_printed": ref, "flag_as_printed": None,
            "date_as_printed": "3.5.2027", "date_role": "sample",
            "evidence": f"{label} {value} {ref}"}  # fmt: skip


Q = _lab_item("S-Faktor Q", "4,20", "1,0-5,0")
R = _lab_item("S-Faktor R", "2,10", "1,0-3,0")
S = _lab_item("B-Faktor S", "7,70", "4,0-9,0")
T = _lab_item("B-Faktor T", "1,30", "1,0-2,0")

TRUNCATED = {"content": '{"facts": [', "finish": "length", "completion": 3999}


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "off")
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "greedy")
    monkeypatch.setattr(config, "LLM_EXTRACT_ANSWER_BUDGET", 4000)
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)
    monkeypatch.setattr(config, "LLM_EXTRACT_SPLIT_LINES", 0)


def _client(*, content="", reasoning="", finish="stop", completion=120):
    def handler(request: httpx.Request) -> httpx.Response:
        message = {"content": content}
        if reasoning:
            message["reasoning_content"] = reasoning
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 900, "completion_tokens": completion,
                          "completion_tokens_details": {"reasoning_tokens": 0}},
            },
        )  # fmt: skip

    return httpx.Client(transport=httpx.MockTransport(handler))


class Model:
    """A fake local model: the reply depends on the page text it is sent
    (the whole page, or one chunk). Records every page text it was sent."""

    def __init__(self, page: str, whole: dict, chunk_replies: tuple[dict, dict] | None = None):
        self.page = page
        self.whole = whole
        self.chunks = xcall.split_page_text(page)
        self.chunk_replies = chunk_replies
        self.sent: list[str] = []

    def __call__(self, system, user, **kw):
        header, _, text = user.partition(":\n")
        assert header.startswith("Page ")
        self.sent.append(text)
        whole = text == self.page
        reply = self.whole if whole else self.chunk_replies[self.chunks.index(text)]
        return llm.chat(system, user, client=_client(**reply), **kw)


def _facts(*items) -> dict:
    return {"content": json.dumps({"facts": list(items)})}


# ── the splitter: line-based, never inside a line ───────────────────────


def test_split_prefers_a_blank_line_or_heading_near_the_middle():
    first, second = xcall.split_page_text(LAB_PAGE)
    assert first + "\n" + second == LAB_PAGE
    assert first.rstrip("\n").endswith("S-Faktor R 2,10 1,0-3,0")  # cut after the blank line
    assert second.lstrip("\n").startswith("KRVNY OBRAZ")


def test_split_without_a_preferred_line_cuts_at_the_nearest_line_boundary():
    text = "\n".join(f"riadok {n} hodnota {n}" for n in range(10))
    first, second = xcall.split_page_text(text)
    assert first + "\n" + second == text
    assert len(first.split("\n")) == len(second.split("\n")) == 5
    lines = text.split("\n")
    assert first.split("\n")[-1] in lines and second.split("\n")[0] in lines


def test_a_page_with_one_line_of_content_is_never_split():
    assert xcall.split_page_text("S-Faktor Q 4,20 1,0-5,0") is None
    assert xcall.split_page_text("\n\nS-Faktor Q 4,20 1,0-5,0\n\n") is None
    assert xcall.split_page_text("") is None


# ── retry on truncation ─────────────────────────────────────────────────


def test_no_split_when_the_first_answer_is_fine(local_llm):
    model = Model(LAB_PAGE, _facts(Q, R, S, T))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert model.sent == [LAB_PAGE]
    assert pr.assessed and not pr.split and len(pr.candidates) == 4
    diag = pr.diagnostics()
    assert diag["split"] is False and "chunks" not in diag


def test_truncation_splits_once_and_merges_both_chunks_without_duplicates(local_llm):
    # chunk 2 repeats Q (a line it did not even get): an exact duplicate
    model = Model(LAB_PAGE, TRUNCATED, (_facts(Q, R), _facts(S, T, Q)))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert model.sent == [LAB_PAGE, *model.chunks]  # exactly 1 split, 2 chunk calls
    assert pr.assessed and not pr.truncated and pr.error is None
    assert [c["label_as_printed"] for c in pr.candidates] == [
        "S-Faktor Q", "S-Faktor R", "B-Faktor S", "B-Faktor T",
    ]  # fmt: skip
    assert pr.split_duplicates_removed == 1
    assert {c["answer_source"] for c in pr.candidates} == {"content"}


def test_two_items_quoting_one_line_are_not_duplicates(local_llm):
    q2 = dict(Q, label_as_printed="S-Faktor", value="1,0")  # same evidence, other item
    model = Model(LAB_PAGE, TRUNCATED, (_facts(Q, q2), _facts(S)))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert len(pr.candidates) == 3 and pr.split_duplicates_removed == 0


@pytest.mark.parametrize("failing", [0, 1])
def test_a_truncated_chunk_leaves_the_page_not_assessed(local_llm, failing):
    replies = [_facts(Q, R), _facts(S, T)]
    replies[failing] = TRUNCATED
    model = Model(LAB_PAGE, TRUNCATED, tuple(replies))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    # no second split level; after a failed chunk 1, chunk 2 is not even called
    assert len(model.sent) == 2 + failing <= xcall.MAX_CALLS_PER_PAGE
    assert not pr.assessed and pr.truncated and pr.candidates == []
    assert pr.error_kind == "truncated" and pr.finish_reason == "length"
    diag = pr.diagnostics()
    assert diag["split"] is True and diag["chunks_called"] == 1 + failing
    assert diag["chunks"][-1]["finish_reason"] == "length"


def test_a_non_truncation_error_is_not_split(local_llm):
    model = Model(LAB_PAGE, {"content": "nothing to report"})
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert model.sent == [LAB_PAGE] and not pr.split and not pr.assessed


def test_a_truncated_single_line_page_stays_not_assessed(local_llm):
    page = "S-Faktor Q 4,20 1,0-5,0"
    model = Model(page, TRUNCATED)
    with local_only("test"):
        pr = llm_extract.extract_page(page, page_number=1, chat=model)
    assert model.sent == [page] and not pr.split and pr.truncated and not pr.assessed


def test_a_reasoning_channel_chunk_keeps_its_items_unverified(local_llm):
    reasoning = {"content": "", "reasoning": json.dumps({"facts": [S, T]})}
    model = Model(LAB_PAGE, TRUNCATED, (_facts(Q, R), reasoning))
    with local_only("test"):
        out = llm_extract.extract_document([(1, LAB_PAGE)], vocab=Vocabulary.from_rows([]),
                                           chat=model)  # fmt: skip
    by_label = {f["label"]: f for f in out["facts"]}
    for label in ("B-Faktor S", "B-Faktor T"):
        assert by_label[label]["metadata"]["answer_source"] == "reasoning"
        assert by_label[label]["verification"] == "unverified"
    assert by_label["S-Faktor Q"]["metadata"]["answer_source"] == "content"
    assert out["page_diagnostics"]["1"]["answer_source"] == "reasoning"


# ── verification against the FULL page ──────────────────────────────────


def test_lab_evidence_is_verified_against_the_full_page(local_llm, monkeypatch):
    from oncofiles.curated import verifier

    pages_seen = []
    real = verifier.verify

    def spy(cand, *, page_text, **kw):
        pages_seen.append(page_text)
        return real(cand, page_text=page_text, **kw)

    monkeypatch.setattr(verifier, "verify", spy)
    model = Model(LAB_PAGE, TRUNCATED, (_facts(Q, R), _facts(S, T)))
    with local_only("test"):
        out = llm_extract.extract_document([(1, LAB_PAGE)], vocab=Vocabulary.from_rows([]),
                                           chat=model)  # fmt: skip
    assert len(out["facts"]) == 4 and pages_seen == [LAB_PAGE] * 4
    assert out["pages_not_assessed"] == [] and out["pages_truncated"] == 0


CLIN_PAGE, CLIN_ANSWER = llm_extract_clinical.EXAMPLES[1]


def _clinical_for(text: str, extra: dict | None = None) -> dict:
    """The worked answer restricted to items whose evidence is in ``text``."""
    answer = {k: [it for it in v if it["evidence"] in text] for k, v in CLIN_ANSWER.items()}
    for kind, item in (extra or {}).items():
        answer[kind].append(item)
    return {"content": json.dumps(answer, ensure_ascii=False)}


def test_clinical_truncation_splits_merges_and_verifies_on_the_full_page(local_llm, monkeypatch):
    from oncofiles.curated import verifier_clinical

    first, second = xcall.split_page_text(CLIN_PAGE)
    dup = next(it for v in CLIN_ANSWER.values() for it in v if it["evidence"] in first)
    dup_kind = next(k for k, v in CLIN_ANSWER.items() if dup in v)
    pages_seen = []
    real = verifier_clinical.verify_item

    def spy(cand, *, page_text, **kw):
        pages_seen.append(page_text)
        return real(cand, page_text=page_text, **kw)

    monkeypatch.setattr(verifier_clinical, "verify_item", spy)
    model = Model(
        CLIN_PAGE, TRUNCATED, (_clinical_for(first), _clinical_for(second, {dup_kind: dup}))
    )
    with local_only("test"):
        out = llm_extract_clinical.extract_document(
            [(1, CLIN_PAGE)], vocab=Vocabulary.from_rows([]), chat=model
        )
    total = sum(len(v) for v in CLIN_ANSWER.values())
    assert len(model.sent) == 3
    assert len(out["items"]) == total and set(pages_seen) == {CLIN_PAGE}
    diag = out["page_diagnostics"]["1"]
    assert diag["split"] is True and diag["duplicates_removed"] == 1
    assert out["pages_not_assessed"] == []


# ── diagnostics: counts only ────────────────────────────────────────────


def test_split_diagnostics_shape_without_text(local_llm):
    model = Model(LAB_PAGE, TRUNCATED, (_facts(Q, R), _facts(S, T)))
    with local_only("test"):
        out = llm_extract.extract_document([(1, LAB_PAGE)], vocab=Vocabulary.from_rows([]),
                                           chat=model)  # fmt: skip
    diag = out["page_diagnostics"]["1"]
    assert diag["split"] is True and diag["split_trigger"] == "truncated"
    assert diag["chunk_count"] == 2 and diag["chunks_called"] == 2
    assert diag["first_call"]["finish_reason"] == "length"
    assert diag["first_call"]["completion_tokens"] == 3999
    for chunk in diag["chunks"]:
        assert {"finish_reason", "prompt_tokens", "completion_tokens", "reasoning_tokens",
                "seconds", "lines", "answer_source"} <= set(chunk)  # fmt: skip
        assert chunk["finish_reason"] == "stop" and chunk["completion_tokens"] == 120
    assert sum(c["lines"] for c in diag["chunks"]) == len(LAB_LINES)
    # page totals cover all 3 calls
    assert diag["completion_tokens"] == 3999 + 120 + 120 and diag["prompt_tokens"] == 2700
    assert diag["finish_reason"] == "stop"
    assert {w["warning"]: w["pages"] for w in out["warnings"]} == {"split_pages": 1}
    dumped = json.dumps({k: out[k] for k in ("page_diagnostics", "warnings")})
    assert not [s for s in SECRETS if s in dumped]


# ── optional proactive split (off by default) ───────────────────────────


def test_proactive_split_sends_two_chunks_without_a_whole_page_call(local_llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_SPLIT_LINES", 5)
    model = Model(LAB_PAGE, _facts(Q), (_facts(Q, R), _facts(S, T)))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert model.sent == list(model.chunks) and len(pr.candidates) == 4
    diag = pr.diagnostics()
    assert diag["split_trigger"] == "proactive" and diag["first_call"] is None


def test_proactive_split_leaves_short_pages_alone(local_llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_SPLIT_LINES", len(LAB_LINES))
    model = Model(LAB_PAGE, _facts(Q, R, S, T))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert model.sent == [LAB_PAGE] and not pr.split


def test_proactive_chunk_truncation_is_not_split_again(local_llm, monkeypatch):
    monkeypatch.setattr(config, "LLM_EXTRACT_SPLIT_LINES", 5)
    model = Model(LAB_PAGE, _facts(Q), (TRUNCATED, _facts(S, T)))
    with local_only("test"):
        pr = llm_extract.extract_page(LAB_PAGE, page_number=1, chat=model)
    assert len(model.sent) == 1 and not pr.assessed and pr.truncated


def test_proactive_split_default_is_off():
    source = Path(config.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("LLM_EXTRACT_SPLIT_LINES", "0")' in source


def test_both_extractors_go_through_the_split_path(local_llm, monkeypatch):
    labels = []
    real = xcall.call_page

    def spy(res, **kw):
        labels.append(kw["label"])
        return real(res, **kw)

    monkeypatch.setattr(xcall, "call_page", spy)
    with local_only("test"):
        llm_extract.extract_page(LAB_PAGE, page_number=1, chat=Model(LAB_PAGE, _facts(Q)))
        llm_extract_clinical.extract_page(
            CLIN_PAGE, page_number=1, chat=Model(CLIN_PAGE, _clinical_for(CLIN_PAGE))
        )
    assert labels == ["llm extract", "clinical extract"]
