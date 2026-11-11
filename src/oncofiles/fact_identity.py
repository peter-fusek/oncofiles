"""Stable fact ids (#726): a deterministic UUIDv5 per fact in ``clinical_records``.

A re-index soft-deletes a document's machine facts and inserts them again, so
the row id changes every time. The fact id must not: a dashboard keys its cache
and its links on it. The id is a UUIDv5 over a natural key:

    patient_id, source_document_id, code (or the printed label when uncoded),
    the fact's day, page_number, and an ordinal among facts with the same key
    ordered by their evidence text.

The day is the one the extractor gives the value, so later rules never move it:

* a value dated from a printed range uses the range itself (``start/end``), so
  where inside the range the value is placed (midpoint, end) never changes it;
* a report-quoted value that corroboration re-dated to its draw keeps the day it
  was quoted on (``metadata.quoted_on``), which is the day the extractor gave it.

``replace_document_facts`` writes the id into ``clinical_records.fhir_id`` when a
fact is inserted. Rows written before that (``fhir_id`` NULL) get the same value
computed at read time by :func:`ids_for_rows`, which mirrors the insert path, so
the first re-index after this change keeps the ids readers already saw.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable

# Fixed forever: changing it changes every fact id.
FACT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://oncofiles.com/ns/fact")
PROTECTED = ("verified", "corrected")


def _meta(value) -> dict:
    if isinstance(value, dict):
        return value
    try:
        out = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return out if isinstance(out, dict) else {}


def key_day(occurred_at: str | None, meta: dict) -> str:
    start, end = meta.get("date_range_start"), meta.get("date_range_end")
    if start and end:
        return f"{str(start)[:10]}/{str(end)[:10]}"
    if meta.get("date_source") == "corroborated" and meta.get("quoted_on"):
        return str(meta["quoted_on"])[:10]
    return (occurred_at or "")[:10]


def natural_key(
    patient_id: str,
    document_id: int | None,
    *,
    code: str | None,
    label: str | None,
    occurred_at: str | None,
    page: int | None,
    meta: dict,
) -> str:
    what = f"code:{code}" if code else f"param:{(label or '').strip()}"
    where = "" if document_id is None else str(document_id)
    return json.dumps(
        [patient_id, where, what, key_day(occurred_at, meta), "" if page is None else str(page)],
        ensure_ascii=False,
    )


def fact_uuid(key: str, ordinal: int) -> str:
    return str(uuid.uuid5(FACT_NAMESPACE, f"{key}#{ordinal}"))


def _assign(keys: list[str], evidence: list[str], taken: set[str]) -> list[str]:
    """Ordinal 0, 1, … within each key, in evidence order (ties keep input
    order). An id already taken (a kept, human-verified row) is skipped, so the
    result is deterministic for the same input and the same taken set."""
    order = sorted(range(len(keys)), key=lambda i: (keys[i], evidence[i]))
    next_ordinal: dict[str, int] = {}
    out = [""] * len(keys)
    for i in order:
        n = next_ordinal.get(keys[i], 0)
        fid = fact_uuid(keys[i], n)
        while fid in taken:
            n += 1
            fid = fact_uuid(keys[i], n)
        next_ordinal[keys[i]] = n + 1
        taken.add(fid)
        out[i] = fid
    return out


def fact_key(patient_id: str, document_id: int | None, f: dict) -> str:
    """Natural key (no ordinal) of an extractor fact."""
    return natural_key(
        patient_id,
        document_id,
        code=f.get("code"),
        label=f.get("label"),
        occurred_at=f.get("occurred_at"),
        page=f.get("page_number"),
        meta=_meta(f.get("metadata")),
    )


def row_key(patient_id: str, document_id: int | None, r: dict) -> str:
    """Natural key (no ordinal) of a stored ``clinical_records`` row."""
    return _row_keys(patient_id, document_id, [r])[0]


def ids_for_new_facts(
    patient_id: str, document_id: int | None, facts: list[dict], taken: set[str]
) -> list[str]:
    """Ids for extractor facts about to be inserted (in list order)."""
    keys = [fact_key(patient_id, document_id, f) for f in facts]
    return _assign(keys, [f.get("evidence") or "" for f in facts], taken)


def _row_keys(patient_id: str, document_id: int | None, rows: list[dict]) -> list[str]:
    return [
        natural_key(
            patient_id,
            document_id,
            code=r.get("code"),
            label=r.get("param"),
            occurred_at=r.get("occurred_at"),
            page=r.get("page_number"),
            meta=_meta(r.get("metadata_json")),
        )
        for r in rows
    ]


def ids_for_rows(patient_id: str, document_id: int | None, rows: Iterable[dict]) -> dict[int, str]:
    """Row id → fact id for the ACTIVE rows of one document, ordered by row id.

    A stored ``fhir_id`` wins. The others are computed the way
    ``replace_document_facts`` assigns them: human-verified rows first, then the
    rest with the verified ids taken.
    """
    rows = sorted(rows, key=lambda r: r["id"])
    out = {r["id"]: r["fhir_id"] for r in rows if r.get("fhir_id")}
    taken = set(out.values())
    pending = [r for r in rows if not r.get("fhir_id")]
    for group in (
        [r for r in pending if r.get("verification") in PROTECTED],
        [r for r in pending if r.get("verification") not in PROTECTED],
    ):
        ids = _assign(
            _row_keys(patient_id, document_id, group),
            [r.get("evidence") or "" for r in group],
            taken,
        )
        out.update(zip((r["id"] for r in group), ids, strict=True))
    return out
