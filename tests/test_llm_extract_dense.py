"""Dense lab pages (#826): density split + segment-sized evidence.

Synthetic data only: invented labels ("Faktor A01"), values and dates
(2027+); the local model is always mocked (httpx MockTransport behind
``llm.chat``).
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from oncofiles import config
from oncofiles.cloud_guard import local_only
from oncofiles.curated import llm, llm_extract, llm_extract_clinical, verifier
from oncofiles.curated import llm_extract_call as xcall
from oncofiles.curated.facts_rules import Vocabulary

DATE = "7.4.2028 06:50"
SECTIONS = (("Biochémia", "A"), ("Krvný obraz", "B"), ("Imunológia", "C"))
PER_LINE = 15


def _segment(letter: str, n: int) -> str:
    return f"Faktor {letter}{n:02d} {n},{n % 10}1 mmol/l (0,50 - 99,00)"


SEGMENTS = [_segment(letter, n) for _, letter in SECTIONS for n in range(1, PER_LINE + 1)]
DENSE_LINES = [
    "Laboratórne vyšetrenie",
    f"Odber: {DATE}",
    *(
        f"{name}: " + "; ".join(_segment(letter, n) for n in range(1, PER_LINE + 1))
        for name, letter in SECTIONS
    ),
]
DENSE_PAGE = "\n".join(DENSE_LINES)  # 45 results on 3 long lines
SPARSE_PAGE = f"Odber: {DATE}\n{_segment('A', 1)}\n{_segment('A', 2)}"
SECRETS = ("Faktor", "mmol", "7.4.2028", "99,00")
VOCAB = Vocabulary.from_rows(
    [
        {"code": f"X{letter}{n:02d}", "category": "lab", "unit_canonical": "mmol/l",
         "aliases": [f"Faktor {letter}{n:02d}"], "unit_conversions": {}}
        for _, letter in SECTIONS
        for n in range(1, PER_LINE + 1)
    ]
)  # fmt: skip
_ITEM = re.compile(r"(Faktor [A-C]\d\d) (\d+,\d+) (mmol/l) \((0,50 - 99,00)\)")
_ODBER = re.compile(r"Odber: ([\d.]+ [\d:]+)")
WHOLE = "Page 1:\n"  # the unsplit request
FULL = xcall.DENSE_SEGMENTS_PER_CHUNK
SIZES = [FULL, FULL, FULL, 9]  # the 45 results of DENSE_PAGE
TRUNCATED = {"content": '{"facts": [', "finish": "length", "completion": 3999}


@pytest.fixture(autouse=True)
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "")
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "off")
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "greedy")
    monkeypatch.setattr(config, "LLM_EXTRACT_ANSWER_BUDGET", 4000)
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)
    monkeypatch.setattr(config, "LLM_EXTRACT_SPLIT_LINES", 0)


def _client(*, content="", finish="stop", completion=120):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 900, "completion_tokens": completion,
                          "completion_tokens_details": {"reasoning_tokens": 0}},
            },
        )  # fmt: skip

    return httpx.Client(transport=httpx.MockTransport(handler))


def _items(user: str) -> list[dict]:
    """What a careful model answers for one request: every result of the
    text it was sent, each with its own segment as evidence, dated from the
    sampling line it can see (the page's, or a Context line)."""
    date = _ODBER.search(user)
    body = user.partition(":\n")[-1]
    if "\nText:\n" in body:
        body = body.partition("\nText:\n")[-1]
    return [
        {"label_as_printed": m.group(1), "value": m.group(2), "comparator": None,
         "unit_as_printed": m.group(3), "ref_range_as_printed": m.group(4),
         "flag_as_printed": None, "date_as_printed": date.group(1) if date else None,
         "date_role": "sample" if date else None, "evidence": m.group(0)}
        for m in _ITEM.finditer(body)
    ]  # fmt: skip


class Model:
    """A fake local model. ``replies`` overrides the answer of the n-th call
    (0-based): a dict for ``_client`` or a function of the careful items."""

    def __init__(self, replies: dict | None = None):
        self.replies = replies or {}
        self.sent: list[str] = []

    def __call__(self, system, user, **kw):
        assert user.startswith("Page ")
        n = len(self.sent)
        self.sent.append(user)
        reply = self.replies.get(n)
        items = _items(user)
        if callable(reply):
            items, reply = reply(items), None
        reply = reply or {"content": json.dumps({"facts": items}, ensure_ascii=False)}
        return llm.chat(system, user, client=_client(**reply), **kw)


# ── the splitter ────────────────────────────────────────────────────────


def test_a_45_result_page_on_3_lines_becomes_bounded_chunks_with_no_segment_cut():
    assert xcall.count_result_segments(DENSE_PAGE) == 45
    chunks = xcall.split_by_density(DENSE_PAGE)
    assert [c.segments for c in chunks] == SIZES
    assert max(c.segments for c in chunks) <= xcall.DENSE_SEGMENTS_PER_CHUNK == 12
    # every printed segment is whole in exactly one chunk
    for seg in SEGMENTS:
        assert sum(seg in c.text for c in chunks) == 1, seg
    # a chunk holds only verbatim pieces of page lines
    for c in chunks:
        for piece in c.text.split("\n"):
            assert any(piece in line for line in DENSE_LINES)
        assert xcall.count_result_segments(c.text) == c.segments


def test_date_and_heading_context_reaches_every_chunk():
    chunks = xcall.split_by_density(DENSE_PAGE)
    assert chunks[0].context == ()  # the first chunk starts with the page's own header
    assert f"Odber: {DATE}" in chunks[0].text
    for c in chunks[1:]:
        assert f"Odber: {DATE}" in c.context
        assert not any(xcall.result_like(line) for line in c.context)  # never a result
    # a chunk cut out of the middle of a long line knows that line's label
    _first, second, third, fourth = chunks
    assert second.text.startswith("Faktor A13") and second.context[-1] == "Biochémia:"
    assert third.text.startswith("Faktor B10") and third.context[-1] == "Krvný obraz:"
    assert fourth.text.startswith("Faktor C07") and fourth.context[-1] == "Imunológia:"
    user = xcall.dense_chunk_user(3, 2, len(chunks), second)
    head = f"Page 3, part 2 of {len(chunks)}:\n"
    assert user.startswith(head + f"Context:\nOdber: {DATE}\nBiochémia:\nText:\nFaktor A13 ")
    # a heading line between two results travels with the results after it
    assert "\nKrvný obraz: Faktor B01 " in second.text


def test_a_dated_label_prefix_of_an_earlier_result_line_is_context():
    first = "labor. 3.5.2029: " + "; ".join(_segment("A", n) for n in range(1, 13))
    second = "; ".join(_segment("B", n) for n in range(1, 4))
    one, two = xcall.split_by_density(first + "\n" + second)
    assert one.context == () and two.context == ("labor. 3.5.2029:",)
    assert two.text == second


def test_separators_inside_brackets_and_decimal_commas_are_never_cut():
    line = "Faktor A01 2,5 mg/l {nefajčiar <2,5; fajčiar <5}; Faktor A02 1,1 (abs 0,4; rel 2)"
    pieces = [line[a:b] for a, b in xcall.segment_spans(line)]
    assert pieces == ["Faktor A01 2,5 mg/l {nefajčiar <2,5; fajčiar <5};",
                      " Faktor A02 1,1 (abs 0,4; rel 2)"]  # fmt: skip
    # no ";": cut at ", " in front of a letter only
    comma = "Lab.: Faktor A01 17,3 mg/l, Faktor A02 9,1, Faktor A03 0,45"
    assert len(xcall.segment_spans(comma)) == 3
    # an unclosed bracket (OCR) must not hide every separator
    assert len(xcall.segment_spans("Faktor A01 1,0 (0,5 - 2,0; Faktor A02 3,0")) == 2
    assert "".join(line[a:b] for a, b in xcall.segment_spans(line)) == line


def test_dates_and_times_alone_are_not_results():
    assert not xcall.result_like("Odber: 05.03.2027 07:40")
    assert not xcall.result_like("Vydané 2029-02-02")
    assert not xcall.result_like("Krvný obraz")
    assert xcall.result_like("Faktor A01 1,01 mmol/l")


def test_a_very_dense_page_is_bounded_by_the_chunk_limit():
    page = "\n".join(_segment("A", n % 100) for n in range(130))
    chunks = xcall.split_by_density(page)
    assert len(chunks) == xcall.DENSE_MAX_CHUNKS
    assert sum(c.segments for c in chunks) == 130
    assert xcall.split_by_density(SPARSE_PAGE) is None  # nothing to gain


# ── the lab extractor on a dense page ───────────────────────────────────


def test_a_dense_page_is_read_in_short_calls_and_every_fact_is_machine():
    model = Model()
    with local_only("test"):
        out = llm_extract.extract_document([(1, DENSE_PAGE)], vocab=VOCAB, chat=model)
    assert len(model.sent) == 4  # no whole-page call
    parts = len(model.sent)
    for i, u in enumerate(model.sent, start=1):
        assert u.startswith(f"Page 1, part {i} of {parts}:\n")
    assert all(f"Odber: {DATE}" in u for u in model.sent)  # the date reaches every chunk
    assert out["pages_not_assessed"] == [] and out["pages_truncated"] == 0
    assert len(out["facts"]) == 45
    assert {f["verification"] for f in out["facts"]} == {"machine"}
    assert {f["occurred_at"] for f in out["facts"]} == {"2028-04-07T06:50"}
    assert sorted(f["evidence"] for f in out["facts"]) == sorted(SEGMENTS)  # segment-sized
    diag = out["page_diagnostics"]["1"]
    assert diag["split"] is True and diag["split_mode"] == "density"
    assert diag["split_trigger"] == "proactive" and diag["first_call"] is None
    assert diag["chunk_count"] == 4 and diag["chunks_called"] == 4
    assert [c["segments"] for c in diag["chunks"]] == SIZES
    assert [c["finish_reason"] for c in diag["chunks"]] == ["stop"] * 4
    assert diag["completion_tokens"] == 4 * 120 and diag["prompt_tokens"] == 4 * 900
    assert diag["items"] == 45 and diag["date_null_items"] == 0
    assert diag["duplicates_removed"] == 0
    assert {w["warning"]: w["pages"] for w in out["warnings"]} == {"split_pages": 1}
    dumped = json.dumps({k: out[k] for k in ("page_diagnostics", "warnings")})
    assert not [s for s in SECRETS if s in dumped]  # counts only, never text


def test_every_item_is_verified_against_the_full_page(monkeypatch):
    pages_seen = []
    real = verifier.verify

    def spy(cand, *, page_text, **kw):
        pages_seen.append(page_text)
        return real(cand, page_text=page_text, **kw)

    monkeypatch.setattr(verifier, "verify", spy)
    with local_only("test"):
        llm_extract.extract_document([(1, DENSE_PAGE)], vocab=VOCAB, chat=Model())
    assert pages_seen == [DENSE_PAGE] * 45


@pytest.mark.parametrize("failing", [0, 2])
def test_a_still_truncated_chunk_leaves_the_page_not_assessed(failing):
    model = Model({failing: TRUNCATED})
    with local_only("test"):
        out = llm_extract.extract_document([(1, DENSE_PAGE)], vocab=VOCAB, chat=model)
    # one split level: the failed chunk is not cut again, later chunks are not called
    assert len(model.sent) == failing + 1 <= xcall.MAX_CALLS_PER_PAGE_DENSE
    assert out["pages_not_assessed"] == [1] and out["pages_truncated"] == 1
    assert out["facts"] == [] and out["pages_used"] == 0  # never "the model found none"
    diag = out["page_diagnostics"]["1"]
    assert diag["split_mode"] == "density" and diag["chunk_count"] == 4
    assert diag["chunks_called"] == failing + 1
    assert diag["chunks"][-1]["finish_reason"] == "length" and diag["error_kind"] == "truncated"
    assert {w["warning"] for w in out["warnings"]} == {"truncated", "split_pages"}


def test_an_item_repeated_across_a_chunk_boundary_is_dropped_once():
    carried = _items(WHOLE + DENSE_PAGE)[FULL - 1]  # the last item of the first chunk
    model = Model({1: lambda items: [carried, *items]})
    with local_only("test"):
        pr = llm_extract.extract_page(DENSE_PAGE, page_number=1, chat=model)
    assert pr.assessed and len(pr.candidates) == 45 and pr.split_duplicates_removed == 1
    assert pr.diagnostics()["duplicates_removed"] == 1


def test_a_sparse_page_makes_exactly_one_call_as_before():
    model = Model()
    with local_only("test"):
        pr = llm_extract.extract_page(SPARSE_PAGE, page_number=1, chat=model)
    assert model.sent == [WHOLE + SPARSE_PAGE]
    assert pr.assessed and not pr.split and len(pr.candidates) == 2
    diag = pr.diagnostics()
    assert diag["split"] is False and "split_mode" not in diag and "chunks" not in diag


def test_a_medium_page_is_density_split_only_after_a_truncated_answer():
    lines = [f"Odber: {DATE}", "; ".join(_segment("A", n) for n in range(1, 16)),
             "; ".join(_segment("B", n) for n in range(1, 6))]  # fmt: skip
    page = "\n".join(lines)  # 20 results: under the proactive threshold
    assert xcall.DENSE_SEGMENTS_PER_CHUNK < 20 <= xcall.DENSE_PROACTIVE_SEGMENTS
    fine = Model()
    with local_only("test"):
        pr = llm_extract.extract_page(page, page_number=1, chat=fine)
    assert fine.sent == [WHOLE + page] and not pr.split and len(pr.candidates) == 20
    model = Model({0: TRUNCATED})
    with local_only("test"):
        pr = llm_extract.extract_page(page, page_number=1, chat=model)
    assert len(model.sent) == 3 and model.sent[0] == WHOLE + page
    assert pr.assessed and len(pr.candidates) == 20
    diag = pr.diagnostics()
    assert diag["split_mode"] == "density" and diag["split_trigger"] == "truncated"
    assert diag["first_call"]["finish_reason"] == "length" and diag["chunk_count"] == 2
    assert [c["segments"] for c in diag["chunks"]] == [12, 8]


def test_prose_with_commas_and_numbers_is_not_taken_for_a_dense_page():
    line = ", ".join(f"cyklus {n} podaný bez komplikácií" for n in range(1, 11))
    page = "\n".join([line] * 4)  # 40 comma pieces with a number, on 4 lines
    assert xcall.count_result_segments(page) == 40
    assert xcall.count_result_segments(page, commas=False) == 4
    model = Model()
    with local_only("test"):
        pr = llm_extract.extract_page(page, page_number=1, chat=model)
    assert model.sent == [WHOLE + page] and pr.assessed and not pr.split


def test_a_truncated_page_with_few_results_keeps_the_line_split():
    page = "\n".join([f"Odber: {DATE}", *(_segment("A", n) for n in range(1, 7))])
    model = Model({0: TRUNCATED})
    with local_only("test"):
        pr = llm_extract.extract_page(page, page_number=1, chat=model)
    first, second = xcall.split_page_text(page)
    assert model.sent == [WHOLE + page, WHOLE + first, WHOLE + second]
    diag = pr.diagnostics()
    assert diag["split_mode"] == "lines" and diag["chunk_count"] == 2
    assert "segments" not in diag["chunks"][0]


def test_only_the_lab_extractor_opts_into_the_density_split(monkeypatch):
    seen = {}
    real = xcall.call_page

    def spy(res, **kw):
        seen[kw["label"]] = kw.get("density")
        return real(res, **kw)

    monkeypatch.setattr(xcall, "call_page", spy)
    clin_page, clin_answer = llm_extract_clinical.EXAMPLES[1]
    reply = json.dumps(clin_answer, ensure_ascii=False)
    with local_only("test"):
        llm_extract.extract_page(SPARSE_PAGE, page_number=1, chat=Model())
        llm_extract_clinical.extract_page(
            clin_page, page_number=1,
            chat=lambda system, user, **kw: llm.chat(system, user, client=_client(content=reply),
                                                     **kw),
        )  # fmt: skip
    assert seen == {"llm extract": llm_extract.DENSITY, "clinical extract": None}
    assert xcall.DensitySplit(12, 24, 8) == llm_extract.DENSITY


# ── the request: segment-sized evidence ─────────────────────────────────


def test_worked_examples_quote_segments_and_every_one_verifies_as_machine():
    labels = sorted({f["label_as_printed"] for _, _, facts in llm_extract.EXAMPLES for f in facts})
    units = {f["label_as_printed"]: f["unit_as_printed"]
             for _, _, facts in llm_extract.EXAMPLES for f in facts}  # fmt: skip
    vocab = Vocabulary.from_rows(
        [
            {"code": f"EX{i}", "category": "lab", "unit_canonical": units[label] or "",
             "aliases": [label], "unit_conversions": {}}
            for i, label in enumerate(labels)
        ]
    )  # fmt: skip
    multi = 0
    for _title, page, facts in llm_extract.EXAMPLES:
        for item in facts:
            fact = verifier.verify(item, page_text=page, vocab=vocab)
            assert fact["verification"] == "machine", (item["label_as_printed"], fact["metadata"])
            line = next(ln for ln in page.split("\n") if item["evidence"] in ln)
            if xcall.count_result_segments(line) > 1:
                multi += 1
                assert item["evidence"] != line and ";" not in item["evidence"]
                others = [o["label_as_printed"] for o in facts if o is not item]
                assert not any(o in item["evidence"] for o in others)
            assert len(item["evidence"]) <= 60
            assert item["date_as_printed"] not in item["evidence"]
    assert multi >= 5  # the multi-result lines are really exercised
    for _title, page, facts in llm_extract.EXAMPLES:
        assert page in llm_extract.SYSTEM_PROMPT
        assert json.dumps({"facts": facts}, ensure_ascii=False) in llm_extract.SYSTEM_PROMPT


def test_the_prompt_asks_for_the_segment_and_explains_context_lines():
    prompt = llm_extract.SYSTEM_PROMPT
    assert llm_extract.EXTRACTOR_VERSION == "llm-extract-v10"
    assert "never the whole line" in prompt and "evidence line" not in prompt
    assert '"Context:"' in prompt and "never return a result from a Context line" in prompt
    assert prompt.count("Example ") == len(llm_extract.EXAMPLES) == 7
