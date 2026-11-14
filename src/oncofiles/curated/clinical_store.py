"""Store the verified non-lab items of ONE document (#726 item 4, #711 phase 2b).

Mapping (design ``docs/architecture/hard_data_model.md`` §3):

* diagnoses → ``conditions`` (+ ``cancer_stages`` when a stage is printed,
  + an MKCH-10 ``codings`` row when an ICD-10-shaped code is printed);
* biomarkers / genomic results → ``clinical_records`` record_type
  ``biomarker`` / ``genetic_variant`` (+ HGVS ``codings`` for printed variants);
* treatment courses / cycles / medication doses → ``treatment_courses`` /
  ``treatment_cycles`` / ``medication_events``;
* procedures, imaging, radiotherapy → ``procedures``;
* CT lesion sizes → ``clinical_records`` record_type ``imaging``, code
  ``LESION_SIZE`` (mm), ``param`` = lesion label, linked to the imaging
  procedure of the same document in ``metadata.imaging_procedure_id``;
* planned dates → a ``treatment_cycles`` row with ``status = planned`` when
  the document names exactly one course, else a planned ``encounters`` row.

Every row carries patient_id, source_document_id, page, evidence,
extracted_by = extractor version + model, verification, and gets one
``provenance`` row (activity extract, agent_role assembler, the extractor
device). Links between rows of a document are made only when unambiguous
(a cycle's printed regimen, the only course of the document, a medication's
printed cycle number, the only primary tumour); otherwise they stay empty.
A cycle without exactly one matching course gets a course derived from a
regimen / drug name printed in its own evidence (#799: unverified, reason
``derived_from_cycle``, provenance activity ``derive``, deduplicated only
within the document, never merged with a printed course); without such a
name the cycle is dropped (``cycle_without_course``).

Re-extraction of a document soft-deletes only what this extractor family
wrote for that document and never a ``verified`` / ``corrected`` row (nor a
row such a row points to); a new item equal to a kept row reuses it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter

from oncofiles.curated import verifier
from oncofiles.curated.llm_extract_clinical import EXTRACTOR_FAMILY, EXTRACTOR_VERSION
from oncofiles.curated.verifier_clinical import CLINICAL_VERIFIER_VERSION, parse_stage
from oncofiles.database._facts import PROTECTED_VERIFICATION
from oncofiles.models import (
    CancerStage,
    Coding,
    Condition,
    Device,
    MedicationEvent,
    Procedure,
    ProvenanceRecord,
    TreatmentCourse,
    TreatmentCycle,
)

MKCH10_SYSTEM = "urn:oid:1.3.158.00165387.100.10.25"  # MKCH-10 (SK ICD-10), as printed
HGVS_SYSTEM = "http://varnomen.hgvs.org"
LESION_CODE = "LESION_SIZE"
CYCLE_STATUSES = ("planned", "administered", "postponed", "cancelled")
ENTITIES = (
    "condition", "cancer_stage", "treatment_course", "treatment_cycle", "medication_event",
    "procedure",
)  # fmt: skip
STAT_OF = {
    "condition": "conditions", "cancer_stage": "cancer_stages",
    "treatment_course": "treatment_courses", "treatment_cycle": "treatment_cycles",
    "medication_event": "medication_events", "procedure": "procedures",
    "biomarker": "biomarkers", "lesion": "lesions", "encounter": "planned_encounters",
}  # fmt: skip
_PLANNED_KIND = {"cycle": "chemo_cycle", "imaging": "imaging", "visit": "visit"}
_HGVS = re.compile(r"\b[cgmnpr]\.[A-Za-z0-9_*+>:-]+")
_MED_KIND = {"administered": "administration", "prescribed": "request"}
DERIVED_REASON = "derived_from_cycle"  # #799: a course derived from a cycle's evidence


def _fold(s) -> str:
    """Case-, diacritic- and whitespace-insensitive form for matching names."""
    nfkd = unicodedata.normalize("NFKD", str(s or ""))
    return _k("".join(ch for ch in nfkd if not unicodedata.combining(ch)))


def _is_derived(r: dict) -> bool:
    """A course ``store_document`` derived from a cycle's evidence (#799)."""
    try:
        return json.loads(r.get("metadata_json") or "{}").get("derived_from") == "cycle"
    except ValueError:
        return False


