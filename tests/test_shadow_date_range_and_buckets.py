"""#826 follow-up: a printed date range in the model's answer, the masked
date-shape histogram and the one-bucket-per-fact accounting of the shadow
report. Synthetic data only: invented labels, values and dates (2027+); the
local model is always mocked."""

from __future__ import annotations

import json
import random
import re

import pytest

from oncofiles.cloud_guard import local_only
from oncofiles.curated import llm_extract, verifier
from oncofiles.curated import llm_extract_call as xcall
from oncofiles.curated.facts_rules import Vocabulary

VOCAB = Vocabulary.from_rows(
    [
        {"code": "ALT", "category": "lab", "unit_canonical": "µkat/L",
         "aliases": ["ALT"], "unit_conversions": {}},
        {"code": "GLU", "category": "lab", "unit_canonical": "mmol/L",
         "aliases": ["GLU"], "unit_conversions": {}},
        {"code": "UREA", "category": "lab", "unit_canonical": "mmol/L",
         "aliases": ["UREA"], "unit_conversions": {}},
    ]
)  # fmt: skip

START, END = "2028-05-13", "2028-05-19"
RANGE = "13.5.2028 - 19.5.2028"
RESULTS = "ALT 0,47 µkat/l; GLU 5,3 mmol/l"
# the layout of the failed production pages: a full-date range and a colon in
# front of a long line of ";"-separated results, another bracketed range
# (start without a year) further down
PAGE = "\n".join(
    [
        "Prepúšťacia správa",
        f"{RANGE}: {RESULTS}",
        "Hospitalizácia (2.5.- 21.5.2028) bez komplikácií.",
        "Kontrola 30.5.2028 v ambulancii.",
    ]
)


def cand(date, **kw):
    base = {
        "label_as_printed": "ALT",
        "value": "0,47",
        "comparator": None,
        "unit_as_printed": "µkat/l",
        "ref_range_as_printed": None,
        "flag_as_printed": None,
        "date_as_printed": date,
        "date_role": "sample",
        "evidence": "ALT 0,47 µkat/l",
        "page_number": 1,
    }
    return {**base, **kw}


def check(date, page=PAGE, **kw):
    return verifier.verify(cand(date), page_text=page, vocab=VOCAB, **kw)


def reasons(date, page=PAGE, **kw):
    return check(date, page, **kw)["metadata"].get("unverified_reasons", [])


# ── root cause: what a density chunk shows the model as its date ────────


def _segment(n: int) -> str:
    return f"Faktor A{n:02d} {n},50 mmol/l (0,50 - 99,00)"


def test_a_chunk_cut_from_a_ranged_line_carries_the_range_with_its_colon():
    """The header of the failed pages is the label prefix of the result line,
    so every later chunk gets it as a Context line, colon included: the model
    copies exactly that as ``date_as_printed``."""
    line = f"{RANGE}: " + "; ".join(_segment(n) for n in range(1, 31))
    chunks = xcall.split_by_density("Prepúšťacia správa\n" + line)
    assert len(chunks) > 1
    assert f"\n{RANGE}: Faktor A01 " in chunks[0].text
    for c in chunks[1:]:
        assert c.context[-1] == f"{RANGE}:"
    # a range line of its own is context as printed, too
    own = f"{RANGE}:\n" + "; ".join(_segment(n) for n in range(1, 31))
    assert all(f"{RANGE}:" in c.context for c in xcall.split_by_density(own)[1:])


def test_the_context_header_as_the_model_returns_it_is_a_verified_range():
    f = check(f"{RANGE}:")
    assert f["verification"] == "machine", f["metadata"]
    assert f["occurred_at"] == END  # dated at the range END (#726)
    meta = f["metadata"]
    assert meta["date_source"] == "printed_range" and meta["date_precision"] == "range"
    assert (meta["date_range_start"], meta["date_range_end"]) == (START, END)
    assert meta["verifier"] == verifier.VERIFIER_VERSION == len("v-3")


# ── what the parser tolerates: the shape of the answer only ─────────────


