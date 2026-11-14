"""Lab trend tracking tools (#59)."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.curated.facts_rules import BIOMARKER_CATEGORIES
from oncofiles.models import LabTrendQuery, LabValue
from oncofiles.tools._helpers import (
    _clamp_limit,
    _gdrive_url,
    _get_db,
    _parse_date,
)

# ── facts model read path (#726) ──
# The lab readers below were written for the legacy ``lab_values`` table, which
# curated patients never fill: their values live in the clinical facts model
# (``clinical_records``, #603). A patient with coded lab facts is read from the
# facts model (deduplicated like fact_series, unverified values excluded unless
# asked); a patient without any keeps the legacy table, so nothing that still
# writes ``lab_values`` loses its reads. These tools are deprecated (#711 §4.4).
DEPRECATED = "[DEPRECATED — use fact_series / dashboard_feed; removed after 2027-01-10]"
LAB_FACT_CATEGORIES = ["lab", "tumor_marker", "vital"]
# legacy lab_values parameter names (the enhance.py prompt) → fact codes
LEGACY_PARAMETER_CODES = {
    "ABS_NEUT": "NEUT", "ANC": "NEUT", "ABS_LYMPH": "LYMPH", "ABS_MONO": "MONO",
    "ABS_EOS": "EOS", "ABS_BASO": "BASO", "GMT": "GGT", "BILIRUBIN": "BILI",
    "CREATININE": "CREA", "GLUCOSE": "GLU", "ALBUMIN": "ALB", "TOTAL_PROTEIN": "TP",
    "FIBRINOGEN": "FIB", "FERRITIN": "FERR", "IRON": "FE", "VITAMIN_D": "VITD",
}  # fmt: skip


class _FactLabs:
    """Fact-model lab rows for one patient, in the legacy tools' terms."""

    def __init__(self, db, pid: str, vocab: list[dict]):
        from oncofiles.tools.clinical_facts import _conversions

        self.db, self.pid = db, pid
        self.codes = {r["code"] for r in vocab}
        self.alias = {}
        for r in vocab:
            if r.get("category") in BIOMARKER_CATEGORIES:
                continue  # gene / marker symbols are never lab parameters
            for a in r["aliases"]:
                self.alias.setdefault(str(a).strip().upper(), r["code"])
        self.conversions = _conversions(vocab)

    @classmethod
    async def for_patient(cls, db, pid: str) -> _FactLabs | None:
        if not await db.has_coded_facts(patient_id=pid, categories=LAB_FACT_CATEGORIES):
            return None
        return cls(db, pid, await db.fact_code_vocabulary())

    def code_for(self, parameter: str) -> str | None:
        p = parameter.strip().upper()
        return p if p in self.codes else LEGACY_PARAMETER_CODES.get(p) or self.alias.get(p)

    async def lab_codes(self) -> list[str]:
        rows = await self.db.fact_catalog_rows(patient_id=self.pid)
        return [r["code"] for r in rows if r["code"] and r["category"] in LAB_FACT_CATEGORIES]

    async def rows(
        self, codes: list[str], date_from: str | None, date_to: str | None
    ) -> tuple[list[dict], list[dict]]:
        """Deduplicated rows (oldest first per code), split into (counted,
        unverified). Unverified values are never counted in a trend, delta or
        comparison: with include_unverified the tools only LIST them, apart
        and labelled."""
        from oncofiles.tools.clinical_facts import _dedupe

        if not codes:
            return [], []
        rows = await self.db.fact_series_rows(
            patient_id=self.pid, codes=sorted(set(codes)), date_from=date_from, date_to=date_to
        )
        rows, _ = _dedupe(rows)
        counted = [r for r in rows if r["verification"] != "unverified"]
        return counted, [r for r in rows if r["verification"] == "unverified"]

    def point(self, r: dict) -> dict:
        """value/unit/range in one unit, as the legacy tools name them."""
        from oncofiles.tools.clinical_facts import _observation

        obs = _observation(r, self.conversions)
        return {
            "value": obs["value"] if obs["value"] is not None else obs["value_text"],
            "unit": obs["unit"],
            "reference_low": obs["ref_low"],
            "reference_high": obs["ref_high"],
        }

    async def doc_urls(self, doc_ids: set[int]) -> dict[int, str | None]:
        docs = await self.db.get_documents_by_ids(doc_ids, patient_id=self.pid)
        return {did: _gdrive_url(doc.gdrive_id) if doc else None for did, doc in docs.items()}


def _numeric(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool)


