"""Preventive care: what the records show, next to EU guideline intervals (#928).

Two different things, never mixed:

* **Evidence** (patient data with provenance): procedures from the facts model
  (the #815 ``procedures`` rows of kind ``screening`` / ``vaccination``, and
  any other procedure whose printed text names a screening, e.g. an
  endoscopy), live documents of the ``vaccination`` / ``dental`` /
  ``preventive`` categories, and manual ``treatment_events`` of type
  ``screening`` / ``vaccination``. Every item carries its source (procedure
  id, document id, page, date and whether the date was printed or is the
  document date, verification).
* **Guideline reference** (``SCREENING_PROTOCOLS``): a general EU/WHO
  screening schedule by age and sex, each interval with its source named.
  It is a reference interval only, never a fact about this patient nor a
  recommendation for them; ``no_evidence`` means "nothing in Oncofiles", not
  "never done". Intervals are calendar months (``add_months``), not 30 days.

Evidence is matched to a guideline item by keywords on the printed text
(folded: lower case, no diacritics, word start); unmatched evidence is listed
separately so nothing the records hold is hidden.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import unicodedata
from datetime import date

logger = logging.getLogger(__name__)

GUIDELINE_NOTE = (
    "Guideline intervals are general EU/WHO screening references by age and sex "
    "(source named per item). They are not a fact about this patient and not a "
    "recommendation for them; the patient's doctor decides. 'no_evidence' means no "
    "record in Oncofiles, not that the screening was never done."
)
DUE_SOON_MONTHS = 3

# EU guideline reference by age and sex (NOT patient data).
# interval_months: guideline repeat interval in calendar months.
# start_age / end_age: guideline age range. sex: "male", "female" or "both".
# match: folded keyword stems that link printed evidence to the item (word start).
# document_categories: document categories that count as evidence on their own.
SCREENING_PROTOCOLS: list[dict] = [
    {
        "id": "colonoscopy",
        "name": "Colonoscopy",
        "name_sk": "Kolonoskopia",
        "start_age": 50,
        "interval_months": 120,  # every 10 years
        "sex": "both",
        "source": "EU Council Recommendation 2022/C 473/01",
        "event_type": "screening",
        "match": ["kolonoskop", "colonoscop", "koloskop"],
    },
    {
        "id": "fobt_fit",
        "name": "Fecal occult blood test (FIT)",
        "name_sk": "Test na skryté krvácanie (FIT)",
        "start_age": 50,
        "end_age": 74,
        "interval_months": 24,
        "sex": "both",
        "source": "EU Council Recommendation 2022/C 473/01",
        "event_type": "screening",
        "match": ["fit", "fobt", "toks", "okult", "occult", "skryte krvacanie"],
    },
    {
        "id": "dental_checkup",
        "name": "Dental checkup",
        "name_sk": "Zubná prehliadka",
        "start_age": 18,
        "interval_months": 6,
        "sex": "both",
        "source": "WHO Oral Health Programme",
        "event_type": "screening",
        "match": ["zub", "dental", "dent", "stomatolog"],
        "document_categories": ["dental"],
    },
    {
        "id": "ophthalmology",
        "name": "Ophthalmology screening",
        "name_sk": "Očné vyšetrenie",
        "start_age": 40,
        "interval_months": 24,
        "sex": "both",
        "source": "AAO / European Society of Ophthalmology",
        "event_type": "screening",
        "match": ["ocn", "oftalmo", "ophthalm", "eye"],
    },
    {
        "id": "dermatology_screening",
        "name": "Dermatology / skin cancer screening",
        "name_sk": "Dermatologická prehliadka",
        "start_age": 35,
        "interval_months": 24,
        "sex": "both",
        "source": "EADV / Euromelanoma",
        "event_type": "screening",
        "match": ["dermat", "kozn", "skin", "znamien", "dermatoskop"],
    },
    {
        "id": "cardiovascular_risk",
        "name": "Cardiovascular risk assessment (SCORE2)",
        "name_sk": "Hodnotenie kardiovaskulárneho rizika (SCORE2)",
        "start_age": 40,
        "interval_months": 60,  # every 5 years
        "sex": "both",
        "source": "ESC 2021 CVD Prevention Guidelines",
        "event_type": "screening",
        "match": ["score2", "kardiovaskul", "cardiovascular"],
    },
    {
        "id": "psa_screening",
        "name": "PSA screening (shared decision)",
        "name_sk": "PSA vyšetrenie (zdieľané rozhodnutie)",
        "start_age": 50,
        "interval_months": 24,
        "sex": "male",
        "source": "EAU Guidelines on Prostate Cancer",
        "event_type": "screening",
        "match": ["psa"],
    },
    {
        "id": "lipid_panel",
        "name": "Lipid panel",
        "name_sk": "Lipidový profil",
        "start_age": 40,
        "interval_months": 60,
        "sex": "both",
        "source": "ESC/EAS 2019 Dyslipidaemia Guidelines",
        "event_type": "screening",
        "match": ["lipid", "cholesterol"],
    },
    {
        "id": "fasting_glucose",
        "name": "Fasting blood glucose",
        "name_sk": "Glykémia nalačno",
        "start_age": 45,
        "interval_months": 36,  # every 3 years
        "sex": "both",
        "source": "ADA / WHO diabetes screening",
        "event_type": "screening",
        "match": ["glykem", "glukoz", "glucose"],
    },
    {
        "id": "mammography",
        "name": "Mammography screening",
        "name_sk": "Mamografický skríning",
        "start_age": 50,
        "end_age": 69,
        "interval_months": 24,
        "sex": "female",
        "source": "EU Council Recommendation 2022/C 473/01",
        "event_type": "screening",
        "match": ["mamograf", "mammograph", "mamo"],
    },
    {
        "id": "cervical_screening",
        "name": "Cervical cancer screening (HPV/Pap)",
        "name_sk": "Skríning rakoviny krčka maternice",
        "start_age": 25,
        "end_age": 64,
        "interval_months": 60,
        "sex": "female",
        "source": "EU Council Recommendation 2022/C 473/01",
        "event_type": "screening",
        "match": ["cytolog", "pap", "hpv", "cervi", "krck"],
    },
    {
        "id": "flu_vaccine",
        "name": "Annual influenza vaccination",
        "name_sk": "Ročné očkovanie proti chrípke",
        "start_age": 50,
        "interval_months": 12,
        "sex": "both",
        "source": "ECDC seasonal influenza vaccination",
        "event_type": "vaccination",
        "match": ["chripk", "influenz", "flu"],
    },
    {
        "id": "tetanus_booster",
        "name": "Tetanus-diphtheria booster",
        "name_sk": "Preočkovanie tetanus-diftéria",
        "start_age": 18,
        "interval_months": 120,  # every 10 years
        "sex": "both",
        "source": "WHO / ÚVZSR vaccination schedule",
        "event_type": "vaccination",
        "match": ["tetan", "difter", "diphther", "td", "tdap", "dtap"],
    },
]

_PROTOCOLS_BY_ID = {p["id"]: p for p in SCREENING_PROTOCOLS}
_SEX_ALIASES = {
    "male": "male", "m": "male", "muz": "male",
    "female": "female", "f": "female", "z": "female", "zena": "female",
}  # fmt: skip


def fold(text: str | None) -> str:
    """Lower case without diacritics (``Očkovanie`` → ``ockovanie``)."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def _matches(proto: dict, text: str | None) -> bool:
    folded = fold(text)
    return any(re.search(r"(?<![a-z0-9])" + re.escape(k), folded) for k in proto["match"])


