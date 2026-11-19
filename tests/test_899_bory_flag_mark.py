"""Bory layout: a flagged value keeps its unit and printed range (#899).

The local OCR writes the printed "up" triangle as "^". The parser did not know
it, so everything after the value (unit, reference range) was dropped and a
count printed with its unit could land under the percentage code. Synthetic
lines only: invented values, dates in 2027.
"""

from __future__ import annotations

import pytest

from oncofiles.curated.facts_rules import parse_bory
from tests.test_clinical_facts import vocab  # noqa: F401  (fixture)

PAGE = """Odber: 09.03.2027 07:40,
Krvný obraz: Leukocyty [WBC] ...12.31 ^  [10^9/l] {3.80 - 10.70 } ; \\
Neutrofily abs. [NEUT] ...8.12 ^ [10^9/l] {1.70 - 7.00 } ; \\
Hemoglobín [HGB] ...104.0 ↓  [g/l] {120.0 - 155.0 } ; \\
Trombocyty [PLT] ...431 ↑ [10^9/l] {150 - 400 } ; \\
Erytrocyty [RBC] ...4.11  [10^12/l] {3.80 - 5.20 }
Sérum: S-CRP ...48.20 ^ [mg/l] {0.00 - 5.00 } ; S-Kreatinín ...61.00 [µmol/l] {42.00 - 79.00 }
"""


@pytest.fixture
def facts(vocab):  # noqa: F811
    return {f["code"]: f for f in parse_bory([PAGE], vocab)}


@pytest.mark.parametrize(
    "code, unit, lo, hi, flag",
    [
        ("WBC", "10^9/l", 3.8, 10.7, "H"),
        ("NEUT", "10^9/l", 1.7, 7.0, "H"),
        ("HGB", "g/l", 120.0, 155.0, "L"),
        ("PLT", "10^9/l", 150.0, 400.0, "H"),
        ("CRP", "mg/l", 0.0, 5.0, "H"),
        ("RBC", "10^12/l", 3.8, 5.2, None),
        ("CREA", "µmol/l", 42.0, 79.0, None),
    ],
)
def test_flagged_value_keeps_unit_and_printed_range(facts, code, unit, lo, hi, flag):
    f = facts[code]
    assert (f["unit"], f["ref_low"], f["ref_high"], f["flag"]) == (unit, lo, hi, flag)
    assert f["verification"] == "machine"
    assert "flag_source" not in (f.get("metadata") or {})  # the lab's own range is there


def test_a_caret_inside_the_unit_is_not_a_mark(vocab):  # noqa: F811
    [f] = parse_bory(["Odber: 09.03.2027 07:40,\nKO: Leukocyty [WBC] ...6.10 [10^9/l]\n"], vocab)
    assert f["unit"] == "10^9/l" and f["flag"] is None and f["ref_low"] is None


def test_mark_without_a_range_is_an_arrow_flag(vocab):  # noqa: F811
    [f] = parse_bory(["Odber: 09.03.2027 07:40,\nKO: Leukocyty [WBC] ...12.31 ^ [10^9/l]\n"], vocab)
    assert f["flag"] == "H" and f["metadata"]["flag_source"] == "arrow"


@pytest.mark.parametrize("mark, value, flag", [("+", "12.31", "H"), ("~", "6.10", None)])
def test_a_mark_of_unknown_direction_is_stepped_over(vocab, mark, value, flag):  # noqa: F811
    head = f"Odber: 09.03.2027 07:40,\nKO: Leukocyty [WBC] ...{value} {mark} [10^9/l]"
    [f] = parse_bory([head + " {3.80 - 10.70 }\n"], vocab)
    assert (f["unit"], f["ref_low"], f["ref_high"], f["flag"]) == ("10^9/l", 3.8, 10.7, flag)
    # without a printed range the mark itself never sets a flag
    [g] = parse_bory([head + "\n"], vocab)
    assert g["unit"] == "10^9/l" and (g.get("metadata") or {}).get("flag_source") != "arrow"


def test_semi_quantitative_plus_stays_a_value(vocab):  # noqa: F811
    [f] = parse_bory(["Odber: 09.03.2027 07:40,\nMoč: U- Krv ...2+ [arb]\n"], vocab)
    assert f["value_text"] == "2+" and f["value_num"] is None and f["unit"] == "arb"
