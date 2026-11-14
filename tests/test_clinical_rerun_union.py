# ruff: noqa: F811  (pytest fixtures imported from test_clinical_extract)
"""#819: a clinical re-extraction is a union keyed per document.

The local model samples non-deterministically, so a re-run may return fewer
items for the same page. An old row whose union key the new run did not
return stays (``kept_missing_in_rerun``); an old row whose key a stored new
item covers is replaced; a new key is added. Synthetic data only: the
documents are the worked examples of the extractor prompt and invented lines
(invented drugs and labels, dates 2027+); the local model is always mocked.
"""

from __future__ import annotations

import copy

from oncofiles.curated import clinical_store
from tests.helpers import ERIKA_UUID
from tests.test_clinical_cycle_course import (
    CYCLE_LINE,
    _course,
    _courses,
    _cycle,
    _cycles,
)
from tests.test_clinical_cycle_course import _store as _store_items
from tests.test_clinical_extract import (  # noqa: F401  (fixtures)
    DOC_A,
    PAGES_A,
    _active,
    _extract,
    _rows,
    seeded,
)

LLM_TABLES = (
    "conditions", "cancer_stages", "treatment_courses", "treatment_cycles", "medication_events",
    "procedures", "clinical_records", "encounters",
)  # fmt: skip


async def _llm(db, table, doc_id=DOC_A):
    return [
        r
        for r in await _active(db, table)
        if str(r.get("extracted_by") or "").startswith("llm-clinical")
        and r.get("source_document_id") == doc_id
    ]


async def _coded(db) -> set:
    """Active coded LLM facts of the document as (code, label, day) — no values."""
    return {
        (r["code"], r["param"], r["occurred_at"])
        for r in await _llm(db, "clinical_records")
        if r["code"]
    }


async def _all_ids(db) -> dict[str, dict[int, str]]:
    return {t: {r["id"]: r.get("fhir_id") for r in await _llm(db, t)} for t in LLM_TABLES}


async def _items(db):
    return (await _extract(db, PAGES_A))["items"]


def _kind(items, kind):
    return [i for i in items if i["kind"] == kind]


def _without(items, *kinds):
    return [i for i in items if i["kind"] not in kinds]


async def _put(db, items):
    return await _store_items(db, items)


# ── a shorter answer never lowers the coded facts ───────────────────────