def _k(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def _day(v) -> str:
    return str(v or "")[:10]


# natural keys: a new item equal to a kept (verified / corrected) row reuses it
KEYS = {
    "condition": lambda r: (_k(r["printed_text"]),),
    "cancer_stage": lambda r: (r["condition_id"], _k(r["printed"])),
    "treatment_course": lambda r: (_k(r["regimen_printed"]), r.get("start_at")),
    "treatment_cycle": lambda r: (
        r["course_id"], _k(r.get("cycle_number_printed")),
        r.get("actual_at") or r.get("planned_at"), r.get("status"),
    ),
    "medication_event": lambda r: (
        _k(r["ingredient_printed"]), r.get("effective_start"), _k(r.get("dose_printed")), r["kind"],
    ),
    "procedure": lambda r: (r["kind"], r.get("performed_at"), _k(r["printed_text"])),
    "record": lambda r: (
        r["record_type"], r.get("code") or _k(r.get("param")), _k(r.get("value_text")),
        _day(r.get("occurred_at")), r.get("value_canonical"),
    ),
    "encounter": lambda r: (r["kind"], r["start_at"]),
}  # fmt: skip


def extracted_by_for(model: str | None) -> str:
    return f"{EXTRACTOR_VERSION}/{model}" if model else EXTRACTOR_VERSION


async def device_id_for(db, model: str | None) -> int:
    device = await db.upsert_device(
        Device(
            kind="llm-extractor",
            name="oncofiles.curated.llm_extract_clinical",
            version=f"{EXTRACTOR_VERSION}+verifier-{CLINICAL_VERIFIER_VERSION}",
            model=model or None,
            note="local model on stored sidecar text; generic verifier decides machine",
        )
    )
    return device.id


def _iso(item: dict, field: str = "date") -> str | None:
    d = item["dates"].get(field)
    return d["iso"] if d else None


def _meta(item: dict, **extra) -> str:
    meta: dict = {
        "extractor": EXTRACTOR_VERSION,
        "verifier": CLINICAL_VERIFIER_VERSION,
        "printed": {k: v for k, v in item["fields"].items() if v not in (None, "")},
        "dates": {k: {"precision": d["precision"], "printed": d["printed"]}
                  for k, d in item["dates"].items()},
        **{k: v for k, v in extra.items() if v not in (None, "", [], {})},
    }  # fmt: skip
    if item["reasons"]:
        meta["unverified_reasons"] = item["reasons"]
    return json.dumps(meta, ensure_ascii=False)


def _common(item: dict, document_id: int, extracted_by: str, **extra) -> dict:
    return {
        "source_document_id": document_id,
        "page_number": item.get("page_number"),
        "evidence": item["evidence"],
        "extracted_by": extracted_by,
        "verification": item["verification"],
        "metadata_json": _meta(item, **extra),
    }


def _mine(r: dict) -> bool:
    return (r.get("extracted_by") or "").startswith(EXTRACTOR_FAMILY)


def _protected(r: dict) -> bool:
    return r.get("verification") in PROTECTED_VERIFICATION


async def _plan_removal(db, *, patient_id: str, document_id: int) -> dict:
    """The active rows of one document and what of it stays: protected
    (verified / corrected) rows and every row a protected row points to.
    Shared by re-extraction (``store_document``) and the user-invoked
    retract (``retract_document``) so both remove exactly the same rows."""
    pid = patient_id
    existing = {
        e: await db.hard_rows_of_document(e, patient_id=pid, document_id=document_id)
        for e in ENTITIES
    }
    records = await db.extracted_records_of_document(
        patient_id=pid, document_id=document_id, family=EXTRACTOR_FAMILY
    )
    encounters = await db.planned_encounters_of_document(
        patient_id=pid, document_id=document_id, family=EXTRACTOR_FAMILY
    )
    keep = {e: {r["id"] for r in rows if _protected(r)} for e, rows in existing.items()}
    for r in records:
        if _protected(r):
            proc = json.loads(r.get("metadata_json") or "{}").get("imaging_procedure_id")
            if proc:
                keep["procedure"].add(proc)
    parents = (
        ("medication_event", "cycle_id", "treatment_cycle"),
        ("medication_event", "course_id", "treatment_course"),
        ("treatment_cycle", "course_id", "treatment_course"),
        ("cancer_stage", "condition_id", "condition"),
        ("condition", "primary_condition_id", "condition"),
    )
    for _ in range(3):  # medication → cycle → course chains
        for child, col, parent in parents:
            for r in existing[child]:
                if r["id"] in keep[child] and r.get(col):
                    keep[parent].add(r[col])
    hard = [
        (e, r) for e, rows in existing.items() for r in rows if _mine(r) and r["id"] not in keep[e]
    ]
    return {
        "existing": existing,
        "records": records,
        "encounters": encounters,
        "keep": keep,
        "remove_hard": hard,
        "remove_records": [r for r in records if not _protected(r)],
        "remove_encounters": [r for r in encounters if not _protected(r)],
    }


def _record_stat(r: dict) -> str:
    return "lesions" if r.get("code") == LESION_CODE else "biomarkers"


def _planned_counts(plan: dict) -> dict[str, Counter]:
    """What ``_soft_delete_planned`` would remove / what stays, per target."""
    stats: dict[str, Counter] = {s: Counter() for s in STAT_OF.values()}
    for e, _ in plan["remove_hard"]:
        stats[STAT_OF[e]]["removed"] += 1
    for e, rows in plan["existing"].items():
        stats[STAT_OF[e]]["kept"] += sum(1 for r in rows if _mine(r) and r["id"] in plan["keep"][e])
    for r in plan["remove_records"]:
        stats[_record_stat(r)]["removed"] += 1
    for r in plan["records"]:
        if _protected(r):
            stats[_record_stat(r)]["kept"] += 1
    stats["planned_encounters"]["removed"] += len(plan["remove_encounters"])
    stats["planned_encounters"]["kept"] += sum(1 for r in plan["encounters"] if _protected(r))
    return stats


async def _soft_delete_planned(
    db, plan: dict, *, patient_id: str, reason: str, stats: dict[str, Counter]
) -> None:
    """Soft-delete the rows ``_plan_removal`` marked (never a hard delete; the
    DB helpers refuse verified / corrected rows on their own as well)."""
    pid = patient_id
    for e, r in plan["remove_hard"]:
        if await db.soft_delete_hard_fact(e, r["id"], patient_id=pid, deleted_by=reason):
            await db.soft_delete_codings_of(e, r["id"], patient_id=pid, deleted_by=reason)
            stats[STAT_OF[e]]["removed"] += 1
    for r in plan["remove_records"]:
        if await db.soft_delete_extracted_record(patient_id=pid, record_id=r["id"], reason=reason):
            await db.soft_delete_codings_of("fact", r["id"], patient_id=pid, deleted_by=reason)
            stats[_record_stat(r)]["removed"] += 1
    for r in plan["remove_encounters"]:
        if await db.soft_delete_planned_encounter(patient_id=pid, encounter_id=r["id"]):
            stats["planned_encounters"]["removed"] += 1


async def retract_document(
    db, *, patient_id: str, document_id: int, reason: str, dry_run: bool = True
) -> dict:
    """User-invoked rollback of this extractor's rows of ONE document
    (``curated_clinical_retract``): soft-deletes exactly what a re-extraction
    would remove (same ``_plan_removal``) and adds nothing. Verified /
    corrected rows, the rows they point to and every row another extractor
    wrote (lab rule facts) stay. ``dry_run`` only counts. Counts only.

    No birth-date guard: that guard protects writes of NEW facts dated from
    the page; a retract writes no fact and no date."""
    plan = await _plan_removal(db, patient_id=patient_id, document_id=document_id)
    would = _planned_counts(plan)
    if dry_run:
        stats = would
    else:
        stats = {s: Counter() for s in STAT_OF.values()}
        await _soft_delete_planned(db, plan, patient_id=patient_id, reason=reason, stats=stats)
        for s, c in would.items():
            if c["kept"]:
                stats[s]["kept"] = c["kept"]
    return {"stored": {k: {n: v for n, v in c.items() if v} for k, c in stats.items() if +c}}


async def store_document(  # noqa: PLR0912, PLR0915
    db,
    *,
    patient_id: str,
    document_id: int,
    items: list[dict],
    document_date: str | None,
    birth_dates,
    model: str | None = None,
) -> dict:
    """Replace this extractor's rows of one document with ``items`` (verified
    items from ``llm_extract_clinical.extract_document``). Returns counts per
    target (added / removed / kept_protected) and dropped items — no values.

    ``birth_dates`` are the ones the items were verified against: without
    any, the birth-date guard was inactive and nothing is written
    (``verifier.NoBirthDateError``)."""
    verifier.require_dob_for_write(birth_dates)
    pid = patient_id
    extracted_by = extracted_by_for(model)
    device_id = await device_id_for(db, model)
    stats: dict[str, Counter] = {s: Counter() for s in STAT_OF.values()}
    dropped: Counter = Counter()

    # ── soft-delete what this extractor wrote for the document (keeps protected rows)
    plan = await _plan_removal(db, patient_id=pid, document_id=document_id)
    await _soft_delete_planned(
        db, plan, patient_id=pid, reason=f"re-extraction by {extracted_by}", stats=stats
    )
    existing, records, encounters, keep = (
        plan["existing"], plan["records"], plan["encounters"], plan["keep"],
    )  # fmt: skip

    kept = {e: {KEYS[e](r): r for r in rows if r["id"] in keep[e]} for e, rows in existing.items()}
    kept["record"] = {KEYS["record"](r): r for r in records if _protected(r)}
    kept["encounter"] = {KEYS["encounter"](r): r for r in encounters if _protected(r)}

    async def provenance(target_type: str, target_id: int, item: dict) -> None:
        await db.add_provenance(
            ProvenanceRecord(
                target_type=target_type, target_id=target_id, target_version=1,
                activity="extract", agent_role="assembler", agent_device_id=device_id,
                source_document_id=document_id, page_number=item.get("page_number"),
                quote=item["evidence"], reason=",".join(item["reasons"]) or None,
            ),
            patient_id=pid,
        )  # fmt: skip

    inserters = {
        "condition": db.insert_condition,
        "cancer_stage": db.insert_cancer_stage,
        "treatment_course": db.insert_treatment_course,
        "treatment_cycle": db.insert_treatment_cycle,
        "medication_event": db.insert_medication_event,
        "procedure": db.insert_procedure,
    }

    async def put(entity: str, model, item: dict) -> tuple[int, bool]:
        """(row id, newly inserted?)"""
        hit = kept[entity].get(KEYS[entity](model.model_dump()))
        if hit is not None:
            stats[STAT_OF[entity]]["kept_protected"] += 1
            return hit["id"], False
        saved = await inserters[entity](model, patient_id=pid)
        await provenance(entity, saved.id, item)
        stats[STAT_OF[entity]]["added"] += 1
        return saved.id, True

    async def put_record(stat: str, record: dict, item: dict) -> tuple[int, bool]:
        hit = kept["record"].get(KEYS["record"](record))
        if hit is not None:
            stats[stat]["kept_protected"] += 1
            return hit["id"], False
        rid = await db.insert_extracted_record(
            patient_id=pid, document_id=document_id, record=record, extracted_by=extracted_by
        )
        await provenance("fact", rid, item)
        stats[stat]["added"] += 1
        return rid, True

    def common(item: dict, **extra) -> dict:
        return _common(item, document_id, extracted_by, **extra)

    by_kind: dict[str, list[dict]] = {}
    for it in items:
        by_kind.setdefault(it["kind"], []).append(it)

    # ── diagnoses → conditions (+ stage, + printed code)
    diagnoses = sorted(
        by_kind.get("diagnoses", []), key=lambda i: i["fields"].get("role") != "primary"
    )
    primaries: set[int] = set()
    for it in diagnoses:
        f, d = it["fields"], it["derived"]
        role = f.get("role")
        # a metastasis points to the primary only when the document names exactly one
        primary_id = next(iter(primaries)) if role == "metastasis" and len(primaries) == 1 else None
        cond = Condition(
            printed_text=f["text_as_printed"],
            asserted_at=_iso(it),
            recorded_at=document_date,
            body_site=f.get("site_as_printed"),
            is_primary_cancer=bool(d.get("is_primary_cancer")),
            primary_condition_id=primary_id,
            clinical_certainty=d.get("certainty"),
            **common(it, role=role, code_printed=f.get("code_as_printed")),
        )
        cid, new = await put("condition", cond, it)
        if role == "primary":
            primaries.add(cid)
        if new and d.get("icd10"):
            await db.add_coding(
                Coding(owner_type="condition", owner_id=str(cid), system=MKCH10_SYSTEM,
                       code=d["icd10"], display=f["text_as_printed"], is_original=True),
                patient_id=pid,
            )  # fmt: skip
        if f.get("stage_as_printed"):
            parts = parse_stage(f["stage_as_printed"])
            stage = CancerStage(
                condition_id=cid, printed=f["stage_as_printed"], system=parts.get("system"),
                prefix=parts.get("prefix"), t=parts.get("t"), n=parts.get("n"), m=parts.get("m"),
                stage_group=parts.get("stage_group"), **common(it),
            )  # fmt: skip
            await put("cancer_stage", stage, it)

    # ── treatment courses (a derived course of a kept cycle stays derived, #799)
    courses: list[tuple[int, str]] = []  # printed courses: (id, folded regimen)
    derived: dict[str, int] = {}  # folded token → derived course id (this document only)
    for r in kept["treatment_course"].values():
        if _is_derived(r):
            derived.setdefault(_fold(r["regimen_printed"]), r["id"])
        else:
            courses.append((r["id"], _fold(r["regimen_printed"])))
    for it in by_kind.get("treatment_courses", []):
        f, d = it["fields"], it["derived"]
        course = TreatmentCourse(
            regimen_printed=f["regimen_as_printed"], line_of_therapy=d.get("line"),
            intent=d.get("intent"), start_at=_iso(it, "start_date"), end_at=_iso(it, "end_date"),
            **common(it, line_printed=f.get("line_as_printed"),
                     intent_printed=f.get("intent_as_printed")),
        )  # fmt: skip
        cid, _ = await put("treatment_course", course, it)
        if all(c != cid for c, _ in courses):
            courses.append((cid, _fold(f["regimen_as_printed"])))

    def matching(tokens) -> set[int]:
        """Printed courses of this document whose regimen matches any token."""
        return {c for c, rk in courses for t in tokens if rk and (_fold(t) in rk or rk in _fold(t))}

    def course_for(regimen: str | None) -> int | None:
        """The course a printed regimen names, or the document's only course
        (printed courses only; exactly one candidate, never a pick)."""
        ids = matching([regimen]) if regimen else {c for c, _ in courses}
        return next(iter(ids)) if len(ids) == 1 else None

    drugs = list(dict.fromkeys(
        m["fields"]["drug_as_printed"] for m in by_kind.get("medications", [])
        if m["fields"].get("drug_as_printed")
    ))  # fmt: skip

    def cycle_tokens(it: dict) -> tuple[str | None, list[str]]:
        """Regimen / drug names printed INSIDE the cycle's own evidence:
        (the cycle's printed regimen or None, the document's drug names)."""
        ev = _fold(it["evidence"])
        reg = it["fields"].get("regimen_as_printed")
        reg = reg if reg and len(_fold(reg)) >= 3 and _fold(reg) in ev else None
        found = [x for x in drugs if len(_fold(x)) >= 3 and _fold(x) in ev]
        return reg, found

    async def derived_course(token: str, it: dict) -> int:
        """A minimal course named by a token printed in the cycle's evidence:
        always unverified, own provenance (activity derive), deduplicated only
        within this document, never merged with a printed course."""
        key = _fold(token)
        if key in derived:
            return derived[key]
        meta = {
            "extractor": EXTRACTOR_VERSION, "verifier": CLINICAL_VERIFIER_VERSION,
            "derived_from": "cycle", "unverified_reasons": [DERIVED_REASON],
        }  # fmt: skip
        course = TreatmentCourse(
            regimen_printed=token, source_document_id=document_id,
            page_number=it.get("page_number"), evidence=it["evidence"],
            extracted_by=extracted_by, verification="unverified",
            metadata_json=json.dumps(meta, ensure_ascii=False),
        )  # fmt: skip
        saved = await db.insert_treatment_course(course, patient_id=pid)
        await db.add_provenance(
            ProvenanceRecord(
                target_type="treatment_course", target_id=saved.id, target_version=1,
                activity="derive", agent_role="assembler", agent_device_id=device_id,
                source_document_id=document_id, page_number=it.get("page_number"),
                quote=it["evidence"], reason=DERIVED_REASON,
            ),
            patient_id=pid,
        )  # fmt: skip
        stats["treatment_courses"]["added"] += 1
        stats["treatment_courses"]["derived"] += 1
        derived[key] = saved.id
        return saved.id

    # ── cycles (administered / postponed)
    cycles: list[tuple[int, int, str]] = []  # (cycle id, course id, cycle number)
    for it in by_kind.get("cycles", []):
        f, d = it["fields"], it["derived"]
        course_id = course_for(f.get("regimen_as_printed"))
        if course_id is None:
            # #799: no single printed course → link by the regimen / drug printed in
            # the cycle's evidence when exactly one course matches, else derive one
            reg, found = cycle_tokens(it)
            hits = matching([reg] if reg else found)
            if len(hits) == 1:
                course_id = next(iter(hits))
            elif reg or len(found) == 1:
                course_id = await derived_course(reg or found[0], it)
        if course_id is None:
            dropped["cycle_without_course"] += 1
            continue
        status = f.get("status")
        day = _iso(it)
        cycle = TreatmentCycle(
            course_id=course_id, cycle_number_printed=f.get("cycle_as_printed"),
            actual_at=day if status != "postponed" else None,
            planned_at=day if status == "postponed" else None,
            status=status if status in CYCLE_STATUSES else None,
            status_reason=f.get("reason_as_printed"), **common(it),
        )  # fmt: skip
        cyc_id, _ = await put("treatment_cycle", cycle, it)
        cycles.append((cyc_id, course_id, str(d.get("cycle_number") or "")))

    # ── medication doses
    for it in by_kind.get("medications", []):
        f, d = it["fields"], it["derived"]
        cycle_id = course_id = None
        m = re.search(r"\d+", f.get("cycle_as_printed") or "")
        if m:
            hits = [(c, co) for c, co, num in cycles if num == m.group(0)]
            if len(hits) == 1:
                cycle_id, course_id = hits[0]
        dose_printed = " ".join(
            x for x in (f.get("dose_as_printed"), f.get("unit_as_printed")) if x
        )
        event = MedicationEvent(
            kind=_MED_KIND.get(f.get("status") or "", "statement"), course_id=course_id,
            cycle_id=cycle_id, ingredient_printed=f["drug_as_printed"],
            dose_value=d.get("dose_value"), dose_unit_ucum=d.get("unit_ucum"),
            dose_printed=dose_printed or None, normalization_basis=d.get("basis"),
            route=f.get("route_as_printed"), effective_start=_iso(it, "start_date"),
            effective_end=_iso(it, "end_date"), **common(it, status_printed=f.get("status")),
        )  # fmt: skip
        await put("medication_event", event, it)

    # ── procedures (surgery, biopsy, imaging, radiotherapy, endoscopy, other)
    imaging: list[tuple[int, str]] = [
        (r["id"], _day(r.get("performed_at")))
        for r in kept["procedure"].values()
        if r["kind"] == "imaging"
    ]
    for it in by_kind.get("procedures", []):
        f = it["fields"]
        proc = Procedure(
            kind=f.get("type") or "other", printed_text=f["description_as_printed"],
            performed_at=_iso(it), performed_end=_iso(it, "end_date"), **common(it),
        )  # fmt: skip
        proc_id, _ = await put("procedure", proc, it)
        if proc.kind == "imaging" and all(p != proc_id for p, _ in imaging):
            imaging.append((proc_id, _day(proc.performed_at)))

    # ── CT lesion sizes → clinical_records LESION_SIZE (mm)
    for it in by_kind.get("lesions", []):
        f, d = it["fields"], it["derived"]
        day = _iso(it)
        same_day = [p for p, pday in imaging if day and pday == _day(day)]
        if len(same_day) != 1 and d.get("date_source") == "document" and len(imaging) == 1:
            same_day = [imaging[0][0]]
        dims = d.get("dims_mm") or []
        printed_nums = re.findall(r"\d+(?:[.,]\d+)?", f.get("size_as_printed") or "")
        record = {
            "record_type": "imaging", "code": LESION_CODE, "param": f["lesion_as_printed"],
            "value_num": max(float(n.replace(",", ".")) for n in printed_nums)
            if printed_nums else None,
            "value_text": f.get("size_as_printed") if len(dims) > 1 else None,
            "unit": f.get("unit_as_printed") or d.get("unit"),
            "value_canonical": d.get("size_mm"),
            "unit_canonical": "mm" if d.get("size_mm") is not None else None,
            "occurred_at": day, "page_number": it.get("page_number"), "evidence": it["evidence"],
            "effective_precision": (it["dates"].get("date") or {}).get("precision"),
            "verification": it["verification"],
        }  # fmt: skip
        record["metadata"] = json.loads(_meta(
            it, date_source=d.get("date_source"), dims_mm=dims,
            size_basis="longest_printed_dimension" if len(dims) > 1 else "single",
            imaging_procedure_id=same_day[0] if len(same_day) == 1 else None,
        ))  # fmt: skip
        await put_record("lesions", record, it)

    # ── biomarkers / genomic results → clinical_records
    for it in by_kind.get("biomarkers", []):
        f, d = it["fields"], it["derived"]
        record = {
            "record_type": d.get("record_type") or "biomarker", "code": d.get("code"),
            "param": f["marker_as_printed"],
            "value_text": f.get("result_as_printed") or f.get("variant_as_printed"),
            "occurred_at": _iso(it), "page_number": it.get("page_number"),
            "evidence": it["evidence"],
            "effective_precision": (it["dates"].get("date") or {}).get("precision"),
            "verification": it["verification"],
        }  # fmt: skip
        record["metadata"] = json.loads(_meta(it, date_source=d.get("date_source")))
        rid, new = await put_record("biomarkers", record, it)
        if new:
            for variant in dict.fromkeys(_HGVS.findall(f.get("variant_as_printed") or "")):
                await db.add_coding(
                    Coding(owner_type="fact", owner_id=str(rid), system=HGVS_SYSTEM,
                           code=variant, display=f.get("variant_as_printed"), is_original=True),
                    patient_id=pid,
                )  # fmt: skip

    # ── planned dates → a planned cycle of the only course, else a planned encounter
    for it in by_kind.get("planned", []):
        f = it["fields"]
        day = _iso(it)
        if not day:
            dropped["planned_without_date"] += 1
            continue
        course_id = course_for(None) if f.get("what") == "cycle" else None
        if course_id is not None:
            cycle = TreatmentCycle(
                course_id=course_id, planned_at=day, status="planned",
                **common(it, description=f.get("description_as_printed")),
            )  # fmt: skip
            await put("treatment_cycle", cycle, it)
            continue
        kind = _PLANNED_KIND.get(f.get("what") or "", "other")
        hit = kept["encounter"].get((kind, day))
        if hit is not None:
            stats["planned_encounters"]["kept_protected"] += 1
            continue
        enc_id = await db.insert_planned_encounter(
            patient_id=pid, document_id=document_id, start_at=day, kind=kind,
            title=f.get("description_as_printed"), page_number=it.get("page_number"),
            evidence=it["evidence"], extracted_by=extracted_by,
            verification=it["verification"], metadata=json.loads(_meta(it)),
        )  # fmt: skip
        await provenance("encounter", enc_id, it)
        stats["planned_encounters"]["added"] += 1

    return {
        "stored": {k: dict(c) for k, c in stats.items() if c},
        "dropped": dict(sorted(dropped.items())),
        "extracted_by": extracted_by,
    }
