# ruff: noqa: F811  (pytest fixtures imported from test_clinical_extract)
"""#799: a cycle the model returned without its treatment course.

``clinical_store.store_document`` links such a cycle to the ONE printed
course of the same document its printed regimen / drug matches, otherwise
derives a minimal, always unverified course from a regimen or drug name
printed in the cycle's own evidence, otherwise drops it as before.
Synthetic data only: invented regimen and drug names, dates 2027+.
"""

from __future__ import annotations

import json

from oncofiles.curated import clinical_store
from tests.helpers import ERIKA_UUID
from tests.test_clinical_extract import (  # noqa: F401  (fixtures)
    DAY,
    DOB,
    DOC_A,
    DOC_B,
    _active,
    _rows,
    seeded,
)

CYCLE_LINE = "14.03.2027 podaný 3. cyklus Zentrolux, bez komplikácií"
CYCLE_LINE_2 = "04.04.2027 podaný 4. cyklus Zentrolux"
DRUG_LINE = "21.03.2027 C2 Vertaplatín 37 mg/m2 i.v."
BARE_LINE = "28.03.2027 podaný 5. cyklus podľa plánu"


def _item(kind, evidence, verification="machine", dates=None, **fields):
    return {
        "kind": kind, "page_number": 1, "evidence": evidence, "verification": verification,
        "reasons": [] if verification == "machine" else ["unit_test_reason"],
        "fields": fields, "dates": dates or {}, "derived": {},
    }  # fmt: skip


def _day(iso, printed):
    return {"date": {"iso": iso, "printed": printed, "precision": "day"}}


def _cycle(evidence, regimen=None, number="3. cyklus", iso="2027-03-14", printed="14.03.2027"):
    it = _item("cycles", evidence, dates=_day(iso, printed), regimen_as_printed=regimen,
               cycle_as_printed=number, date_as_printed=printed, status="administered")  # fmt: skip
    it["derived"] = {"cycle_number": int("".join(c for c in number if c.isdigit()) or 0)}
    return it


def _course(regimen, evidence=None):
    return _item("treatment_courses", evidence or f"Liečba: {regimen}", regimen_as_printed=regimen)


def _med(drug, evidence):
    return _item("medications", evidence, drug_as_printed=drug, dose_as_printed="37",
                 unit_as_printed="mg/m2")  # fmt: skip


async def _store(db, items, doc_id=DOC_A):
    return await clinical_store.store_document(
        db, patient_id=ERIKA_UUID, document_id=doc_id, items=items, document_date=DAY,
        birth_dates={DOB}, model="mock-model",
    )  # fmt: skip


async def _courses(db, doc_id=DOC_A):
    return [c for c in await _active(db, "treatment_courses") if c["source_document_id"] == doc_id]


async def _cycles(db, doc_id=DOC_A):
    return [c for c in await _active(db, "treatment_cycles") if c["source_document_id"] == doc_id]


# ── linking to exactly one printed course ───────────────────────────────


async def test_cycle_links_to_the_single_matching_course(seeded):
    db = seeded
    # two printed courses, so the "only course" rule cannot apply; the cycle's
    # regimen (case and diacritics differ) matches exactly one of them
    res = await _store(db, [
        _course("ZENTROLÚX"), _course("Ortavix"),
        _cycle(CYCLE_LINE, regimen="Zentrolux"),
    ])  # fmt: skip
    courses = await _courses(db)
    assert len(courses) == 2 and res["dropped"] == {}
    zen = next(c for c in courses if c["regimen_printed"] == "ZENTROLÚX")
    assert [c["course_id"] for c in await _cycles(db)] == [zen["id"]]
    assert "derived" not in res["stored"]["treatment_courses"]


async def test_cycle_links_by_a_drug_printed_in_its_evidence(seeded):
    db = seeded
    res = await _store(db, [
        _course("Vertaplatín + Ortavix"), _course("Kelmora"),
        _cycle(DRUG_LINE, number="C2", iso="2027-03-21", printed="21.03.2027"),
        _med("Vertaplatin", DRUG_LINE),
    ])  # fmt: skip
    vert = next(c for c in await _courses(db) if c["regimen_printed"].startswith("Vertaplat"))
    assert [c["course_id"] for c in await _cycles(db)] == [vert["id"]]
    assert "derived" not in res["stored"]["treatment_courses"]


async def test_two_matching_courses_derive_a_course_and_never_pick_one(seeded):
    db = seeded
    res = await _store(db, [
        _course("Zentrolux"), _course("Zentrolux + Kelmora"),
        _cycle(CYCLE_LINE, regimen="Zentrolux"),
    ])  # fmt: skip
    courses = await _courses(db)
    printed = {c["id"] for c in courses if c["verification"] == "machine"}
    derived = [c for c in courses if c["id"] not in printed]
    assert len(printed) == 2 and len(derived) == 1
    assert derived[0]["regimen_printed"] == "Zentrolux"
    assert [c["course_id"] for c in await _cycles(db)] == [derived[0]["id"]]
    assert res["stored"]["treatment_courses"]["derived"] == 1


