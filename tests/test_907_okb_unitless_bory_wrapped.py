"""Printed ranges the rule parsers dropped (#907). Synthetic lines only.

* a lab-table row without a unit (a ratio) prints its range right after the
  value: the row pattern expected a unit in between;
* in the second hospital's layout a result item may be wrapped onto the next
  line inside its unit or range: each line was parsed alone.
"""

from __future__ import annotations

import pytest

from oncofiles.curated.facts_rules import _bory_lines, parse_bory, parse_okb
from tests.test_clinical_facts import vocab  # noqa: F401  (fixture)

OKB = """Odobrané: 11.03.2027 07:50    Prijaté: 11.03.2027 08:20
Test                                Hodnota    Jednotky    Ref. medze        Hodnotenie    SOP
Hematokrit                          0.318      0.35 - 0.47                    /
Hemoglobín                          104        g/l       120 - 160         *             /
Neutrofily                          61.3       %         45 - 70                         2
abs.počet NEUT                      3.12       2
Leukocyty                           5.09       G/l
"""

WRAPPED = """Odber: 09.03.2027 07:40,
Krvný obraz: Leukocyty [WBC] ...6.12  [10^9/l] {3.80 - 10.70 } ; Lymfocyty abs. ...0.91 ~ [10^
9/l] {1.00 - 4.00 } ; Monocyty abs. ...0.52 [10^9/l] {0.10 -
0.90 } ; Lymfocyty [LY%] ...14.90 ^
[%] {20.0 - 45.0 } ; Erytrocyty [RBC] ...4.11
[10^12/l] {3.80 - 5.20 }
"""


def test_table_row_without_a_unit_keeps_its_range(vocab):  # noqa: F811
    by = {f["code"]: f for f in parse_okb([OKB], vocab)}
    hct = by["HCT"]
    assert (hct["unit"], hct["ref_low"], hct["ref_high"], hct["flag"]) == (None, 0.35, 0.47, "L")
    assert hct["value_canonical"] == hct["value_num"]
    # rows that worked before are unchanged
    assert (by["HGB"]["unit"], by["HGB"]["ref_low"], by["HGB"]["ref_high"]) == ("g/l", 120.0, 160.0)
    assert (by["NEUT_PCT"]["ref_low"], by["NEUT_PCT"]["ref_high"]) == (45.0, 70.0)
    # an absolute count is printed without a range: a lone trailing column is not one
    assert by["NEUT"]["ref_low"] is None and by["NEUT"]["ref_high"] is None
    assert by["WBC"]["unit"] == "G/l" and by["WBC"]["ref_low"] is None


@pytest.mark.parametrize(
    "code, unit, lo, hi, flag",
    [
        ("WBC", "10^9/l", 3.8, 10.7, None),
        ("LYMPH", "10^9/l", 1.0, 4.0, "L"),
        ("MONO", "10^9/l", 0.1, 0.9, None),
        ("LYMPH_PCT", "%", 20.0, 45.0, "H"),
        ("RBC", "10^12/l", 3.8, 5.2, None),
    ],
)
def test_wrapped_item_is_read_whole(vocab, code, unit, lo, hi, flag):  # noqa: F811
    by = {f["code"]: f for f in parse_bory([WRAPPED], vocab)}
    assert len(by) == len(["WBC", "LYMPH", "MONO", "LYMPH_PCT", "RBC"])
    f = by[code]
    assert (f["unit"], f["ref_low"], f["ref_high"], f["flag"]) == (unit, lo, hi, flag)


def test_only_a_result_line_is_continued():
    text = (
        "Poznámka k odberu\n[chart]\nLeukocyty [WBC] ...6.12\n[rotated region]\n[?]\nStrana 1 / 1"
    )
    assert _bory_lines(text) == text.splitlines()  # fences are never glued to a result
    assert _bory_lines("Záver: bez zmien [pozri\ngraf] {príloha -\n2}") == [
        "Záver: bez zmien [pozri", "graf] {príloha -", "2}"
    ]  # fmt: skip
    whole = "Leukocyty [WBC] ...7.35 [10^9/l] {3.80 - 10.70 }"
    broken = whole.replace("[10^", "[10^\n").replace("{3.80 - ", "{3.80 -\n")
    assert broken.count("\n") == len(["in the unit", "in the range"])
    sampled = "Odber: 09.03.2027 07:40,"
    assert _bory_lines(f"{broken}\n{sampled}") == [whole, sampled]


def test_text_after_the_range_of_a_wrapped_item_is_not_its_evidence(vocab):  # noqa: F811
    page = (
        "Odber: 09.03.2027 07:40,\n"
        "Sérum: S-Kreatinín ...61.00\n"
        "[µmol/l] {42.00 - 79.00 } Hodnotenie doplní ošetrujúci lekár.\n"
        "Moč: U- pH ...5.5 {5.5 - 6.5 } poznámka na tom istom riadku\n"
    )
    by = {f["code"] or f["label"]: f for f in parse_bory([page], vocab)}
    crea = by["CREA"]
    assert (crea["unit"], crea["ref_low"], crea["ref_high"]) == ("µmol/l", 42.0, 79.0)
    assert crea["evidence"].rstrip().endswith("}") and "Hodnotenie" not in crea["evidence"]
    # an item printed on one line keeps its evidence exactly as before
    [ph] = [f for f in by.values() if f["evidence"].startswith("U- pH")]
    assert ph["evidence"].endswith("riadku")