@pytest.mark.parametrize(
    "printed",
    [
        "13.5.2028 - 19.5.2028",
        "13.5.2028 - 19.5.2028:",
        "  13.5.2028 - 19.5.2028 : ",
        "13.5.2028 - 19.5.2028.",
        "13.5.2028 - 19.5.2028,",
        "13.5.2028-19.5.2028",
        "13.5.2028 – 19.5.2028",  # en dash
        "13.5.2028—19.5.2028:",  # em dash
        "13. 5. 2028 - 19. 5. 2028",
        "13.05.2028 - 19.05.2028",
    ],
)
def test_harmless_decoration_of_a_printed_range_is_tolerated(printed):
    f = check(printed)
    assert f["verification"] == "machine", (printed, f["metadata"])
    assert f["occurred_at"] == END and f["metadata"]["date_range_start"] == START


@pytest.mark.parametrize("printed", ["2.5.- 21.5.2028", "2.5.-21.5.2028:", "2.5. – 21.5.2028"])
def test_a_start_without_a_year_takes_the_year_of_the_end_printed_with_it(printed):
    f = check(printed)
    assert f["verification"] == "machine", (printed, f["metadata"])
    assert f["occurred_at"] == "2028-05-21"
    assert f["metadata"]["date_range_start"] == "2028-05-02"


@pytest.mark.parametrize(
    "printed",
    [
        "13.5.28 - 19.5.28",  # two-digit years are never guessed
        "13.5.2028 - 19.5.28",
        "Context: 13.5.2028 - 19.5.2028:",  # a label is not decoration
        "Odber 13.5.2028 - 19.5.2028",
        "(13.5.2028 - 19.5.2028)",  # brackets are not decoration
        "13.-19.5.2028",  # a day-only start
        "19.5.2028 - 13.5.2028",  # reversed
        "13.5.2028 - 19.5.",  # the end must carry its year
        "13.5.2028 až 19.5.2028",
        "13.5.2028 / 19.5.2028",
        "13.5.2028 - 19.5.2028 - 30.5.2028",
        "31.2.2028 - 19.5.2028",  # not a calendar day
    ],
)
def test_anything_else_stays_unparsed(printed):
    f = check(printed)
    assert f["verification"] == "unverified" and f["occurred_at"] is None
    assert f["metadata"]["unverified_reasons"] == ["date_unparsed"]
    assert "date_range_end" not in f["metadata"]


@pytest.mark.parametrize(
    "printed",
    [
        "14.5.2028 - 19.5.2028:",  # a decorated range that is NOT on the page
        "13.5.2028 - 20.5.2028",
        "13.5.2029 - 19.5.2029",
        "5.13.2028 - 5.19.2028",  # day/month swapped: unparsed, never "repaired"
        "13.5.2028 - 30.5.2028",  # both ends printed, but not as one range
        "13.5. - 19.5.2028",  # the page prints the start WITH its year
        "2.5.2028 - 21.5.2028",  # the page prints this start WITHOUT a year
    ],
)
def test_a_range_that_is_not_printed_on_the_page_is_never_verified(printed):
    f = check(printed)
    assert f["verification"] == "unverified", printed
    assert set(f["metadata"]["unverified_reasons"]) & {"date_not_printed", "date_unparsed"}


def test_a_range_is_dated_at_its_end_only_when_both_ends_are_printed():
    only_end = PAGE.replace(RANGE, "do 19.5.2028")
    assert reasons(RANGE, only_end) == ["date_not_printed"]
    only_start = PAGE.replace(RANGE, "od 13.5.2028")
    assert reasons(RANGE, only_start) == ["date_not_printed"]
    # the two ends on different lines are two dates, not a range
    apart = PAGE.replace(RANGE, "13.5.2028 odber, 19.5.2028")
    assert reasons(RANGE, apart) == ["date_not_printed"]


