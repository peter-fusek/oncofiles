"""Facts-model rows for the older timeline readers (#928).

``get_journey_timeline`` and the ``export_document_package`` timeline read
``timeline_v`` (the same view as the ``timeline`` tool) instead of the legacy
``lab_values`` / ``treatment_events`` / ``research_entries`` tables. Curated
patients have no legacy rows at all, so the old readers showed them nothing.

Fallback rule: the legacy tables are read only when the facts model has NO row
for the patient (``has_facts``), whatever date filter the caller uses. The
facts rows here leave out ``document`` (both readers list documents
themselves) and ``treatment_event`` (a legacy table, part of the fallback).
"""

from __future__ import annotations

from oncofiles.fact_labels import FLAG_GENERIC, label_row

# how a generic flag is spelled out in a text summary (#929)
GENERIC_RANGE_TEXT = "generic range"
# timeline_v entities that are not facts-model rows for these readers
NON_FACT_ENTITIES = ("document", "treatment_event")
# timeline_v kinds a legacy ``treatment_events`` filter maps to
TREATMENT_KINDS = frozenset({"treatment", "medication", "procedure", "planned"})
FACT_ROW_LIMIT = 2000


async def has_facts(db, patient_id: str) -> bool:
    return await db.has_timeline_facts(patient_id=patient_id, exclude_entities=NON_FACT_ENTITIES)


async def fact_rows(
    db,
    patient_id: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = FACT_ROW_LIMIT,
) -> list[dict]:
    """``timeline_v`` rows of the patient (newest first) without documents and
    legacy treatment events, each with ``method`` and ``verification_label``."""
    rows = await db.timeline_rows(
        patient_id=patient_id,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        exclude_entities=NON_FACT_ENTITIES,
    )
    return [label_row(r) for r in rows]


def summary_of(row: dict) -> str:
    """``value unit [flag] · detail`` of a timeline row, empty parts left out.

    A flag computed from a generic range (#929, ``flag_source = generic``,
    set by ``timeline_rows``) is written ``[H, generic range]`` so the text
    never passes it off as the lab's own mark."""
    value = " ".join(str(x) for x in (row.get("value"), row.get("unit")) if x not in (None, ""))
    if row.get("flag"):
        mark = row["flag"]
        if row.get("flag_source") == FLAG_GENERIC:
            mark = f"{mark}, {GENERIC_RANGE_TEXT}"
        value = f"{value} [{mark}]".strip()
    return " · ".join(x for x in (value, str(row.get("detail") or "")) if x)