async def test_a_rerun_returning_fewer_items_keeps_every_coded_fact(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    before = await _coded(db)
    assert len(before) >= 5  # biomarkers + lesion sizes of the example pages

    fewer = _without(items, "lesions") + _kind(items, "lesions")[:1]
    biomarkers = _kind(items, "biomarkers")
    fewer = [i for i in fewer if i["kind"] != "biomarkers"] + biomarkers[:1]
    res = await _put(db, fewer)

    assert await _coded(db) >= before  # nothing coded was lost
    stored = res["stored"]
    assert stored["biomarkers"]["kept_missing_in_rerun"] == len(biomarkers) - 1
    assert stored["lesions"]["kept_missing_in_rerun"] == len(_kind(items, "lesions")) - 1
    # the rows the re-run did return were replaced (soft delete + insert)
    assert stored["biomarkers"]["removed"] == stored["biomarkers"]["added"] == 1


async def test_kept_rows_keep_their_ids_and_fhir_ids(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    before = await _all_ids(db)
    res = await _put(db, [])  # an empty answer: everything stays as it was
    assert await _all_ids(db) == before
    changed = [k for c in res["stored"].values() for k in c if k in ("removed", "added")]
    assert not changed
    missing = sum(c.get("kept_missing_in_rerun", 0) for c in res["stored"].values())
    assert missing == sum(len(ids) for ids in before.values())


async def test_the_same_key_with_a_new_value_is_replaced(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    marker = next(i for i in _kind(items, "biomarkers") if i["fields"].get("result_as_printed"))
    code = marker["derived"]["code"]
    [old] = [r for r in await _llm(db, "clinical_records") if r["code"] == code]

    changed = copy.deepcopy(marker)
    changed["fields"]["result_as_printed"] = "zmenený výsledok"
    res = await _put(db, [changed])

    [new] = [r for r in await _llm(db, "clinical_records") if r["code"] == code]
    assert new["id"] != old["id"] and new["value_text"] == "zmenený výsledok"
    assert new["fhir_id"] != old["fhir_id"]
    gone = {r["id"] for r in await _rows(db, "clinical_records", where="deleted_at IS NOT NULL")}
    assert old["id"] in gone  # soft delete, never a hard delete
    assert res["stored"]["biomarkers"]["removed"] == res["stored"]["biomarkers"]["added"] == 1


async def test_new_keys_are_added(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    before = await _coded(db)
    marker = copy.deepcopy(_kind(items, "biomarkers")[0])
    marker["dates"] = {"date": {"iso": "2027-09-01", "printed": "01.09.2027", "precision": "day"}}
    res = await _put(db, [*items, marker])
    after = await _coded(db)
    assert after > before and len(after - before) == 1
    assert res["stored"]["biomarkers"]["added"] == len(_kind(items, "biomarkers")) + 1


# ── existing guarantees ─────────────────────────────────────────────────


async def test_retract_still_removes_every_unprotected_row_after_a_partial_rerun(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    await _put(db, _kind(items, "diagnoses"))  # most rows are kept as missing
    done = await clinical_store.retract_document(
        db, patient_id=ERIKA_UUID, document_id=DOC_A, reason="test", dry_run=False
    )
    assert sum(c.get("removed", 0) for c in done["stored"].values()) > 0
    for table in LLM_TABLES:
        assert await _llm(db, table) == [], table


async def test_verified_rows_are_never_replaced(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    marker = next(i for i in _kind(items, "biomarkers") if i["fields"].get("result_as_printed"))
    code = marker["derived"]["code"]
    [old] = [r for r in await _llm(db, "clinical_records") if r["code"] == code]
    await db.db.execute(
        "UPDATE clinical_records SET verification = 'verified' WHERE id = ?", (old["id"],)
    )
    await db.db.commit()

    changed = copy.deepcopy(marker)
    changed["fields"]["result_as_printed"] = "zmenený výsledok"
    await _put(db, [changed])  # same key, new value: the verified row stays as it is
    rows = [r for r in await _llm(db, "clinical_records") if r["code"] == code]
    kept = next(r for r in rows if r["id"] == old["id"])
    assert kept["verification"] == "verified" and kept["value_text"] == old["value_text"]

    await clinical_store.retract_document(
        db, patient_id=ERIKA_UUID, document_id=DOC_A, reason="test", dry_run=False
    )
    assert [r["id"] for r in await _llm(db, "clinical_records")] == [old["id"]]


async def test_rerun_lost_many_fires_above_the_threshold_only(seeded):
    db = seeded
    items = await _items(db)
    await _put(db, items)
    biomarkers = _kind(items, "biomarkers")
    assert len(biomarkers) - 1 <= clinical_store.RERUN_LOST_MANY
    few = await _put(db, [i for i in items if i is not biomarkers[0]])  # loses one coded row
    assert few["warnings"] == []

    res = await _put(db, [])
    [warning] = res["warnings"]
    assert warning["warning"] == "rerun_lost_many" and warning["document_id"] == DOC_A
    assert warning["coded_rows"] > clinical_store.RERUN_LOST_MANY
    assert set(warning) == {"warning", "document_id", "coded_rows"}  # counts only


async def test_a_first_extraction_behaves_as_before(seeded):
    db = seeded
    res = await _put(db, await _items(db))
    assert res["warnings"] == []
    for counts in res["stored"].values():
        assert set(counts) <= {"added", "derived"}


# ── a dropped item never replaces; derived courses follow their cycles ──


async def test_a_dropped_cycle_never_replaces_the_stored_one(seeded):
    db = seeded
    await _store_items(db, [_course("Zentrolux"), _cycle(CYCLE_LINE, regimen="Zentrolux")])
    [old] = await _cycles(db)
    # the same cycle (number + day) without any name: two other courses make the
    # link ambiguous, so it is dropped — the stored cycle must stay
    bare = _cycle("14.03.2027 podaný 3. cyklus podľa plánu")
    res = await _store_items(db, [_course("Arvemix"), _course("Doltraxa"), bare])
    assert res["dropped"] == {"cycle_without_course": 1}
    assert [c["id"] for c in await _cycles(db)] == [old["id"]]
    assert res["stored"]["treatment_cycles"]["kept_missing_in_rerun"] == 1


async def test_a_derived_course_stays_with_its_kept_cycle(seeded):
    db = seeded
    await _store_items(db, [_cycle(CYCLE_LINE, regimen="Zentrolux")])
    [course] = await _courses(db)
    [cycle] = await _cycles(db)
    res = await _store_items(db, [])
    assert [c["id"] for c in await _courses(db)] == [course["id"]]
    assert [c["id"] for c in await _cycles(db)] == [cycle["id"]]
    assert res["stored"] == {"treatment_cycles": {"kept_missing_in_rerun": 1}}
