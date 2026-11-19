"""Dot leaders in front of a printed value (#833, ``VERIFIER_VERSION`` 3).

Some lab printouts fill the gap between a label and its value with dots
("Glukóza ..........5.2"). The verifier's numeric pattern refuses a "."
right before a value (so "5.2" is not found inside "15.2" or a date), which
rejected every such value. A run of filler dots now counts as whitespace,
unless it directly follows a digit. Everything here is invented (2027+).
"""

from __future__ import annotations

import re

import pytest

from oncofiles.curated import verifier, verifier_clinical
from oncofiles.curated.facts_rules import Vocabulary

VOCAB = Vocabulary.from_rows(
    [
        {"code": "GLU", "category": "lab", "unit_canonical": "mmol/l",
         "aliases": ["Glukóza"], "unit_conversions": {}},
        {"code": "CREA", "category": "lab", "unit_canonical": "µmol/l",
         "aliases": ["Kreatinín"], "unit_conversions": {}},
        {"code": "UREA", "category": "lab", "unit_canonical": "mmol/l",
         "aliases": ["Urea"], "unit_conversions": {}},
        {"code": "NA", "category": "lab", "unit_canonical": "mmol/l",
         "aliases": ["Sodík"], "unit_conversions": {}},
        {"code": "CRP", "category": "lab", "unit_canonical": "mg/l",
         "aliases": ["CRP"], "unit_conversions": {}},
        {"code": "U_PH", "category": "lab", "unit_canonical": None,
         "aliases": ["U-pH"], "unit_conversions": {}},
    ]
)  # fmt: skip

VALUE_REASONS = {"value_not_in_evidence", "comparator_not_in_evidence"}


def value_reasons(evidence, value, *, label, unit=None, ref=None, comparator=None):
    """Only the reasons of the value rule, for one claimed value."""
    cand = {"label_as_printed": label, "value": value, "comparator": comparator,
            "unit_as_printed": unit, "ref_range_as_printed": ref, "flag_as_printed": None,
            "date_as_printed": None, "date_role": None, "evidence": evidence,
            "page_number": 1}  # fmt: skip
    fact = verifier.verify(cand, page_text=evidence, vocab=VOCAB, report_date="2027-03-05")
    return sorted(VALUE_REASONS & set(fact["metadata"].get("unverified_reasons", [])))


OK: list = []
NO_VALUE = ["value_not_in_evidence"]
NO_COMPARATOR = ["comparator_not_in_evidence"]
GLU = {"label": "Glukóza", "unit": "mmol/l"}
CREA = {"label": "Kreatinín", "unit": "µmol/l"}
CRP = {"label": "CRP", "unit": "mg/l"}
PH = {"label": "U-pH", "ref": "5.0 - 8.0"}
UREA = {"label": "Urea", "unit": "mmol/l", "ref": "2.1 - 7.6"}
TEXT = {"label": "Bielkoviny", "unit": "arb"}

