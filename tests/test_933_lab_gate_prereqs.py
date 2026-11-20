"""#933 steps 1 and 3, before the LLM lab write path can be enabled:

* every bucket of ``llm_extract.bucket_facts`` is summed by the lab job
  totals (``tools.curated._LAB_SUM_KEYS``) and the scorecard (``_LAB_COUNTS``);
* an OPEN rule range (``date_range_open``: start unknown, dated at the
  printed end) blocks an LLM day or range that reaches into it, like a
  closed range does (``lab_store._blocked``);
* every LLM date is tied to its result: ``date_evidence`` must be printed
  and contain the date; a fact without a date of its own, or with a report
  date on a report, is ``date_unknown`` instead of machine.

Synthetic data only: invented labels, values and dates (2027+).
"""

from __future__ import annotations

import json

from oncofiles.curated import lab_store, llm_extract, verifier
from oncofiles.curated import scorecard as sc
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.tools import curated as tool

# ── step 1: every bucket is summed ──────────────────────────────────────


def _fact(code, day, value):
    return {"code": code, "occurred_at": day, "value_canonical": value, "page_number": 1,
            "verification": "machine"}  # fmt: skip


def test_buckets_constant_names_every_bucket_bucket_facts_produces():
    stored = [_fact("ALT", "2027-08-02", 0.5), _fact("GLU", "2027-08-02", 5.0),
              _fact("UREA", "2027-08-02", 4.0), _fact("CRP", "2027-08-02", 3.0)]  # fmt: skip
    llm_facts = [_fact("ALT", "2027-08-02", 0.5), _fact("GLU", "2027-08-02", 6.0),
                 _fact("UREA", None, 4.0), _fact("K", "2027-08-02", 4.5),
                 _fact("NA", "2027-08-02", None), _fact(None, "2027-08-02", 1.0)]  # fmt: skip
    rule_side, llm_side = llm_extract.bucket_facts(stored, llm_facts)
    cross_side, _unused = llm_extract.bucket_facts(stored[:1], llm_facts[:1],
                                                   crosscheck_pages=(1,))  # fmt: skip
    produced = {b for b, _ in rule_side + llm_side + cross_side}
    assert produced == set(llm_extract.BUCKETS)


def test_lab_job_totals_and_scorecard_sum_every_bucket():
    assert set(llm_extract.BUCKETS) <= set(tool._LAB_SUM_KEYS)
    assert set(llm_extract.BUCKETS) <= set(sc._LAB_COUNTS)


def test_lab_totals_carry_the_llm_only_buckets():
    no_value, uncoded = 11, 13
    entry = {"agree": 7, "rule_facts": 7, "llm_no_value": no_value, "llm_uncoded": uncoded}
    entry["llm_facts"] = entry["agree"] + no_value + uncoded
    totals = tool._lab_totals([entry, dict(entry)])
    assert totals["llm_no_value"] == 2 * no_value
    assert totals["llm_uncoded"] == 2 * uncoded
    assert totals["llm_facts"] == sum(totals[b] for b in llm_extract.BUCKETS if b != "rule_only")


# ── step 1: an open rule range blocks LLM days ──────────────────────────

OPEN_META = {"date_source": "printed_range", "date_precision": "range",
             "date_range_end": "2028-05-19", "date_range_open": True}  # fmt: skip
CLOSED_META = {"date_source": "printed_range", "date_precision": "range",
               "date_range_start": "2028-05-13", "date_range_end": "2028-05-19"}  # fmt: skip


def _rule(meta, day="2028-05-19"):
    return {"code": "ALT", "occurred_at": day, "metadata_json": json.dumps(meta)}


def _llm(day, meta=None):
    return {"code": "ALT", "occurred_at": day, "metadata": dict(meta or {})}


def test_open_range_window_is_every_day_up_to_its_end():
    assert lab_store._span("2028-05-19", OPEN_META) == (lab_store._NO_START, "2028-05-19")
    no_end = {"date_precision": "range", "date_range_open": True, "date_range_end": None}
    assert lab_store._span(None, no_end) == (lab_store._NO_START, lab_store._NO_END)
    assert lab_store._span("2028-05-19", CLOSED_META) == ("2028-05-13", "2028-05-19")


def test_open_rule_range_blocks_llm_days_inside_and_at_its_end():
    others = [_rule(OPEN_META)]
    for day in ("2028-05-19", "2028-05-14", "2028-04-01", "2027-12-31"):
        assert lab_store._blocked(_llm(day), others), day
    # an LLM range reaching into the open window is blocked as well
    llm_range = {"date_precision": "range", "date_range_start": "2028-05-18",
                 "date_range_end": "2028-05-25"}  # fmt: skip
    assert lab_store._blocked(_llm("2028-05-25", llm_range), others)


