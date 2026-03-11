"""Lab trend tracking tools (#59)."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import LabTrendQuery, LabValue
from oncofiles.tools._helpers import _clamp_limit, _gdrive_url, _get_db, _parse_date


async def store_lab_values(
    ctx: Context,
    document_id: int,
    lab_date: str,
    values: str,
) -> str:
    """Store parsed lab values from a document for trend tracking.

    Called by Oncoteam after analyzing a lab document. Values are stored
    with INSERT OR REPLACE — safe to call multiple times for the same document.

    Standardized parameter names:
        WBC, ABS_NEUT, ABS_LYMPH, PLT, HGB, ANC, ALT, AST, GMT, ALP,
        BILIRUBIN, CREATININE, eGFR, CEA, CA19_9, SII, NE_LY_RATIO

    Args:
        document_id: Source document ID (should be a labs document).
        lab_date: Date of the lab test (YYYY-MM-DD).
        values: JSON array of objects, each with: parameter, value, unit,
                and optionally reference_low, reference_high, flag.
                Example: [{"parameter": "WBC", "value": 6.8, "unit": "10^9/L",
                          "reference_low": 4.0, "reference_high": 10.0, "flag": ""}]
    """
    try:
        parsed_date = _parse_date(lab_date)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    db = _get_db(ctx)

    # Verify document exists
    doc = await db.get_document(document_id)
    if not doc:
        return json.dumps({"error": f"Document not found: {document_id}"})

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

    count = await db.insert_lab_values(lab_values)
    return json.dumps(
        {
            "stored": count,
            "document_id": document_id,
            "lab_date": parsed_date.isoformat(),
            "parameters": [v.parameter for v in lab_values],
        }
    )


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
        query = LabTrendQuery(
            parameter=parameter,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
        )
        values = await db.get_lab_trends(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    # Batch-fetch gdrive_urls for all source documents
    doc_ids = {v.document_id for v in values}
    doc_urls: dict[int, str | None] = {}
    for did in doc_ids:
        doc = await db.get_document(did)
        doc_urls[did] = _gdrive_url(doc.gdrive_id) if doc else None

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


async def get_lab_safety_check(ctx: Context) -> str:
    """Pre-cycle lab safety check against mFOLFOX6 thresholds.

    For each safety parameter, returns:
    - The threshold (min or max) with source/guideline reference
    - The patient's most recent value with date and source document
    - Safety status: green (safe), red (unsafe), yellow (borderline ±10%)
    - Clickable gdrive_url to verify the source lab document

    Used by Oncoteam's clinical protocol UI for the "Laborat. prahy" section.
    """
    db = _get_db(ctx)
    results = []

    for param, threshold in _MFOLFOX6_THRESHOLDS.items():
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
        latest = await db.get_latest_lab_value(param)
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

    return json.dumps({
        "protocol": "mFOLFOX6",
        "cycle_safe": safe,
        "summary": summary,
        "parameters": results,
    })


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
                latest = await db.get_latest_lab_value(item["parameter"])
                if latest:
                    entry["last_value"] = latest.value
                    entry["last_date"] = latest.lab_date.isoformat()
                    entry["last_document_id"] = latest.document_id
                    doc = await db.get_document(latest.document_id)
                    if doc:
                        entry["last_document_gdrive_url"] = _gdrive_url(
                            doc.gdrive_id
                        )

            items.append(entry)

        sections.append({
            "section": section_key,
            "title": section["title"],
            "items": items,
        })

    return json.dumps({
        "protocol": "mFOLFOX6",
        "cycle": cycle_number,
        "sections": sections,
    })


def register(mcp):
    mcp.tool()(store_lab_values)
    mcp.tool()(get_lab_trends)
    mcp.tool()(get_lab_safety_check)
    mcp.tool()(get_precycle_checklist)
