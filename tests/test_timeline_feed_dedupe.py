"""Read-time dedupe in timeline and dashboard_feed (#802).

The same lab panel printed in two documents shows once, exactly as
fact_series keeps it; every hidden row is listed with its provenance; stored
rows are never touched; dashboard_feed paging stays consistent.

Synthetic data only: invented values, dates in 2027 or later. Units are
named constants so no line carries a value next to a unit.
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from oncofiles.tools import clinical_facts as cf
from tests.helpers import ERIKA_UUID
from tests.test_clinical_facts import _Ctx
from tests.test_dashboard_feed import PER_NL, _doc, _fact, _feed, _put

YEAR = 2028
DAY = date(YEAR, 4, 6)
TWIN_DAYS = 4
PANEL = (("WBC", 13.1), ("HGB", 91.0), ("PLT", 512.0), ("NEUT", 9.9), ("CRP", 41.0))


async def _fhir(db) -> dict[int, str]:
    """clinical_records row id → stored stable fact id (active rows)."""
    async with db.db.execute(
        "SELECT id, fhir_id FROM clinical_records WHERE deleted_at IS NULL"
    ) as cur:
        return {r["id"]: r["fhir_id"] for r in await cur.fetchall()}


async def _active_count(db) -> int:
    async with db.db.execute(
        "SELECT COUNT(*) FROM clinical_records WHERE deleted_at IS NULL"
    ) as cur:
        return (await cur.fetchone())[0]


def _panel(day: date, **over) -> list[dict]:
    over = {"flag": "H", **over}
    return [_fact(code, day.isoformat(), value, PER_NL, **over) for code, value in PANEL]


async def _twin_panels(db, day: date = DAY, first: int = 1) -> tuple[int, int]:
    """Two documents carrying the same flagged panel (a discharge summary
    quoting the lab sheet on the same day)."""
    a = await _doc(db, ERIKA_UUID, first, day)
    b = await _doc(db, ERIKA_UUID, first + 1, day)
    await _put(db, ERIKA_UUID, a.id, _panel(day))
    await _put(db, ERIKA_UUID, b.id, _panel(day, page_number=3))
    return a.id, b.id


async def _series_pairs(db, codes) -> set[tuple[str, str]]:
    """(hidden, kept) stable ids as fact_series decides them."""
    out = json.loads(await cf.fact_series(_Ctx(db), codes=list(codes)))
    ids = await _fhir(db)
    return {(ids[d["removed"]["fact_id"]], ids[d["kept"]["fact_id"]]) for d in out["duplicates"]}


async def _pages(db, **kw) -> list[dict]:
    pages, cursor = [], None
    while True:
        page = await _feed(db, cursor=cursor, **kw)
        pages.append(page)
        cursor = page["next_cursor"]
        if not cursor:
            return pages


# ── dashboard_feed ──


async def test_feed_shows_a_twin_panel_once_and_lists_the_hidden_one(db):
    a, b = await _twin_panels(db)
    before = await _active_count(db)
    out = await _feed(db)
    assert sorted(f["code"] for f in out["facts"]) == sorted(c for c, _ in PANEL)
    assert {f["document_id"] for f in out["facts"]} == {a}
    assert len(out["duplicates"]) == len(PANEL)
    for d in out["duplicates"]:
        assert d["reason"] == "exact" and d["document_id"] == b and d["page"] == 3
        assert d["kept"]["document_id"] == a and d["kept"]["page"] == 1
        assert d["kept"]["at"] == d["at"] == DAY.isoformat()
    # both documents are listed, so the hidden row's provenance opens too
    assert {doc["id"] for doc in out["documents"]} == {a, b}
    assert await _active_count(db) == before  # read time only: nothing merged or deleted


async def test_feed_keeps_the_same_row_and_id_as_fact_series(db):
    await _twin_panels(db)
    out = await _feed(db)
    feed_pairs = {(d["id"], d["kept"]["id"]) for d in out["duplicates"]}
    assert feed_pairs == await _series_pairs(db, [c for c, _ in PANEL])
    stored = set((await _fhir(db)).values())
    assert {f["id"] for f in out["facts"]} <= stored  # the kept fact keeps its stored id
    assert {d["kept"]["id"] for d in out["duplicates"]} == {f["id"] for f in out["facts"]}


async def test_feed_prefers_the_machine_row_over_an_unverified_twin(db):
    a = await _doc(db, ERIKA_UUID, 1, DAY)
    b = await _doc(db, ERIKA_UUID, 2, DAY)
    await _put(db, ERIKA_UUID, a.id, _panel(DAY, verification="unverified"))
    await _put(db, ERIKA_UUID, b.id, _panel(DAY))
    out = await _feed(db)
    assert {f["document_id"] for f in out["facts"]} == {b.id}
    assert {f["verification"] for f in out["facts"]} == {"machine"}
    assert {(d["id"], d["kept"]["id"]) for d in out["duplicates"]} == await _series_pairs(
        db, [c for c, _ in PANEL]
    )


async def test_feed_without_duplicates_is_unchanged(db):
    a = await _doc(db, ERIKA_UUID, 1, DAY)
    b = await _doc(db, ERIKA_UUID, 2, DAY)
    await _put(db, ERIKA_UUID, a.id, _panel(DAY))
    # same codes and day, different values: two measurements, nothing hidden
    other = [_fact(c, DAY.isoformat(), v + 1, PER_NL) for c, v in PANEL]
    await _put(db, ERIKA_UUID, b.id, other)
    out = await _feed(db)
    assert len(out["facts"]) == 2 * len(PANEL) and out["duplicates"] == []


async def test_non_lab_kinds_are_never_deduped(db):
    a = await _doc(db, ERIKA_UUID, 1, DAY)
    b = await _doc(db, ERIKA_UUID, 2, DAY)
    for doc in (a, b):
        variant = _fact("KRAS", DAY.isoformat(), None, None, category="genetic_variant",
                        value_text="G12V")  # fmt: skip
        await _put(db, ERIKA_UUID, doc.id, [variant])
    out = await _feed(db, kinds=["genetic_variant"])
    assert len(out["facts"]) == 2 and out["duplicates"] == []
    tl = json.loads(await cf.timeline(_Ctx(db), kinds=["genetic_variant"]))
    assert len([r for r in tl["rows"] if r["entity"] == "fact"]) == 2
    assert tl["duplicates"] == []


async def test_feed_range_and_quote_twins_follow_dedupe(db):
    draw = date(YEAR, 9, 4)
    lab = await _doc(db, ERIKA_UUID, 1, draw)
    await _put(db, ERIKA_UUID, lab.id, [
        _fact("WBC", draw.isoformat(), 7.7, PER_NL),
        _fact("PLT", draw.isoformat(), 288.0, PER_NL),
        _fact("CRP", draw.isoformat(), 12.0, PER_NL),
    ])  # fmt: skip
    # a summary quoting the draw under a printed range (the value sits at the end)
    end = draw + timedelta(days=3)
    window = {"date_range_start": (draw - timedelta(days=2)).isoformat(),
              "date_range_end": end.isoformat(), "date_source": "printed_range",
              "date_precision": "range"}  # fmt: skip
    ranged = await _doc(db, ERIKA_UUID, 2, end)
    await _put(db, ERIKA_UUID, ranged.id,
               [_fact("CRP", end.isoformat(), 12.0, PER_NL, metadata=window)])  # fmt: skip
    # a later report quoting two values of the draw on its own date
    report_day = draw + timedelta(days=20)
    report = await _doc(db, ERIKA_UUID, 3, report_day)
    quote = {"date_source": "report"}
    await _put(db, ERIKA_UUID, report.id, [
        _fact("WBC", report_day.isoformat(), 7.7, PER_NL, metadata=quote),
        _fact("PLT", report_day.isoformat(), 288.0, PER_NL, metadata=quote),
    ])  # fmt: skip
    out = await _feed(db)
    assert {f["document_id"] for f in out["facts"]} == {lab.id}
    reasons = sorted((d["code"], d["reason"], d["document_id"]) for d in out["duplicates"])
    assert reasons == [
        ("CRP", "range", ranged.id),
        ("PLT", "quote", report.id),
        ("WBC", "quote", report.id),
    ]
    assert {(d["id"], d["kept"]["id"]) for d in out["duplicates"]} == await _series_pairs(
        db, ["WBC", "PLT", "CRP"]
    )
    # a `since` after the draw leaves the quotes hidden: the dedupe never sees
    # only a window of the patient's rows
    late = await _feed(db, since=end.isoformat())
    assert late["facts"] == [] and len(late["duplicates"]) == 3


# ── paging across the boundary ──


async def _many_twins(db) -> None:
    """Twin panels on several days plus single values in between, so hidden and
    kept rows of one measurement land on either side of many page breaks."""
    for i in range(TWIN_DAYS):
        day = DAY + timedelta(days=7 * i)
        await _twin_panels(db, day, first=10 * i + 1)
        single = await _doc(db, ERIKA_UUID, 10 * i + 5, day + timedelta(days=1))
        await _put(db, ERIKA_UUID, single.id,
                   [_fact("CEA", (day + timedelta(days=1)).isoformat(), 2.0 + i, "ug/L",
                          category="tumor_marker")])  # fmt: skip


def _flatten(pages: list[dict]) -> tuple[list[dict], list[dict]]:
    facts = [f for p in pages for f in p["facts"]]
    dups = [d for p in pages for d in p["duplicates"]]
    return facts, dups


# odd page sizes, so page breaks fall all over the stream
PAGE_LIMITS = [k for k in range(len(PANEL) + TWIN_DAYS * TWIN_DAYS) if k % 2]


@pytest.mark.parametrize("limit", PAGE_LIMITS)
async def test_paging_by_limit_never_skips_or_repeats(db, limit):
    await _many_twins(db)
    [whole] = await _pages(db)
    facts, dups = _flatten(await _pages(db, limit=limit))
    assert facts == whole["facts"] and dups == whole["duplicates"]
    panels = TWIN_DAYS * len(PANEL)
    assert len(facts) == panels + TWIN_DAYS
    assert len(dups) == panels
    ids = [f["id"] for f in facts] + [d["id"] for d in dups]
    assert len(ids) == len(set(ids))  # each item on exactly one page
    assert {d["kept"]["id"] for d in dups} <= {f["id"] for f in facts}
    order = sorted(facts + dups, key=lambda x: (x["at"], x["id"]))
    paged = [x for p in await _pages(db, limit=limit) for x in p["facts"] + p["duplicates"]]
    assert sorted(paged, key=lambda x: (x["at"], x["id"])) == order


@pytest.mark.parametrize("budget", [900, 1300, 2100])
async def test_paging_by_byte_budget_never_skips_or_repeats(db, budget, monkeypatch):
    await _many_twins(db)
    [whole] = await _pages(db)
    monkeypatch.setattr(cf, "FEED_MAX_BYTES", budget)
    pages = await _pages(db)
    assert len(pages) > 2
    facts, dups = _flatten(pages)
    assert facts == whole["facts"] and dups == whole["duplicates"]
    for p in pages:  # every page lists the documents its items cite
        listed = {d["id"] for d in p["documents"]}
        cited = {f["document_id"] for f in p["facts"]} | {d["document_id"] for d in p["duplicates"]}
        assert cited <= listed


# ── timeline ──


async def test_timeline_shows_a_twin_panel_once_with_provenance(db):
    a, b = await _twin_panels(db)
    before = await _active_count(db)
    out = json.loads(await cf.timeline(_Ctx(db)))
    facts = [r for r in out["rows"] if r["entity"] == "fact"]
    assert len(facts) == len(PANEL) and {r["source_document_id"] for r in facts} == {a}
    assert out["duplicates_removed"] == len(PANEL)
    for d in out["duplicates"]:
        assert d["reason"] == "exact"
        assert (d["removed"]["document_id"], d["removed"]["page"]) == (b, 3)
        assert (d["kept"]["document_id"], d["kept"]["page"]) == (a, 1)
    # the kept row is the one fact_series keeps
    ids = await _fhir(db)
    pairs = {(ids[d["removed"]["fact_id"]], ids[d["kept"]["fact_id"]]) for d in out["duplicates"]}
    assert pairs == await _series_pairs(db, [c for c, _ in PANEL])
    assert {r["entity_id"] for r in facts} == {d["kept"]["fact_id"] for d in out["duplicates"]}
    # both documents still appear as document rows; nothing stored changed
    assert {r["entity_id"] for r in out["rows"] if r["entity"] == "document"} == {a, b}
    assert await _active_count(db) == before


async def test_timeline_limit_counts_shown_rows_only(db):
    """The limit counts shown rows; hidden rows are listed up to the last shown day."""
    await _twin_panels(db)
    out = json.loads(await cf.timeline(_Ctx(db), kinds=["lab"], limit=len(PANEL)))
    assert len(out["rows"]) == len(PANEL) and out["duplicates_removed"] == len(PANEL)


async def test_timeline_never_hides_a_flagged_value_behind_an_unflagged_twin(db):
    a = await _doc(db, ERIKA_UUID, 1, DAY)
    b = await _doc(db, ERIKA_UUID, 2, DAY)
    await _put(db, ERIKA_UUID, a.id, _panel(DAY, flag=None))  # kept by dedupe, no flag
    await _put(db, ERIKA_UUID, b.id, _panel(DAY))
    out = json.loads(await cf.timeline(_Ctx(db), kinds=["lab"]))
    assert {r["source_document_id"] for r in out["rows"]} == {b.id}
    assert out["duplicates"] == []


async def test_timeline_without_duplicates_is_unchanged(db):
    a = await _doc(db, ERIKA_UUID, 1, DAY)
    b = await _doc(db, ERIKA_UUID, 2, DAY + timedelta(days=1))
    await _put(db, ERIKA_UUID, a.id, _panel(DAY))
    await _put(db, ERIKA_UUID, b.id, _panel(DAY + timedelta(days=1)))
    out = json.loads(await cf.timeline(_Ctx(db), kinds=["lab"]))
    assert len(out["rows"]) == 2 * len(PANEL) and out["duplicates"] == []
