"""Lab trend tracking tools (#59)."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.models import LabTrendQuery, LabValue
from oncofiles.tools._helpers import _clamp_limit, _get_db, _parse_date


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
        }
        for v in values
    ]
    return json.dumps({"values": items, "total": len(items)})


def register(mcp):
    mcp.tool()(store_lab_values)
    mcp.tool()(get_lab_trends)