def add_months(d: date, months: int) -> date:
    """``d`` plus calendar months; the day is clamped to the month's last day."""
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _calculate_age(dob: date, reference_date: date | None = None) -> int:
    """Calculate age in years from date of birth."""
    ref = reference_date or date.today()
    age = ref.year - dob.year
    if (ref.month, ref.day) < (dob.month, dob.day):
        age -= 1
    return age


def get_applicable_screenings(
    dob: date,
    sex: str,
    reference_date: date | None = None,
) -> list[dict]:
    """Guideline items whose age and sex range covers the patient."""
    age = _calculate_age(dob, reference_date)
    applicable = []
    for proto in SCREENING_PROTOCOLS:
        if proto["sex"] != "both" and proto["sex"] != sex:
            continue
        if age < proto["start_age"]:
            continue
        if "end_age" in proto and age > proto["end_age"]:
            continue
        applicable.append(proto)
    return applicable


def _to_date(value) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def match_evidence(items: list[dict]) -> tuple[dict[str, list[dict]], list[dict]]:
    """Link evidence items to guideline ids.

    Each item: ``source`` (procedure | document | treatment_event), ``kind``
    (procedure kind / document category / event type), ``text`` (printed
    text, title, filename), optional ``screening_id`` (an explicit id, manual
    entries), ``date`` and provenance fields. A vaccination-type guideline item
    only takes vaccination evidence, a screening item only non-vaccination
    evidence. Returns ({guideline id: [items]}, unmatched items).
    """
    by_id: dict[str, list[dict]] = {}
    unmatched: list[dict] = []
    for it in items:
        explicit = it.get("screening_id")
        if explicit in _PROTOCOLS_BY_ID:
            ids = [explicit]
        else:
            is_vacc = it.get("kind") == "vaccination"
            ids = [
                p["id"]
                for p in SCREENING_PROTOCOLS
                if (p["event_type"] == "vaccination") == is_vacc
                and (
                    _matches(p, it.get("text"))
                    or (
                        it.get("source") == "document"
                        and it.get("kind") in p.get("document_categories", ())
                    )
                )
            ]
        if not ids:
            unmatched.append(it)
        for sid in ids:
            by_id.setdefault(sid, []).append(it)
    return by_id, unmatched


