"""Read tools over the clinical facts model (#603): one question, one call.

Every row carries its ``source`` (document id, title, date, Drive link, page)
and its ``verification``. Unverified values (``[?]``, rotated pages, charts,
older cloud transcriptions) are never presented as facts: they are excluded
from statistics unless ``include_unverified`` is set, and always flagged.
"""

from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, date, datetime

from fastmcp import Context

from oncofiles.curated.facts_rules import _unit_key
from oncofiles.fact_labels import LLM_NOTE, group_llm, is_llm, label_row, norm
from oncofiles.tools._helpers import _gdrive_url, _get_db, _is_admin_caller, _resolve_patient_id

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
KEY_CODES = ("WBC", "HGB", "PLT", "NEUT", "CREA", "ALT", "AST", "BILI", "CRP", "CEA", "CA19_9")
UNVERIFIED_NOTE = (
    "Values with verification='unverified' were machine-read from uncertain text "
    "([?], rotated page, chart, or an older transcription). Do not state them as facts: "
    "say they are unverified and link the source page."
)

# ── dashboard feed (#726) ──
# Every kind a dashboard may ask for. Observation kinds read clinical_records
# today (lab = lab values, tumour markers and vital signs). The others come
# from the #711 tables once the extractor (#714) fills them; until then they
# return nothing.
FEED_KINDS = (
    "lab", "condition", "treatment", "medication", "procedure", "imaging", "lesion",
    "biomarker", "genetic_variant",
)  # fmt: skip
FEED_CATEGORIES = {
    "lab": ("lab", "tumor_marker", "vital"),
    "biomarker": ("biomarker",),
    "genetic_variant": ("genetic_variant",),
    # CT lesion sizes (LESION_SIZE, mm) from the LLM extractor (#726 item 4)
    "imaging": ("imaging",),
    "lesion": ("imaging",),
}
FEED_DEFAULT_LIMIT = 1000
FEED_MAX_LIMIT = 2000
FEED_MAX_BYTES = 140_000  # one page stays under ~150 kB of UTF-8 JSON
_FEED_CURSOR_RESERVE = 200  # bytes kept free for next_cursor


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, indent=1)


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str, separators=(",", ":"))


def _conversions(vocab: list[dict]) -> dict[str, dict[str, float]]:
    return {
        r["code"]: {_unit_key(u): float(f) for u, f in (r.get("unit_conversions") or {}).items()}
        for r in vocab
    }


def _observation(r: dict, conversions: dict[str, dict[str, float]]) -> dict:
    """Value, unit and reference range of one fact row, all in ONE unit.

    Reference ranges are stored as printed, values also in the canonical unit.
    A printed unit that converts to the canonical one gives the canonical value
    with the range converted by the same factor; a printed unit that does not
    convert keeps the printed value, unit and range together. A text value
    keeps its printed unit and range.
    """
    lo, hi = r.get("ref_range_low"), r.get("ref_range_high")
    num = r.get("value_num")
    if num is None:
        return {"value": None, "value_text": r.get("value_text"), "unit": r.get("unit"),
                "ref_low": lo, "ref_high": hi}  # fmt: skip
    printed, canonical = _unit_key(r.get("unit")), _unit_key(r.get("unit_canonical"))
    if not printed or printed == canonical:
        factor: float | None = 1.0
    else:
        factor = conversions.get(r.get("code") or "", {}).get(printed)
    if factor is None:
        return {"value": num, "value_text": r.get("value_text"), "unit": r.get("unit"),
                "ref_low": lo, "ref_high": hi}  # fmt: skip

    def conv(x):
        return None if x is None else round(x * factor, 6)

    return {"value": conv(num), "value_text": r.get("value_text"),
            "unit": r.get("unit_canonical"), "ref_low": conv(lo), "ref_high": conv(hi)}  # fmt: skip


