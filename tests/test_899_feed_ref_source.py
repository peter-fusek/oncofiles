"""dashboard_feed: where a reference range comes from (#899).

A range is only ever one a lab printed: next to the value (``printed``), for
the very same measurement in the duplicate the feed hides
(``same_result_other_doc``), or by the same lab for the same code and unit in
another document (``same_lab_other_doc``). Synthetic data only: invented
values, dates in 2027 or later, invented institution codes.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from oncofiles.tools import clinical_facts as cf
from tests.helpers import ERIKA_UUID, make_doc
from tests.test_clinical_facts import _Ctx
from tests.test_dashboard_feed import _caller, _fact, _put

YEAR = 2027
PER_NL = "10^9/L"
OTHER_UNIT = "tis/ul"  # a printed unit with no conversion
LAB_A, LAB_B = "LabAlfa", "LabBeta"
OLD_REF = {"ref_low": 3.5, "ref_high": 9.5}
NEW_REF = {"ref_low": 4.0, "ref_high": 10.0}
SLUG = "q1b"


@pytest.fixture(autouse=True)
def caregiver():
    yield from _caller(False)


def _day(month: int, day: int) -> str:
    return date(YEAR, month, day).isoformat()


async def _doc(db, n: int, institution: str | None, category: str = "labs") -> int:
    doc = make_doc(
        file_id=f"ref899_{n}",
        filename=f"Doc899_{n}.pdf",
        original_filename=f"Doc899_{n}.pdf",
        document_date=date(YEAR, 1, 1),
        gdrive_id=f"gd899x{n}",
        institution=institution,
        category=category,
    )
    return (await db.insert_document(doc, patient_id=ERIKA_UUID)).id


def _bare(code: str, day: str, value: float, **over) -> dict:
    """A value from an inline list: no printed unit, no range."""
    return _fact(code, day, value, None, unit_canonical=PER_NL, value_canonical=value, **over)


async def _feed(db, **kw) -> dict:
    return json.loads(await cf.dashboard_feed(_Ctx(db), **kw))


def _one(out: dict, document_id: int, code: str = "WBC") -> dict:
    [fact] = [f for f in out["facts"] if f["document_id"] == document_id and f["code"] == code]
    return fact


async def test_own_range_is_printed_and_names_no_other_document(db):
    sheet = await _doc(db, 1, LAB_A)
    await _put(db, ERIKA_UUID, sheet, [_fact("WBC", _day(3, 1), 5.1, PER_NL, **NEW_REF)])
    fact = _one(await _feed(db), sheet)
    assert fact["ref_source"] == "printed" and "ref_document_id" not in fact
    assert (fact["ref_low"], fact["ref_high"]) == (NEW_REF["ref_low"], NEW_REF["ref_high"])


async def test_report_value_takes_the_nearest_range_of_the_same_lab(db):
    old_sheet, new_sheet = await _doc(db, 1, LAB_A), await _doc(db, 2, LAB_A)
    report = await _doc(db, 3, LAB_A, category="report")
    await _put(db, ERIKA_UUID, old_sheet, [_fact("WBC", _day(1, 10), 5.1, PER_NL, **OLD_REF)])
    await _put(db, ERIKA_UUID, new_sheet, [_fact("WBC", _day(6, 10), 6.2, PER_NL, **NEW_REF)])
    await _put(db, ERIKA_UUID, report, [_bare("WBC", _day(5, 20), 2.9)])
    out = await _feed(db)
    fact = _one(out, report)
    assert fact["ref_source"] == "same_lab_other_doc" and fact["ref_document_id"] == new_sheet
    assert (fact["ref_low"], fact["ref_high"]) == (NEW_REF["ref_low"], NEW_REF["ref_high"])
    assert "flag" not in fact  # never derived from a range that is not the value's own
    assert new_sheet in {d["id"] for d in out["documents"]}
    # the stored row keeps what was printed: nothing
    async with db.db.execute(
        "SELECT ref_range_low, ref_range_high FROM clinical_records WHERE source_document_id = ?",
        (report,),
    ) as cur:
        assert [tuple(r) for r in await cur.fetchall()] == [(None, None)]


@pytest.mark.parametrize(
    "donor_inst, taker_inst, donor_over",
    [
        (LAB_A, LAB_B, {}),  # another lab
        (None, None, {}),  # institution unknown on both: no offer
        (LAB_A, LAB_A, {"verification": "unverified"}),  # a misread range must not spread
    ],
)
async def test_no_offer_from_another_lab_unknown_lab_or_unverified_donor(
    db, donor_inst, taker_inst, donor_over
):
    sheet = await _doc(db, 1, donor_inst)
    report = await _doc(db, 2, taker_inst, category="report")
    await _put(
        db, ERIKA_UUID, sheet, [_fact("WBC", _day(3, 1), 5.1, PER_NL, **NEW_REF, **donor_over)]
    )
    await _put(db, ERIKA_UUID, report, [_bare("WBC", _day(3, 9), 2.9)])
    fact = _one(await _feed(db), report)
    assert not {"ref_low", "ref_high", "ref_source", "ref_document_id"} & set(fact)


async def test_no_offer_across_units_codes_or_inside_one_document(db):
    sheet = await _doc(db, 1, LAB_A)
    report = await _doc(db, 2, LAB_A, category="report")
    await _put(db, ERIKA_UUID, sheet, [
        # a range printed in a unit that does not convert to the feed's unit
        _fact("WBC", _day(3, 1), 5.1, OTHER_UNIT, value_canonical=None, **NEW_REF),
        _fact("PLT", _day(3, 1), 250.0, PER_NL, **NEW_REF),
    ])  # fmt: skip
    await _put(db, ERIKA_UUID, report, [
        _bare("WBC", _day(3, 9), 2.9),
        _fact("NEUT", _day(3, 9), 1.9, PER_NL, **OLD_REF),
        _bare("NEUT", _day(3, 2), 2.4),  # the same document's range is not "another document"
        _fact("PLT", _day(3, 9), None, PER_NL, value_text="zhluky", value_canonical=None),
    ])  # fmt: skip
    out = await _feed(db)
    assert "ref_source" not in _one(out, report, "WBC")
    neut = {f["at"]: f for f in out["facts"] if f["code"] == "NEUT"}
    assert neut[_day(3, 9)]["ref_source"] == "printed"
    assert "ref_source" not in neut[_day(3, 2)]
    assert "ref_source" not in _one(out, report, "PLT")  # a text value takes no range


async def test_hidden_duplicate_gives_its_printed_range_to_the_kept_fact(db):
    report = await _doc(db, 1, LAB_A, category="report")
    sheet = await _doc(db, 2, LAB_B)  # the lab does not matter: it is the same measurement
    day = _day(4, 2)
    await _put(db, ERIKA_UUID, report, [_fact("WBC", day, 5.1, PER_NL)])
    await _put(db, ERIKA_UUID, sheet, [_fact("WBC", day, 5.1, PER_NL, **NEW_REF)])
    out = await _feed(db)
    [fact] = out["facts"]
    [hidden] = out["duplicates"]
    assert fact["document_id"] == report and hidden["document_id"] == sheet
    assert fact["ref_source"] == "same_result_other_doc" and fact["ref_document_id"] == sheet
    assert (fact["ref_low"], fact["ref_high"]) == (NEW_REF["ref_low"], NEW_REF["ref_high"])


async def test_every_page_gives_the_same_range_as_one_page(db):
    sheet = await _doc(db, 1, LAB_A)
    report = await _doc(db, 2, LAB_A, category="report")
    await _put(db, ERIKA_UUID, sheet, [_fact("WBC", _day(1, 2), 5.1, PER_NL, **NEW_REF)])
    await _put(
        db, ERIKA_UUID, report, [_bare("WBC", _day(m, 5), 3.0 + m / 10) for m in range(2, 8)]
    )
    whole = (await _feed(db))["facts"]
    paged, cursor = [], None
    while True:
        page = await _feed(db, limit=2, cursor=cursor)
        paged += page["facts"]
        if any(f.get("ref_document_id") for f in page["facts"]):
            assert sheet in {d["id"] for d in page["documents"]}
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert paged == whole
    assert [f["ref_source"] for f in whole] == ["printed"] + ["same_lab_other_doc"] * 6
    # a window that leaves the donor's own fact out still finds its range
    late = await _feed(db, since=_day(4, 1))
    assert [f["document_id"] for f in late["facts"]] == [report] * 4
    assert {f["ref_document_id"] for f in late["facts"]} == {sheet}
    assert sheet in {d["id"] for d in late["documents"]}


async def test_a_range_never_crosses_from_another_patient(db):
    """The same lab and code at another patient is not a donor: neither the
    same-lab offer nor the duplicate carry-over, and the other patient's
    document is never named."""
    from tests.test_clinical_facts import BOB_UUID
    from tests.test_dashboard_feed import _add_bob

    await _add_bob(db)
    day = _day(3, 9)
    theirs = make_doc(
        file_id="ref899_bob", filename="Doc899_bob.pdf", original_filename="Doc899_bob.pdf",
        document_date=date(YEAR, 1, 1), gdrive_id="gd899bob", institution=LAB_A, category="labs",
    )  # fmt: skip
    their_sheet = (await db.insert_document(theirs, patient_id=BOB_UUID)).id
    # the other patient: the same lab, code, unit, day and even the same value, with a range
    await _put(db, BOB_UUID, their_sheet, [_fact("WBC", day, 2.9, PER_NL, **NEW_REF)])
    report = await _doc(db, 1, LAB_A, category="report")
    await _put(db, ERIKA_UUID, report, [_bare("WBC", day, 2.9)])
    out = await _feed(db)
    [fact] = out["facts"]
    assert fact["document_id"] == report
    assert not {"ref_low", "ref_high", "ref_source", "ref_document_id"} & set(fact)
    assert out["duplicates"] == [] and [d["id"] for d in out["documents"]] == [report]
    assert their_sheet not in {fact["document_id"], fact.get("ref_document_id")}
    # with a sheet of her own she gets her own lab's range, still never the other patient's
    own = await _doc(db, 2, LAB_A)
    await _put(db, ERIKA_UUID, own, [_fact("WBC", _day(2, 1), 5.1, PER_NL, **OLD_REF)])
    fact = _one(await _feed(db), report)
    assert fact["ref_document_id"] == own and fact["ref_high"] == OLD_REF["ref_high"]
