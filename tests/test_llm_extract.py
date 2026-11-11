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


def test_bad_json_is_a_page_error_and_structured_output_falls_back():
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
    dumped = json.dumps(out)
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
    assert doc["llm_only_codes"] == ["GLU"] and doc["rule_only_codes"] == []
    assert doc["unverified_reasons"] == {"label_unmapped": 1}
    assert job["report"]["extractor"] == llm_extract.EXTRACTOR_VERSION
    assert seen == ["curated LLM extract shadow"]
    assert await _snapshot(db) == before  # nothing written anywhere
    dumped = json.dumps(job)
    assert "0.45" not in dumped and "0,45" not in dumped and "S-ALT" not in dumped