async def store_lab_values(
    ctx: Context,
    document_id: int,
    lab_date: str,
    values: str,
    force: bool = False,
    patient_slug: str | None = None,
) -> str:
    """Store parsed lab values from a document for trend tracking.

    Includes deduplication checks:
    - If document_id already has stored values, returns skipped
      (unless force=True to update/replace).
    - If another document has values for the same lab_date, warns
      about collision (stores anyway but flags it).

    Standardized parameter names:
        WBC, ABS_NEUT, ABS_LYMPH, PLT, HGB, ANC, ALT, AST, GMT, ALP,
        BILIRUBIN, CREATININE, eGFR, CEA, CA19_9, SII, NE_LY_RATIO

    Args:
        document_id: Source document ID (should be a labs document).
        lab_date: Date of the lab test (YYYY-MM-DD).
        values: JSON array of objects, each with: parameter, value, unit,
                and optionally reference_low, reference_high, flag.
                Example: [{"parameter": "WBC", "value": 6.8, "unit": "10^9/L",
                          "reference_low": 4.0, "reference_high": 10.0,
                          "flag": ""}]
        force: If True, store even if document already has values
               (replaces existing via INSERT OR REPLACE).
        patient_slug: Optional — explicit patient slug (#429).
    """
    try:
        parsed_date = _parse_date(lab_date)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    db = _get_db(ctx)
    from oncofiles.tools._helpers import _resolve_patient_id

    pid = await _resolve_patient_id(patient_slug, ctx)

    # Patient isolation: verify caller owns this document
    if not await db.check_document_ownership(document_id, pid):
        return json.dumps({"error": f"Document not found: {document_id}"})
    doc = await db.get_document(document_id, patient_id=pid)
    if not doc:
        return json.dumps({"error": f"Document not found: {document_id}"})

    # Dedup check 1: document already has stored values
    existing = await db.get_lab_snapshot(document_id, patient_id=pid)
    if existing and not force:
        return json.dumps(
            {
                "action": "skipped",
                "reason": "already_stored",
                "document_id": document_id,
                "lab_date": parsed_date.isoformat(),
                "existing_parameters": [v.parameter for v in existing],
                "hint": "Set force=True to replace existing values.",
            }
        )

    try:
        raw_values = json.loads(values)
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid JSON: {e}"})

    if not isinstance(raw_values, list):
        return json.dumps({"error": "values must be a JSON array"})

    lab_values = []
    for item in raw_values:
        if not isinstance(item, dict) or "parameter" not in item or "value" not in item:
            continue
        lab_values.append(
            LabValue(
                document_id=document_id,
                lab_date=parsed_date,
                parameter=item["parameter"],
                value=float(item["value"]),
                unit=item.get("unit", ""),
                reference_low=item.get("reference_low"),
                reference_high=item.get("reference_high"),
                flag=item.get("flag", ""),
            )
        )

    if not lab_values:
        return json.dumps({"error": "No valid lab values found in input"})

    # Dedup check 2: same date collision from another document
    date_collision = None
    date_existing = await db.get_lab_values_by_date(
        parsed_date.isoformat(),
        patient_id=pid,
    )
    other_docs = {v.document_id for v in date_existing if v.document_id != document_id}
    if other_docs:
        date_collision = {
            "collision_document_ids": sorted(other_docs),
            "existing_parameters": list({v.parameter for v in date_existing}),
        }

    count = await db.insert_lab_values(lab_values)

    result = {
        "action": "stored",
        "stored": count,
        "document_id": document_id,
        "lab_date": parsed_date.isoformat(),
        "parameters": [v.parameter for v in lab_values],
    }
    if date_collision:
        result["warning"] = "date_collision"
        result["collision"] = date_collision
    if existing and force:
        result["note"] = "replaced_existing"

    return json.dumps(result)