def test_regimen_names_match_as_whole_words_only():
    same = clinical_store._same_regimen
    assert not same("FOLFOX", "FOLFOXIRI")  # different regimens
    assert not same("FOLFOXIRI", "FOLFOX")
    assert not same("Zentrolux", "mZentrolux-2")
    assert same("mFOLFOX6 cyklus", "mFOLFOX6")
    assert same("mfolfox6", "mFOLFOX6 + Ortavix")
    assert same("Vertaplatin", "VERTAPLATÍN")  # case and diacritics ignored
    assert not same("", "FOLFOX")


async def test_a_longer_regimen_name_is_not_the_cycles_course(seeded):
    db = seeded
    # the cycle says FOLFOX; the only other printed course is FOLFOXIRI, which is a
    # different regimen: no link, a derived FOLFOX course instead
    res = await _store(db, [
        _course("FOLFOXIRI"), _course("Ortavix"),
        _cycle("14.03.2027 podaný 3. cyklus FOLFOX", regimen="FOLFOX"),
    ])  # fmt: skip
    iri = next(c for c in await _courses(db) if c["regimen_printed"] == "FOLFOXIRI")
    [cycle] = await _cycles(db)
    assert cycle["course_id"] != iri["id"]
    assert res["stored"]["treatment_courses"]["derived"] == 1


async def test_a_regimen_with_a_suffix_word_still_finds_its_course(seeded):
    db = seeded
    res = await _store(db, [
        _course("mZentrolux6"), _course("Ortavix"),
        _cycle("19.05.2027 podaný mZentrolux6 cyklus", regimen="mZentrolux6 cyklus"),
    ])  # fmt: skip
    mf = next(c for c in await _courses(db) if c["regimen_printed"] == "mZentrolux6")
    assert [c["course_id"] for c in await _cycles(db)] == [mf["id"]]
    assert "derived" not in res["stored"]["treatment_courses"]


async def test_a_name_inside_a_longer_word_of_the_evidence_is_not_printed(seeded):
    db = seeded
    # "FOLFOX" occurs only inside the word "FOLFOXIRI" of the evidence → dropped
    res = await _store(db, [_cycle("14.03.2027 podaný 3. cyklus FOLFOXIRI", regimen="FOLFOX")])
    assert res["dropped"] == {"cycle_without_course": 1} and await _courses(db) == []


# ── derived courses ─────────────────────────────────────────────────────


async def test_derived_course_from_a_regimen_printed_in_the_cycle_evidence(seeded):
    db = seeded
    res = await _store(db, [
        _cycle(CYCLE_LINE, regimen="Zentrolux"),
        _cycle(CYCLE_LINE_2, regimen="ZENTROLUX", number="4. cyklus", iso="2027-04-04",
               printed="04.04.2027"),
    ])  # fmt: skip
    [course] = await _courses(db)  # one derived course per token per document
    cycles = await _cycles(db)
    assert len(cycles) == 2 and {c["course_id"] for c in cycles} == {course["id"]}
    assert all(c["verification"] == "machine" for c in cycles)  # cycles keep their own
    assert res["dropped"] == {}
    assert res["stored"]["treatment_courses"] == {"added": 1, "derived": 1}


async def test_derived_course_rows_are_unverified_with_reason_and_provenance(seeded):
    db = seeded
    await _store(db, [_cycle(CYCLE_LINE, regimen="Zentrolux")])
    [course] = await _courses(db)
    assert course["verification"] == "unverified"  # never machine
    assert course["evidence"] == CYCLE_LINE and course["page_number"] == 1
    assert course["extracted_by"].startswith("llm-clinical")
    meta = json.loads(course["metadata_json"])
    assert meta["derived_from"] == "cycle"
    assert meta["unverified_reasons"] == [clinical_store.DERIVED_REASON]
    prov = await _rows(
        db, "provenance", where=f"target_type = 'treatment_course' AND target_id = {course['id']}"
    )
    assert len(prov) == 1
    assert prov[0]["activity"] == "derive" and prov[0]["reason"] == "derived_from_cycle"
    assert prov[0]["quote"] == CYCLE_LINE and prov[0]["source_document_id"] == DOC_A


async def test_derived_course_from_the_single_drug_printed_in_the_evidence(seeded):
    db = seeded
    await _store(db, [
        _cycle(DRUG_LINE, number="C2", iso="2027-03-21", printed="21.03.2027"),
        _med("Vertaplatín", DRUG_LINE),
    ])  # fmt: skip
    [course] = await _courses(db)
    assert course["regimen_printed"] == "Vertaplatín" and course["verification"] == "unverified"