def evaluate_screening_compliance(
    dob: date,
    sex: str,
    completed_screenings: list[dict],
    reference_date: date | None = None,
) -> list[dict]:
    """Per applicable guideline item: the newest evidence and the guideline interval.

    ``completed_screenings``: evidence items with ``screening_id`` and ``date``
    (plus any provenance fields, returned untouched under ``evidence``).

    ``status`` (relative to the guideline interval, not a clinical judgement):
    ``within_interval``, ``due_soon`` (next guideline date within
    ``DUE_SOON_MONTHS`` calendar months), ``past_interval``, ``no_evidence``,
    ``not_yet_applicable``.
    """
    ref = reference_date or date.today()
    applicable = get_applicable_screenings(dob, sex, ref)

    by_id: dict[str, list[dict]] = {}
    for cs in completed_screenings:
        d = _to_date(cs.get("date"))
        sid = cs.get("screening_id", "")
        if sid and d:
            by_id.setdefault(sid, []).append({**cs, "date": d.isoformat()})

    results = []
    for proto in applicable:
        evidence = sorted(by_id.get(proto["id"], []), key=lambda e: e["date"], reverse=True)
        last_done = _to_date(evidence[0]["date"]) if evidence else None
        if last_done:
            next_due = add_months(last_done, proto["interval_months"])
            if next_due <= ref:
                status = "past_interval"
            elif next_due <= add_months(ref, DUE_SOON_MONTHS):
                status = "due_soon"
            else:
                status = "within_interval"
        else:
            next_due = add_months(dob, 12 * proto["start_age"])
            status = "no_evidence" if next_due <= ref else "not_yet_applicable"

        results.append(
            {
                "id": proto["id"],
                "name": proto["name"],
                "name_sk": proto["name_sk"],
                "guideline": {
                    "kind": "guideline_interval",
                    "interval_months": proto["interval_months"],
                    "start_age": proto["start_age"],
                    "end_age": proto.get("end_age"),
                    "sex": proto["sex"],
                    "source": proto["source"],
                },
                "status": status,
                "last_evidence_date": last_done.isoformat() if last_done else None,
                "next_by_guideline": next_due.isoformat(),
                "days_until_next_by_guideline": (next_due - ref).days,
                "evidence": evidence,
            }
        )

    priority = {
        "past_interval": 0,
        "no_evidence": 1,
        "due_soon": 2,
        "within_interval": 3,
        "not_yet_applicable": 4,
    }
    results.sort(key=lambda r: (priority.get(r["status"], 5), r["days_until_next_by_guideline"]))
    return results


