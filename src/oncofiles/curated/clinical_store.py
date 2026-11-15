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
  the document names exactly one course, else a planned ``encounters`` row;
* general-health kinds (#815, profile ``general``): allergies → ``conditions``
  (category ``problem-list-item``, ``metadata.condition_kind = allergy``,
  substance and reaction as printed); vaccinations → ``procedures`` kind
  ``vaccination`` (vaccine name as ``printed_text``, the printed dose and its
  number in metadata); screenings / preventive examinations → ``procedures``
  kind ``screening`` (printed result in ``outcome``). Chronic diagnoses use
  the diagnoses mapping; general genetic results (carrier, pharmacogenomic,
  risk variant) use the biomarker mapping.

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

Re-extraction of a document is a union keyed per document (#819): an old
row of this extractor family is replaced only when the new run stored an item
with the same ``UNION_KEYS`` key; an old row whose key the new run did not
return stays (``kept_missing_in_rerun``), because the local model samples
non-deterministically and a shorter answer is not evidence that a fact is
gone. A ``verified`` / ``corrected`` row (and every row such a row, or a kept
row, points to) is never touched; a new item equal to a kept row reuses it.
The user-invoked retract still removes every unprotected row.
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
# #819: a re-run that keeps more coded rows than this because its answer no
# longer had them is reported as ``rerun_lost_many`` (document id) for review
RERUN_LOST_MANY = 3


def _fold(s) -> str:
    """Case-, diacritic- and whitespace-insensitive form for matching names."""
    nfkd = unicodedata.normalize("NFKD", str(s or ""))
    return _k("".join(ch for ch in nfkd if not unicodedata.combining(ch)))


def _words(s) -> tuple[str, ...]:
    """The folded alphanumeric words of a name ("mFOLFOX6 cyklus" →
    ("mfolfox6", "cyklus"))."""
    return tuple(re.findall(r"[a-z0-9]+", _fold(s)))


def _within(needle: tuple, hay: tuple) -> bool:
    """``needle`` is a contiguous whole-word run of ``hay`` (never a
    substring of a word: "FOLFOX" is not inside "FOLFOXIRI")."""
    n = len(needle)
    return n > 0 and any(hay[i : i + n] == needle for i in range(len(hay) - n + 1))


def _same_regimen(a, b) -> bool:
    """Whole-word match in either direction on the folded words."""
    wa, wb = _words(a), _words(b)
    return _within(wa, wb) or _within(wb, wa)


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


def _digits(s) -> str:
    m = re.search(r"\d+", str(s or ""))
    return m.group(0) if m else ""


def _cond_key(code, text) -> tuple:
    """A printed ICD-10-shaped code (spacing / case ignored), else the folded text."""
    c = re.sub(r"[^A-Z0-9.]", "", str(code or "").upper())
    return ("condition", c or _fold(text))


def _record_key(code, param, day) -> tuple:
    """(code, day); a lesion also by its site, an uncoded marker by its label."""
    site = _fold(param) if not code or code == LESION_CODE else ""
    return ("record", code or "", site, _day(day))


def _metadata(r: dict) -> dict:
    try:
        return json.loads(r.get("metadata_json") or "{}")
    except ValueError:
        return {}


# #819 union keys: one key per stored row and per new item, coarser than KEYS
# (no value), so the same fact with a new value replaces the old row and a
# fact the new run did not return at all keeps it. A stage follows its
# condition and a derived course its cycles (None = no own key). A cycle's key
# also holds the folded regimen of its course (``_cycle_key``): known for an
# old row from its linked course, for a new item only once it is linked, so
# the plan before the inserts matches cycles by ``_cycle_hint`` instead.
UNION_KEYS = {
    "condition": lambda r: _cond_key(_metadata(r).get("code_printed"), r["printed_text"]),
    "cancer_stage": lambda r: None,
    "treatment_course": lambda r: (
        None if _is_derived(r) else ("course", _fold(r["regimen_printed"]))
    ),
    "medication_event": lambda r: (
        "medication", _fold(r["ingredient_printed"]), _day(r.get("effective_start")),
    ),
    "procedure": lambda r: (
        "procedure", r["kind"], _day(r.get("performed_at")), _fold(r["printed_text"]),
    ),
    "record": lambda r: _record_key(r.get("code"), r.get("param"), r.get("occurred_at")),
    "encounter": lambda r: ("encounter", r["kind"], _day(r.get("start_at"))),
}  # fmt: skip


def _cycle_key(regimen, number, day) -> tuple:
    """(folded regimen of the cycle's course, cycle number, day)."""
    return ("cycle", _fold(regimen), _digits(number), _day(day))


def _cycle_hint(it: dict) -> tuple | None:
    """(printed regimen or None = any, number, day) of a cycle / planned-cycle
    item, for the plan before the inserts (its course is not linked yet)."""
    f = it["fields"]
    if it["kind"] == "cycles":
        return (f.get("regimen_as_printed") or None, _digits(f.get("cycle_as_printed")),
                _day(_iso(it)))  # fmt: skip
    if it["kind"] == "planned" and f.get("what") == "cycle" and _iso(it):
        return (None, "", _day(_iso(it)))
    return None


def _item_keys(it: dict) -> list[tuple]:
    """The union keys a new item covers, cycles aside (``_cycle_hint`` before
    the inserts, ``_cycle_key`` of the linked course after)."""
    f, d, kind = it["fields"], it.get("derived") or {}, it["kind"]
    if kind == "diagnoses":
        return [_cond_key(f.get("code_as_printed"), f.get("text_as_printed"))]
    if kind == "treatment_courses":
        return [("course", _fold(f.get("regimen_as_printed")))]
    if kind == "medications":
        return [("medication", _fold(f.get("drug_as_printed")), _day(_iso(it, "start_date")))]
    if kind == "procedures":
        return [("procedure", f.get("type") or "other", _day(_iso(it)),
                 _fold(f.get("description_as_printed")))]  # fmt: skip
    if kind == "lesions":
        return [_record_key(LESION_CODE, f.get("lesion_as_printed"), _iso(it))]
    if kind == "biomarkers":
        return [_record_key(d.get("code"), f.get("marker_as_printed"), _iso(it))]
    if kind == "planned":
        day = _iso(it)
        if not day:
            return []
        return [("encounter", _PLANNED_KIND.get(f.get("what") or "", "other"), _day(day))]
    return []


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


async def _plan_removal(
    db,
    *,
    patient_id: str,
    document_id: int,
    new_keys: set | None = None,
    cycle_hints: list | None = None,
) -> dict:
    """The active rows of one document and what of it stays: protected
    (verified / corrected) rows, with ``new_keys`` (a re-extraction, #819)
    also every unprotected row whose union key is not among them, and every
    row a staying row points to. Shared by re-extraction (``store_document``)
    and the user-invoked retract (``retract_document``, ``new_keys=None``:
    every unprotected row goes) so both use the same keep / parent logic."""
    pid = patient_id
    rows = {
        "existing": {
            e: await db.hard_rows_of_document(e, patient_id=pid, document_id=document_id)
            for e in ENTITIES
        },
        "records": await db.extracted_records_of_document(
            patient_id=pid, document_id=document_id, family=EXTRACTOR_FAMILY
        ),
        "encounters": await db.planned_encounters_of_document(
            patient_id=pid, document_id=document_id, family=EXTRACTOR_FAMILY
        ),
    }
    return _plan_from(rows, new_keys, cycle_hints=cycle_hints)


def _plan_from(
    rows: dict,
    new_keys: set | None,
    *,
    cycle_hints: list | None = None,
    carry_also: dict | None = None,
) -> dict:
    """``_plan_removal`` on rows already read. A re-extraction plans twice on
    the same pre-insert snapshot: before inserting with the keys of every new
    item (cycles by ``cycle_hints``), after it with the keys of the items
    actually stored (a cycle by the regimen of the course it was linked to; a
    dropped item never replaces anything) plus every row the first plan kept
    (``carry_also``), so a kept row a new row links to never goes."""
    existing, records, encounters = rows["existing"], rows["records"], rows["encounters"]
    also = carry_also or {}
    regimen_of = {c["id"]: c["regimen_printed"] for c in existing["treatment_course"]}

    def missing(key) -> bool:
        return new_keys is not None and key is not None and key not in new_keys

    def cycle_missing(r: dict) -> bool:
        reg = regimen_of.get(r.get("course_id")) or ""
        num = _digits(r.get("cycle_number_printed"))
        day = _day(r.get("actual_at") or r.get("planned_at"))
        if not missing(_cycle_key(reg, num, day)):
            return False
        return not any(
            n == num and d == day and (hint is None or _same_regimen(hint, reg))
            for hint, n, d in cycle_hints or ()
        )

    keep = {e: {r["id"] for r in rows_ if _protected(r)} for e, rows_ in existing.items()}
    carried: dict[str, set] = {e: set() for e in ENTITIES}
    for e, rows_ in existing.items():
        for r in rows_:
            if not _mine(r) or r["id"] in keep[e]:
                continue
            gone = cycle_missing(r) if e == "treatment_cycle" else missing(UNION_KEYS[e](r))
            if gone or r["id"] in also.get(e, ()):
                carried[e].add(r["id"])
    for r in existing["cancer_stage"]:  # a stage stays with its kept condition
        if (
            _mine(r)
            and r["id"] not in keep["cancer_stage"]
            and r.get("condition_id") in carried["condition"]
        ):
            carried["cancer_stage"].add(r["id"])
    carried_records = {
        r["id"] for r in records if not _protected(r)
        and (missing(UNION_KEYS["record"](r)) or r["id"] in also.get("record", ()))
    }  # fmt: skip
    carried_encounters = {
        r["id"] for r in encounters if not _protected(r)
        and (missing(UNION_KEYS["encounter"](r)) or r["id"] in also.get("encounter", ()))
    }  # fmt: skip
    for e in ENTITIES:
        keep[e] |= carried[e]
    for r in records:
        if _protected(r) or r["id"] in carried_records:
            proc = _metadata(r).get("imaging_procedure_id")
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
        (e, r) for e, rows_ in existing.items() for r in rows_
        if _mine(r) and r["id"] not in keep[e]
    ]  # fmt: skip
    return {
        **rows,
        "keep": keep,
        "carried": carried,
        "carried_records": [r for r in records if r["id"] in carried_records],
        "carried_encounters": carried_encounters,
        "remove_hard": hard,
        "remove_records": [
            r for r in records if not _protected(r) and r["id"] not in carried_records
        ],
        "remove_encounters": [
            r for r in encounters if not _protected(r) and r["id"] not in carried_encounters
        ],
    }


_RERUN_FIELDS = (
    ("inserted", "added"), ("removed", "removed"), ("kept_missing", "kept_missing_in_rerun"),
)  # fmt: skip


def rerun_summary(stored: dict) -> dict:
    """Per kind: rows inserted / removed / kept as missing in the re-run
    (#819 report; counts only, every field present, kinds with any count)."""
    out = {}
    for kind, c in stored.items():
        row = {name: int(c.get(src, 0)) for name, src in _RERUN_FIELDS}
        if any(row.values()):
            out[kind] = row
    return out


def _missing_counts(plan: dict) -> tuple[dict[str, int], int]:
    """(``kept_missing_in_rerun`` per target, how many of them are coded: a
    coded biomarker / variant / lesion, a diagnosis with a printed code, a
    procedure)."""
    counts: Counter = Counter()
    coded = 0
    for e, ids in plan["carried"].items():
        counts[STAT_OF[e]] += len(ids)
    for r in plan["existing"]["condition"]:
        if r["id"] in plan["carried"]["condition"] and _metadata(r).get("code_printed"):
            coded += 1
    coded += len(plan["carried"]["procedure"])
    for r in plan["carried_records"]:
        counts[_record_stat(r)] += 1
        coded += bool(r.get("code"))
    counts["planned_encounters"] += len(plan["carried_encounters"])
    return {k: n for k, n in counts.items() if n}, coded


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
        if await db.soft_delete_planned_encounter(
            patient_id=pid, encounter_id=r["id"], deleted_by=reason
        ):
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
    """Merge ``items`` (verified items from ``llm_extract_clinical.
    extract_document``) into this extractor's rows of one document: a row
    whose union key a stored item covers is replaced, a row the run did not
    return stays (#819). Returns counts per target (added / removed /
    kept_protected / kept_missing_in_rerun), dropped items and warnings
    (``rerun_lost_many`` above ``RERUN_LOST_MANY`` coded rows) — no values.

    ``birth_dates`` are the ones the items were verified against: without
    any, the birth-date guard was inactive and nothing is written
    (``verifier.NoBirthDateError``)."""
    verifier.require_dob_for_write(birth_dates)
    pid = patient_id
    extracted_by = extracted_by_for(model)
    device_id = await device_id_for(db, model)
    stats: dict[str, Counter] = {s: Counter() for s in STAT_OF.values()}
    dropped: Counter = Counter()

    # ── plan the union (#819): only rows whose key a new item covers are replaced,
    # protected rows and rows the new run did not return stay. Nothing is
    # soft-deleted before the items are stored: the final plan uses the keys
    # of the items actually stored, so a dropped item never replaces a row.
    plan = await _plan_removal(
        db, patient_id=pid, document_id=document_id,
        new_keys={k for it in items for k in _item_keys(it)},
        cycle_hints=[h for it in items if (h := _cycle_hint(it))],
    )  # fmt: skip
    dropped_items: list[dict] = []
    cycle_keys: set[tuple] = set()  # _cycle_key of every cycle stored / reused
    regimen_by_id = {c["id"]: c["regimen_printed"] for c in plan["existing"]["treatment_course"]}
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

    # ── allergies → conditions (problem-list item, kind allergy in metadata, #815)
    for it in by_kind.get("allergies", []):
        f = it["fields"]
        cond = Condition(
            printed_text=f.get("text_as_printed") or f["substance_as_printed"],
            category="problem-list-item", asserted_at=_iso(it), recorded_at=document_date,
            **common(it, condition_kind="allergy", substance=f.get("substance_as_printed"),
                     reaction=f.get("reaction_as_printed")),
        )  # fmt: skip
        await put("condition", cond, it)

    # ── treatment courses (a derived course of a kept cycle stays derived, #799)
    courses: list[tuple[int, str]] = []  # printed courses: (id, regimen as printed)
    derived: dict[tuple, int] = {}  # folded words → derived course id (this document only)
    for r in kept["treatment_course"].values():
        if _is_derived(r):
            derived.setdefault(_words(r["regimen_printed"]), r["id"])
        else:
            courses.append((r["id"], r["regimen_printed"]))
    for it in by_kind.get("treatment_courses", []):
        f, d = it["fields"], it["derived"]
        course = TreatmentCourse(
            regimen_printed=f["regimen_as_printed"], line_of_therapy=d.get("line"),
            intent=d.get("intent"), start_at=_iso(it, "start_date"), end_at=_iso(it, "end_date"),
            **common(it, line_printed=f.get("line_as_printed"),
                     intent_printed=f.get("intent_as_printed")),
        )  # fmt: skip
        cid, _ = await put("treatment_course", course, it)
        regimen_by_id.setdefault(cid, f["regimen_as_printed"])
        if all(c != cid for c, _ in courses):
            courses.append((cid, f["regimen_as_printed"]))

    def matching(tokens) -> set[int]:
        """Printed courses of this document whose regimen matches any token
        as whole words (case / diacritics ignored; never inside a word)."""
        return {c for c, reg in courses for t in tokens if _same_regimen(t, reg)}

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
        ev = _words(it["evidence"])

        def printed(name) -> bool:
            return bool(name) and len("".join(_words(name))) >= 3 and _within(_words(name), ev)

        reg = it["fields"].get("regimen_as_printed")
        return (reg if printed(reg) else None), [x for x in drugs if printed(x)]

    async def derived_course(token: str, it: dict) -> int:
        """A minimal course named by a token printed in the cycle's evidence:
        always unverified, own provenance (activity derive), deduplicated only
        within this document, never merged with a printed course."""
        key = _words(token)
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
        regimen_by_id[saved.id] = token
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
            dropped_items.append(it)
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
        cycle_keys.add(_cycle_key(regimen_by_id.get(course_id), f.get("cycle_as_printed"), day))

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

    # ── vaccinations / screenings → procedures (kind vaccination / screening, #815)
    for it in by_kind.get("vaccinations", []):
        f, d = it["fields"], it["derived"]
        proc = Procedure(
            kind="vaccination", printed_text=f["vaccine_as_printed"], performed_at=_iso(it),
            **common(it, dose_printed=f.get("dose_as_printed"),
                     dose_number=d.get("dose_number")),
        )  # fmt: skip
        await put("procedure", proc, it)
    for it in by_kind.get("screenings", []):
        f = it["fields"]
        proc = Procedure(
            kind="screening", printed_text=f["test_as_printed"], performed_at=_iso(it),
            outcome=f.get("result_as_printed"), **common(it),
        )  # fmt: skip
        await put("procedure", proc, it)

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
            cycle_keys.add(_cycle_key(regimen_by_id.get(course_id), "", day))
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

    # ── replace only what a stored item covers; report what the re-run did not return
    stored_keys = cycle_keys | {
        k for it in items if not any(it is x for x in dropped_items) for k in _item_keys(it)
    }
    final = _plan_from(plan, stored_keys, carry_also={
        **plan["carried"],
        "record": {r["id"] for r in plan["carried_records"]},
        "encounter": plan["carried_encounters"],
    })  # fmt: skip
    await _soft_delete_planned(
        db, final, patient_id=pid, reason=f"re-extraction by {extracted_by}", stats=stats
    )
    missing, coded_missing = _missing_counts(final)
    for stat, n in missing.items():
        stats[stat]["kept_missing_in_rerun"] += n
    warnings = []
    if coded_missing > RERUN_LOST_MANY:
        warnings.append(
            {"warning": "rerun_lost_many", "document_id": document_id, "coded_rows": coded_missing}
        )
    stored = {k: dict(c) for k, c in stats.items() if c}
    return {
        "stored": stored,
        "rerun": rerun_summary(stored),
        "dropped": dict(sorted(dropped.items())),
        "extracted_by": extracted_by,
        "warnings": warnings,
    }
