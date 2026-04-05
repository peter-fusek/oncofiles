"""EU preventive care screening schedules and compliance tracking.

Provides screening protocols by age/sex based on EU Council Recommendations,
ESC, EAU, and WHO guidelines. Checks patient's treatment_events history
to determine what is due/overdue.
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# EU-recommended screening protocols by age and sex.
# interval_months: how often (in months) the screening should be repeated.
# start_age / end_age: age range for the screening.
# sex: "male", "female", or "both".
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
    },
]


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
    """Return screening protocols applicable to a patient based on age and sex."""
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


def evaluate_screening_compliance(
    dob: date,
    sex: str,
    completed_screenings: list[dict],
    reference_date: date | None = None,
) -> list[dict]:
    """Evaluate which screenings are due, overdue, or up-to-date.

    Args:
        dob: Patient date of birth.
        sex: "male" or "female".
        completed_screenings: List of dicts with at least:
            - screening_id: str (matches protocol id)
            - date: date (when it was completed)
        reference_date: Date to evaluate against (default: today).

    Returns:
        List of screening status dicts with:
            - id, name, name_sk, source
            - status: "up_to_date", "due_soon" (within 3 months), "overdue", "never_done"
            - last_done: date or None
            - next_due: date
            - days_until_due: int (negative = overdue)
    """
    ref = reference_date or date.today()
    applicable = get_applicable_screenings(dob, sex, ref)

    # Index completed screenings by id, keep latest
    latest_by_id: dict[str, date] = {}
    for cs in completed_screenings:
        sid = cs.get("screening_id", "")
        d = cs.get("date")
        if isinstance(d, str):
            d = date.fromisoformat(d)
        if sid and d and (sid not in latest_by_id or d > latest_by_id[sid]):
            latest_by_id[sid] = d

    results = []
    for proto in applicable:
        pid = proto["id"]
        interval = timedelta(days=proto["interval_months"] * 30)  # approximate
        last_done = latest_by_id.get(pid)

        if last_done:
            next_due = last_done + interval
            days_until = (next_due - ref).days
            if days_until > 90:
                status = "up_to_date"
            elif days_until > 0:
                status = "due_soon"
            else:
                status = "overdue"
        else:
            # Never done — calculate when it should have started
            start_date = date(dob.year + proto["start_age"], dob.month, dob.day)
            next_due = start_date
            days_until = (next_due - ref).days
            status = "never_done" if days_until <= 0 else "not_yet_applicable"

        results.append(
            {
                "id": pid,
                "name": proto["name"],
                "name_sk": proto["name_sk"],
                "source": proto["source"],
                "interval_months": proto["interval_months"],
                "status": status,
                "last_done": last_done.isoformat() if last_done else None,
                "next_due": next_due.isoformat(),
                "days_until_due": days_until,
            }
        )

    # Sort: overdue first, then never_done, then due_soon, then up_to_date
    priority = {
        "overdue": 0,
        "never_done": 1,
        "due_soon": 2,
        "up_to_date": 3,
        "not_yet_applicable": 4,
    }
    results.sort(key=lambda r: (priority.get(r["status"], 5), r["days_until_due"]))
    return results


async def get_preventive_care_status(db, patient_id: str) -> str:
    """Get preventive care compliance status for a patient.

    Reads patient context for DOB/sex, queries treatment_events for
    completed screenings, and evaluates compliance against EU protocols.

    Returns JSON string with screening compliance report.
    """
    from oncofiles.patient_context import get_context

    ctx = get_context(patient_id)
    patient_type = ctx.get("patient_type", "oncology")
    if patient_type != "general":
        return json.dumps(
            {
                "error": "Preventive care screening is for general health patients only. "
                "This patient is configured as oncology.",
            }
        )

    dob_str = ctx.get("date_of_birth", "")
    sex = ctx.get("sex", "")
    if not dob_str or not sex:
        return json.dumps(
            {
                "error": "Patient context missing date_of_birth and/or sex. "
                "Update patient context first via update_patient_context.",
            }
        )

    dob = date.fromisoformat(dob_str)

    # Query treatment_events for completed screenings/vaccinations
    events = await db.list_treatment_events(
        patient_id=patient_id,
        event_type=None,  # get all types
        limit=500,
    )

    completed = []
    for ev in events:
        if ev.event_type in ("screening", "vaccination"):
            meta = json.loads(ev.metadata) if ev.metadata and ev.metadata != "{}" else {}
            screening_id = meta.get("screening_id") or meta.get("screening_type", "")
            if screening_id:
                completed.append(
                    {
                        "screening_id": screening_id,
                        "date": ev.event_date,
                    }
                )

    results = evaluate_screening_compliance(dob, sex, completed)

    # Summary
    statuses = [r["status"] for r in results]
    summary = {
        "patient": ctx.get("name", "Unknown"),
        "age": _calculate_age(dob),
        "sex": sex,
        "total_screenings": len(results),
        "overdue": statuses.count("overdue"),
        "never_done": statuses.count("never_done"),
        "due_soon": statuses.count("due_soon"),
        "up_to_date": statuses.count("up_to_date"),
    }

    return json.dumps({"summary": summary, "screenings": results}, default=str)