def _iso_at(value: str | None) -> str | None:
    """ISO date or datetime as stored ("2027-01-05" or "2027-01-05T08:10"),
    or None when the stored value is not a date."""
    s = (value or "").strip()
    if len(s) > 10 and s[10] == " ":
        s = f"{s[:10]}T{s[11:]}"
    try:
        date.fromisoformat(s[:10])
    except ValueError:
        return None
    return s


def _encode_cursor(at: str, fact_id: str) -> str:
    raw = json.dumps([at, fact_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> tuple[str, str] | None:
    if not cursor:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        at, fact_id = json.loads(raw)
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("invalid cursor: pass next_cursor from the previous page") from None
    if not isinstance(at, str) or not isinstance(fact_id, str):
        raise ValueError("invalid cursor: pass next_cursor from the previous page")
    return at, fact_id


def _feed_kinds(kinds: list[str] | None) -> tuple[list[str], list[str]]:
    asked = [str(k).strip().lower() for k in (kinds or ["lab"]) if str(k).strip()] or ["lab"]
    known = [k for k in dict.fromkeys(asked) if k in FEED_KINDS]
    unknown = [k for k in dict.fromkeys(asked) if k not in FEED_KINDS]
    return known, unknown


def _feed_fact(r: dict, fact_id: str, at: str, conversions: dict) -> dict:
    obs = _observation(r, conversions)
    row: dict = {"id": fact_id, "code": r["code"], "at": at}
    if r["code"] is None:
        row["label"] = r["label_original"]  # uncoded: the printed label
    meta = _meta(r)
    if meta.get("date_range_start"):
        row["at_from"] = meta["date_range_start"]
    if meta.get("date_range_end"):
        row["at_to"] = meta["date_range_end"]
    for key in ("value", "value_text", "unit", "ref_low", "ref_high"):
        if obs[key] is not None:
            row[key] = obs[key]
    if r["flag"]:
        row["flag"] = r["flag"]
    row["verification"] = r["verification"]
    if is_llm(r.get("extracted_by")):  # read by the model (#729): say so, as everywhere
        labelled = label_row({"verification": r["verification"],
                              "extracted_by": r["extracted_by"]})  # fmt: skip
        row["method"] = labelled["method"]
        row["verification_label"] = labelled["verification_label"]
    row["document_id"] = r["source_document_id"]
    if r["page_number"] is not None:
        row["page"] = r["page_number"]
    return row


def _feed_document(d: dict) -> dict:
    return {
        "id": d["id"],
        "title": d["title"],
        "date": d["document_date"],
        "category": d["category"],
        "institution": d["institution"],
        "url": _gdrive_url(d["gdrive_id"]) or d["source_url"],
    }


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


RANGE_AT_NOTE = (
    "For a range (date_precision='range'), `at` is the latest possible day, not a sample "
    "date: the value was drawn sometime between at_from and at_to."
)


def _date_fields(row: dict) -> dict:
    """``date_precision`` and the printed window of a value dated under a date
    range (#726): ``at`` is then the range end, the latest possible day. Empty
    for a value with its own day. An open range has no ``at_from``."""
    m = _meta(row)
    if m.get("date_precision") != "range":
        return {}
    return {"date_precision": "range", "at_from": m.get("date_range_start"),
            "at_to": m.get("date_range_end")}  # fmt: skip


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
    """Where do we stand? Diagnoses, markers, documents by type, latest key values,
    last visits, upcoming planned dates.

    ``diagnoses_and_markers`` lists conditions (printed text, code, stage) and
    biomarker / genomic results, each with its verification, document and
    page. Every row says how it was obtained (``method``, ``verification_label``):
    ``llm-clinical`` rows were read by the local model and only their text was
    checked against the page; which line is the diagnosis is the model's
    interpretation and is not verified. Identical model-read rows from several
    documents are one item with ``sources``. Start here for any question about a
    patient. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    latest = await db.latest_any_rows(patient_id=pid)  # newest, with its verification
    key = [r for r in latest if r["code"] in KEY_CODES]
    visits = group_llm(
        await db.timeline_rows(patient_id=pid, kinds=["encounter"], limit=5), _timeline_key,
        page_key="page_number", id_key="entity_id",
    )  # fmt: skip
    catalog = await db.fact_catalog_rows(patient_id=pid)
    conditions = group_llm(
        [
            {
                "kind": "condition",
                "id": r["id"],
                "code": r["code"],
                "name": r["printed_text"],
                "value": r["stage"],
                "primary_cancer": bool(r["is_primary_cancer"]),
                "site": r["body_site"],
                "at": r["asserted_at"] or r["recorded_at"],
                "date_source": "printed" if r["asserted_at"] else "document",
                "verification": r["verification"],
                "source_document_id": r["source_document_id"],
                "page": r["page_number"],
                "extracted_by": r["extracted_by"],
            }
            for r in await db.condition_rows(patient_id=pid)  # #726: the conditions table
        ],
        # same text, stage and printed date: the same diagnosis, whatever document
        lambda r: (norm(r["name"]), norm(r["value"]), r["at"] if r["date_source"] == "printed"
                   else None),
        page_key="page",
    )  # fmt: skip
    markers = group_llm(
        [
            {
                "kind": r["category"],
                "id": r["id"],
                "code": r["code"],
                "name": r["name_en"],
                "label": r["label_original"],
                "value": r["value_text"] or r["value_num"],
                "at": r["effective_at"],
                "verification": r["verification"],
                "source_document_id": r["source_document_id"],
                "page": r["page_number"],
                "extracted_by": r["extracted_by"],
            }
            for r in await db.marker_rows(patient_id=pid, newest_only=False)
        ],
        lambda r: (r["kind"], r["code"] or norm(r["label"]), norm(r["value"])),
        page_key="page",
    )
    newest: dict = {}  # rows come newest first per code / label: keep the first
    for m in markers:
        newest.setdefault((m["kind"], m["code"] or norm(m["label"])), m)
    today = datetime.now(UTC).date().isoformat()
    upcoming = group_llm(
        await db.planned_rows(patient_id=pid, from_day=today, limit=5), _timeline_key,
        page_key="page_number", id_key="entity_id",
    )  # fmt: skip
    return _dump(
        {
            "documents_by_type": await db.document_category_counts(patient_id=pid),
            "diagnoses_and_markers": [*conditions, *newest.values()],
            "upcoming": upcoming,
            "latest_key_values": [
                label_row(
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
                        "extracted_by": r["extracted_by"],
                    }
                )
                for r in key
            ],  # fmt: skip
            "recent_visits": visits,
            "measured_codes": len(catalog),
            "unverified_values": sum(r["unverified"] or 0 for r in catalog),
            "note": UNVERIFIED_NOTE,
            "llm_note": LLM_NOTE,
        }
    )


def _timeline_key(r: dict) -> tuple:
    """Identical timeline rows across documents: same kind, entity type, name,
    value, detail and date."""
    return (r["kind"], r["entity"], norm(r["title"]), norm(r["value"]), norm(r.get("detail")),
            r["at"])  # fmt: skip


async def timeline(
    ctx: Context,
    patient_slug: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    kinds: list[str] | None = None,
    limit: int = 200,
) -> str:
    """What happened, newest first: visits, documents, treatments, diagnoses, abnormal values.

    kinds filters by: encounter, document, treatment (courses, cycles and legacy
    treatment events), condition, procedure (surgery, biopsy, imaging,
    radiotherapy), medication, genetic_variant, biomarker, imaging (CT lesion
    sizes), planned (next cycle, examination or visit), lab, tumor_marker.
    Dates are ISO (YYYY-MM-DD). Every row links its source document and page;
    ``date_source = document`` means no date was printed for the item and the
    document date is shown. Every row has ``method`` and ``verification_label``:
    ``llm-clinical`` rows (diagnoses, treatments, doses, procedures, lesion
    sizes, biomarkers, planned dates) were read by the local model; only their
    text was checked against the page, while the interpretation (which line is
    the diagnosis, a current vs a historical regimen) is the model's and is not
    verified. Identical model-read rows from several documents are one row with
    ``sources`` (document, page). Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    rows = await _get_db(ctx).timeline_rows(
        patient_id=pid, date_from=date_from, date_to=date_to, kinds=kinds, limit=min(limit, 1000)
    )
    rows = group_llm(rows, _timeline_key, page_key="page_number", id_key="entity_id")
    return _dump({"rows": rows, "note": UNVERIFIED_NOTE, "llm_note": LLM_NOTE})


async def fact_catalog(ctx: Context, patient_slug: str | None = None) -> str:
    """What has been measured for this patient: every code with count, first/last date.

    ``local_codes`` maps a laboratory's own abbreviation system to its code for
    ours, e.g. {"urn:oncofiles:local:nou": "ANC"} for NEUT. Use it before
    fact_series to know which codes exist. Read-only.
    """
    pid = await _resolve_patient_id(patient_slug, ctx)
    db = _get_db(ctx)
    rows = await db.fact_catalog_rows(patient_id=pid)
    local = await db.fact_code_local_codes()
    for r in rows:
        r["local_codes"] = local.get(r["code"] or "", {})
    return _dump({"codes": rows})


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
    both documents. A value printed under a date range carries
    ``date_precision="range"`` with ``at_from``/``at_to``: for a range, ``at``
    is the latest possible day, not a sample date. Read-only.
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
            **_date_fields(r),
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
            {
                k: newest[k]
                for k in (
                    "at",
                    "date_precision",
                    "at_from",
                    "at_to",
                    "value",
                    "flag",
                    "verification",
                    "source",
                )
                if k in newest
            }  # fmt: skip
            if newest
            else None
        )
    return _dump(
        {
            "series": out,
            "duplicates_removed": len(duplicates),
            "duplicates": duplicates,  # what was hidden, why, and which value was kept
            "note": UNVERIFIED_NOTE + " " + RANGE_AT_NOTE,
        }
    )


async def document_detail(ctx: Context, document_id: int, patient_slug: str | None = None) -> str:
    """One document: metadata, doctor/institution, the facts extracted from it, links.

    The original opens via source_url (Drive) or view_document. A fact printed
    under a date range carries ``date_precision="range"`` with
    ``at_from``/``at_to``: for a range, ``at`` is the latest possible day, not
    a sample date. Read-only.
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
                    **_date_fields(f),
                    "page": f["page_number"],
                    "verification": f["verification"],
                    "evidence": f["evidence"],
                }
                for f in facts
            ],  # fmt: skip
            "links": await db.links_for(patient_id=pid, entity="document", entity_id=document_id),
            "note": UNVERIFIED_NOTE + " " + RANGE_AT_NOTE,
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


async def dashboard_feed(
    ctx: Context,
    patient_slug: str | None = None,
    since: str | None = None,
    kinds: list[str] | None = None,
    cursor: str | None = None,
    limit: int = FEED_DEFAULT_LIMIT,
) -> str:
    """Compact facts for a patient's dashboard: one row per fact, plus its source documents.

    Facts only, never interpretation: no evidence text, no statistics. Returns
    {facts: [{id, code, at, value, value_text, unit, ref_low, ref_high, flag,
    verification, document_id, page}], documents: [{id, title, date, category,
    institution, url}], generated_at, next_cursor}.

    * ``id`` is a stable fact id (UUID): the same fact keeps it across re-indexing.
    * ``at`` is ISO (date, or date and time as printed). A value dated from a
      printed date range also has ``at_from`` / ``at_to``. ``since`` (ISO)
      keeps facts with ``at >= since``.
    * Values are in the code's canonical unit when the printed unit converts,
      with the reference range in the same unit. An uncoded fact has
      ``code: null`` and its printed ``label``. Optional keys without a value are
      omitted. ``verification='unverified'`` values are included and labelled.
    * ``documents`` lists only the documents the returned facts cite; ``url``
      opens the original in Drive.
    * ``kinds`` defaults to ["lab"] (lab values, tumour markers, vital signs).
      Also accepted: biomarker, genetic_variant, imaging / lesion (CT lesion
      sizes, code LESION_SIZE in mm), condition, treatment, medication,
      procedure (the last four still empty here: use timeline); anything else
      is listed in ``unknown_kinds`` and returns nothing.
    * A fact read by the local model carries ``method: "llm-clinical"`` and a
      ``verification_label`` ({en, sk}): only its text was checked against the
      page; the interpretation is the model's and is not verified.
    * Pages of at most ``limit`` facts (default 1000, max 2000) and about 140 kB,
      ordered by (at, id). Pass ``next_cursor`` back as ``cursor`` until it is null.

    Admin callers must pass patient_slug. Read-only.
    """
    if _is_admin_caller() and not patient_slug:
        # same rule as query_db / export_manifest: no implicit admin patient
        raise ValueError(
            "Admin callers must pass patient_slug=<slug>; there is no implicit admin patient scope."
        )
    pid = await _resolve_patient_id(patient_slug, ctx)
    known, unknown = _feed_kinds(kinds)
    since_at = _iso_at(since) if since else None
    if since and since_at is None:
        raise ValueError(f"since must be an ISO date or datetime, got {since!r}")
    after = _decode_cursor(cursor)
    limit = max(1, min(int(limit), FEED_MAX_LIMIT))
    db = _get_db(ctx)

    categories = sorted({c for k in known for c in FEED_CATEGORIES.get(k, ())})
    lower = max(since_at[:10] if since_at else "", after[0][:10] if after else "") or None
    rows = await db.feed_fact_rows(patient_id=pid, categories=categories, since_day=lower)
    unstored = [r["source_document_id"] for r in rows if not r["fhir_id"]]
    computed = (
        await db.effective_fact_ids(patient_id=pid, document_ids=unstored) if unstored else {}
    )
    candidates = []
    for r in rows:
        at = _iso_at(r["effective_at"])
        if at is None or (since_at and at < since_at):
            continue
        fact_id = r["fhir_id"] or computed[r["id"]]
        if after and (at, fact_id) <= after:
            continue
        candidates.append((at, fact_id, r))
    candidates.sort(key=lambda c: (c[0], c[1]))
    window = candidates[:limit]

    doc_ids = sorted({r["source_document_id"] for _, _, r in window if r["source_document_id"]})
    docs = {d["id"]: _feed_document(d)
            for d in await db.feed_documents(patient_id=pid, document_ids=doc_ids)}  # fmt: skip
    conversions = _conversions(await db.fact_code_vocabulary())

    out: dict = {
        "facts": [],
        "documents": [],
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "next_cursor": None,
    }
    if unknown:
        out["unknown_kinds"] = unknown
    size = len(_compact(out).encode()) + _FEED_CURSOR_RESERVE
    listed: set[int] = set()
    last = None
    for at, fact_id, r in window:
        fact = _feed_fact(r, fact_id, at, conversions)
        cost = len(_compact(fact).encode()) + 1
        doc_id = r["source_document_id"]
        new_doc = docs.get(doc_id) if doc_id not in listed else None
        if new_doc:
            cost += len(_compact(new_doc).encode()) + 1
        if out["facts"] and size + cost > FEED_MAX_BYTES:
            break
        size += cost
        out["facts"].append(fact)
        if new_doc:
            out["documents"].append(new_doc)
            listed.add(doc_id)
        last = (at, fact_id)
    if last and len(out["facts"]) < len(candidates):
        out["next_cursor"] = _encode_cursor(*last)
    return _compact(out)


def register(mcp):
    for fn in (
        patient_overview,
        timeline,
        fact_catalog,
        fact_series,
        document_detail,
        encounter_detail,
        people_and_places,
        dashboard_feed,
    ):
        mcp.tool(annotations=READ_ONLY)(fn)
