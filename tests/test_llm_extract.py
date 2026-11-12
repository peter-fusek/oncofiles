"""LLM-first extraction, shadow mode (#714). Synthetic data only: invented
labels, values and dates (2027+); the local model is always mocked."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from oncofiles import cloud_guard
from oncofiles.cloud_guard import CloudForbiddenError, local_only
from oncofiles.curated import llm, llm_extract, verifier
from oncofiles.curated.facts_rules import Vocabulary
from tests.helpers import ERIKA_UUID

VOCAB = Vocabulary.from_rows(
    [
        {"code": "ALT", "category": "lab", "unit_canonical": "µkat/L",
         "aliases": ["ALT", "ALAT"], "unit_conversions": {}},
        {"code": "GLU", "category": "lab", "unit_canonical": "mmol/L",
         "aliases": ["GLU", "Glukóza"], "unit_conversions": {}},
        {"code": "CA19_9", "category": "tumor_marker", "unit_canonical": "kU/L",
         "aliases": ["CA 19-9"], "unit_conversions": {"U/ml": 1}},
    ]
)  # fmt: skip

# A layout no rule parser knows (#712 acceptance case): specimen-prefixed rows,
# value, range, then unit. Nothing in the verifier knows this layout.
PAGE = "\n".join(
    [
        "Laboratórny nález",
        "Dátum odberu: 14.6.2027",
        "S-ALT 0,45 0,10-0,60 µkat/l",
        "S-GLU 5,61 mmol/l",
        "S-XYZ 1,5 mg/l",
        "CA 19-9 7,7 U/ml",
    ]
)


def cand(**kw):
    base = {
        "label_as_printed": "S-ALT",
        "value": "0,45",
        "comparator": None,
        "unit_as_printed": "µkat/l",
        "ref_range_as_printed": "0,10-0,60",
        "flag_as_printed": None,
        "date_as_printed": "14.6.2027",
        "date_role": "sample",
        "evidence": "S-ALT 0,45 0,10-0,60 µkat/l",
        "page_number": 1,
    }
    return {**base, **kw}


def check(c, **kw):
    return verifier.verify(c, page_text=PAGE, vocab=VOCAB, **kw)


# ── verifier ────────────────────────────────────────────────────────────


def test_a_correct_fact_is_machine():
    f = check(cand())
    assert f["verification"] == "machine", f["metadata"]
    assert f["code"] == "ALT" and f["value_canonical"] == 0.45
    assert f["unit_canonical"] == "µkat/L" and f["occurred_at"] == "2027-06-14"
    assert (f["ref_low"], f["ref_high"], f["flag"]) == (0.1, 0.6, None)
    assert f["metadata"]["date_role"] == "sample" and "unverified_reasons" not in f["metadata"]


def test_whitespace_is_the_only_normalisation_of_evidence():
    assert check(cand(evidence="S-ALT  0,45\t0,10-0,60   µkat/l"))["verification"] == "machine"
    assert "evidence_not_found" in reasons(cand(evidence="S-ALT 0.45 0,10-0,60 µkat/l"))


def reasons(c, **kw):
    return check(c, **kw)["metadata"].get("unverified_reasons", [])


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"evidence": "S-ALT 0,46 0,10-0,60 µkat/l", "value": "0,46"}, "evidence_not_found"),
        ({"value": "0,47"}, "value_not_in_evidence"),
        ({"value": "0,10"}, "value_not_in_evidence"),  # a range bound is not the value
        ({"value": "0,4"}, "value_not_in_evidence"),  # digits must match whole
        ({"label_as_printed": "S-AST"}, "label_not_in_evidence"),
        ({"unit_as_printed": "mmol/l"}, "unit_not_in_evidence"),
        ({"comparator": "<"}, "comparator_not_in_evidence"),
        ({"date_as_printed": "15.6.2027"}, "date_not_printed"),
        ({"date_as_printed": "14/6/2027"}, "date_unparsed"),
        ({"evidence": "S-ALT 0,45 [?] 0,10-0,60 µkat/l"}, "ocr_uncertain"),
    ],
)
def test_each_failure_mode_keeps_the_fact_unverified(change, reason):
    c = cand(**change)
    page = PAGE + "\nS-ALT 0,45 [?] 0,10-0,60 µkat/l\n14/6/2027"
    f = verifier.verify(c, page_text=page, vocab=VOCAB)
    assert f["verification"] == "unverified"
    assert reason in f["metadata"]["unverified_reasons"]


def test_a_value_inside_the_label_never_counts():
    c = cand(label_as_printed="CA 19-9", value="19", unit_as_printed="U/ml",
             ref_range_as_printed=None, evidence="CA 19-9 7,7 U/ml")  # fmt: skip
    assert "value_not_in_evidence" in reasons(c)
    ok = check(c | {"value": "7,7"})
    assert ok["verification"] == "machine" and ok["code"] == "CA19_9"
    assert ok["value_canonical"] == 7.7  # U/ml → kU/L from fact_codes data


def test_unmapped_label_is_returned_unverified_without_a_code():
    c = cand(label_as_printed="S-XYZ", value="1,5", unit_as_printed="mg/l",
             ref_range_as_printed=None, evidence="S-XYZ 1,5 mg/l")  # fmt: skip
    f = check(c)
    assert f["code"] is None and f["label"] == "S-XYZ"
    assert f["metadata"]["unverified_reasons"] == ["label_unmapped"]


def test_unit_without_a_conversion_and_implausible_values():
    page = PAGE + "\nS-ALT 0,45 U/l\nS-GLU 77,7 mmol/l"
    no_conv = cand(unit_as_printed="U/l", ref_range_as_printed=None, evidence="S-ALT 0,45 U/l")
    f = verifier.verify(no_conv, page_text=page, vocab=VOCAB)
    assert "unit_unconvertible" in f["metadata"]["unverified_reasons"]
    assert f["value_canonical"] is None
    wild = cand(label_as_printed="S-GLU", value="77,7", unit_as_printed="mmol/l",
                ref_range_as_printed=None, evidence="S-GLU 77,7 mmol/l")  # fmt: skip
    f = verifier.verify(wild, page_text=page, vocab=VOCAB)
    assert f["metadata"]["unverified_reasons"] == ["implausible"]


def test_birth_date_and_missing_date():
    assert "birth_date" in reasons(cand(), birth_dates={"20270614"})
    assert "birth_date" in reasons(cand(), birth_dates={"2027-06-14"})
    no_date = cand(date_as_printed=None, date_role=None)
    assert reasons(no_date) == ["date_missing"]
    f = check(no_date, report_date="2027-06-20")  # the document date, as the rules do
    assert f["verification"] == "machine" and f["occurred_at"] == "2027-06-20"
    assert f["metadata"]["date_source"] == "document"


def test_source_reason_is_carried():
    assert reasons(cand(), source_reason="cowork_ocr") == ["cowork_ocr"]


def test_printed_dates():
    assert verifier.parse_printed_date("05.03.2027 07:40") == "2027-03-05T07:40"
    assert verifier.parse_printed_date("2027-03-05") == "2027-03-05"
    assert verifier.parse_printed_date("31.2.2027") is None
    assert verifier.parse_printed_date("včera") is None


# ── extractor ───────────────────────────────────────────────────────────

ITEM = {k: v for k, v in cand().items() if k != "page_number"}


def test_parse_answer_is_robust():
    body = json.dumps({"facts": [ITEM]})
    assert llm_extract.parse_answer(body) == [ITEM]
    assert llm_extract.parse_answer(f"Here you go:\n```json\n{body}\n```\nDone.") == [ITEM]
    assert llm_extract.parse_answer(f"<think>a {{ b</think>{body} trailing {{x}}") == [ITEM]
    assert llm_extract.parse_answer(f"note {{not json}} then {body}") == [ITEM]
    assert llm_extract.parse_answer(json.dumps([ITEM])) == [ITEM]
    with pytest.raises(llm.LocalLLMError):
        llm_extract.parse_answer("no json here")


def test_items_without_evidence_are_dropped_and_values_kept_as_printed():
    raw = [
        ITEM,
        {**ITEM, "evidence": ""},
        {**ITEM, "evidence": None},
        {**ITEM, "value": None},
        "junk",
        {**ITEM, "value": "<0,5", "comparator": None},
        {**ITEM, "value": 0.45, "date_role": "visit"},
    ]
    with local_only("test"):
        res = llm_extract.extract_page(
            PAGE, page_number=2, chat=lambda *a, **k: json.dumps({"facts": raw})
        )
    assert res.dropped["no_evidence"] == len(["", None])
    assert res.dropped["no_label_or_value"] == res.dropped["not_an_object"] == len(["x"])
    assert [c["value"] for c in res.candidates] == ["0,45", "0,5", "0.45"]
    assert res.candidates[1]["comparator"] == "<"
    assert res.candidates[2]["date_role"] == "other"
    assert {c["page_number"] for c in res.candidates} == {2}


def test_the_extractor_refuses_to_run_outside_local_only():
    with pytest.raises(CloudForbiddenError):
        llm_extract.extract_page(PAGE, page_number=1, chat=lambda *a, **k: "{}")


def test_bad_json_is_a_page_error_and_structured_output_falls_back(monkeypatch):
    from oncofiles import config

    monkeypatch.setattr(config, "LLM_EXTRACT_STRUCTURED_OUTPUT", True)
    with local_only("test"):
        res = llm_extract.extract_page(PAGE, page_number=1, chat=lambda *a, **k: "sorry")
    assert res.error and not res.candidates
    calls = []

    def chat(system, user, **kw):
        calls.append(kw.get("response_format"))
        if kw.get("response_format"):
            raise llm.LocalLLMError("local LLM HTTP 400")
        return json.dumps({"facts": [ITEM]})

    with local_only("test"):
        res = llm_extract.extract_page(PAGE, page_number=1, chat=chat)
    assert calls == [llm_extract.RESPONSE_FORMAT, None] and len(res.candidates) == 1


def test_prompt_is_versioned_and_carries_worked_examples():
    assert llm_extract.EXTRACTOR_VERSION.startswith("llm-extract-v")
    assert llm_extract.SYSTEM_PROMPT.count("Example ") >= 2
    assert "Copy, never compute" in llm_extract.SYSTEM_PROMPT


def model_answer(*_a, **_k):
    """What the model would answer for PAGE (mocked, never a real call)."""
    rows = [
        ("S-ALT", "0,45", "µkat/l", "0,10-0,60", "S-ALT 0,45 0,10-0,60 µkat/l"),
        ("S-GLU", "5,61", "mmol/l", None, "S-GLU 5,61 mmol/l"),
        ("S-XYZ", "1,5", "mg/l", None, "S-XYZ 1,5 mg/l"),
    ]
    return json.dumps({"facts": [
        {"label_as_printed": label, "value": v, "comparator": None, "unit_as_printed": u,
         "ref_range_as_printed": r, "flag_as_printed": None, "date_as_printed": "14.6.2027",
         "date_role": "sample", "evidence": ev}
        for label, v, u, r, ev in rows
    ]})  # fmt: skip


def test_a_new_layout_needs_no_layout_code():
    """#712 acceptance: specimen-prefixed rows are handled by the model +
    the generic verifier alone."""
    with local_only("test"):
        out = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=model_answer)
    by_label = {f["label"]: f for f in out["facts"]}
    assert by_label["S-ALT"]["verification"] == "machine"
    assert by_label["S-ALT"]["code"] == "ALT" and by_label["S-ALT"]["value_canonical"] == 0.45
    assert by_label["S-GLU"]["verification"] == "machine" and by_label["S-GLU"]["code"] == "GLU"
    assert by_label["S-XYZ"]["metadata"]["unverified_reasons"] == ["label_unmapped"]
    assert out["page_errors"] == 0 and out["pages"] == 1


# Wall-clock / duration fields: a timestamp like "...:38:00.451087" or a
# time.time() float can contain a synthetic value's digits by chance, which made
# the leak checks below flaky. They carry no extracted data, so the substring
# checks run on the report without them; every other key and value is checked.
_TIMING_KEYS = frozenset({"started", "started_at", "finished_at", "seconds"})


def _without_timing(obj):
    if isinstance(obj, dict):
        return {k: _without_timing(v) for k, v in obj.items() if k not in _TIMING_KEYS}
    if isinstance(obj, list):
        return [_without_timing(v) for v in obj]
    return obj


def test_without_timing_keeps_everything_else():
    report = {"started_at": "2027-01-01T00:38:00.451087", "seconds": 0.45,
              "documents": [{"agree": 1, "finished_at": "x", "codes": ["ALT"]}]}  # fmt: skip
    assert _without_timing(report) == {"documents": [{"agree": 1, "codes": ["ALT"]}]}


def test_shadow_compare_reports_keys_and_counts_only():
    with local_only("test"):
        facts = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=model_answer)["facts"]
    stored = [
        {"code": "ALT", "occurred_at": "2027-06-14", "value_canonical": 0.45},
        {"code": "GLU", "occurred_at": "2027-06-14", "value_canonical": 5.62},
        {"code": "CA19_9", "occurred_at": "2027-06-14", "value_canonical": 7.7},
    ]
    out = llm_extract.shadow_compare(stored, facts)
    assert out["agree"] == 1 and out["agree_machine"] == 1
    assert out["rule_only"] == len(["GLU", "CA19_9"])
    assert out["llm_only"] == out["value_mismatch"] == len(["GLU"])
    assert out["rule_only_codes"] == ["CA19_9"] and out["llm_only_codes"] == []
    assert out["unverified_reasons"] == {"label_unmapped": 1}
    dumped = json.dumps(_without_timing(out))
    assert "0.45" not in dumped and "5.6" not in dumped and "S-XYZ" not in dumped


# ── the shadow tool ─────────────────────────────────────────────────────

DOC = 7141


async def _seed(db):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": "side"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, 'labs', 'application/pdf', ?, ?, ?, ?, '2027-06-14')",
        (DOC, f"gdrive:g{DOC}", f"d{DOC}.pdf", f"d{DOC}.pdf", len(meta), ERIKA_UUID,
         f"g{DOC}", json.dumps(meta)),
    )  # fmt: skip
    await db.save_ocr_page(DOC, 1, PAGE, "curated-sidecar")
    await db.db.execute(
        "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
        "code, occurred_at, value_canonical, verification, metadata_json, page_number, "
        "source) VALUES (?, 'lab', ?, 'ALT', 'ALT', '2027-06-14', ?, 'machine', '{}', 1, 't')",
        (ERIKA_UUID, DOC, 0.45),
    )
    await db.db.commit()


async def _snapshot(db):
    out = {}
    for table in ("clinical_records", "clinical_record_audit", "curated_runs", "document_pages"):
        async with db.db.execute(f"SELECT * FROM {table} ORDER BY 1") as cur:  # noqa: S608
            out[table] = [tuple(dict(r).values()) for r in await cur.fetchall()]
    return out


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


def test_the_preview_is_enabled_by_default(monkeypatch):
    import importlib

    from oncofiles import config

    monkeypatch.delenv("LLM_EXTRACT_SHADOW_DISABLED", raising=False)
    try:
        assert importlib.reload(config).LLM_EXTRACT_SHADOW_DISABLED is False
    finally:
        importlib.reload(config)


async def test_the_off_switch_and_shadow_only(db, monkeypatch):
    from oncofiles import config
    from oncofiles.tools import curated as tool

    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", True)
    out = json.loads(await tool.curated_llm_extract_preview(_ctx(db), document_ids=[DOC]))
    assert "LLM_EXTRACT_SHADOW_DISABLED" in out["error"]
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    out = json.loads(
        await tool.curated_llm_extract_preview(_ctx(db), document_ids=[DOC], dry_run=False)
    )
    assert "shadow mode only" in out["error"]


async def test_the_preview_writes_nothing_and_runs_local_only(db, monkeypatch):
    from oncofiles import config
    from oncofiles.tools import curated as tool

    await _seed(db)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    seen = []

    def fake_chat(system, user, **kw):
        seen.append(cloud_guard.local_only_job())
        with pytest.raises(CloudForbiddenError):  # no cloud client inside the job
            cloud_guard.check_cloud_allowed("anthropic")
        return model_answer()

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    before = await _snapshot(db)
    started = json.loads(await tool.curated_llm_extract_preview(_ctx(db), document_ids=[DOC]))
    assert started["state"] == "running" and started["kind"] == "llm_shadow"
    assert not tool.run_active()  # a shadow job never blocks a deploy
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = json.loads(await tool.curated_reocr_status(_ctx(db)))
    assert job["state"] == "done", job
    doc = job["report"]["documents"][0]
    assert doc["result"] == "compared" and doc["agree"] == 1 and doc["agree_machine"] == 1
    assert doc["method"] == "stored_sidecar_text" and doc["pages_not_assessed"] == []
    assert doc["dob_known"] is False and doc["birth_date_guard"] == "inactive_no_dob"
    assert doc["parser_crosscheck"] == 0 and doc["crosscheck_pages"] == []  # local sidecar
    assert doc["llm_only_codes"] == ["GLU"] and doc["rule_only_codes"] == []
    assert doc["unverified_reasons"] == {"label_unmapped": 1}
    assert job["report"]["extractor"] == llm_extract.EXTRACTOR_VERSION
    assert seen == ["curated LLM extract shadow"]
    assert await _snapshot(db) == before  # nothing written anywhere
    dumped = json.dumps(_without_timing(job))
    assert "0.45" not in dumped and "0,45" not in dumped and "S-ALT" not in dumped


# ── infra review: truncation, budget, DOB guard ─────────────────────────


def two_pages():
    return [(1, PAGE), (2, PAGE.replace("Laboratórny nález", "Pokračovanie"))]


def test_a_truncated_page_is_not_assessed_and_excluded_from_the_metrics():
    def chat(system, user, info=None, **kw):
        if user.startswith("Page 2"):
            info["finish_reason"] = "length"
            return ""  # the whole budget went to reasoning
        info["finish_reason"] = "stop"
        return model_answer()

    with local_only("test"):
        out = llm_extract.extract_document(two_pages(), vocab=VOCAB, chat=chat)
    assert out["pages_not_assessed"] == [2] and out["pages_truncated"] == len([2])
    assert out["pages_used"] == len([1]) and out["finish_reasons"]["2"] == "length"
    assert out["method"] == "stored_sidecar_text"
    stored = [
        {"code": "ALT", "occurred_at": "2027-06-14", "value_canonical": 0.45, "page_number": 1},
        {"code": "CA19_9", "occurred_at": "2027-06-14", "value_canonical": 7.7, "page_number": 2},
    ]
    cmp = llm_extract.shadow_compare(stored, out["facts"], not_assessed_pages=[2])
    assert cmp["rule_only"] == 0 and cmp["rule_only_codes"] == []  # page 2 is unknown
    assert cmp["rule_facts"] == cmp["agree"] == len(["ALT"])


@pytest.mark.parametrize("answer", ["", 'half an answer {"facts": ['])
def test_empty_or_unparseable_answers_are_not_assessed(answer):
    with local_only("test"):
        out = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=lambda *a, **k: answer)
    assert out["pages_not_assessed"] == [1] and out["pages_used"] == 0
    assert out["facts"] == []


def test_a_valid_empty_answer_is_assessed():
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=lambda *a, **k: '{"facts": []}'
        )
    assert out["pages_not_assessed"] == [] and out["pages_used"] == len([1])


@pytest.mark.parametrize(("thinking", "budget"), [("on", llm.REASONING_BUDGET), ("off", 4000)])
def test_thinking_keeps_the_reasoning_budget_and_off_is_bounded(monkeypatch, thinking, budget):
    """#767: thinking ON needs room to reason; OFF gets the bounded answer
    budget so a repetition loop ends early. Both carry the extractor timeout."""
    from oncofiles import config

    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", thinking)
    monkeypatch.setattr(config, "LLM_EXTRACT_ANSWER_BUDGET", 4000)
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)
    seen = []

    def chat(system, user, max_tokens=None, timeout=None, **kw):
        seen.append((max_tokens, timeout))
        return model_answer()

    with local_only("test"):
        llm_extract.extract_page(PAGE, page_number=1, chat=chat)
    assert seen == [(budget, 300.0)]


def test_dob_unknown_is_flagged_and_counted():
    with local_only("test"):
        unknown = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=model_answer)
        known = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=model_answer, birth_dates={"2027-01-02"}
        )
    assert unknown["dob_known"] is False and unknown["birth_date_guard"] == "inactive_no_dob"
    assert unknown["dates_unchecked_no_dob"] == len(unknown["facts"]) > 0
    assert known["dob_known"] is True and known["birth_date_guard"] == "active"
    assert known["dates_unchecked_no_dob"] == 0


def test_future_write_paths_must_refuse_without_a_dob():
    with pytest.raises(verifier.NoBirthDateError):
        verifier.require_dob_for_write(set())
    verifier.require_dob_for_write({"2027-01-02"})


# ── diagnostics after the first prod shadow run ─────────────────────────


def _raise(exc):
    def chat(*_a, **_k):
        raise exc

    return chat


def _timeout():
    import httpx

    try:
        raise httpx.ReadTimeout("slow")
    except httpx.ReadTimeout as cause:
        try:
            raise llm.LocalLLMError("local LLM unreachable: ReadTimeout") from cause
        except llm.LocalLLMError as exc:
            return exc


def _answer(text, finish="stop", reasoning=""):
    def chat(*_a, info=None, **_k):
        info.update(finish_reason=finish, raw_content=text, reasoning_content=reasoning)
        return text

    return chat


@pytest.mark.parametrize(
    ("chat", "kind", "cls"),
    [
        (_raise(llm.LocalLLMError("local LLM HTTP 500")), "http_status", "LocalLLMError"),
        (_raise(_timeout()), "timeout", "ReadTimeout"),
        (_answer("I found nothing useful here."), "json_decode", "AnswerError"),
        (_answer('{"results": []}'), "schema_shape", "AnswerError"),
        (_answer(""), "empty", "AnswerError"),
        (_answer('{"facts": [', finish="length"), "truncated", "LocalLLMError"),
        (_raise(ValueError("boom")), "other", "ValueError"),
    ],
)
def test_each_error_kind_is_reported_and_the_page_not_assessed(chat, kind, cls):
    with local_only("test"):
        pr = llm_extract.extract_page(PAGE, page_number=1, chat=chat)
    assert not pr.assessed and pr.candidates == []
    diag = pr.diagnostics()
    assert (diag["error_kind"], diag["error_class"]) == (kind, cls)
    assert kind in llm_extract.ERROR_KINDS


def test_the_answer_shape_is_masked():
    raw = '<think>ok</think>```json\n{"results": ["Hodnota 5,83 S-ALT"]}\n```'
    with local_only("test"):
        pr = llm_extract.extract_page(PAGE, page_number=1, chat=_answer(raw),
                                      known=llm_extract.known_words(VOCAB))  # fmt: skip
    shape = pr.diagnostics()["answer"]
    assert shape["has_think"] and shape["has_fence"] and shape["top_level"] == "object"
    assert shape["chars"] == len(raw) and shape["field"] == "content"
    assert "5" not in shape["masked"] and "8" not in shape["masked"]
    assert "Hodnota" not in shape["masked"] and "aaaaaaa 9,99 a-ALT" in shape["masked"]
    assert '"results"' not in shape["masked"]  # an unknown key is masked too


def test_a_good_page_reports_its_shape_without_the_masked_text():
    with local_only("test"):
        pr = llm_extract.extract_page(PAGE, page_number=1, chat=_answer(model_answer()))
    shape = pr.diagnostics()["answer"]
    assert pr.assessed and "masked" not in shape and shape["top_level"] == "object"
    assert pr.diagnostics()["structured_output"] == "off"  # default since v3


def test_an_empty_content_falls_back_to_reasoning_content(monkeypatch):
    """LM Studio may leave content empty and put the JSON in reasoning_content."""
    import functools

    from oncofiles import config

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "choices": [{"finish_reason": "stop",
                     "message": {"content": "", "reasoning_content": model_answer()}}],
        "usage": {},
    }  # fmt: skip
    client = MagicMock()
    client.post.return_value = resp
    with local_only("test"):
        pr = llm_extract.extract_page(
            PAGE, page_number=1, chat=functools.partial(llm.chat, client=client)
        )
    assert pr.assessed and len(pr.candidates) == len(["ALT", "GLU", "XYZ"])
    assert pr.diagnostics()["answer"]["field"] == "reasoning_content"


def test_stored_unverified_facts_are_counted_and_labelled():
    stored = [
        {"code": "ALT", "occurred_at": "2027-06-14", "value_canonical": 0.45,
         "page_number": 1, "verification": "unverified"},
        {"code": "GLU", "occurred_at": "2027-06-14", "value_canonical": 5.61,
         "page_number": 1, "verification": "unverified"},
        {"code": "CA19_9", "occurred_at": "2027-06-14", "value_canonical": None,
         "page_number": 1, "verification": "machine"},
    ]  # fmt: skip
    # the page was not assessed: nothing compared, but the stored side is visible
    out = llm_extract.shadow_compare(stored, [], not_assessed_pages=[1])
    assert out["rule_facts"] == 0 and out["rule_facts_stored"] == len(stored)
    assert out["rule_facts_on_unassessed_pages"] == len(stored)
    assert out["rule_facts_by_verification"] == {"machine": 1, "unverified": len(["a", "b"])}
    assert out["rule_facts_no_value"] == len(["CA19_9"])
    # assessed: unverified stored facts are compared and their agreement labelled
    with local_only("test"):
        facts = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=model_answer)["facts"]
    out = llm_extract.shadow_compare(stored, facts)
    assert out["rule_facts"] == out["rule_facts_compared_unverified"] == len(["ALT", "GLU"])
    assert out["agree_with_unverified_rule"] == out["agree"] == len(["ALT", "GLU"])


# ── reasoning channel rule (infra review of #736) ───────────────────────


def _reasoning_only(reasoning):
    """content empty, everything in reasoning_content (LM Studio behaviour)."""
    return _answer("", reasoning=reasoning)


def test_one_valid_object_in_reasoning_is_accepted_and_tagged():
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=_reasoning_only(model_answer())
        )
    assert out["pages_not_assessed"] == [] and out["facts"]
    assert out["facts_from_reasoning"] == len(out["facts"]) and out["facts_from_content"] == 0
    assert {f["metadata"]["answer_source"] for f in out["facts"]} == {"reasoning"}
    assert out["page_diagnostics"]["1"]["answer_source"] == "reasoning"
    # never machine: every such fact carries the reasoning-channel reason
    for f in out["facts"]:
        assert f["verification"] == "unverified"
        assert verifier.REASONING_CHANNEL_REASON in f["metadata"]["unverified_reasons"]


def test_content_answers_are_tagged_content():
    with local_only("test"):
        out = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=model_answer)
    assert out["facts_from_content"] == len(out["facts"]) and out["facts_from_reasoning"] == 0
    assert {f["metadata"]["answer_source"] for f in out["facts"]} == {"content"}


@pytest.mark.parametrize(
    "reasoning",
    [
        '{"facts": []} then the final answer ' + "{}",  # draft + final
        "Draft:\n" + '{"facts": []}' + "\nFinal:\n" + model_answer(),  # two objects
        '{"facts": [{"label_as_printed": "S-ALT"}]}',  # fails the schema
        '{"results": []}',  # wrong top-level key
        "[]",  # a list, not an object
        '{"facts": [',  # partial
        "no JSON at all",
    ],
)
def test_ambiguous_reasoning_is_not_assessed(reasoning):
    with local_only("test"):
        pr = llm_extract.extract_page(PAGE, page_number=1, chat=_reasoning_only(reasoning))
    assert not pr.assessed and pr.candidates == []
    assert pr.diagnostics()["error_kind"] == "reasoning_ambiguous"


def test_the_verifier_never_makes_a_reasoning_fact_machine():
    f = check(cand(answer_source="reasoning"))
    assert f["verification"] == "unverified"
    assert f["metadata"]["unverified_reasons"] == ["llm_reasoning_channel"]
    assert verifier.REASONING_CHANNEL_NEVER_MACHINE is True
    assert verifier.from_reasoning_channel(f)
    assert check(cand())["verification"] == "machine"  # the same fact from content


# ── text path never upgrades a transcription (#714 design rule) ─────────

COWORK_ROW = {
    "code": "ALT", "occurred_at": "2027-06-14", "value_canonical": 0.45, "page_number": 1,
    "verification": "unverified",
    "metadata_json": json.dumps({"unverified_reasons": ["cowork_ocr"]}),
}  # fmt: skip


def test_a_cowork_page_stays_unverified_and_agreement_is_only_a_parser_crosscheck():
    assert verifier.TEXT_PATH_NEVER_UPGRADES_TRANSCRIPTION_REASONS is True
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=model_answer, source_reason="cowork_ocr"
        )
    facts = verifier.inherit_transcription_reasons(out["facts"], [COWORK_ROW])
    alt = next(f for f in facts if f["code"] == "ALT")
    assert alt["value_canonical"] == COWORK_ROW["value_canonical"]  # an exact match
    assert alt["verification"] == "unverified"
    assert "cowork_ocr" in alt["metadata"]["unverified_reasons"]
    assert not any(f["verification"] == "machine" for f in facts)
    pages = verifier.transcription_pages([COWORK_ROW], "cowork_ocr", [1])
    cmp = llm_extract.shadow_compare([COWORK_ROW], facts, crosscheck_pages=pages)
    assert cmp["parser_crosscheck"] == len(["ALT"]) and cmp["crosscheck_pages"] == [1]
    assert cmp["agree"] == cmp["agree_machine"] == cmp["agree_with_unverified_rule"] == 0


def test_a_stored_counterpart_reason_is_inherited_on_a_local_sidecar():
    row = {**COWORK_ROW, "metadata_json": json.dumps({"unverified_reasons": ["ocr_uncertain"]})}
    with local_only("test"):
        facts = llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=model_answer)["facts"]
    by_code = {f["code"]: f for f in facts}
    assert by_code["ALT"]["verification"] == "machine"  # before: the LLM alone says machine
    verifier.inherit_transcription_reasons(facts, [row])
    assert by_code["ALT"]["verification"] == "unverified"
    assert by_code["ALT"]["metadata"]["inherited_from_stored"] == ["ocr_uncertain"]
    assert by_code["GLU"]["verification"] == "machine"  # no stored counterpart: untouched
    assert verifier.transcription_pages([row], None, [1]) == {1}
    assert verifier.transcription_pages([{**row, "metadata_json": "{}"}], None, [1]) == set()


def test_inheritance_never_removes_reasons():
    f = check(cand(), source_reason="handwritten")
    verifier.inherit_transcription_reasons([f], [COWORK_ROW])
    assert f["metadata"]["unverified_reasons"] == ["handwritten", "cowork_ocr"]


def test_lab_structured_output_is_off_by_default_and_switchable(monkeypatch):
    from oncofiles import config
    from oncofiles.cloud_guard import local_only

    seen = []

    def chat(system, user, info=None, **kw):
        seen.append("response_format" in kw)
        if info is not None:
            info["finish_reason"] = "stop"
        return '{"facts": []}'

    with local_only("test"):
        monkeypatch.setattr(config, "LLM_EXTRACT_STRUCTURED_OUTPUT", False)
        off = llm_extract.extract_page("Hemoglobin 140 g/L", page_number=1, chat=chat)
        monkeypatch.setattr(config, "LLM_EXTRACT_STRUCTURED_OUTPUT", True)
        on = llm_extract.extract_page("Hemoglobin 140 g/L", page_number=1, chat=chat)
    assert seen == [False, True]
    assert off.structured_output == "off" and on.structured_output == "accepted"


# ── #767: per-job A/B overrides (dry run only) ──────────────────────────


async def test_preview_overrides_are_echoed_used_and_refused_on_write(db, monkeypatch):
    from oncofiles import config
    from oncofiles.tools import curated as tool

    await _seed(db)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "off")
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "greedy")
    seen = []

    def fake_chat(system, user, **kw):
        seen.append({k: kw.get(k) for k in ("thinking", "temperature", "top_p", "max_tokens")})
        return model_answer()

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    for kw in ({"thinking": "on"}, {"sampling": "qwen"}):
        with pytest.raises(ValueError, match="dry run"):
            await tool.curated_llm_extract_preview(_ctx(db), [DOC], dry_run=False, **kw)
    with pytest.raises(ValueError):
        await tool.curated_llm_extract_preview(_ctx(db), [DOC], sampling="hot")
    assert ERIKA_UUID not in tool._reocr_jobs
    started = json.loads(
        await tool.curated_llm_extract_preview(_ctx(db), [DOC], thinking="on", sampling="qwen")
    )
    assert started["overrides"] == {"thinking": "on", "sampling": "qwen", "prefill": None,
                                    "timeout_s": None}  # fmt: skip
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    report = json.loads(await tool.curated_reocr_status(_ctx(db)))["report"]
    assert report["settings"]["thinking"] == "on" and report["settings"]["sampling"] == "qwen"
    assert report["settings"]["overrides"] == {"thinking": "on", "sampling": "qwen",
                                               "prefill": None, "timeout_s": None}  # fmt: skip
    assert report["warnings"] == []
    assert seen == [{"thinking": True, "temperature": 0.6, "top_p": 0.95,
                     "max_tokens": llm.REASONING_BUDGET}]  # fmt: skip
    page = report["documents"][0]["page_diagnostics"]["1"]
    assert (page["thinking"], page["sampling"]) == ("on", "qwen")
    assert report["documents"][0]["warnings"] == []


# ── #770: prefilled closed think block + timeout override (dry run only) ──


async def test_preview_prefill_and_timeout_overrides(db, monkeypatch):
    from oncofiles import config
    from oncofiles.curated import llm_extract_call as xcall
    from oncofiles.tools import curated as tool

    await _seed(db)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_EXTRACT_THINKING", "off")
    monkeypatch.setattr(config, "LLM_EXTRACT_SAMPLING", "greedy")
    monkeypatch.setattr(config, "LLM_EXTRACT_PREFILL", False)
    monkeypatch.setattr(config, "LLM_EXTRACT_TIMEOUT_S", 300.0)
    seen = []

    def fake_chat(system, user, **kw):
        seen.append({k: kw.get(k) for k in ("prefill_assistant", "timeout", "thinking")})
        return model_answer()

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    for kw in ({"prefill": "on"}, {"prefill": "off"}, {"timeout_s": 120}):
        with pytest.raises(ValueError, match="dry run"):
            await tool.curated_llm_extract_preview(_ctx(db), [DOC], dry_run=False, **kw)
    for kw in ({"prefill": "yes"}, {"timeout_s": 29}, {"timeout_s": 901}, {"timeout_s": 60.5},
               {"timeout_s": True}):  # fmt: skip
        with pytest.raises(ValueError):
            await tool.curated_llm_extract_preview(_ctx(db), [DOC], **kw)
    assert ERIKA_UUID not in tool._reocr_jobs and seen == []
    started = json.loads(
        await tool.curated_llm_extract_preview(
            _ctx(db), [DOC], thinking="off", sampling="qwen", prefill="on", timeout_s=120
        )
    )
    assert started["overrides"] == {"thinking": "off", "sampling": "qwen", "prefill": "on",
                                    "timeout_s": 120}  # fmt: skip
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    report = json.loads(await tool.curated_reocr_status(_ctx(db)))["report"]
    assert report["extractor"] == "llm-extract-v5"
    assert report["settings"]["prefill"] == "on" and report["settings"]["timeout_s"] == 120.0
    assert report["settings"]["overrides"]["prefill"] == "on"
    assert seen == [{"prefill_assistant": xcall.THINK_PREFILL, "timeout": 120.0,
                     "thinking": False}]  # fmt: skip
    assert report["documents"][0]["page_diagnostics"]["1"]["prefill"] is True
