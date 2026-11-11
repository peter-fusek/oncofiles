"""Read tools over the clinical facts model (#603): one question, one call.

Every row carries its ``source`` (document id, title, date, Drive link, page)
and its ``verification``. Unverified values (``[?]``, rotated pages, charts,
older cloud transcriptions) are never presented as facts: they are excluded
from statistics unless ``include_unverified`` is set, and always flagged.
"""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.tools._helpers import _get_db, _resolve_patient_id

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
KEY_CODES = ("WBC", "HGB", "PLT", "NEUT", "CREA", "ALT", "AST", "BILI", "CRP", "CEA", "CA19_9")
UNVERIFIED_NOTE = (
    "Values with verification='unverified' were machine-read from uncertain text "
    "([?], rotated page, chart, or an older transcription). Do not state them as facts: "
    "say they are unverified and link the source page."
)


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, indent=1)


def _source(row: dict) -> dict:
    return {
        "document_id": row.get("source_document_id"),
        "title": row.get("source_title"),
        "date": row.get("source_date"),
        "url": row.get("source_url"),
        "page": row.get("page_number"),
    }


def _meta(row: dict) -> dict:
    try:
        return json.loads(row.get("metadata_json") or "{}")
    except ValueError:
        return {}


def _dedupe(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """One measurement counts once (#635). Read time only: stored rows are never
    merged or deleted, and every hidden point is listed with its provenance.

    * ``exact``: the same code, day and value from two documents (a duplicated
      lab sheet); the verified/machine one is kept;
    * ``quote``: a value dated to its report (``date_source=report``) equals an
      earlier draw of another document within 60 days, and at least TWO codes
      of the same report day equal that draw. One equal number alone is a
      coincidence, not a quote;
    * ``range``: a value dated under a printed range (``date_range_start`` …
      ``date_range_end``; it sits at the range end, #726) equals, by code,
      canonical value and unit, a value of another document dated inside that
      window. A different value is never hidden. A value with an exact day is
      always preferred: a range value is hidden against another range value
      only when that one was kept, so two documents quoting the same range
      never hide each other, and on a shared day the exact value sorts first.
    """
    from datetime import date as _date

    def day_of(r: dict) -> str:
        return (r["effective_at"] or "")[:10]

    def as_date(day: str | None):
        try:
            return _date.fromisoformat((day or "")[:10])
        except ValueError:
            return None

    # every draw (source document, day) → {code: (value, unit, row)}
    draws: dict[tuple, dict] = {}
    for r in rows:
        if r["value_canonical"] is not None:
            draws.setdefault((r["source_document_id"], day_of(r)), {})[r["code"]] = r

    def same(a: dict, b: dict) -> bool:
        return (a["value_canonical"], a.get("unit_canonical")) == (
            b["value_canonical"],
            b.get("unit_canonical"),
        )

    def quoted(r: dict) -> dict | None:
        if _meta(r).get("date_source") != "report" or r["value_canonical"] is None:
            return None
        d0 = as_date(day_of(r))
        own = draws.get((r["source_document_id"], day_of(r)), {})
        for (doc, day), values in draws.items():
            d1 = as_date(day)
            if doc == r["source_document_id"] or not d0 or not d1 or not 0 < (d0 - d1).days <= 60:
                continue
            hit = values.get(r["code"])
            if not hit or not same(hit, r):
                continue
            if sum(1 for c, v in own.items() if c in values and same(values[c], v)) >= 2:
                return hit
        return None

    def ranged(r: dict) -> bool:
        m = _meta(r)
        return bool(m.get("date_range_start") and m.get("date_range_end"))

    def in_range(r: dict) -> dict | None:
        m = _meta(r)
        lo, hi = as_date(m.get("date_range_start")), as_date(m.get("date_range_end"))
        if not lo or not hi or r["value_canonical"] is None:
            return None
        for (doc, day), values in draws.items():
            d = as_date(day)
            hit = values.get(r["code"])
            if doc == r["source_document_id"] or not d or not lo <= d <= hi:
                continue
            if hit and same(hit, r) and (not ranged(hit) or id(hit) in kept_ids):
                return hit
        return None

    rank = {"verified": 0, "corrected": 0, "machine": 1, "unverified": 2}
    rows = sorted(
        rows,
        key=lambda r: (
            r["code"] or "", r["effective_at"] or "", ranged(r), rank.get(r["verification"], 3),
        ),
    )  # fmt: skip
    kept: list[dict] = []
    kept_ids: set[int] = set()
    first: dict = {}
    removed: list[dict] = []
    for r in rows:
        key = (r["code"], day_of(r), r["value_canonical"], r["value_text"])
        # a range value checks its window first, so the reason never depends on
        # whether the matching draw happens to fall on the range end (#726)
        other, reason = in_range(r), "range"
        if other is None:
            other, reason = first.get(key), "exact"
        if other is None:
            other, reason = quoted(r), "quote"
        if other is not None and other is not r:
            removed.append({
                "code": r["code"], "at": r["effective_at"], "value": r["value_canonical"],
                "reason": reason,
                "removed": {"fact_id": r.get("id"), "document_id": r["source_document_id"]},
                "kept": {"fact_id": other.get("id"), "document_id": other["source_document_id"],
                         "at": other["effective_at"]},
            })  # fmt: skip
            continue
        first[key] = r
        kept.append(r)
        kept_ids.add(id(r))
    return kept, removed


def _stats(points: list[dict]) -> dict | None:
    vals = [p["value"] for p in points if isinstance(p.get("value"), int | float)]
    if not vals:
        return None
    first, last = points[0], points[-1]
    return {
        "n": len(vals),
        "min": min(vals),
        "max": max(vals),
        "first": {"at": first["at"], "value": first["value"]},
        "last": {"at": last["at"], "value": last["value"]},
        "change": round(last["value"] - first["value"], 4)
        if isinstance(last["value"], int | float) and isinstance(first["value"], int | float)
        else None,
        "out_of_range": sum(1 for p in points if p.get("flag")),
    }


async def patient_overview(ctx: Context, patient_slug: str | None = None) -> str:
    """Where do we stand? Diagnoses, documents by type, latest key values, last visits.

    Start here for any question about a patient. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    latest = await db.latest_any_rows(patient_id=pid)  # newest, with its verification
    conditions = [
        r for r in latest if r["category"] in ("condition", "genetic_variant", "biomarker")
    ]
    key = [r for r in latest if r["code"] in KEY_CODES]
    visits = await db.timeline_rows(patient_id=pid, kinds=["encounter"], limit=5)
    catalog = await db.fact_catalog_rows(patient_id=pid)
    return _dump(
        {
            "documents_by_type": await db.document_category_counts(patient_id=pid),
            "diagnoses_and_markers": [
                {
                    "code": r["code"],
                    "name": r["name_en"],
                    "value": r["value_text"] or r["value_num"],
                    "at": r["effective_at"],
                    "source_document_id": r["source_document_id"],
                }
                for r in conditions
            ],  # fmt: skip
            "latest_key_values": [
                {
                    "code": r["code"],
                    "name": r["name_en"],
                    "value": r["value_canonical"],
                    "unit": r["unit_canonical"],
                    "flag": r["flag"],
                    "at": r["effective_at"],
                    "verification": r["verification"],
                    "source_document_id": r["source_document_id"],
                    "page": r["page_number"],
                }
                for r in key
            ],  # fmt: skip
            "recent_visits": visits,
            "measured_codes": len(catalog),
            "unverified_values": sum(r["unverified"] or 0 for r in catalog),
            "note": UNVERIFIED_NOTE,
        }
    )


async def timeline(
    ctx: Context,
    patient_slug: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 200,
) -> str:
    """What happened, newest first: visits, documents, treatments, diagnoses, abnormal values.

    kinds filters by: encounter, document, treatment, condition, procedure,
    genetic_variant, biomarker, medication, lab, tumor_marker. Dates are ISO
    (YYYY-MM-DD). Every row links its source document. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    rows = await _get_db(ctx).timeline_rows(
        patient_id=pid, date_from=date_from, date_to=date_to, kinds=kinds, limit=min(limit, 1000)
    )
    return _dump({"rows": rows, "note": UNVERIFIED_NOTE})


async def fact_catalog(ctx: Context, patient_slug: str | None = None) -> str:
    """What has been measured for this patient: every code with count, first/last date.

    Use it before fact_series to know which codes exist. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    return _dump({"codes": await _get_db(ctx).fact_catalog_rows(patient_id=pid)})


async def fact_series(
    ctx: Context,
    codes: list[str],
    patient_slug: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    include_unverified: bool = False,
) -> str:
    """How values developed over time, e.g. codes=["PLT","CEA"], in canonical units.

    Returns each value with its source (document, page, Drive link) and
    statistics (min, max, first, last, change, out-of-range count). Unverified
    values are listed separately and excluded from the statistics unless
    include_unverified=true; ``latest_any`` is the newest value of any
    verification (say which). ``duplicates`` lists every hidden point (exact
    duplicate, report quote, or same value inside a printed date range) with
    both documents. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    rows = await _get_db(ctx).fact_series_rows(
        patient_id=pid, codes=[c.upper() for c in codes], date_from=date_from, date_to=date_to
    )
    rows, duplicates = _dedupe(rows)
    out: dict = {}
    for r in rows:
        s = out.setdefault(
            r["code"],
            {"name_en": r["name_en"], "name_sk": r["name_sk"], "unit": r["unit_canonical"],
             "values": [], "unverified": []},
        )  # fmt: skip
        point = {
            "at": r["effective_at"],
            "value": r["value_canonical"] if r["value_canonical"] is not None else r["value_text"],
            "original": " ".join(
                str(x)
                for x in (r["label_original"], r["value_num"] or r["value_text"], r["unit"])
                if x is not None
            ),
            "ref": [r["ref_range_low"], r["ref_range_high"]],
            "flag": r["flag"],
            "flag_source": _meta(r).get("flag_source") or ("lab" if r["flag"] else None),
            "verification": r["verification"],
            "evidence": r["evidence"],
            "source": _source(r),
        }
        verified = r["verification"] != "unverified"
        (s["values"] if verified or include_unverified else s["unverified"]).append(point)
    for s in out.values():
        s["stats"] = _stats(s["values"])
        s["stats_include_unverified"] = include_unverified
        # the newest value of any verification, so "current" is never months old (#635)
        newest = max(s["values"] + s["unverified"], key=lambda p: p["at"] or "", default=None)
        s["latest_any"] = (
            {k: newest[k] for k in ("at", "value", "flag", "verification", "source")}
            if newest
            else None
        )
    return _dump(
        {
            "series": out,
            "duplicates_removed": len(duplicates),
            "duplicates": duplicates,  # what was hidden, why, and which value was kept
            "note": UNVERIFIED_NOTE,
        }
    )


async def document_detail(ctx: Context, document_id: int, patient_slug: str | None = None) -> str:
    """One document: metadata, doctor/institution, the facts extracted from it, links.

    The original opens via source_url (Drive) or view_document. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    doc = await db.document_row(patient_id=pid, document_id=document_id)
    if doc is None:
        return _dump({"error": "not_found"})
    doc.pop("structured_metadata", None)
    facts = await db.facts_of_document(patient_id=pid, document_id=document_id)
    return _dump(
        {
            "document": doc,
            "facts": [
                {
                    "code": f["code"],
                    "label": f["label_original"],
                    "value": f["value_num"] if f["value_num"] is not None else f["value_text"],
                    "unit": f["unit"],
                    "ref": [f["ref_range_low"], f["ref_range_high"]],
                    "flag": f["flag"],
                    "at": f["effective_at"],
                    "page": f["page_number"],
                    "verification": f["verification"],
                    "evidence": f["evidence"],
                }
                for f in facts
            ],  # fmt: skip
            "links": await db.links_for(patient_id=pid, entity="document", entity_id=document_id),
            "note": UNVERIFIED_NOTE,
        }
    )


async def encounter_detail(ctx: Context, encounter_id: int, patient_slug: str | None = None) -> str:
    """One visit (or lab draw, procedure, chemo day): its documents and doctor. Read-only."""
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    enc = await db.encounter_row(patient_id=pid, encounter_id=encounter_id)
    if enc is None:
        return _dump({"error": "not_found"})
    return _dump(
        {
            "encounter": enc,
            "documents": await db.encounter_documents(patient_id=pid, encounter_id=encounter_id),
        }
    )


async def people_and_places(ctx: Context, patient_slug: str | None = None) -> str:
    """Doctors and institutions involved in this patient's care, with counts and dates.

    Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    return _dump(await _get_db(ctx).people_and_places_rows(patient_id=pid))


def register(mcp):
    for fn in (
        patient_overview,
        timeline,
        fact_catalog,
        fact_series,
        document_detail,
        encounter_detail,
        people_and_places,
    ):
        mcp.tool(annotations=READ_ONLY)(fn)