async def get_lab_trends(
    ctx: Context,
    parameter: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
    patient_slug: str | None = None,
    include_unverified: bool = False,
) -> str:
    """[DEPRECATED — use fact_series / dashboard_feed; removed after 2027-01-10]
    Retrieve stored lab values for trend analysis.

    Returns values sorted chronologically (oldest first) for plotting trends.
    Patients with lab facts (curated folders) are read from the facts model;
    unverified values are never counted; include_unverified=true lists them apart, labelled.

    Args:
        parameter: Filter by parameter name (e.g. PLT, SII, CEA). If None, returns all.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return.
        patient_slug: Optional — explicit patient slug (#429).
        include_unverified: Facts model only — also list unverified values, apart
            (``unverified``) and labelled; they never enter trends or comparisons.
    """
    try:
        db = _get_db(ctx)
        from oncofiles.tools._helpers import _resolve_patient_id

        pid = await _resolve_patient_id(patient_slug, ctx)
        _parse_date(date_from)
        _parse_date(date_to)
        facts = await _FactLabs.for_patient(db, pid)
        if facts is not None:
            return await _fact_lab_trends(
                facts, parameter, date_from, date_to, _clamp_limit(limit), include_unverified
            )
        query = LabTrendQuery(
            parameter=parameter,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
            patient_id=pid,
        )
        values = await db.get_lab_trends(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # Batch-fetch gdrive_urls for all source documents (single query)
    doc_ids = {v.document_id for v in values}
    docs_map = await db.get_documents_by_ids(doc_ids, patient_id=pid)
    doc_urls: dict[int, str | None] = {
        did: _gdrive_url(doc.gdrive_id) if doc else None for did, doc in docs_map.items()
    }

    items = [
        {
            "lab_date": v.lab_date.isoformat(),
            "parameter": v.parameter,
            "value": v.value,
            "unit": v.unit,
            "reference_low": v.reference_low,
            "reference_high": v.reference_high,
            "flag": v.flag,
            "document_id": v.document_id,
            "gdrive_url": doc_urls.get(v.document_id),
        }
        for v in values
    ]
    return json.dumps({"values": items, "total": len(items)})


async def _fact_lab_trends(
    facts: _FactLabs,
    parameter: str | None,
    date_from: str | None,
    date_to: str | None,
    limit: int,
    include_unverified: bool,
) -> str:
    codes = [facts.code_for(parameter)] if parameter else await facts.lab_codes()
    rows, unverified = await facts.rows([c for c in codes if c], date_from, date_to)

    def ordered(rs: list[dict]) -> list[dict]:
        return sorted(rs, key=lambda r: (r["effective_at"] or "", r["code"]))[:limit]

    rows = ordered(rows)
    listed = ordered(unverified) if include_unverified else []
    urls = await facts.doc_urls(
        {r["source_document_id"] for r in rows + listed if r["source_document_id"]}
    )

    def item(r: dict) -> dict:
        return {
            "lab_date": (r["effective_at"] or "")[:10],
            "parameter": parameter if parameter else r["code"],
            **facts.point(r),
            "flag": r["flag"],
            "document_id": r["source_document_id"],
            "gdrive_url": urls.get(r["source_document_id"]),
            "verification": r["verification"],
        }

    out = {
        "values": [item(r) for r in rows],
        "total": len(rows),
        "source": "facts",
        "unverified_excluded": len(unverified),
    }
    if include_unverified:  # listed apart, labelled, never part of "values"
        out["unverified"] = [item(r) for r in listed]
    return json.dumps(out)


# mFOLFOX6 pre-cycle safety thresholds (NCCN Guidelines + SmPC)
_MFOLFOX6_THRESHOLDS = {
    "ABS_NEUT": {
        "min": 1.5,
        "unit": "10^9/L",
        "source": "NCCN Colon Cancer v1.2025, DOS-A: mFOLFOX6 pre-cycle requirements",
        "source_url": "https://www.nccn.org/guidelines/guidelines-detail?category=1&id=1428",
    },
    "PLT": {
        "min": 75.0,
        "unit": "10^9/L",
        "source": "NCCN Colon Cancer v1.2025, DOS-A + oxaliplatin SmPC",
        "source_url": "https://www.nccn.org/guidelines/guidelines-detail?category=1&id=1428",
    },
    "HGB": {
        "min": 80.0,
        "unit": "g/L",
        "source": "Institutional protocol (NOU Bratislava) — Hb ≥80 for chemo",
        "source_url": None,
    },
    "WBC": {
        "min": 3.0,
        "unit": "10^9/L",
        "source": "5-FU SmPC: WBC ≥3.0 before administration",
        "source_url": None,
    },
    "BILIRUBIN": {
        "max": 26.0,
        "unit": "µmol/L",
        "source": "Oxaliplatin SmPC: bilirubin ≤1.5× ULN (ULN ~17 µmol/L)",
        "source_url": None,
    },
    "CREATININE": {
        "max": 132.6,
        "unit": "µmol/L",
        "source": "Oxaliplatin SmPC: creatinine ≤1.5× ULN (ULN ~88.4 µmol/L)",
        "source_url": None,
    },
    "ALT": {
        "max": 135.0,
        "unit": "U/L",
        "source": "5-FU SmPC: ALT ≤3× ULN (ULN ~45 U/L); ≤5× if hepatic mets",
        "source_url": None,
    },
    "AST": {
        "max": 105.0,
        "unit": "U/L",
        "source": "5-FU SmPC: AST ≤3× ULN (ULN ~35 U/L); ≤5× if hepatic mets",
        "source_url": None,
    },
    "eGFR": {
        "min": 30.0,
        "unit": "mL/min",
        "source": "Oxaliplatin SmPC: GFR ≥30 (dose reduce if 30-60)",
        "source_url": None,
    },
}

# General health reference ranges (EU/WHO/ESC guidelines)
_GENERAL_HEALTH_THRESHOLDS = {
    "GLUCOSE": {
        "min": 3.9,
        "max": 5.6,
        "unit": "mmol/L",
        "source": "WHO: fasting plasma glucose normal range",
        "source_url": None,
    },
    "CHOLESTEROL_TOTAL": {
        "max": 5.0,
        "unit": "mmol/L",
        "source": "ESC/EAS 2019 Dyslipidaemia Guidelines: desirable total cholesterol",
        "source_url": "https://doi.org/10.1093/eurheartj/ehz455",
    },
    "HDL": {
        "min": 1.0,
        "unit": "mmol/L",
        "source": "ESC/EAS 2019: low HDL threshold (male)",
        "source_url": "https://doi.org/10.1093/eurheartj/ehz455",
    },
    "LDL": {
        "max": 3.0,
        "unit": "mmol/L",
        "source": "ESC/EAS 2019: moderate CV risk target LDL <3.0",
        "source_url": "https://doi.org/10.1093/eurheartj/ehz455",
    },
    "TRIGLYCERIDES": {
        "max": 1.7,
        "unit": "mmol/L",
        "source": "ESC/EAS 2019: desirable fasting triglycerides",
        "source_url": "https://doi.org/10.1093/eurheartj/ehz455",
    },
    "HBA1C": {
        "max": 42.0,
        "unit": "mmol/mol",
        "source": "WHO: HbA1c <42 mmol/mol (6.0%) = normal; 42-47 = prediabetes",
        "source_url": None,
    },
    "TSH": {
        "min": 0.4,
        "max": 4.0,
        "unit": "mIU/L",
        "source": "ATA Guidelines: normal TSH reference range",
        "source_url": None,
    },
    "VITAMIN_D": {
        "min": 75.0,
        "unit": "nmol/L",
        "source": "Endocrine Society: 25(OH)D sufficiency ≥75 nmol/L (30 ng/mL)",
        "source_url": None,
    },
    "FERRITIN": {
        "min": 30.0,
        "max": 300.0,
        "unit": "µg/L",
        "source": "WHO: adult male ferritin reference range",
        "source_url": None,
    },
    "PSA": {
        "max": 4.0,
        "unit": "ng/mL",
        "source": "EAU Guidelines: PSA screening threshold (age-adjusted shared decision)",
        "source_url": None,
    },
    # Standard CBC with general population ranges
    "WBC": {
        "min": 4.0,
        "max": 10.0,
        "unit": "10^9/L",
        "source": "Standard adult reference range",
        "source_url": None,
    },
    "HGB": {
        "min": 130.0,
        "max": 175.0,
        "unit": "g/L",
        "source": "WHO: adult male haemoglobin reference range",
        "source_url": None,
    },
    "PLT": {
        "min": 150.0,
        "max": 400.0,
        "unit": "10^9/L",
        "source": "Standard adult platelet reference range",
        "source_url": None,
    },
    "CREATININE": {
        "min": 62.0,
        "max": 106.0,
        "unit": "µmol/L",
        "source": "Standard adult male creatinine reference range",
        "source_url": None,
    },
    "eGFR": {
        "min": 90.0,
        "unit": "mL/min/1.73m2",
        "source": "KDIGO: eGFR ≥90 = normal; 60-89 = mild decrease",
        "source_url": None,
    },
    "ALT": {
        "max": 45.0,
        "unit": "U/L",
        "source": "Standard adult male ALT upper limit of normal",
        "source_url": None,
    },
    "AST": {
        "max": 35.0,
        "unit": "U/L",
        "source": "Standard adult male AST upper limit of normal",
        "source_url": None,
    },
    "BILIRUBIN": {
        "max": 17.0,
        "unit": "µmol/L",
        "source": "Standard adult total bilirubin upper limit of normal",
        "source_url": None,
    },
}


def _get_thresholds(patient_id: str) -> dict:
    """Return the appropriate lab thresholds for a patient based on patient_type."""
    from oncofiles.patient_context import get_context

    patient_type = get_context(patient_id).get("patient_type", "oncology")
    if patient_type == "general":
        return _GENERAL_HEALTH_THRESHOLDS
    return _MFOLFOX6_THRESHOLDS


async def get_lab_safety_check(ctx: Context, patient_slug: str | None = None) -> str:
    """Lab safety check against thresholds appropriate for the patient type.

    For oncology patients: mFOLFOX6 pre-cycle thresholds (NCCN + SmPC).
    For general patients: standard health reference ranges (EU/WHO/ESC).

    For each safety parameter, returns:
    - The threshold (min or max) with source/guideline reference
    - The patient's most recent value with date and source document
    - Safety status: green (safe), red (unsafe), yellow (borderline ±10%)
    - Clickable gdrive_url to verify the source lab document
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _resolve_patient_id

    pid = await _resolve_patient_id(patient_slug, ctx)
    thresholds = _get_thresholds(pid)
    results = []

    for param, threshold in thresholds.items():
        entry: dict = {
            "parameter": param,
            "unit": threshold["unit"],
            "threshold_source": threshold["source"],
            "threshold_source_url": threshold.get("source_url"),
        }

        # Determine threshold direction
        if "min" in threshold:
            entry["threshold_min"] = threshold["min"]
            entry["threshold_type"] = "min"
        if "max" in threshold:
            entry["threshold_max"] = threshold["max"]
            entry["threshold_type"] = "max"

        # Get latest value
        latest = await db.get_latest_lab_value(param, patient_id=pid)
        if latest:
            entry["last_value"] = latest.value
            entry["last_date"] = latest.lab_date.isoformat()
            entry["last_document_id"] = latest.document_id

            # Get source document for gdrive_url
            doc = await db.get_document(latest.document_id, patient_id=pid)
            if doc:
                entry["last_document_filename"] = doc.filename
                entry["last_document_gdrive_url"] = _gdrive_url(doc.gdrive_id)

            # Evaluate safety
            value = latest.value
            if "min" in threshold:
                t = threshold["min"]
                if value >= t:
                    entry["status"] = "green"
                elif value >= t * 0.9:
                    entry["status"] = "yellow"
                else:
                    entry["status"] = "red"
            elif "max" in threshold:
                t = threshold["max"]
                if value <= t:
                    entry["status"] = "green"
                elif value <= t * 1.1:
                    entry["status"] = "yellow"
                else:
                    entry["status"] = "red"
        else:
            entry["last_value"] = None
            entry["status"] = "missing"

        results.append(entry)

    # Summary counts
    statuses = [r["status"] for r in results]
    summary = {
        "green": statuses.count("green"),
        "yellow": statuses.count("yellow"),
        "red": statuses.count("red"),
        "missing": statuses.count("missing"),
    }
    safe = summary["red"] == 0 and summary["missing"] == 0

    from oncofiles.tools._helpers import _with_clinical_disclaimer

    return json.dumps(
        _with_clinical_disclaimer(
            {
                "protocol": "mFOLFOX6",
                "cycle_safe": safe,
                "summary": summary,
                "parameters": results,
            }
        )
    )


# Full pre-cycle checklist: labs + toxicity + VTE + general
_PRECYCLE_CHECKLIST = {
    "lab_safety": {
        "title": "Laboratórna bezpečnosť",
        "items": [
            {
                "id": "anc",
                "text": "ANC >= 1,500/µL",
                "parameter": "ABS_NEUT",
                "source": "NCCN Colon Cancer v1.2025, DOS-A",
                "source_url": "https://www.nccn.org/guidelines/guidelines-detail"
                "?category=1&id=1428",
            },
            {
                "id": "plt",
                "text": "PLT >= 75,000/µL (tiež >= 50,000 pre plnú dávku LMWH)",
                "parameter": "PLT",
                "source": "NCCN Colon Cancer v1.2025 + LMWH SmPC",
                "source_url": "https://www.nccn.org/guidelines/guidelines-detail"
                "?category=1&id=1428",
            },
            {
                "id": "creatinine",
                "text": "Kreatinín <= 1.5× HHN",
                "parameter": "CREATININE",
                "source": "Oxaliplatin SmPC: creatinine ≤1.5× ULN",
                "source_url": None,
            },
            {
                "id": "alt_ast",
                "text": "ALT/AST <= 5× HHN (prah pri pečeňových metastázach)",
                "parameter": "ALT",
                "source": "5-FU SmPC: ALT/AST ≤5× ULN if hepatic mets",
                "source_url": None,
            },
            {
                "id": "bilirubin",
                "text": "Bilirubín <= 1.5× HHN",
                "parameter": "BILIRUBIN",
                "source": "Oxaliplatin SmPC: bilirubin ≤1.5× ULN",
                "source_url": None,
            },
        ],
    },
    "toxicity_assessment": {
        "title": "Hodnotenie toxicity (NCI-CTC)",
        "items": [
            {
                "id": "neuropathy",
                "text": "Stupeň periférnej neuropatie",
                "parameter": None,
                "source": "NCI-CTCAE v5.0, Nervous system — Peripheral sensory neuropathy",
                "source_url": "https://ctep.cancer.gov/protocoldevelopment/"
                "electronic_applications/ctc.htm",
            },
            {
                "id": "diarrhea",
                "text": "Stupeň hnačky",
                "parameter": None,
                "source": "NCI-CTCAE v5.0, Gastrointestinal — Diarrhea",
                "source_url": "https://ctep.cancer.gov/protocoldevelopment/"
                "electronic_applications/ctc.htm",
            },
            {
                "id": "mucositis",
                "text": "Stupeň mukozitídy",
                "parameter": None,
                "source": "NCI-CTCAE v5.0, Gastrointestinal — Mucositis oral",
                "source_url": "https://ctep.cancer.gov/protocoldevelopment/"
                "electronic_applications/ctc.htm",
            },
            {
                "id": "fatigue",
                "text": "Stupeň únavy",
                "parameter": None,
                "source": "NCI-CTCAE v5.0, General — Fatigue",
                "source_url": "https://ctep.cancer.gov/protocoldevelopment/"
                "electronic_applications/ctc.htm",
            },
            {
                "id": "hfs",
                "text": "Stupeň syndrómu ruka-noha",
                "parameter": None,
                "source": "NCI-CTCAE v5.0, Skin — Palmar-plantar erythrodysesthesia",
                "source_url": "https://ctep.cancer.gov/protocoldevelopment/"
                "electronic_applications/ctc.htm",
            },
            {
                "id": "nausea",
                "text": "Stupeň nauzey/vracania",
                "parameter": None,
                "source": "NCI-CTCAE v5.0, Gastrointestinal — Nausea/Vomiting",
                "source_url": "https://ctep.cancer.gov/protocoldevelopment/"
                "electronic_applications/ctc.htm",
            },
        ],
    },
    "vte_monitoring": {
        "title": "Monitorovanie VTE (žilovej trombózy)",
        "items": [
            {
                "id": "plt_clexane",
                "text": "PLT dostatočné pre pokračovanie antikoagulancia (LMWH)",
                "parameter": "PLT",
                "source": "LMWH SmPC: monitor PLT, risk of HIT",
                "source_url": None,
            },
            {
                "id": "dvt_symptoms",
                "text": "Žiadne nové príznaky DVT/PE (opuch nôh, dýchavica, bolesť)",
                "parameter": None,
                "source": "ACCP Guidelines 10th Ed — VTE in cancer patients",
                "source_url": "https://pubmed.ncbi.nlm.nih.gov/26867832/",
            },
            {
                "id": "clexane_adherence",
                "text": "Potvrdená adherencia k antikoagulačnej liečbe (LMWH)",
                "parameter": None,
                "source": "ASCO VTE Prophylaxis Guideline 2023",
                "source_url": "https://pubmed.ncbi.nlm.nih.gov/36652645/",
            },
        ],
    },
    "general_assessment": {
        "title": "Všeobecné hodnotenie",
        "items": [
            {
                "id": "ecog",
                "text": "ECOG výkonnostný stav",
                "parameter": None,
                "source": "NCCN Colon Cancer v1.2025 — ECOG PS assessment",
                "source_url": "https://www.nccn.org/guidelines/guidelines-detail"
                "?category=1&id=1428",
            },
            {
                "id": "weight",
                "text": "Hmotnosť + zmena oproti východiskovej",
                "parameter": None,
                "source": "ESPEN Guidelines on nutrition in cancer patients 2021",
                "source_url": "https://pubmed.ncbi.nlm.nih.gov/33946039/",
            },
            {
                "id": "nutrition",
                "text": "Nutričný stav adekvátny",
                "parameter": None,
                "source": "ESPEN Guidelines on nutrition in cancer patients 2021",
                "source_url": "https://pubmed.ncbi.nlm.nih.gov/33946039/",
            },
        ],
    },
}


async def get_precycle_checklist(
    ctx: Context, cycle_number: int = 3, patient_slug: str | None = None
) -> str:
    """Get the full pre-cycle checklist for mFOLFOX6 with source references.

    Returns all checklist sections (lab safety, toxicity, VTE, general)
    with guideline source URLs for each item. Lab items include the
    patient's latest value and safety status.

    Args:
        cycle_number: Current cycle number (for display context).
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _resolve_patient_id

    pid = await _resolve_patient_id(patient_slug, ctx)
    sections = []

    for section_key, section in _PRECYCLE_CHECKLIST.items():
        items = []
        for item in section["items"]:
            entry = {
                "id": item["id"],
                "text": item["text"],
                "source": item["source"],
                "source_url": item["source_url"],
            }

            # For items linked to a lab parameter, fetch latest value
            if item["parameter"]:
                latest = await db.get_latest_lab_value(item["parameter"], patient_id=pid)
                if latest:
                    entry["last_value"] = latest.value
                    entry["last_date"] = latest.lab_date.isoformat()
                    entry["last_document_id"] = latest.document_id
                    doc = await db.get_document(latest.document_id, patient_id=pid)
                    if doc:
                        entry["last_document_gdrive_url"] = _gdrive_url(doc.gdrive_id)

            items.append(entry)

        sections.append(
            {
                "section": section_key,
                "title": section["title"],
                "items": items,
            }
        )

    from oncofiles.tools._helpers import _with_clinical_disclaimer

    return json.dumps(
        _with_clinical_disclaimer(
            {
                "protocol": "mFOLFOX6",
                "cycle": cycle_number,
                "sections": sections,
            }
        )
    )


async def get_lab_time_series(
    ctx: Context,
    parameters: str,
    date_from: str | None = None,
    date_to: str | None = None,
    patient_slug: str | None = None,
    include_unverified: bool = False,
) -> str:
    """[DEPRECATED — use fact_series / dashboard_feed; removed after 2027-01-10]
    Get structured time series data for one or more lab parameters.

    Returns chronological values with reference ranges, units, and computed
    deltas (absolute change and % change between consecutive measurements).
    Patients with lab facts (curated folders) are read from the facts model:
    values in the code's canonical unit, duplicates removed as in fact_series,
    unverified values never in a series or delta (include_unverified=true lists
    them apart, labelled).

    Args:
        parameters: Comma-separated parameter names (e.g. "CEA,CA19_9" or "PLT").
        date_from: Start date filter (YYYY-MM-DD). Optional.
        date_to: End date filter (YYYY-MM-DD). Optional.
        patient_slug: Optional — explicit patient slug (#429).
        include_unverified: Facts model only — also list unverified values, apart
            (``unverified``) and labelled; they never enter trends or comparisons.
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _resolve_patient_id

    pid = await _resolve_patient_id(patient_slug, ctx)
    param_list = [p.strip() for p in parameters.split(",") if p.strip()]
    if not param_list:
        return json.dumps({"error": "No parameters specified"})

    try:
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    facts = await _FactLabs.for_patient(db, pid)
    if facts is not None:
        return await _fact_time_series(facts, param_list, date_from, date_to, include_unverified)

    result: dict = {"parameters": {}}
    for param in param_list:
        query = LabTrendQuery(
            parameter=param, date_from=parsed_from, date_to=parsed_to, limit=200, patient_id=pid
        )
        values = await db.get_lab_trends(query)
        series = []
        prev_value = None
        for v in values:
            point: dict = {
                "date": v.lab_date.isoformat(),
                "value": v.value,
                "unit": v.unit,
                "reference_low": v.reference_low,
                "reference_high": v.reference_high,
                "flag": v.flag,
                "document_id": v.document_id,
            }
            if prev_value is not None:
                point["delta"] = round(v.value - prev_value, 4)
                if prev_value != 0:
                    point["delta_pct"] = round((v.value - prev_value) / abs(prev_value) * 100, 1)
            prev_value = v.value
            series.append(point)

        result["parameters"][param] = {
            "count": len(series),
            "series": series,
        }

    return json.dumps(result)


async def _fact_time_series(
    facts: _FactLabs,
    param_list: list[str],
    date_from: str | None,
    date_to: str | None,
    include_unverified: bool,
) -> str:
    codes = {p: facts.code_for(p) for p in param_list}
    rows, unverified = await facts.rows([c for c in codes.values() if c], date_from, date_to)

    def point_of(r: dict) -> dict:
        return {
            "date": (r["effective_at"] or "")[:10],
            **facts.point(r),
            "flag": r["flag"],
            "document_id": r["source_document_id"],
            "at": r["effective_at"],
            "verification": r["verification"],
        }

    result: dict = {"parameters": {}, "source": "facts"}
    for param in param_list:
        series: list[dict] = []
        hidden = [point_of(r) for r in unverified if codes[param] and r["code"] == codes[param]]
        prev = None  # (value, unit) of the previous numeric point
        for r in (r for r in rows if codes[param] and r["code"] == codes[param]):
            point = point_of(r)
            value = point["value"]
            if _numeric(value):
                if prev is not None and prev[1] == point["unit"]:
                    point["delta"] = round(value - prev[0], 4)
                    if prev[0] != 0:
                        point["delta_pct"] = round((value - prev[0]) / abs(prev[0]) * 100, 1)
                prev = (value, point["unit"])
            series.append(point)
        result["parameters"][param] = {
            "count": len(series),
            "series": series,
            "code": codes[param],
            "unverified_excluded": len(hidden),
        }
        if include_unverified:  # listed apart, labelled, never in series or deltas
            result["parameters"][param]["unverified"] = hidden
    return json.dumps(result)


async def compare_lab_panels(
    ctx: Context,
    date_a: str,
    date_b: str,
    patient_slug: str | None = None,
    include_unverified: bool = False,
) -> str:
    """[DEPRECATED — use fact_series / dashboard_feed; removed after 2027-01-10]
    Compare lab values between two dates side-by-side.

    Returns all parameters measured on both dates with change direction,
    absolute delta, percentage change, and out-of-range flags. Patients with
    lab facts (curated folders) are read from the facts model; unverified
    values are never compared (include_unverified=true lists them apart).

    Args:
        date_a: First date (YYYY-MM-DD), typically the earlier measurement.
        date_b: Second date (YYYY-MM-DD), typically the later measurement.
        patient_slug: Optional — explicit patient slug (#429).
        include_unverified: Facts model only — also list unverified values, apart
            (``unverified``) and labelled; they never enter trends or comparisons.
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _resolve_patient_id

    pid = await _resolve_patient_id(patient_slug, ctx)
    try:
        _parse_date(date_a)
        _parse_date(date_b)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    facts = await _FactLabs.for_patient(db, pid)
    if facts is not None:
        return await _fact_compare(facts, date_a, date_b, include_unverified)

    values_a = await db.get_lab_values_by_date(date_a, patient_id=pid)
    values_b = await db.get_lab_values_by_date(date_b, patient_id=pid)

    map_a = {v.parameter: v for v in values_a}
    map_b = {v.parameter: v for v in values_b}
    all_params = sorted(set(map_a.keys()) | set(map_b.keys()))

    comparisons = []
    for param in all_params:
        entry: dict = {"parameter": param}
        va = map_a.get(param)
        vb = map_b.get(param)

        if va:
            entry["date_a"] = {
                "date": date_a,
                "value": va.value,
                "unit": va.unit,
                "reference_low": va.reference_low,
                "reference_high": va.reference_high,
            }
        if vb:
            entry["date_b"] = {
                "date": date_b,
                "value": vb.value,
                "unit": vb.unit,
                "reference_low": vb.reference_low,
                "reference_high": vb.reference_high,
            }

        if va and vb:
            delta = round(vb.value - va.value, 4)
            entry["delta"] = delta
            if va.value != 0:
                entry["delta_pct"] = round(delta / abs(va.value) * 100, 1)
            entry["direction"] = "rising" if delta > 0 else "falling" if delta < 0 else "stable"
            # Flag if moved out of range
            ref_hi = vb.reference_high
            ref_lo = vb.reference_low
            if ref_hi is not None and vb.value > ref_hi:
                entry["status"] = "above_range"
            elif ref_lo is not None and vb.value < ref_lo:
                entry["status"] = "below_range"
            else:
                entry["status"] = "in_range"
        else:
            entry["status"] = "only_one_date"

        comparisons.append(entry)

    return json.dumps(
        {
            "date_a": date_a,
            "date_b": date_b,
            "parameters": comparisons,
            "total": len(comparisons),
            "available_dates": await db.get_distinct_lab_dates(patient_id=pid),
        }
    )


async def _fact_compare(
    facts: _FactLabs, date_a: str, date_b: str, include_unverified: bool
) -> str:
    codes = await facts.lab_codes()
    panels: list[dict[str, dict]] = []
    unverified: list[dict] = []
    for day in (date_a, date_b):  # sequential: one DB connection
        rows, apart = await facts.rows(codes, day, day)
        unverified += apart
        panel: dict[str, dict] = {}
        for r in rows:  # the day's latest value, the best verified among equal times
            cur = panel.get(r["code"])
            if cur is None or (r["effective_at"] or "") > (cur["effective_at"] or ""):
                panel[r["code"]] = r
        panels.append(panel)
    map_a, map_b = panels
    comparisons = []
    for code in sorted(set(map_a) | set(map_b)):
        entry: dict = {"parameter": code}
        pa = facts.point(map_a[code]) if code in map_a else None
        pb = facts.point(map_b[code]) if code in map_b else None
        if pa:
            entry["date_a"] = {"date": date_a, **pa}
        if pb:
            entry["date_b"] = {"date": date_b, **pb}
        comparable = bool(pa and pb) and pa["unit"] == pb["unit"]
        if comparable and _numeric(pa["value"]) and _numeric(pb["value"]):
            delta = round(pb["value"] - pa["value"], 4)
            entry["delta"] = delta
            if pa["value"] != 0:
                entry["delta_pct"] = round(delta / abs(pa["value"]) * 100, 1)
            entry["direction"] = "rising" if delta > 0 else "falling" if delta < 0 else "stable"
            if pb["reference_high"] is not None and pb["value"] > pb["reference_high"]:
                entry["status"] = "above_range"
            elif pb["reference_low"] is not None and pb["value"] < pb["reference_low"]:
                entry["status"] = "below_range"
            else:
                entry["status"] = "in_range"
        elif pa and pb:
            entry["status"] = "not_comparable"  # text value or different units
        else:
            entry["status"] = "only_one_date"
        comparisons.append(entry)
    out = {
        "date_a": date_a,
        "date_b": date_b,
        "parameters": comparisons,
        "total": len(comparisons),
        "available_dates": await facts.db.fact_days(
            patient_id=facts.pid, categories=LAB_FACT_CATEGORIES
        ),
        "source": "facts",
        "unverified_excluded": len(unverified),
    }
    if include_unverified:  # listed apart, labelled, never compared
        out["unverified"] = [
            {"parameter": r["code"], "date": (r["effective_at"] or "")[:10], **facts.point(r),
             "document_id": r["source_document_id"], "verification": r["verification"]}
            for r in unverified
        ]  # fmt: skip
    return json.dumps(out)


async def get_lab_summary(ctx: Context, patient_slug: str | None = None) -> str:
    """[DEPRECATED — use patient_overview / fact_series / dashboard_feed; removed after 2027-01-10]
    Get a summary of the latest value for every tracked lab parameter.

    Reads the legacy lab_values table only: curated patients' values live in
    the facts model, use patient_overview (latest key values) or fact_series.
    Returns status (normal/high/low), trend direction (rising/falling/stable),
    days since last measurement, and computed indices (SII, Ne/Ly ratio).
    Designed as a quick overview for clinical decision support.

    Args:
        patient_slug: Optional — explicit patient slug (#429).
    """
    from datetime import date as date_type

    from oncofiles.tools._helpers import _resolve_patient_id

    db = _get_db(ctx)
    pid = await _resolve_patient_id(patient_slug, ctx)
    latest_values = await db.get_all_latest_lab_values(patient_id=pid)
    # Batch-fetch previous values for trend calculation (single query instead of N)
    previous_values = await db.get_previous_lab_values(patient_id=pid)

    today = date_type.today()
    summaries = []

    for v in latest_values:
        entry: dict = {
            "parameter": v.parameter,
            "value": v.value,
            "unit": v.unit,
            "date": v.lab_date.isoformat(),
            "days_ago": (today - v.lab_date).days,
            "document_id": v.document_id,
            "reference_low": v.reference_low,
            "reference_high": v.reference_high,
        }

        # Status based on reference range
        if v.reference_high is not None and v.value > v.reference_high:
            entry["status"] = "high"
        elif v.reference_low is not None and v.value < v.reference_low:
            entry["status"] = "low"
        elif v.reference_low is not None or v.reference_high is not None:
            entry["status"] = "normal"
        else:
            entry["status"] = "no_range"

        # Trend: compare with previous value (from batch-fetched data)
        prev_val = previous_values.get(v.parameter)
        if prev_val:
            if v.value > prev_val.value * 1.05:
                entry["trend"] = "rising"
            elif v.value < prev_val.value * 0.95:
                entry["trend"] = "falling"
            else:
                entry["trend"] = "stable"
        else:
            entry["trend"] = "insufficient_data"

        summaries.append(entry)

    # Compute derived indices from latest values
    param_map = {v.parameter: v.value for v in latest_values}
    computed = []
    neut = param_map.get("ABS_NEUT")
    lymph = param_map.get("ABS_LYMPH")
    plt = param_map.get("PLT")

    if neut is not None and lymph is not None and plt is not None and lymph > 0:
        sii = round((neut * plt) / lymph, 1)
        computed.append(
            {
                "parameter": "SII",
                "value": sii,
                "formula": "(ABS_NEUT × PLT) / ABS_LYMPH",
                "interpretation": "high" if sii > 1800 else "normal",
                "threshold": 1800,
            }
        )

    if neut is not None and lymph is not None and lymph > 0:
        ne_ly = round(neut / lymph, 2)
        computed.append(
            {
                "parameter": "NE_LY_RATIO",
                "value": ne_ly,
                "formula": "ABS_NEUT / ABS_LYMPH",
                "interpretation": (
                    "poor_prognosis"
                    if ne_ly > 3.0
                    else "improving"
                    if ne_ly < 2.5
                    else "borderline"
                ),
                "thresholds": {"poor": 3.0, "improving": 2.5},
            }
        )

    return json.dumps(
        {
            "parameters": summaries,
            "computed_indices": computed,
            "total_parameters": len(summaries),
            "available_dates": await db.get_distinct_lab_dates(patient_id=pid),
        }
    )


async def get_preventive_care_status(ctx: Context, patient_slug: str | None = None) -> str:
    """Get EU preventive care screening compliance for a general health patient.

    Evaluates which screenings (colonoscopy, dental, ophthalmology, PSA, etc.)
    are up-to-date, due soon, overdue, or never done — based on patient age,
    sex, and treatment_events history.

    Only available for patients with patient_type="general" in their context.
    Requires date_of_birth and sex in patient context.

    Returns a compliance report with actionable screening status for each
    applicable protocol.

    Args:
        patient_slug: Optional — explicit patient slug (#429).
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _resolve_patient_id

    pid = await _resolve_patient_id(patient_slug, ctx)

    from oncofiles.preventive_care import get_preventive_care_status as _get_status

    return await _get_status(db, pid)


def register(mcp):
    mcp.tool()(store_lab_values)
    mcp.tool()(get_lab_trends)
    mcp.tool()(get_lab_safety_check)
    mcp.tool()(get_precycle_checklist)
    mcp.tool()(get_lab_time_series)
    mcp.tool()(compare_lab_panels)
    mcp.tool()(get_lab_summary)
    mcp.tool()(get_preventive_care_status)