def test_a_decorated_single_date_must_be_printed_exactly():
    assert check("30.5.2028:")["verification"] == "machine"
    assert check(" 30.5.2028. ")["occurred_at"] == "2028-05-30"
    assert reasons("29.5.2028:") == ["date_not_printed"]  # decorated, not on the page
    # the decorated form never leans on a 2-digit year or a bare day.month
    assert reasons("30.5.2028:", PAGE.replace("30.5.2028", "30.5.28")) == ["date_not_printed"]
    assert reasons("30.5.2028:", PAGE.replace("30.5.2028", "30.5.")) == ["date_not_printed"]
    assert reasons("30.5.28:") == ["date_unparsed"]


def test_plain_dates_verify_exactly_as_before():
    assert check("30.5.2028")["verification"] == "machine"
    assert check("30.5.2028")["metadata"]["date_source"] == "printed"
    assert reasons("29.5.2028") == ["date_not_printed"]
    assert reasons("koncom mája") == ["date_unparsed"]


def test_neither_end_of_a_range_may_be_a_birth_date():
    assert reasons(RANGE, birth_dates=[START]) == ["birth_date"]
    assert reasons(RANGE, birth_dates=[END]) == ["birth_date"]
    assert reasons(RANGE, birth_dates=["1990-01-01"]) == []


def test_range_parser_units():
    assert verifier.parse_printed_range(f"{RANGE}:")[:2] == (START, END)
    assert verifier.parse_printed_range("19.5.2028") is None
    assert verifier.parse_printed_range(None) is None
    assert verifier.undecorate_date("  19.5.2028 :., ") == "19.5.2028"
    assert verifier.date_exactly_printed(END, PAGE)
    assert not verifier.date_exactly_printed("2028-05-02", PAGE)  # printed without a year
    key = verifier.parse_printed_range("11.5.2028 - 19.5.2028")[2]
    assert not verifier.range_printed(key, "211.5.2028 - 19.5.20281")  # no partial tokens


# ── the masked date-shape histogram ─────────────────────────────────────


def test_date_shape_masks_every_digit_and_letter():
    assert llm_extract.date_shape("13.5.2028 - 19.5.2028:") == "99.9.9999 - 99.9.9999:"
    assert llm_extract.date_shape("Odber:  13.5.2028\n07:10") == "aaaaa: 99.9.9999 99:99"
    shape = llm_extract.date_shape("máj 2028 " * 20)
    assert len(shape) == llm_extract.DATE_SHAPE_CHARS and not re.search(r"[0-8b-zá-ž]", shape)
    assert llm_extract.date_shape(None) == ""


def _answer(dates):
    def chat(*_a, **_k):
        return json.dumps({"facts": [cand(d) | {"page_number": None} for d in dates]})

    return chat


def test_the_report_carries_shapes_of_failed_dates_only():
    dates = ["(13.5.2028 - 19.5.2028)"] * 3 + ["Odber 13.5.2028", "14.5.2028 - 19.5.2028:", RANGE]
    answer = _answer(dates)
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=lambda *a, **k: _strip(answer())
        )
    assert out["date_shapes_unverified"] == {
        "(99.9.9999 - 99.9.9999)": 3,
        "aaaaa 99.9.9999": 1,
        "99.9.9999 - 99.9.9999:": 1,  # parsed, but not printed on the page
    }
    dumped = json.dumps(out["date_shapes_unverified"])
    assert "2028" not in dumped and "Odber" not in dumped
    assert [f["verification"] for f in out["facts"]].count("machine") == 1


def _strip(answer: str) -> str:
    facts = json.loads(answer)["facts"]
    return json.dumps(
        {"facts": [{k: v for k, v in f.items() if k != "page_number"} for f in facts]}
    )


def test_shapes_are_capped_at_the_top_five():
    dates = [f"{'x' * n} 13.5.2028" for n in range(1, 9) for _ in range(n)]
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=lambda *a, **k: _strip(_answer(dates)())
        )
    shapes = out["date_shapes_unverified"]
    assert len(shapes) == llm_extract.DATE_SHAPE_TOP
    assert list(shapes.values()) == sorted(shapes.values(), reverse=True)
    assert max(shapes.values()) == len("x" * 8)