def test_open_rule_range_does_not_block_a_day_after_its_end():
    """Disjoint spans are two results, as for a closed range (#826)."""
    others = [_rule(OPEN_META)]
    assert not lab_store._blocked(_llm("2028-05-20"), others)
    llm_range = {"date_precision": "range", "date_range_start": "2028-05-20",
                 "date_range_end": "2028-05-25"}  # fmt: skip
    assert not lab_store._blocked(_llm("2028-05-25", llm_range), others)


def test_closed_rule_range_rules_are_unchanged():
    others = [_rule(CLOSED_META)]
    assert lab_store._blocked(_llm("2028-05-15"), others)
    assert not lab_store._blocked(_llm("2028-05-12"), others)
    assert not lab_store._blocked(_llm("2028-05-20"), others)


def test_open_llm_range_is_symmetric():
    """The span is the same on both sides: an open LLM range (the verifier
    never makes one today) would be blocked by an earlier rule day."""
    others = [_rule({}, day="2028-05-01")]
    assert lab_store._blocked(_llm("2028-05-19", OPEN_META), others)
    assert not lab_store._blocked(_llm("2028-04-30", {}), others)


def test_open_range_with_no_end_blocks_every_llm_day_of_its_code():
    no_end = {"date_precision": "range", "date_range_open": True}
    others = [_rule(no_end, day=None)]
    assert lab_store._blocked(_llm("2031-01-01"), others)
    assert not lab_store._blocked({**_llm("2031-01-01"), "code": "GLU"}, others)


# ── step 3: a date tied to its result ───────────────────────────────────

VOCAB = Vocabulary.from_rows(
    [{"code": "CRP", "category": "lab", "unit_canonical": "mg/L", "aliases": ["CRP"],
      "unit_conversions": {}}]
)  # fmt: skip
REPORT_PAGE = "\n".join([
    "Ambulantná správa",
    "Kontrola 12.4.2028.",
    "Lab. z 10.4.2028: CRP 17,3 mg/l",
    "Ďalšia kontrola 3.5.2028.",
])  # fmt: skip


def cand(**kw):
    base = {"label_as_printed": "CRP", "value": "17,3", "comparator": None,
            "unit_as_printed": "mg/l", "ref_range_as_printed": None, "flag_as_printed": None,
            "date_as_printed": "10.4.2028", "date_role": "sample",
            "date_evidence": "Lab. z 10.4.2028", "evidence": "CRP 17,3 mg/l",
            "page_number": 1}  # fmt: skip
    return {**base, **kw}


def check(c, **kw):
    kw.setdefault("report_date", "2028-04-12")
    return verifier.verify(c, page_text=REPORT_PAGE, vocab=VOCAB, **kw)


def reasons(c, **kw):
    return check(c, **kw)["metadata"].get("unverified_reasons", [])


def test_a_sampling_date_with_its_printed_evidence_is_machine_on_a_report():
    f = check(cand(), document_category="report")
    assert f["verification"] == "machine", f["metadata"]
    assert f["occurred_at"] == "2028-04-10"


def test_no_date_of_its_own_is_date_unknown_not_the_document_date_as_machine():
    for category in ("report", "labs", None):
        f = check(cand(date_as_printed=None, date_role=None, date_evidence=None),
                   document_category=category)  # fmt: skip
        assert f["verification"] == "unverified"
        assert "date_unknown" in f["metadata"]["unverified_reasons"]
        # the document date is kept (as the rule parser keeps it), only never as machine
        assert f["occurred_at"] == "2028-04-12" and f["metadata"]["date_source"] == "document"


def test_no_date_and_no_document_date_stays_date_missing():
    r = reasons(cand(date_as_printed=None, date_evidence=None), report_date=None)
    assert "date_missing" in r and "date_unknown" not in r


def test_a_report_date_on_a_report_is_date_unknown():
    visit = cand(
        date_as_printed="12.4.2028", date_role="report", date_evidence="Kontrola 12.4.2028"
    )
    for category in ("report", "discharge", "chemo_sheet", "imaging"):
        f = check(visit, document_category=category)
        assert f["verification"] == "unverified" and f["occurred_at"] == "2028-04-12"
        assert f["metadata"]["unverified_reasons"] == ["date_unknown"], category


def test_a_report_date_on_a_lab_sheet_or_unknown_category_is_unchanged():
    visit = cand(
        date_as_printed="12.4.2028", date_role="report", date_evidence="Kontrola 12.4.2028"
    )
    assert check(visit, document_category="labs")["verification"] == "machine"
    assert check(visit)["verification"] == "machine"
    assert not verifier.is_report_category(None) and not verifier.is_report_category("labs")
    assert verifier.is_report_category("report")


def test_date_evidence_not_on_the_page_is_a_mismatch():
    assert "date_evidence_mismatch" in reasons(cand(date_evidence="Odber 10.4.2028"))