# evidence, claimed value, candidate fields, expected value reasons
ROWS = [
    # accepted: a leader of two or more dots, a single "…", spaced dots
    ("Glukóza ..........5.2 mmol/l", "5.2", GLU, OK),
    ("Glukóza..........5.2 mmol/l", "5.2", GLU, OK),
    ("Glukóza ..5,2 mmol/l", "5.2", GLU, OK),
    ("U-pH ...6.5 {5.0 - 8.0}", "6.5", PH, OK),
    ("U-pH ...6.5 {5.0 - 8.0}", "6.5", {"label": "U-pH"}, OK),  # range not returned
    ("Kreatinín …71 µmol/l", "71", CREA, OK),
    ("Kreatinín ……71 µmol/l", "71", CREA, OK),
    ("Kreatinín . . . . 71 µmol/l", "71", CREA, OK),
    ("Kreatinín . . . .71 µmol/l", "71", CREA, OK),
    ("Sodík 2 ....141 mmol/l", "141", {"label": "Sodík", "unit": "mmol/l"}, OK),
    # the reference range is not the value
    ("U-pH ...6.5 {5.0 - 8.0}", "5.0", PH, NO_VALUE),
    ("U-pH ...6.5 {5.0 - 8.0}", "8.0", PH, NO_VALUE),
    # rejected exactly as before: digit, comma or ONE dot right before the value
    ("Sodík 15.2", "5.2", {"label": "Sodík"}, NO_VALUE),
    ("odber 1.5.2027 hodnota 3.1", "5.2027", {"label": "hodnota"}, NO_VALUE),
    ("odber 1.5.2027 hodnota 3.1", "5.2", {"label": "hodnota"}, NO_VALUE),
    ("Počet 1,234.5", "234.5", {"label": "Počet"}, NO_VALUE),
    ("Hodnota .5.2", "5.2", {"label": "Hodnota"}, NO_VALUE),
    ("Hodnota ,5.2", "5.2", {"label": "Hodnota"}, NO_VALUE),
    # a leader right after a digit is no leader ("3." may be a cut number)
    ("Hodnota 3...4.71", "4.71", {"label": "Hodnota"}, NO_VALUE),
    ("Hodnota 3. . .4.71", "4.71", {"label": "Hodnota"}, NO_VALUE),
    ("Hodnota 3. ...4.71", "4.71", {"label": "Hodnota"}, NO_VALUE),
    ("Hodnota 3.....4.71", "4.71", {"label": "Hodnota"}, NO_VALUE),
    # ... also when that digit is the end of the label (strict on purpose)
    ("Faktor 12.....4.71", "4.71", {"label": "Faktor 12"}, NO_VALUE),
    # a lone "…" after a digit blocks like "3...4.71" (verifier 2 accepted it)
    ("Hodnota 3…4.71", "4.71", {"label": "Hodnota"}, NO_VALUE),
    ("Hodnota 3…..4.71", "4.71", {"label": "Hodnota"}, NO_VALUE),
    ("Hodnota 3 …4.71", "4.71", {"label": "Hodnota"}, OK),  # not after a digit
    # comparators: found between the leader and the value, never invented
    ("CRP .....< 0.5 mg/l", "0.5", {**CRP, "comparator": "<"}, OK),
    ("CRP .....<0.5 mg/l", "0.5", {**CRP, "comparator": "<"}, OK),
    ("CRP .....< 0.5 mg/l", "0.5", {**CRP, "comparator": ">"}, NO_COMPARATOR),
    ("CRP .....0.5 mg/l", "0.5", {**CRP, "comparator": "<"}, NO_COMPARATOR),
    ("CRP .....0.5 mg/l", "0.5", CRP, OK),
    # text values: "." is no word character, so this branch never minded
    ("Bielkoviny ...neg [arb]", "neg", TEXT, OK),
    ("Bielkoviny ...pozit. [arb]", "pozit.", TEXT, OK),
    ("Bielkoviny …neg [arb]", "neg", TEXT, OK),
    ("Bielkoviny ...negat [arb]", "neg", TEXT, NO_VALUE),
    # a value printed ONLY in the label or in the reference range
    ("Faktor 38/61 .....neg", "61", {"label": "Faktor 38/61"}, NO_VALUE),
    ("Urea ...2.1 - 7.6 mmol/l", "2.1", UREA, NO_VALUE),
    ("Urea ........ {2.1 - 7.6} mmol/l", "7.6", UREA, NO_VALUE),
]


@pytest.mark.parametrize(("evidence", "value", "fields", "expected"), ROWS)
def test_value_rule_with_dot_leaders(evidence, value, fields, expected):
    assert value_reasons(evidence, value, **fields) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Glukóza ..........5.2", "Glukóza           5.2"),
        ("A …7", "A  7"),
        ("A . . . 7", "A       7"),
        ("A .5.2", "A .5.2"),  # one dot: untouched
        ("koniec vety. Nová 7", "koniec vety. Nová 7"),
        ("1.5.2027", "1.5.2027"),
        ("5. 3. 2027", "5. 3. 2027"),
        ("3...4.71", "3...4.71"),
        ("3. . .4.71", "3. . .4.71"),
    ],
)
def test_blank_dot_leaders_only_turns_leaders_into_spaces(text, expected):
    out = verifier.blank_dot_leaders(text)
    assert out == expected
    assert len(out) == len(text)
    assert all(a == b or (a in ".…" and b == " ") for a, b in zip(text, out, strict=True))


def test_a_blanked_digit_still_blocks_the_leader():
    printed = "Faktor 12.....4.71"
    rest = verifier._blank(printed, "Faktor 12")
    assert verifier.blank_dot_leaders(rest, printed) == rest  # leader follows the "2"
    assert verifier.blank_dot_leaders(rest) != rest  # which is why ``printed`` is passed


# ── end to end: a dot-leader page ───────────────────────────────────────