def test_a_clean_document_has_an_empty_histogram():
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=lambda *a, **k: _strip(_answer([RANGE, f"{RANGE}:"])())
        )
    assert out["date_shapes_unverified"] == {}
    from oncofiles.tools import curated as tools

    assert "date_shapes_unverified" in tools._SHADOW_DOC_KEYS


# ── one bucket per fact ─────────────────────────────────────────────────

RULE_SUM = llm_extract.RULE_BUCKETS
LLM_SUM = llm_extract.LLM_BUCKETS


def rule(code, day, value, page=1, **kw):
    return {"code": code, "occurred_at": day, "value_canonical": value, "page_number": page, **kw}


def fact(code, day, value, page=1, verification="machine", why=()):
    meta = {"unverified_reasons": list(why)} if why else {}
    return {"code": code, "occurred_at": day, "value_canonical": value, "page_number": page,
            "verification": "unverified" if why else verification, "metadata": meta}  # fmt: skip


def assert_invariant(out):
    assert out["rule_facts"] == sum(out[b] for b in RULE_SUM), out
    assert out["llm_facts"] == sum(out[b] for b in LLM_SUM), out
    for bucket in ("value_mismatch", "date_mismatch", "rule_only", "llm_only", "llm_no_value"):
        assert bool(out[bucket]) == bool(out[f"{bucket}_codes"]), bucket


def test_each_bucket_is_defined_and_counted_once():
    day = "2028-05-19"
    stored = [
        rule("ALT", day, 0.47),  # agree
        rule("GLU", day, 5.3),  # value_mismatch (LLM read 5.4)
        rule("UREA", day, 6.1),  # date_mismatch (LLM date unparsed)
        rule("CA19_9", day, 7.7),  # rule_only
        rule("ALT", day, 0.52, page=2),  # parser_crosscheck (transcription page)
        rule("CEA", day, None),  # stored without a value: not compared
    ]
    facts = [
        fact("ALT", day, 0.47),
        fact("GLU", day, 5.4),
        fact("UREA", None, 6.1, why=["date_unparsed"]),
        fact("CRP", day, 12.0),  # llm_only
        fact("ALT", day, 0.52, page=2, why=["cowork_ocr"]),
        fact("GLU", day, None, why=["unit_unconvertible"]),  # llm_no_value
        fact(None, day, None, why=["label_unmapped"]),  # llm_uncoded
    ]
    out = llm_extract.shadow_compare(stored, facts, crosscheck_pages=[2])
    assert {b: out[b] for b in (*RULE_SUM, *LLM_SUM)} == dict.fromkeys((*RULE_SUM, *LLM_SUM), 1)
    assert out["rule_facts"] == len(RULE_SUM) and out["llm_facts"] == len(LLM_SUM)
    assert out["rule_facts_no_value"] == 1 and out["agree_machine"] == 1
    assert out["value_mismatch_codes"] == ["GLU"] and out["date_mismatch_codes"] == ["UREA"]
    assert out["rule_only_codes"] == ["CA19_9"] and out["llm_only_codes"] == ["CRP"]
    assert out["llm_no_value_codes"] == ["GLU"]
    assert_invariant(out)
    by_code = llm_extract.agreement_by_code(stored, facts, crosscheck_pages=[2])
    assert by_code == {
        "ALT": {"agree": 1, "parser_crosscheck": 1},
        "CA19_9": {"rule_only": len(["CA19_9"])},
        "CRP": {"llm_only": 1},
        "GLU": {"value_mismatch": 1},
        "UREA": {"date_mismatch": 1},
    }


def test_the_failed_production_shape_is_a_date_mismatch_with_codes():
    """43 facts whose date was unparsed: before, rule_only 43 + llm_only 43
    with empty code lists; now one named bucket."""
    codes = [f"C{n:02d}" for n in range(43)]
    stored = [rule(c, "2028-05-19", n + 0.5) for n, c in enumerate(codes)]
    facts = [fact(c, None, n + 0.5, why=["date_unparsed"]) for n, c in enumerate(codes)]
    out = llm_extract.shadow_compare(stored, facts)
    assert out["date_mismatch"] == len(codes) and out["date_mismatch_codes"] == codes
    assert out["agree"] == out["rule_only"] == out["llm_only"] == 0
    assert_invariant(out)


