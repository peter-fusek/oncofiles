"""Lab trend tracking tools (#59)."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import LabTrendQuery, LabValue
from oncofiles.tools._helpers import (
    _clamp_limit,
    _gdrive_url,
    _get_db,
    _parse_date,
)


async def store_lab_values(
    ctx: Context,
    document_id: int,
    lab_date: str,
    values: str,
    force: bool = False,
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
    """
    try:
        parsed_date = _parse_date(lab_date)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    db = _get_db(ctx)
    from oncofiles.tools._helpers import _get_patient_id

    pid = _get_patient_id()

    # Verify document exists
    doc = await db.get_document(document_id)
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
) -> str:
    """Retrieve stored lab values for trend analysis.

    Returns values sorted chronologically (oldest first) for plotting trends.

    Args:
        parameter: Filter by parameter name (e.g. PLT, SII, CEA). If None, returns all.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        from oncofiles.tools._helpers import _get_patient_id

        query = LabTrendQuery(
            parameter=parameter,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
            patient_id=_get_patient_id(),
        )
        values = await db.get_lab_trends(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # Batch-fetch gdrive_urls for all source documents (single query)
    doc_ids = {v.document_id for v in values}
    docs_map = await db.get_documents_by_ids(doc_ids)
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


async def get_lab_safety_check(ctx: Context) -> str:
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
    from oncofiles.tools._helpers import _get_patient_id

    pid = _get_patient_id()
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
            doc = await db.get_document(latest.document_id)
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

    return json.dumps(
        {
            "protocol": "mFOLFOX6",
            "cycle_safe": safe,
            "summary": summary,
            "parameters": results,
        }
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
                "text": "PLT >= 75,000/µL (tiež >= 50,000 pre plnú dávku [MEDICATION_REDACTED])",
                "parameter": "PLT",
                "source": "NCCN Colon Cancer v1.2025 + [MEDICATION_REDACTED] SmPC",
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
                "source": "5-FU SmPC: ALT/AST ≤5× ULN if hepatic mets ([CODE_REDACTED])",
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
                "text": "PLT dostatočné pre pokračovanie [MEDICATION_REDACTED]",
                "parameter": "PLT",
                "source": "[MEDICATION_REDACTED] SmPC: monitor PLT, risk of HIT",
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
                "text": "Potvrdená adherencia [MEDICATION_REDACTED]",
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


async def get_precycle_checklist(ctx: Context, cycle_number: int = 3) -> str:
    """Get the full pre-cycle checklist for mFOLFOX6 with source references.

    Returns all checklist sections (lab safety, toxicity, VTE, general)
    with guideline source URLs for each item. Lab items include the
    patient's latest value and safety status.

    Args:
        cycle_number: Current cycle number (for display context).
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _get_patient_id

    pid = _get_patient_id()
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
                    doc = await db.get_document(latest.document_id)
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

    return json.dumps(
        {
            "protocol": "mFOLFOX6",
            "cycle": cycle_number,
            "sections": sections,
        }
    )


async def get_lab_time_series(
    ctx: Context,
    parameters: str,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Get structured time series data for one or more lab parameters.

    Returns chronological values with reference ranges, units, and computed
    deltas (absolute change and % change between consecutive measurements).
    Designed for Oncoteam and MCP clients to build trend charts and analysis.

    Args:
        parameters: Comma-separated parameter names (e.g. "CEA,CA19_9" or "PLT").
        date_from: Start date filter (YYYY-MM-DD). Optional.
        date_to: End date filter (YYYY-MM-DD). Optional.
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _get_patient_id

    pid = _get_patient_id()
    param_list = [p.strip() for p in parameters.split(",") if p.strip()]
    if not param_list:
        return json.dumps({"error": "No parameters specified"})

    try:
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
    except ValueError as e:
        return json.dumps({"error": str(e)})

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


async def compare_lab_panels(
    ctx: Context,
    date_a: str,
    date_b: str,
) -> str:
    """Compare lab values between two dates side-by-side.

    Returns all parameters measured on both dates with change direction,
    absolute delta, percentage change, and out-of-range flags.

    Args:
        date_a: First date (YYYY-MM-DD), typically the earlier measurement.
        date_b: Second date (YYYY-MM-DD), typically the later measurement.
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _get_patient_id

    pid = _get_patient_id()
    try:
        _parse_date(date_a)
        _parse_date(date_b)
    except ValueError as e:
        return json.dumps({"error": str(e)})

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


async def get_lab_summary(ctx: Context) -> str:
    """Get a summary of the latest value for every tracked lab parameter.

    Returns status (normal/high/low), trend direction (rising/falling/stable),
    days since last measurement, and computed indices (SII, Ne/Ly ratio).
    Designed as a quick overview for clinical decision support.
    """
    from datetime import date as date_type

    from oncofiles.tools._helpers import _get_patient_id

    db = _get_db(ctx)
    pid = _get_patient_id()
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


async def get_preventive_care_status(ctx: Context) -> str:
    """Get EU preventive care screening compliance for a general health patient.

    Evaluates which screenings (colonoscopy, dental, ophthalmology, PSA, etc.)
    are up-to-date, due soon, overdue, or never done — based on patient age,
    sex, and treatment_events history.

    Only available for patients with patient_type="general" in their context.
    Requires date_of_birth and sex in patient context.

    Returns a compliance report with actionable screening status for each
    applicable protocol.
    """
    db = _get_db(ctx)
    from oncofiles.tools._helpers import _get_patient_id

    pid = _get_patient_id()

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