async def test_derived_courses_are_never_shared_across_documents(seeded):
    db = seeded
    await _store(db, [_cycle(CYCLE_LINE, regimen="Zentrolux")], doc_id=DOC_A)
    await _store(db, [_cycle(CYCLE_LINE, regimen="Zentrolux")], doc_id=DOC_B)
    [a], [b] = await _courses(db, DOC_A), await _courses(db, DOC_B)
    assert a["id"] != b["id"]
    assert [c["course_id"] for c in await _cycles(db, DOC_B)] == [b["id"]]


# ── still dropped ───────────────────────────────────────────────────────


async def test_no_regimen_or_drug_in_the_evidence_still_drops_the_cycle(seeded):
    db = seeded
    res = await _store(db, [
        _cycle(BARE_LINE, number="5. cyklus", iso="2027-03-28", printed="28.03.2027"),
        # a regimen the model returned that is NOT inside the cycle's evidence
        _cycle(BARE_LINE, regimen="Zentrolux", number="5. cyklus", iso="2027-03-28",
               printed="28.03.2027"),
        # two drugs printed: which one names the course is a guess → dropped
        _cycle("28.03.2027 C5 Vertaplatín a Kelmora", number="C5", iso="2027-03-28",
               printed="28.03.2027"),
        _med("Vertaplatín", "Vertaplatín 37 mg/m2"), _med("Kelmora", "Kelmora 37 mg/m2"),
    ])  # fmt: skip
    assert res["dropped"] == {"cycle_without_course": 3}
    assert await _courses(db) == [] and await _cycles(db) == []


async def test_planned_cycles_never_attach_to_a_derived_course(seeded):
    db = seeded
    planned = _item("planned", "Ďalší cyklus 25.04.2027", what="cycle",
                    description_as_printed="Ďalší cyklus",
                    dates=_day("2027-04-25", "25.04.2027"))  # fmt: skip
    res = await _store(db, [_cycle(CYCLE_LINE, regimen="Zentrolux"), planned])
    assert res["stored"]["planned_encounters"]["added"] == 1
    assert len(await _cycles(db)) == 1


# ── retract and re-extraction ───────────────────────────────────────────


async def test_retract_counts_and_removes_derived_courses_with_their_cycles(seeded):
    db = seeded
    await _store(db, [
        _cycle(CYCLE_LINE, regimen="Zentrolux"),
        _cycle(CYCLE_LINE_2, regimen="Zentrolux", number="4. cyklus", iso="2027-04-04",
               printed="04.04.2027"),
    ])  # fmt: skip
    dry = await clinical_store.retract_document(
        db, patient_id=ERIKA_UUID, document_id=DOC_A, reason="test", dry_run=True
    )
    assert dry["stored"]["treatment_courses"] == {"removed": 1}
    assert dry["stored"]["treatment_cycles"] == {"removed": 2}
    done = await clinical_store.retract_document(
        db, patient_id=ERIKA_UUID, document_id=DOC_A, reason="test", dry_run=False
    )
    assert done["stored"] == dry["stored"]
    assert await _courses(db) == [] and await _cycles(db) == []
    gone = await _rows(db, "treatment_courses", where="deleted_at IS NOT NULL")
    assert len(gone) == 1  # soft delete, never a hard delete


async def test_re_extraction_replaces_derived_courses(seeded):
    db = seeded
    items = [_cycle(CYCLE_LINE, regimen="Zentrolux")]
    await _store(db, items)
    [old] = await _courses(db)
    res = await _store(db, items)
    [new] = await _courses(db)
    assert new["id"] != old["id"]
    course_stats = res["stored"]["treatment_courses"]
    assert course_stats["removed"] == course_stats["added"] == course_stats["derived"] == 1
    assert set(course_stats) == {"removed", "added", "derived"}
    assert [c["course_id"] for c in await _cycles(db)] == [new["id"]]


async def test_a_verified_cycle_keeps_its_derived_course(seeded):
    db = seeded
    items = [_cycle(CYCLE_LINE, regimen="Zentrolux")]
    await _store(db, items)
    [course] = await _courses(db)
    [cycle] = await _cycles(db)
    await db.db.execute(
        "UPDATE treatment_cycles SET verification = 'verified' WHERE id = ?", (cycle["id"],)
    )
    await db.db.commit()

    dry = await clinical_store.retract_document(
        db, patient_id=ERIKA_UUID, document_id=DOC_A, reason="test", dry_run=True
    )
    assert dry["stored"]["treatment_courses"] == {"kept": 1}
    assert dry["stored"]["treatment_cycles"] == {"kept": 1}

    res = await _store(db, items)  # re-extraction reuses the kept derived course
    assert [c["id"] for c in await _courses(db)] == [course["id"]]
    assert [c["id"] for c in await _cycles(db)] == [cycle["id"]]
    assert res["stored"]["treatment_cycles"] == {"kept_protected": 1}
    assert "derived" not in res["stored"].get("treatment_courses", {})
    kept = await _courses(db)
    assert kept[0]["verification"] == "unverified"