async def _collect_evidence(db, patient_id: str) -> list[dict]:
    """Evidence items from the facts model, documents and manual treatment events."""
    from oncofiles.fact_labels import method_of, verification_label
    from oncofiles.models import TreatmentEventQuery

    rows = await db.preventive_evidence_rows(patient_id=patient_id)
    items: list[dict] = []
    for p in rows["procedures"]:
        items.append(
            {
                "source": "procedure",
                "procedure_id": p["id"],
                "kind": p["kind"],
                "text": p["printed_text"],
                "outcome": p["outcome"],
                "date": p["at"],
                "date_source": p["date_source"],
                "document_id": p["source_document_id"],
                "page_number": p["page_number"],
                "verification": p["verification"],
                "method": method_of(p["extracted_by"]),
                "verification_label": verification_label(p["verification"], p["extracted_by"]),
            }
        )
    for d in rows["documents"]:
        items.append(
            {
                "source": "document",
                "document_id": d["id"],
                "kind": d["category"],
                "text": " ".join(x for x in (d["title"], d["filename"]) if x),
                "date": d["document_date"],
                "date_source": "document",
            }
        )
    # manual entries (add_treatment_event): newest 200 screening/vaccination events
    for event_type in ("screening", "vaccination"):
        events = await db.list_treatment_events(
            TreatmentEventQuery(event_type=event_type, limit=200), patient_id=patient_id
        )
        for ev in events:
            try:
                meta = json.loads(ev.metadata) if ev.metadata else {}
            except (TypeError, ValueError):
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            items.append(
                {
                    "source": "treatment_event",
                    "treatment_event_id": ev.id,
                    "kind": ev.event_type,
                    "text": ev.title,
                    "screening_id": meta.get("screening_id") or meta.get("screening_type"),
                    "date": ev.event_date.isoformat() if ev.event_date else None,
                    "date_source": "manual",
                }
            )
    return items


async def get_preventive_care_status(
    db, patient_id: str, reference_date: date | None = None
) -> str:
    """Preventive-care evidence of a general patient next to EU guideline intervals.

    Reads date of birth and sex from the patient context, evidence from the
    facts model (procedures), the preventive document categories and manual
    treatment events. Returns JSON.
    """
    from oncofiles import patient_context

    await patient_context.ensure_loaded(db.db, patient_id)
    ctx = patient_context.get_context(patient_id)
    patient_type = ctx.get("patient_type", "oncology")
    if patient_type != "general":
        return json.dumps(
            {
                "error": "Preventive care screening is for general health patients only. "
                "This patient is configured as oncology.",
            }
        )

    dob = _to_date(ctx.get("date_of_birth"))
    sex = _SEX_ALIASES.get(fold(ctx.get("sex")).strip(), "")
    if not dob or not sex:
        return json.dumps(
            {
                "error": "Patient context missing a valid date_of_birth (YYYY-MM-DD) and/or "
                "sex (male | female). Update patient context first via update_patient_context.",
            }
        )

    ref = reference_date or date.today()
    items = [it for it in await _collect_evidence(db, patient_id) if _to_date(it.get("date"))]
    by_id, unmatched = match_evidence(items)
    completed = [
        {**{k: v for k, v in it.items() if k != "screening_id"}, "screening_id": sid}
        for sid, its in by_id.items()
        for it in its
    ]
    results = evaluate_screening_compliance(dob, sex, completed, ref)
    for r in results:
        for e in r["evidence"]:
            e.pop("screening_id", None)

    statuses = [r["status"] for r in results]
    summary = {
        "age": _calculate_age(dob, ref),
        "sex": sex,
        "reference_date": ref.isoformat(),
        "total_screenings": len(results),
        **{
            s: statuses.count(s)
            for s in ("past_interval", "no_evidence", "due_soon", "within_interval")
        },
        "evidence_items": len(items),
    }
    return json.dumps(
        {
            "summary": summary,
            "guideline_note": GUIDELINE_NOTE,
            "screenings": results,
            "unmatched_evidence": unmatched,
        },
        default=str,
        ensure_ascii=False,
    )
