"""#672 items 3 and 4.

3. A value printed without a unit (a report quoting a lab result) takes, at
   READ time only, the unit the same lab printed for the same code in its
   trusted facts (``unit_source: inferred_same_lab`` + ``unit_document_id``)
   in dashboard_feed and fact_series; a printed unit is ``printed``. Never
   stored, never used to convert a value or recompute a flag, never from an
   unverified fact, another lab, an unknown lab or another patient, and never
   when the lab printed the code in more than one unit.
4. The calculated transferrin saturation / TIBC labels have their own codes
   (``TSAT_CALC`` / ``TIBC_CALC``, migration 105); the eGFR / EGFR gene
   collision was already solved by ``EGFR_GENE`` (#815).

Synthetic data only: invented values, dates in 2027 or later, invented
institution codes. Mirrors tests/test_899_feed_ref_source.py.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from oncofiles.curated import indexer
from oncofiles.curated.facts_rules import DERIVED_CODES, Vocabulary, parse_okb
from oncofiles.tools import clinical_facts as cf
from tests.helpers import PRIMARY_TEST_UUID, make_doc
from tests.test_clinical_facts import _Ctx
from tests.test_dashboard_feed import _caller, _fact, _put

YEAR = 2027
PER_NL = "10^9/L"
LAB_A, LAB_B = "LabGama", "LabDelta"
REF = {"ref_low": 4.0, "ref_high": 10.0}
HGB_DL_REF = {"ref_low": 12.0, "ref_high": 16.0}  # printed next to a g/dl value
MIGRATION = (
    Path(__file__).resolve().parent.parent / "migrations" / "105_derived_tsat_tibc_codes.sql"
)


@pytest.fixture(autouse=True)
def caregiver():
    yield from _caller(False)


def _day(month: int, day: int) -> str:
    return date(YEAR, month, day).isoformat()


async def _doc(db, n: int, institution: str | None, category: str = "labs", pid=None) -> int:
    doc = make_doc(
        file_id=f"unit672_{n}",
        filename=f"Doc672_{n}.pdf",
        original_filename=f"Doc672_{n}.pdf",
        document_date=date(YEAR, 1, 1),
        gdrive_id=f"gd672x{n}",
        institution=institution,
        category=category,
    )
    return (await db.insert_document(doc, patient_id=pid or PRIMARY_TEST_UUID)).id


def _bare(code: str, day: str, value: float, canonical: str = PER_NL, **over) -> dict:
    """A value quoted in a report: no printed unit, no range."""
    return _fact(code, day, value, None, unit_canonical=canonical, value_canonical=value, **over)


def _hgb_dl(day: str, value: float, **over) -> dict:
    """HGB printed in g/dl, stored with its canonical g/L value."""
    return _fact("HGB", day, value, "g/dl", unit_canonical="g/L",
                 value_canonical=round(value * 10, 6), **HGB_DL_REF, **over)  # fmt: skip


async def _feed(db, **kw) -> dict:
    return json.loads(await cf.dashboard_feed(_Ctx(db), **kw))


async def _series(db, codes: list[str], **kw) -> dict:
    return json.loads(await cf.fact_series(_Ctx(db), codes=codes, **kw))


def _one(out: dict, document_id: int, code: str = "WBC") -> dict:
    [fact] = [f for f in out["facts"] if f["document_id"] == document_id and f["code"] == code]
    return fact


UNIT_KEYS = {"unit_source", "unit_document_id"}


# ── item 3: dashboard_feed ──


async def test_report_value_takes_the_nearest_unit_of_the_same_lab(db):
    old_sheet, new_sheet = await _doc(db, 1, LAB_A), await _doc(db, 2, LAB_A)
    report = await _doc(db, 3, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, old_sheet, [_fact("WBC", _day(1, 10), 5.1, "G/l")])
    await _put(db, PRIMARY_TEST_UUID, new_sheet, [_fact("WBC", _day(6, 10), 6.2, "10^9/l")])
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("WBC", _day(5, 20), 2.9, flag=None)])
    out = await _feed(db)
    fact = _one(out, report)
    # "G/l" and "10^9/l" are one unit for a raw number: no ambiguity, nearest donor wins
    assert fact["unit_source"] == "inferred_same_lab" and fact["unit_document_id"] == new_sheet
    assert fact["unit"] == "10^9/l" and fact["value"] == 2.9
    assert "flag" not in fact  # never derived from an inferred unit
    assert new_sheet in {d["id"] for d in out["documents"]}
    printed = _one(out, new_sheet)
    assert printed["unit_source"] == "printed" and "unit_document_id" not in printed
    # the stored row keeps what was printed: no unit
    async with db.db.execute(
        "SELECT unit FROM clinical_records WHERE source_document_id = ?", (report,)
    ) as cur:
        assert [tuple(r) for r in await cur.fetchall()] == [(None,)]


@pytest.mark.parametrize(
    "donor_inst, taker_inst, donor_over",
    [
        (LAB_A, LAB_B, {}),  # another lab
        (None, None, {}),  # institution unknown on both: no offer
        (LAB_A, LAB_A, {"verification": "unverified"}),  # a misread unit must not spread
    ],
)
async def test_no_unit_from_another_lab_unknown_lab_or_unverified_donor(
    db, donor_inst, taker_inst, donor_over
):
    sheet = await _doc(db, 1, donor_inst)
    report = await _doc(db, 2, taker_inst, category="report")
    await _put(db, PRIMARY_TEST_UUID, sheet, [_fact("WBC", _day(3, 1), 5.1, PER_NL, **donor_over)])
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("WBC", _day(3, 9), 2.9)])
    fact = _one(await _feed(db), report)
    assert not UNIT_KEYS & set(fact)


async def test_two_units_at_one_lab_give_no_unit_and_no_range(db):
    sheet_l, sheet_dl = await _doc(db, 1, LAB_A), await _doc(db, 2, LAB_A)
    report = await _doc(db, 3, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, sheet_l,
               [_fact("HGB", _day(2, 1), 131.0, "g/L", ref_low=120.0, ref_high=160.0)])  # fmt: skip
    await _put(db, PRIMARY_TEST_UUID, sheet_dl, [_hgb_dl(_day(3, 1), 12.9)])
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("HGB", _day(3, 5), 12.4, canonical="g/L")])
    fact = _one(await _feed(db), report, "HGB")
    # the number's scale is unknown: neither a unit nor a same-lab range is offered
    assert not UNIT_KEYS & set(fact)
    assert not {"ref_low", "ref_high", "ref_source", "ref_document_id"} & set(fact)


async def test_unit_in_another_scale_never_converts_the_value_nor_takes_a_canonical_range(db):
    sheet = await _doc(db, 1, LAB_A)
    report = await _doc(db, 2, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, sheet, [_hgb_dl(_day(3, 1), 12.9)])
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("HGB", _day(3, 5), 12.4, canonical="g/L")])
    out = await _feed(db)
    fact = _one(out, report, "HGB")
    assert fact["unit"] == "g/dl" and fact["unit_source"] == "inferred_same_lab"
    assert fact["value"] == 12.4  # as printed, never multiplied by the g/dl factor
    # the donor's range is converted to g/L in the feed: never offered to a g/dl number
    assert not {"ref_low", "ref_high", "ref_source"} & set(fact) and "flag" not in fact
    donor = _one(out, sheet, "HGB")
    assert donor["unit"] == "g/L" and donor["unit_source"] == "printed"


async def test_text_value_inside_one_document_and_a_printed_unit_take_nothing(db):
    sheet = await _doc(db, 1, LAB_A)
    await _put(db, PRIMARY_TEST_UUID, sheet, [
        _fact("WBC", _day(3, 1), 5.1, PER_NL),
        _fact("PLT", _day(3, 1), None, None, value_text="zhluky", value_canonical=None),
    ])  # fmt: skip
    out = await _feed(db)
    assert not UNIT_KEYS & set(_one(out, sheet, "PLT"))  # a text value takes no unit
    assert _one(out, sheet)["unit_source"] == "printed"


async def test_every_page_gives_the_same_unit_as_one_page(db):
    sheet = await _doc(db, 1, LAB_A)
    report = await _doc(db, 2, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, sheet, [_fact("WBC", _day(1, 2), 5.1, PER_NL, **REF)])
    await _put(
        db, PRIMARY_TEST_UUID, report, [_bare("WBC", _day(m, 5), 3.0 + m / 10) for m in range(2, 8)]
    )
    whole = (await _feed(db))["facts"]
    paged, cursor = [], None
    while True:
        page = await _feed(db, limit=2, cursor=cursor)
        paged += page["facts"]
        if any(f.get("unit_document_id") for f in page["facts"]):
            assert sheet in {d["id"] for d in page["documents"]}
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == whole
    assert [f["unit_source"] for f in whole] == ["printed"] + ["inferred_same_lab"] * 6
    # the unit is the canonical one here, so the same lab's range still comes along (#899)
    assert [f["ref_source"] for f in whole] == ["printed"] + ["same_lab_other_doc"] * 6
    late = await _feed(db, since=_day(4, 1))
    assert {f["unit_document_id"] for f in late["facts"]} == {sheet}
    assert sheet in {d["id"] for d in late["documents"]}


async def test_a_unit_never_crosses_from_another_patient(db):
    from tests.test_clinical_facts import BOB_UUID
    from tests.test_dashboard_feed import _add_bob

    await _add_bob(db)
    their_sheet = await _doc(db, 9, LAB_A, pid=BOB_UUID)
    await _put(db, BOB_UUID, their_sheet, [_fact("WBC", _day(3, 1), 5.1, PER_NL)])
    report = await _doc(db, 1, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("WBC", _day(3, 9), 2.9)])
    out = await _feed(db)
    [fact] = out["facts"]
    assert not UNIT_KEYS & set(fact)
    assert [d["id"] for d in out["documents"]] == [report]


# ── item 3: fact_series ──


async def test_fact_series_marks_units_and_finds_a_donor_outside_the_window(db):
    sheet = await _doc(db, 1, LAB_A)
    report = await _doc(db, 2, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, sheet, [_fact("WBC", _day(1, 10), 5.1, PER_NL)])
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("WBC", _day(5, 20), 2.9)])
    out = await _series(db, ["WBC"])
    by_doc = {p["source"]["document_id"]: p for p in out["series"]["WBC"]["values"]}
    assert by_doc[sheet]["unit_source"] == "printed" and "unit_document_id" not in by_doc[sheet]
    inferred = by_doc[report]
    assert inferred["unit_source"] == "inferred_same_lab" and inferred["unit"] == PER_NL
    assert inferred["unit_document_id"] == sheet and inferred["value"] == 2.9
    assert out["series"]["WBC"]["latest_any"]["unit_source"] == "inferred_same_lab"
    windowed = await _series(db, ["WBC"], date_from=_day(5, 1))
    [point] = windowed["series"]["WBC"]["values"]
    assert point["unit_document_id"] == sheet  # the donor is outside the window


async def test_fact_series_keeps_an_off_scale_point_out_of_the_statistics(db):
    sheet = await _doc(db, 1, LAB_A)
    report = await _doc(db, 2, LAB_A, category="report")
    await _put(db, PRIMARY_TEST_UUID, sheet, [_hgb_dl(_day(3, 1), 12.9)])
    await _put(db, PRIMARY_TEST_UUID, report, [_bare("HGB", _day(3, 5), 12.4, canonical="g/L")])
    s = (await _series(db, ["HGB"]))["series"]["HGB"]
    report_point = next(p for p in s["values"] if p["source"]["document_id"] == report)
    assert report_point["unit"] == "g/dl" and report_point["value"] == 12.4
    assert s["stats_excluded_inferred_unit"] == 1
    assert s["stats"]["min"] == s["stats"]["max"] == 129.0  # only the g/L-converted value


# ── item 4: TSAT / TIBC calculated codes; EGFR ──


async def test_calculated_tsat_tibc_labels_have_their_own_codes(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    for label, code in [
        ("Sat. transferínu", "TSAT_CALC"),
        ("Sat.transferínu", "TSAT_CALC"),
        ("CVK výp. z TRF", "TIBC_CALC"),
        ("Saturácia transferínu", "TSAT"),
        ("Saturácia železa v sére", "TSAT"),
        ("Celková väzobná kapacita železa", "TIBC"),
        ("Celk.väzobná kapacita železa", "TIBC"),
        ("CVK", "TIBC"),
    ]:
        assert vocab.match(label)["code"] == code, label
    assert {"TSAT_CALC", "TIBC_CALC"} <= DERIVED_CODES  # never corroboration evidence
    assert indexer.INDEX_VERSION >= 27  # stored facts move only through a re-index


async def test_migration_105_is_idempotent_and_keeps_every_other_alias(db):
    async def aliases() -> dict[str, list[str]]:
        return {r["code"]: r["aliases"] for r in await db.fact_code_vocabulary()
                if r["code"] in {"TSAT", "TIBC", "TSAT_CALC", "TIBC_CALC"}}  # fmt: skip

    before = await aliases()
    assert "Sat.transferínu" not in before["TSAT"] and "CVK výp. z TRF" not in before["TIBC"]
    assert {"Saturácia transferínu", "Saturácia železa", "TSAT"} <= set(before["TSAT"])
    assert {"Celková väzobná kapacita železa", "CVK", "TIBC"} <= set(before["TIBC"])
    await db.db.executescript(MIGRATION.read_text())
    assert await aliases() == before


async def test_one_draw_with_both_tsat_readings_gives_two_codes(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    page = (
        "Odobrané: 14.07.2027 07:40\n"
        "Test                                Hodnota    Jednotky    Ref. medze\n"
        "Saturácia transferínu v sére        31.0       %           20.0 - 50.0     /\n"
        "Sat. transferínu                    30.5       %           20.0 - 50.0     /\n"
    )
    by = {f["code"]: f for f in parse_okb([page], vocab)}
    assert by["TSAT"]["value_num"] == 31.0 and by["TSAT_CALC"]["value_num"] == 30.5
    assert by["TSAT"]["occurred_at"] == by["TSAT_CALC"]["occurred_at"]


async def test_egfr_collision_is_already_solved_by_the_gene_code(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    assert vocab.match("EGFR")["code"] == "EGFR"  # the eGFR lab code
    assert vocab.match_biomarker("EGFR")["code"] == "EGFR_GENE"  # #815, biomarker side only