def test_uncomparable_llm_facts_are_counted_not_lost():
    day = "2028-05-19"
    stored = [rule("ALT", day, 0.47)]
    facts = [fact("ALT", day, 0.47)]
    facts += [fact("GLU", day, None, why=["value_not_in_evidence", "unit_unconvertible"])] * 4
    facts += [fact(None, day, None, why=["label_unmapped"])] * 3
    out = llm_extract.shadow_compare(stored, facts)
    assert out["agree"] == len(stored) and out["llm_only"] == 0
    assert out["llm_no_value"] == len("GLU ") and out["llm_uncoded"] == len("GLU")
    assert_invariant(out)


def _legacy_agree(stored, facts, skip, cross):
    """``agree`` / ``parser_crosscheck`` as computed before #826 (Counter math)."""
    from collections import Counter

    def keys(rows, pages=None):
        return Counter(
            llm_extract._key(r["code"], r.get("occurred_at"), r["value_canonical"])
            for r in rows
            if r.get("code")
            and r.get("value_canonical") is not None
            and int(r.get("page_number") or 1) not in skip
            and (pages is None or int(r.get("page_number") or 1) in pages)
        )

    crosscheck = keys(stored, cross) & keys(facts, cross)
    return sum(((keys(stored) & keys(facts)) - crosscheck).values()), sum(crosscheck.values())


def test_invariant_holds_and_agree_is_unchanged_on_random_documents():
    rng = random.Random(826)
    codes, days = ["ALT", "GLU", "UREA", None], ["2028-05-19", "2028-05-20", None]
    values = [1.5, 2.5, None]
    for _ in range(300):
        stored = [
            rule(rng.choice(codes), rng.choice(days), rng.choice(values), rng.randint(1, 4))
            for _ in range(rng.randint(0, 12))
        ]
        facts = [
            fact(rng.choice(codes), rng.choice(days), rng.choice(values), rng.randint(1, 4),
                 rng.choice(["machine", "unverified"]))
            for _ in range(rng.randint(0, 12))
        ]  # fmt: skip
        skip = {p for p in (3, 4) if rng.random() < 0.3}
        cross = {p for p in range(1, 4) if rng.random() < 0.3}
        out = llm_extract.shadow_compare(
            stored, facts, not_assessed_pages=skip, crosscheck_pages=cross
        )
        assert_invariant(out)
        assert (out["agree"], out["parser_crosscheck"]) == _legacy_agree(stored, facts, skip, cross)
        assert out["agree_machine"] <= out["agree"]
        by_code = llm_extract.agreement_by_code(
            stored, facts, not_assessed_pages=skip, crosscheck_pages=cross
        )
        for bucket in ("agree", "parser_crosscheck", "value_mismatch", "date_mismatch",
                       "rule_only", "llm_only"):  # fmt: skip
            assert sum(c.get(bucket, 0) for c in by_code.values()) == out[bucket], bucket
        rule_side, llm_side = llm_extract.bucket_facts(
            stored, facts, not_assessed_pages=skip, crosscheck_pages=cross
        )
        # every compared fact exactly once, in a bucket of its own side
        assert len(rule_side) == out["rule_facts"] and len(llm_side) == out["llm_facts"]
        assert len({id(r) for _, r in rule_side}) == len(rule_side)
        assert {b for b, _ in rule_side} <= set(RULE_SUM)
        assert {b for b, _ in llm_side} <= set(LLM_SUM)


def test_a_verified_range_agrees_with_the_rule_fact_dated_at_the_range_end():
    with local_only("test"):
        out = llm_extract.extract_document(
            [(1, PAGE)], vocab=VOCAB, chat=lambda *a, **k: _strip(_answer([f"{RANGE}:"])())
        )
    stored = [rule("ALT", END, 0.47, verification="machine")]
    cmp = llm_extract.shadow_compare(stored, out["facts"])
    assert cmp["agree"] == cmp["agree_machine"] == 1 and cmp["date_mismatch"] == 0
    assert_invariant(cmp)