def test_date_evidence_without_the_chosen_date_is_a_mismatch():
    """The visit line is printed, but the chosen date is not in it."""
    assert "date_evidence_mismatch" in reasons(cand(date_evidence="Kontrola 12.4.2028."))


def test_missing_date_evidence_for_a_printed_date_is_a_mismatch():
    assert reasons(cand(date_evidence=None)) == ["date_evidence_mismatch"]


def test_date_evidence_tolerates_whitespace_and_decoration_only():
    assert check(cand(date_evidence="Lab.  z\t10.4.2028"))["verification"] == "machine"
    assert (
        check(cand(date_as_printed="10.4.2028:", date_evidence="Lab. z 10.4.2028:"))["verification"]
        == "machine"
    )


def test_a_range_date_needs_its_range_in_the_date_evidence():
    page = "Prepúšťacia správa\n13.5.2028 - 19.5.2028: CRP 17,3 mg/l"
    rng = cand(date_as_printed="13.5.2028 - 19.5.2028:", date_evidence="13.5.2028 - 19.5.2028:")
    f = verifier.verify(rng, page_text=page, vocab=VOCAB)
    assert f["verification"] == "machine", f["metadata"]
    bad = {**rng, "date_evidence": "19.5.2028"}
    f = verifier.verify(bad, page_text=page, vocab=VOCAB)
    assert f["metadata"]["unverified_reasons"] == ["date_evidence_mismatch"]


def test_versions_are_bumped_and_reasons_listed():
    assert llm_extract.EXTRACTOR_VERSION == "llm-extract-v11"
    assert verifier.VERIFIER_VERSION == 5
    assert {"date_unknown", "date_evidence_mismatch"} <= set(verifier.REASONS)
    assert lab_store.WRITER_VERSION == "llm-lab-v11"


def test_schema_prompt_and_examples_carry_date_evidence():
    item = llm_extract.SCHEMA["properties"]["facts"]["items"]
    assert "date_evidence" in item["required"] and "date_evidence" in item["properties"]
    assert '"date_evidence"' in llm_extract.SYSTEM_PROMPT
    for _title, page, facts in llm_extract.EXAMPLES:
        for f in facts:
            assert f["date_evidence"] and f["date_evidence"] in page
            assert f["date_as_printed"] in f["date_evidence"]
    # the report example shows the draw date apart from the visit date
    assert any("Kontrola" in page and facts and all(f["date_role"] == "sample" for f in facts)
               for _t, page, facts in llm_extract.EXAMPLES)  # fmt: skip


def test_normalize_item_keeps_date_evidence_only_with_a_date():
    raw = {"label_as_printed": "CRP", "value": "17,3", "evidence": "CRP 17,3 mg/l",
           "date_as_printed": "10.4.2028", "date_role": "sample",
           "date_evidence": "Lab. z 10.4.2028"}  # fmt: skip
    c, _ = llm_extract.normalize_item(raw)
    assert c["date_evidence"] == "Lab. z 10.4.2028"
    c, _ = llm_extract.normalize_item({**raw, "date_as_printed": None})
    assert c["date_evidence"] is None


def test_reasoning_channel_schema_check_requires_date_evidence():
    item = {"label_as_printed": "CRP", "value": "17,3", "comparator": None,
            "unit_as_printed": None, "ref_range_as_printed": None, "flag_as_printed": None,
            "date_as_printed": "10.4.2028", "date_role": "sample",
            "date_evidence": "Lab. z 10.4.2028", "evidence": "CRP 17,3"}  # fmt: skip
    assert llm_extract.valid_against_schema({"facts": [item]})
    old = {k: v for k, v in item.items() if k != "date_evidence"}
    assert not llm_extract.valid_against_schema({"facts": [old]})


def test_extract_document_passes_the_document_category_to_the_verifier():
    page = REPORT_PAGE
    visit = {**cand(date_as_printed="12.4.2028", date_role="report",
                    date_evidence="Kontrola 12.4.2028"), "page_number": None}  # fmt: skip
    visit.pop("page_number")

    def chat(system, user, info=None, **kw):
        answer = json.dumps({"facts": [visit]}, ensure_ascii=False)
        if info is not None:
            info.update(finish_reason="stop", raw_content=answer)
        return answer

    from oncofiles.cloud_guard import local_only

    with local_only("test"):
        on_report = llm_extract.extract_document(
            [(1, page)], vocab=VOCAB, report_date="2028-04-12", chat=chat,
            document_category="report",
        )  # fmt: skip
        on_labs = llm_extract.extract_document(
            [(1, page)], vocab=VOCAB, report_date="2028-04-12", chat=chat,
            document_category="labs",
        )  # fmt: skip
    assert [f["verification"] for f in on_report["facts"]] == ["unverified"]
    assert [f["verification"] for f in on_labs["facts"]] == ["machine"]
