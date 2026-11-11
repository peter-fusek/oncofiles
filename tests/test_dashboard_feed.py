"""dashboard_feed, stable fact ids, local codes, lab tools on facts (#726).

Synthetic data only: invented values, dates in 2027 or later. Units and
reference ranges are named constants so no line carries a value next to a unit.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from oncofiles import fact_identity
from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from oncofiles.tools import lab_trends
from tests.helpers import ERIKA_UUID, make_doc
from tests.test_clinical_facts import BOB_SLUG, BOB_UUID, BORY_SIDECAR, FILED, _Ctx
from tests.test_curated_runner import LABS, FakeDrive

YEAR = 2027
PER_NL = "10^9/L"
NOU = "urn:oncofiles:local:nou"
PAGE_LIMIT_BYTES = 150_000
MIGRATIONS = Path(__file__).parent.parent / "migrations"
DOC = 7  # a document id for the pure-function tests
ERIKA_SLUG = "q1b"  # the seeded test patient (ERIKA_UUID)
WBC_REF = {"ref_low": 4.0, "ref_high": 10.0}


@pytest.fixture
def curated_env():
    d = FakeDrive()
    src = d.add(FILED, d.folder[LABS], b"%PDF")
    side = d.add(FILED + ".md", d.folder[LABS], BORY_SIDECAR.encode(), modified="rev-a")
    with (
        patch(
            "oncofiles.server._create_patient_clients",
            AsyncMock(return_value=(MagicMock(), None, None, d.root)),
        ),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: d),
    ):
        yield d, src, side


def _caller(admin: bool):
    tokens = (
        _verified_caller_is_admin.set(admin),
        _verified_caller_email.set(""),
        _verified_patient_id.set(ERIKA_UUID),
    )
    yield
    _verified_caller_is_admin.reset(tokens[0])
    _verified_caller_email.reset(tokens[1])
    _verified_patient_id.reset(tokens[2])


@pytest.fixture
def caregiver_of_erika():
    """A non-admin caller bound to Erika (a caregiver's patient token)."""
    yield from _caller(False)


@pytest.fixture
def admin_caller():
    yield from _caller(True)


async def _add_bob(db):
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


async def _feed(db, **kw) -> dict:
    return json.loads(await cf.dashboard_feed(_Ctx(db), **kw))


async def _all_pages(db, **kw) -> tuple[list[dict], list[int]]:
    facts, sizes, cursor = [], [], None
    while True:
        raw = await cf.dashboard_feed(_Ctx(db), cursor=cursor, **kw)
        sizes.append(len(raw.encode()))
        page = json.loads(raw)
        facts += page["facts"]
        cursor = page["next_cursor"]
        if not cursor:
            return facts, sizes


async def _doc(db, pid: str, n: int, day: date):
    tag = pid[-1]
    doc = make_doc(
        file_id=f"feed_{tag}_{n}",
        filename=f"Labs_{tag}_{n}.pdf",
        original_filename=f"Labs_{tag}_{n}.pdf",
        document_date=day,
        gdrive_id=f"gd{tag}x{n}",
    )
    return await db.insert_document(doc, patient_id=pid)


def _fact(code: str | None, day: str, value, unit: str | None, **over) -> dict:
    fact = {
        "category": "lab",
        "code": code,
        "label": code,
        "value_num": value,
        "unit": unit,
        "value_canonical": value,
        "unit_canonical": unit,
        "occurred_at": day,
        "page_number": 1,
        "evidence": f"{code} {value} {unit}",
        "verification": "machine",
    }
    fact.update(over)
    return fact


async def _put(db, pid: str, doc_id: int, facts: list[dict]):
    return await db.replace_document_facts(
        patient_id=pid, document_id=doc_id, facts=facts, extracted_by="rule:test"
    )


async def _rows(db, doc_id: int) -> list[dict]:
    async with db.db.execute(
        "SELECT id, code, occurred_at, fhir_id, version_id, deleted_at FROM clinical_records "
        "WHERE source_document_id = ? ORDER BY id",
        (doc_id,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _ids(patient: str, doc: int, facts: list[dict], taken=()) -> list[str]:
    return fact_identity.ids_for_new_facts(patient, doc, facts, set(taken))


# ── stable ids: pure functions ──


def test_ordinal_follows_evidence_not_input_order():
    day = date(YEAR, 3, 9).isoformat()
    a = _fact("WBC", day, 5.1, PER_NL, evidence="WBC first line")
    b = _fact("WBC", day, 5.4, PER_NL, evidence="WBC second line")
    ab, ba = _ids(ERIKA_UUID, DOC, [a, b]), _ids(ERIKA_UUID, DOC, [b, a])
    assert ab[0] != ab[1]
    assert ab == list(reversed(ba))  # each fact keeps its id


def test_key_parts_patient_document_page_and_label():
    day = date(YEAR, 3, 9).isoformat()
    base = _fact("CEA", day, 4.2, "µg/L")
    [one] = _ids(ERIKA_UUID, DOC, [base])
    assert _ids(ERIKA_UUID, DOC, [dict(base)]) == [one]
    assert _ids(BOB_UUID, DOC, [base]) != [one]
    assert _ids(ERIKA_UUID, DOC + 1, [base]) != [one]
    assert _ids(ERIKA_UUID, DOC, [{**base, "page_number": 2}]) != [one]
    assert _ids(ERIKA_UUID, DOC, [{**base, "value_num": 9.9}]) == [one]  # value not in key
    assert _ids(ERIKA_UUID, DOC, [{**base, "code": None, "label": "Marker"}]) != [one]


def test_a_range_value_keeps_its_id_wherever_it_is_dated_in_the_range():
    start = date(YEAR, 5, 1)
    mid = start + timedelta(days=4)
    end = mid + timedelta(days=4)
    meta = {"date_range_start": start.isoformat(), "date_range_end": end.isoformat()}
    at_mid = _fact("CRP", mid.isoformat(), 12.0, "mg/L", metadata=meta)
    at_end = {**at_mid, "occurred_at": end.isoformat()}
    assert _ids(ERIKA_UUID, DOC, [at_mid]) == _ids(ERIKA_UUID, DOC, [at_end])


def test_a_taken_id_moves_the_new_fact_to_the_next_ordinal():
    day = date(YEAR, 3, 9).isoformat()
    f = _fact("PLT", day, 210.0, PER_NL)
    [first] = _ids(ERIKA_UUID, DOC, [f])
    [bumped] = _ids(ERIKA_UUID, DOC, [f], taken={first})
    assert bumped != first
    assert bumped == _ids(ERIKA_UUID, DOC, [f, f])[1]


# ── stable ids: through the index ──


async def test_reindexing_the_same_document_keeps_every_fact_id(db, curated_env):
    _, src, _ = curated_env
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    before = (await _feed(db))["facts"]
    assert len(before) >= 7

    stats = await tool.index_patient(db, ERIKA_UUID, force_ids={doc.id})
    assert stats["facts"] == len(before)  # really re-extracted
    after = (await _feed(db))["facts"]
    assert [f["id"] for f in after] == [f["id"] for f in before]

    rows = await _rows(db, doc.id)
    live = [r for r in rows if r["deleted_at"] is None]
    old = [r for r in rows if r["deleted_at"] is not None]
    assert {r["fhir_id"] for r in live} == {f["id"] for f in after}  # stored at insert
    assert {r["version_id"] for r in live} == {2}
    assert {r["fhir_id"] for r in old} == {r["fhir_id"] for r in live}  # older versions


async def test_a_changed_value_keeps_its_id_as_a_new_version(db, curated_env):
    d, _, side = curated_env
    await tool.index_patient(db, ERIKA_UUID)
    wbc = next(f for f in (await _feed(db))["facts"] if f["code"] == "WBC")
    d.items[side]["content"] = BORY_SIDECAR.replace("11.42", "11.55").encode()
    d.items[side]["modified"] = "rev-b"
    await tool.index_patient(db, ERIKA_UUID)
    again = next(f for f in (await _feed(db))["facts"] if f["code"] == "WBC")
    assert again["id"] == wbc["id"] and again["value"] != wbc["value"]


async def test_rows_without_a_stored_id_read_the_id_a_reindex_will_write(db, curated_env):
    _, src, _ = curated_env
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    stored = {f["code"] or f["label"]: f["id"] for f in (await _feed(db))["facts"]}

    # rows written before #726 have no fhir_id. One is human-verified, one was
    # re-dated by corroboration (it keeps the day it was quoted on).
    wbc = next(r for r in await _rows(db, doc.id) if r["code"] == "WBC")
    meta = {"date_source": "corroborated", "quoted_on": wbc["occurred_at"][:10]}
    await db.db.execute("UPDATE clinical_records SET fhir_id = NULL")
    await db.db.execute(
        "UPDATE clinical_records SET occurred_at = ?, metadata_json = ? WHERE id = ?",
        (date(YEAR, 4, 2).isoformat(), json.dumps(meta), wbc["id"]),
    )
    await db.db.execute("UPDATE clinical_records SET verification = 'verified' WHERE code = 'HGB'")
    await db.db.commit()
    computed = {f["code"] or f["label"]: f["id"] for f in (await _feed(db))["facts"]}
    assert computed == stored

    await tool.index_patient(db, ERIKA_UUID, force_ids={doc.id})
    reindexed = {f["code"] or f["label"]: f["id"] for f in (await _feed(db))["facts"]}
    assert reindexed == stored  # WBC re-extracted at its quoted day: same id
    hgb = [r for r in await _rows(db, doc.id) if r["code"] == "HGB"]
    assert len(hgb) == 1 and hgb[0]["fhir_id"] == stored["HGB"]  # frozen, not duplicated


async def test_fhir_id_is_unique_among_active_rows_only(db):
    day = date(YEAR, 2, 3)
    doc = await _doc(db, ERIKA_UUID, 1, day)
    fact = _fact("WBC", day.isoformat(), 5.0, PER_NL)
    await _put(db, ERIKA_UUID, doc.id, [fact])
    await _put(db, ERIKA_UUID, doc.id, [fact])  # soft-delete + insert, same fact
    old, new = await _rows(db, doc.id)
    assert old["deleted_at"] and new["deleted_at"] is None
    assert old["fhir_id"] == new["fhir_id"]
    assert (old["version_id"], new["version_id"]) == (1, 2)
    with pytest.raises(sqlite3.IntegrityError):
        await db.db.execute(
            "INSERT INTO clinical_records (patient_id, record_type, source, fhir_id) "
            "VALUES (?, 'lab', 'test', ?)",
            (ERIKA_UUID, new["fhir_id"]),
        )


# ── feed content ──


async def test_feed_shape_documents_and_unverified(db, curated_env):
    _, src, _ = curated_env
    await tool.index_patient(db, ERIKA_UUID)
    raw = await cf.dashboard_feed(_Ctx(db))
    out = json.loads(raw)
    assert set(out) == {"facts", "documents", "generated_at", "next_cursor"}
    assert out["next_cursor"] is None and out["generated_at"].endswith("Z")
    assert "evidence" not in raw and "stats" not in raw
    allowed = {"id", "code", "label", "at", "at_from", "at_to", "value", "value_text", "unit",
               "ref_low", "ref_high", "flag", "verification", "document_id", "page"}  # fmt: skip
    for f in out["facts"]:
        assert {"id", "code", "at", "verification", "document_id"} <= set(f) <= allowed
        date.fromisoformat(f["at"][:10])
    plt = next(f for f in out["facts"] if f["code"] == "PLT")
    assert plt["verification"] == "unverified"  # included, labelled
    hgb = next(f for f in out["facts"] if f["code"] == "HGB")
    assert hgb["flag"] == "L" and hgb["value"] < hgb["ref_low"] < hgb["ref_high"]
    [doc] = out["documents"]
    assert doc["url"].endswith(f"/d/{src}/view") and doc["category"] == "labs"
    assert doc["institution"] == "BoryNemocnica" and doc["date"]
    assert {f["document_id"] for f in out["facts"]} == {doc["id"]}


async def test_soft_deleted_rows_and_documents_are_left_out(db):
    day = date(YEAR, 6, 1)
    at = day.isoformat()
    keep = await _doc(db, ERIKA_UUID, 1, day)
    gone = await _doc(db, ERIKA_UUID, 2, day)
    await _put(db, ERIKA_UUID, keep.id, [_fact("WBC", at, 5.0, PER_NL)])
    await _put(db, ERIKA_UUID, keep.id, [_fact("WBC", at, 5.2, PER_NL)])
    await _put(db, ERIKA_UUID, gone.id, [_fact("PLT", at, 230.0, PER_NL)])
    await db.delete_document(gone.id, patient_id=ERIKA_UUID)
    out = await _feed(db)
    assert [(f["code"], f["value"]) for f in out["facts"]] == [("WBC", 5.2)]
    assert [d["id"] for d in out["documents"]] == [keep.id]


async def test_units_ranges_and_date_ranges(db):
    day = date(YEAR, 7, 1)
    at = day.isoformat()
    doc = await _doc(db, ERIKA_UUID, 1, day)
    meta = {"date_range_start": date(YEAR, 6, 26).isoformat(), "date_range_end": at}
    hgb_ref = {"ref_low": 12.0, "ref_high": 15.5}
    crea_ref = {"ref_low": 0.5, "ref_high": 1.1}
    facts = [
        _fact(
            "HGB",
            at,
            10.1,
            "g/dl",
            value_canonical=101.0,
            unit_canonical="g/L",
            flag="L",
            **hgb_ref,
        ),  # fmt: skip
        _fact("CREA", at, 0.9, "mg/d", value_canonical=None, unit_canonical=None, **crea_ref),
        _fact("CRP", at, 18.0, "mg/L", metadata=meta),
        _fact(None, at, None, None, label="U-Krv", value_text="NEGAT"),
    ]
    await _put(db, ERIKA_UUID, doc.id, facts)
    by = {f["code"] or f["label"]: f for f in (await _feed(db))["facts"]}
    hgb, crea = by["HGB"], by["CREA"]
    assert (hgb["value"], hgb["unit"]) == (101.0, "g/L")  # canonical value …
    assert (hgb["ref_low"], hgb["ref_high"]) == (120.0, 155.0)  # … and range in that unit
    assert (crea["value"], crea["unit"]) == (0.9, "mg/d")  # not convertible: as printed
    assert crea["ref_high"] == crea_ref["ref_high"]
    assert (by["CRP"]["at_from"], by["CRP"]["at_to"]) == (meta["date_range_start"], at)
    assert "at_from" not in hgb and "at_to" not in hgb
    assert by["U-Krv"]["code"] is None and by["U-Krv"]["value_text"] == "NEGAT"
    assert "value" not in by["U-Krv"]


async def test_kinds_since_and_cursor_validation(db):
    early = date(YEAR, 1, 5)
    late = date(YEAR, 2, 5)
    doc = await _doc(db, ERIKA_UUID, 1, early)
    await _put(db, ERIKA_UUID, doc.id, [
        _fact("WBC", early.isoformat(), 5.0, PER_NL),
        _fact("CEA", late.isoformat(), 3.0, "µg/L", category="tumor_marker"),
        _fact("KRAS", late.isoformat(), None, None, category="genetic_variant", value_text="G12D"),
    ])  # fmt: skip
    assert {f["code"] for f in (await _feed(db))["facts"]} == {"WBC", "CEA"}  # default lab
    assert [f["code"] for f in (await _feed(db, since=late.isoformat()))["facts"]] == ["CEA"]
    gv = await _feed(db, kinds=["genetic_variant"])
    assert [f["code"] for f in gv["facts"]] == ["KRAS"]
    future = await _feed(db, kinds=["condition", "lesion", "treatment"])
    assert future["facts"] == [] and future["documents"] == [] and "unknown_kinds" not in future
    odd = await _feed(db, kinds=["lab", "Bogus"])
    assert odd["unknown_kinds"] == ["bogus"] and len(odd["facts"]) == 2
    with pytest.raises(ValueError, match="cursor"):
        await _feed(db, cursor="not-a-cursor")
    with pytest.raises(ValueError, match="since"):
        await _feed(db, since="yesterday")


# ── size budget ──

STEP, SPAN = 37, 900  # spread synthetic values
LOW, HIGH = 0.75, 1.5  # synthetic reference range around each value


async def _synthetic_patient(db, docs: int, per_doc: int) -> int:
    vocab = await db.fact_code_vocabulary()
    codes = sorted((r["code"], r["unit_canonical"]) for r in vocab
                   if r["category"] == "lab" and r["unit_canonical"])[:per_doc]  # fmt: skip
    start = date(YEAR, 1, 4)
    n = 0
    for i in range(docs):
        day = start + timedelta(days=i * 3)
        doc = await _doc(db, ERIKA_UUID, i, day)
        facts = []
        for j, (code, unit) in enumerate(codes):
            n += 1
            value = (100 + (n * STEP) % SPAN) / 100
            fact = _fact(code, day.isoformat(), value, unit, page_number=1 + j // 12)
            fact["ref_low"] = round(value * LOW, 2)
            fact["ref_high"] = round(value * HIGH, 2)
            fact["flag"] = "H" if j % 5 == 0 else None
            fact["verification"] = "unverified" if j % 9 == 0 else "machine"
            facts.append(fact)
        await _put(db, ERIKA_UUID, doc.id, facts)
    return n


async def test_900_fact_patient_pages_stay_under_150_kb(db):
    total = await _synthetic_patient(db, docs=30, per_doc=30)
    assert total == 900
    facts, sizes = await _all_pages(db)
    assert all(s < PAGE_LIMIT_BYTES for s in sizes), sizes
    assert len(facts) == total and len({f["id"] for f in facts}) == total
    assert facts == sorted(facts, key=lambda f: (f["at"], f["id"]))  # stable (at, id) order
    # all 900 rows would not fit in one ~150 kB page: paging must kick in
    assert len(sizes) > 1 and json.loads(await cf.dashboard_feed(_Ctx(db)))["next_cursor"]
    small, small_sizes = await _all_pages(db, limit=100)
    assert len(small_sizes) == total // 100
    assert [f["id"] for f in small] == [f["id"] for f in facts]


# ── isolation ──


async def _two_patients(db):
    await _add_bob(db)
    day = date(YEAR, 3, 3)
    at = day.isoformat()
    a = await _doc(db, ERIKA_UUID, 1, day)
    b = await _doc(db, BOB_UUID, 1, day)
    await _put(db, ERIKA_UUID, a.id, [_fact("WBC", at, 5.0, PER_NL)])
    bob_wbc = _fact("WBC", at, 5.0, PER_NL)
    bob_plt = _fact("PLT", at, 240.0, PER_NL)
    await _put(db, BOB_UUID, b.id, [bob_wbc, bob_plt])
    return a, b


async def test_a_caregiver_sees_only_their_patient(db, caregiver_of_erika):
    a, _ = await _two_patients(db)
    own = await _feed(db)
    assert [d["id"] for d in own["documents"]] == [a.id]
    assert [f["document_id"] for f in own["facts"]] == [a.id]
    assert await _feed(db, patient_slug=ERIKA_SLUG) == {**own, "generated_at": ANY}
    with pytest.raises(ValueError, match="access denied"):
        await _feed(db, patient_slug=BOB_SLUG)  # a foreign slug is denied


async def test_an_admin_must_name_the_patient_and_gets_only_that_one(db, admin_caller):
    a, b = await _two_patients(db)
    with pytest.raises(ValueError, match="must pass patient_slug"):
        await _feed(db)  # no implicit admin patient
    bob = await _feed(db, patient_slug=BOB_SLUG)
    assert [d["id"] for d in bob["documents"]] == [b.id]
    assert {f["document_id"] for f in bob["facts"]} == {b.id} and len(bob["facts"]) == 2
    erika = await _feed(db, patient_slug=ERIKA_SLUG)
    assert [d["id"] for d in erika["documents"]] == [a.id]
    assert not {f["id"] for f in bob["facts"]} & {f["id"] for f in erika["facts"]}


# ── local institution codes (migration 081) ──


async def test_migration_081_seeds_shared_local_codes_once(db):
    local = await db.fact_code_local_codes()
    assert len(local) == 12
    assert local["NEUT"] == {NOU: "ANC"}
    marker = local["CA19_9"]
    assert marker == {NOU: "CA199"}
    assert local["GLU"] == {NOU: "GLUK"} and local["CA_CORR"] == {NOU: "CAKOR"}
    async with db.db.execute(
        "SELECT COUNT(*) AS n FROM codings WHERE patient_id IS NOT NULL "
        "OR owner_id NOT IN (SELECT code FROM fact_codes)"
    ) as cur:
        assert (await cur.fetchone())["n"] == 0  # shared vocabulary of known codes only
    await db.db.executescript((MIGRATIONS / "081_fact_local_codes.sql").read_text())
    assert await db.fact_code_local_codes() == local  # idempotent


async def test_fact_catalog_lists_local_codes(db, curated_env):
    await tool.index_patient(db, ERIKA_UUID)
    codes = {r["code"]: r for r in json.loads(await cf.fact_catalog(_Ctx(db)))["codes"]}
    assert codes["CREA"]["local_codes"] == {NOU: "KREA"}
    assert codes["GLU"]["local_codes"] == {NOU: "GLUK"}
    assert codes["WBC"]["local_codes"] == {}


# ── legacy lab tools read the facts model ──

OUTLIER = 25.0  # an unverified value far off the verified trend
LATER = "T10:00"  # later on the second day: it would be "the latest" if counted


async def _curated_labs(db) -> tuple[str, str]:
    first = date(YEAR, 8, 3)
    second = date(YEAR, 8, 24)
    a = await _doc(db, ERIKA_UUID, 1, first)
    b = await _doc(db, ERIKA_UUID, 2, second)
    first_at, second_at = first.isoformat(), second.isoformat()
    await _put(db, ERIKA_UUID, a.id, [
        _fact("WBC", first_at, 6.0, PER_NL, **WBC_REF),
        _fact("NEUT", first_at, 3.5, PER_NL),
        _fact("PLT", first_at, 190.0, PER_NL, verification="unverified"),
    ])  # fmt: skip
    await _put(db, ERIKA_UUID, b.id, [
        _fact("WBC", second_at, 4.5, PER_NL, **WBC_REF),
        _fact("WBC", second_at + LATER, OUTLIER, PER_NL, verification="unverified"),
        _fact("NEUT", second_at, 2.8, PER_NL),
    ])  # fmt: skip
    return first_at, second_at


def _values(points: list[dict]) -> list:
    return [p["value"] for p in points]


async def test_lab_time_series_reads_curated_facts(db):
    first_at, second_at = await _curated_labs(db)
    ctx = _Ctx(db)
    out = json.loads(await lab_trends.get_lab_time_series(ctx, parameters="WBC,ABS_NEUT"))
    assert out["source"] == "facts"
    wbc = out["parameters"]["WBC"]
    assert [p["date"] for p in wbc["series"]] == [first_at, second_at]
    assert wbc["count"] == len(wbc["series"])
    first, second = wbc["series"]
    assert first["reference_high"] == WBC_REF["ref_high"] and "delta" not in first
    assert second["delta"] == -1.5 and "delta_pct" in second
    neut = out["parameters"]["ABS_NEUT"]
    assert neut["code"] == "NEUT" and neut["count"] == len(neut["series"])  # legacy name


async def test_lab_time_series_counts_only_verified_values_by_default(db):
    await _curated_labs(db)
    out = json.loads(await lab_trends.get_lab_time_series(_Ctx(db), parameters="WBC,PLT"))
    wbc, plt = out["parameters"]["WBC"], out["parameters"]["PLT"]
    assert OUTLIER not in _values(wbc["series"])  # not in the trend …
    assert wbc["series"][-1]["delta"] == -1.5  # … and not in any delta
    assert {p["verification"] for p in wbc["series"]} == {"machine"}
    assert wbc["unverified_excluded"] == 1 and "unverified" not in wbc
    assert plt["series"] == [] and plt["unverified_excluded"] == 1


async def test_lab_time_series_lists_unverified_apart_when_asked(db):
    await _curated_labs(db)
    raw = await lab_trends.get_lab_time_series(
        _Ctx(db), parameters="WBC,PLT", include_unverified=True
    )
    wbc, plt = json.loads(raw)["parameters"]["WBC"], json.loads(raw)["parameters"]["PLT"]
    assert OUTLIER not in _values(wbc["series"])  # the trend is unchanged
    assert wbc["series"][-1]["delta"] == -1.5
    [apart] = wbc["unverified"]
    assert apart["value"] == OUTLIER and apart["verification"] == "unverified"
    assert "delta" not in apart  # listed, never counted
    assert plt["series"] == []
    assert [p["verification"] for p in plt["unverified"]] == ["unverified"]


async def test_lab_trends_excludes_unverified_by_default_and_lists_them_apart(db):
    first_at, second_at = await _curated_labs(db)
    ctx = _Ctx(db)
    trends = json.loads(await lab_trends.get_lab_trends(ctx, parameter="WBC"))
    assert trends["source"] == "facts"
    assert [v["lab_date"] for v in trends["values"]] == [first_at, second_at]
    assert OUTLIER not in _values(trends["values"])
    assert trends["unverified_excluded"] == 1 and "unverified" not in trends
    assert trends["values"][0]["gdrive_url"].startswith("https://drive.google.com/file/d/")
    every = json.loads(await lab_trends.get_lab_trends(ctx))
    assert {v["parameter"] for v in every["values"]} == {"WBC", "NEUT"}  # no unverified PLT

    asked = json.loads(
        await lab_trends.get_lab_trends(ctx, parameter="WBC", include_unverified=True)
    )
    assert asked["values"] == trends["values"]  # the counted values do not change
    assert [(v["value"], v["verification"]) for v in asked["unverified"]] == [
        (OUTLIER, "unverified")
    ]


async def test_compare_lab_panels_never_compares_unverified_values(db):
    first_at, second_at = await _curated_labs(db)
    ctx = _Ctx(db)
    panels = json.loads(await lab_trends.compare_lab_panels(ctx, first_at, second_at))
    assert panels["source"] == "facts" and panels["available_dates"] == [second_at, first_at]
    wbc = next(p for p in panels["parameters"] if p["parameter"] == "WBC")
    assert wbc["date_b"]["value"] != OUTLIER  # the later unverified value is not "the latest"
    assert wbc["delta"] == -1.5 and wbc["direction"] == "falling"
    assert wbc["status"] == "in_range"
    assert not any(p["parameter"] == "PLT" for p in panels["parameters"])  # unverified only
    assert panels["unverified_excluded"] == 2 and "unverified" not in panels

    asked = json.loads(
        await lab_trends.compare_lab_panels(ctx, first_at, second_at, include_unverified=True)
    )
    assert asked["parameters"] == panels["parameters"]  # comparisons unchanged
    assert {(u["parameter"], u["verification"]) for u in asked["unverified"]} == {
        ("PLT", "unverified"),
        ("WBC", "unverified"),
    }


async def test_reordered_evidence_is_logged_as_id_churn(db, caplog):
    day = date(YEAR, 9, 6)
    at = day.isoformat()
    doc = await _doc(db, ERIKA_UUID, 1, day)
    low = _fact("CRP", at, 4.0, "mg/L", evidence="CRP line a")
    high = _fact("CRP", at, 9.0, "mg/L", evidence="CRP line b")
    assert (await _put(db, ERIKA_UUID, doc.id, [low, high]))["id_churn"] == 0
    assert (await _put(db, ERIKA_UUID, doc.id, [low, high]))["id_churn"] == 0  # same order
    caplog.set_level("INFO", logger="oncofiles.database._facts")
    moved = {**low, "evidence": "CRP line c"}  # now sorts after the other: ordinals swap
    res = await _put(db, ERIKA_UUID, doc.id, [moved, high])
    assert res["id_churn"] == 2
    [record] = [r for r in caplog.records if "fact id churn" in r.getMessage()]
    assert record.levelname == "INFO"
    assert f"document_id={doc.id}" in record.getMessage()
    assert record.args[1] == res["id_churn"]


def test_legacy_lab_readers_are_marked_deprecated():
    until = lab_trends.DEPRECATED.rsplit(";", 1)[1]
    for fn in (
        lab_trends.get_lab_time_series,
        lab_trends.get_lab_trends,
        lab_trends.compare_lab_panels,
        lab_trends.get_lab_summary,
    ):
        first = fn.__doc__.splitlines()[0]
        assert first.startswith("[DEPRECATED — use ") and first.endswith(until), fn.__name__