DAY = "5.3.2027"
LINES = [
    # label, value, comparator, unit, ref, printed line
    ("Glukóza", "5.2", None, "mmol/l", "3.9 - 5.6", "Glukóza ..........5.2 mmol/l {3.9 - 5.6}"),
    ("Kreatinín", "71", None, "µmol/l", "49 - 90", "Kreatinín ........71 µmol/l {49 - 90}"),
    ("Urea", "4,8", None, "mmol/l", "2.8 - 7.2", "Urea .............4,8 mmol/l {2.8 - 7.2}"),
    ("Sodík", "141", None, "mmol/l", "136 - 145", "Sodík ............141 mmol/l {136 - 145}"),
    ("CRP", "0.5", "<", "mg/l", "0 - 5", "CRP ..............< 0.5 mg/l {0 - 5}"),
    ("U-pH", "6.5", None, None, "5.0 - 8.0", "U-pH ...6.5 {5.0 - 8.0}"),
]
PAGE = "\n".join([f"Laboratórny výsledok, vzorka zo dňa {DAY}", *(row[-1] for row in LINES)])


def _candidates():
    return [
        {"label_as_printed": label, "value": value, "comparator": comparator,
         "unit_as_printed": unit, "ref_range_as_printed": ref, "flag_as_printed": None,
         "date_as_printed": DAY, "date_role": "sample", "evidence": line, "page_number": 1}
        for label, value, comparator, unit, ref, line in LINES
    ]  # fmt: skip


def _old_value_found(cand) -> bool:
    """The value lookup of verifier 2: label and range blanked, no leaders."""
    rest = verifier._blank(verifier.ws(cand["evidence"]), cand["label_as_printed"])
    rest = verifier._blank(rest, cand["ref_range_as_printed"])
    return bool(verifier._value_pattern(cand["value"]).search(rest))


def test_every_fact_of_a_dot_leader_page_is_machine():
    facts = [verifier.verify(c, page_text=PAGE, vocab=VOCAB) for c in _candidates()]
    assert len(facts) == 6
    for f in facts:
        assert f["verification"] == "machine", (f["label"], f["metadata"])
        assert f["metadata"]["verifier"] == verifier.VERIFIER_VERSION
        assert f["occurred_at"][:10] == "2027-03-05"
    assert [f["code"] for f in facts] == ["GLU", "CREA", "UREA", "NA", "CRP", "U_PH"]
    assert [f["value_num"] for f in facts] == [5.2, 71.0, 4.8, 141.0, 0.5, 6.5]
    assert facts[4]["metadata"]["comparator"] == "<"


def test_the_same_page_failed_under_the_old_value_lookup():
    # verifier 2 looked the value up without the leader step; only the CRP
    # value, which a comparator and a space separate from the dots, was found
    assert [_old_value_found(c) for c in _candidates()] == [False] * 4 + [True, False]
    # the numeric pattern itself is unchanged: glued to a dot is no match
    old = re.compile(r"(?<![\d.,])5[.,]20*(?!\d)")
    assert verifier._value_pattern("5.2").pattern == old.pattern
    assert not old.search("Glukóza ..........5.2 mmol/l")


def test_version_is_at_least_three():
    # v3 (#833) introduced the leader rule, v4 (#814) the label normalisation
    assert verifier.VERIFIER_VERSION >= 3


# ── clinical verifier: same lookbehind, NOT changed (separate decision) ──


def test_a_lone_ellipsis_after_a_digit_blocked_nothing_in_verifier_two():
    # the tightening is the lab verifier's keyword; the shared default pattern
    # (which the clinical verifier uses) is the one of main
    assert verifier._value_pattern("4.71").search("Hodnota 3…4.71")
    assert not verifier._value_pattern("4.71", ellipsis_blocks=True).search("Hodnota 3…4.71")
    assert verifier._value_pattern("71", ellipsis_blocks=True).search("Kreatinín  71")


@pytest.mark.parametrize(
    ("numbers", "evidence", "expected"),
    [
        ("85", "Liek A dávka .....85 mg i.v.", False),  # the lock: no leader rule here
        ("85", "Liek A dávka ..85 mg i.v.", False),
        ("85", "Liek A dávka 85 mg i.v.", True),
        ("85", "Liek A dávka …85 mg i.v.", True),  # as on main
    ],
)
def test_clinical_numbers_do_not_take_the_leader_rule(numbers, evidence, expected):
    assert verifier_clinical._numbers_in(numbers, evidence) is expected


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("ložisko S6 .....23 x 18 mm", False),  # the lock
        ("ložisko S6 23 x 18 mm", True),
    ],
)
def test_clinical_size_with_unit_does_not_take_the_leader_rule(evidence, expected):
    assert verifier_clinical._size_with_unit_printed("23 x 18", "mm", evidence) is expected


def test_clinical_verifier_version_is_unchanged():
    assert verifier_clinical.CLINICAL_VERIFIER_VERSION == 5
    assert "blank_dot_leaders" not in vars(verifier_clinical)
