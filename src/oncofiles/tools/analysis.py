"""Analysis tools: view_document, analyze_labs, compare_labs."""

from __future__ import annotations

from datetime import date

from fastmcp import Context

from oncofiles.models import DocumentCategory, SearchQuery
from oncofiles.tools._helpers import (
    _check_baseline_labs,
    _doc_header,
    _ensure_ocr_text,
    _get_db,
    _get_files,
    _get_gdrive,
    _parse_date,
    _patient_context_text,
    _try_download,
)


async def view_document(ctx: Context, file_id: str) -> list:
    """Download a document and return its content for Claude to read.

    Returns the actual file content (image or PDF) inline so Claude
    can see and analyze it directly.

    Args:
        file_id: The Anthropic Files API file_id.
    """
    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)
    doc = await db.get_document_by_file_id(file_id)
    if not doc:
        return [f"Document not found: {file_id}"]

    ok, content, raw_bytes = _try_download(files, doc, gdrive)
    if not ok:
        return [_doc_header(doc), *content]

    # Extract/cache OCR text and return text before images
    texts = await _ensure_ocr_text(db, doc, content, raw_bytes)
    result: list = [_doc_header(doc)]
    if texts:
        result.append("--- Extracted Text ---")
        result.extend(texts)
        result.append("--- Document Images ---")
    result.extend(content)
    return result


async def analyze_labs(
    ctx: Context,
    file_id: str | None = None,
    limit: int = 3,
) -> list:
    """Analyze recent lab results with oncology context.

    Downloads lab documents and returns them inline for Claude to read,
    along with patient context for interpreting results under chemotherapy.

    Note: Each lab document is 100KB-2MB. Keep limit low to avoid large responses.

    Args:
        file_id: Specific lab file_id to analyze. If omitted, fetches the most recent labs.
        limit: Maximum number of lab documents to include (default 3).
    """
    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)

    if file_id:
        doc = await db.get_document_by_file_id(file_id)
        if not doc:
            return [f"Document not found: {file_id}"]
        if doc.category != DocumentCategory.LABS:
            return [f"Document {file_id} is not a lab result (category: {doc.category.value})"]
        labs = [doc]
    else:
        labs = await db.get_latest_labs(limit=limit)
        if not labs:
            return ["No lab results found."]

    result: list = [_patient_context_text()]

    # Baseline labs availability check
    baseline_warning = await _check_baseline_labs(db)
    if baseline_warning:
        result.append(baseline_warning)
    download_errors = 0
    for doc in labs:
        result.append(_doc_header(doc))
        ok, content, raw_bytes = _try_download(files, doc, gdrive)
        if not ok:
            download_errors += 1
            result.extend(content)
        else:
            texts = await _ensure_ocr_text(db, doc, content, raw_bytes)
            if texts:
                result.append("--- Extracted Text ---")
                result.extend(texts)
                result.append("--- Document Images ---")
            result.extend(content)

    if download_errors == len(labs):
        result.append(
            "**Error:** All file downloads failed. Files uploaded via the Anthropic Files API "
            "cannot be downloaded back (see issue #35). Documents need to be re-imported "
            "with a content store that supports retrieval."
        )
    else:
        result.append(
            "**Instructions:** Analyze these lab results using the following protocol:\n\n"
            "1. **CRITICAL VALUES** — Flag immediately: ANC <1.0, PLT <75 or >400 with active VTE, "
            "Cr >1.5x baseline, K+ <3.0 or >5.5, any value requiring urgent intervention.\n\n"
            "2. **SII (Systemic Immune-Inflammation Index)** = (abs_NEUT x PLT) / abs_LYMPH\n"
            "   - >1800 = high inflammatory burden\n"
            "   - >30% decline after C1 = favorable response signal\n"
            "   - Calculate and report the value.\n\n"
            "3. **Ne/Ly ratio** = abs_NEUT / abs_LYMPH\n"
            "   - >3.0 = poor prognosis indicator\n"
            "   - <2.5 = improving\n\n"
            "4. **CBC delta table**: "
            "[Parameter | Baseline | Current | Change% | Reference | Status]\n"
            "   - If pre-treatment baseline is missing, "
            "FLAG: 'Baseline labs needed for trend analysis'\n\n"
            "5. **Liver enzyme pattern**: hepatocellular (ALT/AST up) vs cholestatic (GMT/ALP up) "
            "vs mixed — relate to known [CLINICAL_REDACTED] ([CODE_REDACTED]).\n\n"
            "6. **PLT + thrombosis cross-check**: Patient has active [CLINICAL_REDACTED] on [MEDICATION_REDACTED]. "
            "If PLT elevated (>400), FLAG IMMEDIATELY as high-risk for thromboembolic event.\n\n"
            "7. **Tumor markers**: CEA, CA 19-9 trends. "
            "Note if baseline pre-treatment values missing.\n\n"
            "8. **Chemotherapy toxicity**: myelosuppression (ANC, PLT, Hgb), "
            "nephrotoxicity (Cr, eGFR), "
            "hepatotoxicity (ALT, AST, bilirubin), neurotoxicity indicators.\n\n"
            "**Output sections:** Critical / Watch / Stable / Inflammatory Markers (SII, Ne/Ly) / "
            "Tumor Markers / Questions for Oncologist (2-4 specific questions)"
        )
    return result


async def compare_labs(
    ctx: Context,
    file_id_a: str | None = None,
    file_id_b: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 10,
) -> list:
    """Compare lab results over time to identify trends.

    Two modes:
    - Specific: provide file_id_a and file_id_b to compare two specific lab sets.
    - Date range: provide date_from/date_to to compare all labs in a period.

    Note: Each lab document is 100KB-2MB. Keep limit reasonable.

    Args:
        file_id_a: First lab file_id (optional).
        file_id_b: Second lab file_id (optional).
        date_from: Start date for range query (YYYY-MM-DD).
        date_to: End date for range query (YYYY-MM-DD).
        limit: Maximum number of lab documents to include (default 10).
    """
    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)

    if file_id_a or file_id_b:
        # Specific file_ids mode
        labs = []
        for fid in [file_id_a, file_id_b]:
            if fid:
                doc = await db.get_document_by_file_id(fid)
                if not doc:
                    return [f"Document not found: {fid}"]
                labs.append(doc)
    elif date_from or date_to:
        # Date range mode
        try:
            parsed_from = _parse_date(date_from)
            parsed_to = _parse_date(date_to)
        except ValueError as e:
            return [str(e)]
        query = SearchQuery(
            category=DocumentCategory.LABS,
            date_from=parsed_from,
            date_to=parsed_to,
            limit=limit,
        )
        labs = await db.search_documents(query)
        if not labs:
            return ["No lab results found in the specified date range."]
    else:
        # Default: latest labs
        labs = await db.get_latest_labs(limit=limit)
        if not labs:
            return ["No lab results found."]

    # Sort chronologically (oldest -> newest)
    labs.sort(key=lambda d: d.document_date or date.min)

    result: list = [_patient_context_text()]
    download_errors = 0
    for doc in labs:
        result.append(_doc_header(doc))
        ok, content, raw_bytes = _try_download(files, doc, gdrive)
        if not ok:
            download_errors += 1
            result.extend(content)
        else:
            texts = await _ensure_ocr_text(db, doc, content, raw_bytes)
            if texts:
                result.append("--- Extracted Text ---")
                result.extend(texts)
                result.append("--- Document Images ---")
            result.extend(content)

    if download_errors == len(labs):
        result.append(
            "**Error:** All file downloads failed. Files uploaded via the Anthropic Files API "
            "cannot be downloaded back (see issue #35). Documents need to be re-imported "
            "with a content store that supports retrieval."
        )
    else:
        result.append(
            "**Instructions:** Compare these lab results chronologically using this protocol:\n\n"
            "1. **CBC delta table**: [Parameter | Date1 | Date2 | ... | Change% | Trend | Status]\n"
            "   - If pre-treatment baseline is missing, FLAG: 'Baseline labs needed'\n\n"
            "2. **SII trend** = (abs_NEUT x PLT) / abs_LYMPH — calculate for each date.\n"
            "   - >30% decline post-C1 = favorable response\n\n"
            "3. **Ne/Ly ratio trend** = abs_NEUT / abs_LYMPH — calculate for each date.\n"
            "   - Crossing 3.0 threshold in either direction is significant.\n\n"
            "4. **PLT + thrombosis cross-check**: Patient has active [CLINICAL_REDACTED] on [MEDICATION_REDACTED]. "
            "If PLT trending up or >400, FLAG IMMEDIATELY.\n\n"
            "5. **Liver enzyme pattern**: track hepatocellular vs cholestatic pattern changes "
            "across dates — relate to [CLINICAL_REDACTED] ([CODE_REDACTED]).\n\n"
            "6. **Tumor markers**: CEA, CA 19-9 direction and velocity of change.\n\n"
            "7. **Threshold crossings**: Flag any value that crossed normal/abnormal boundary.\n\n"
            "8. **Chemotherapy toxicity trends**: "
            "cumulative myelosuppression, renal/hepatic function.\n\n"
            "**Output sections:** Critical Trends / Improving / Worsening / Stable / "
            "Inflammatory Markers (SII, Ne/Ly) / Tumor Markers / Questions for Oncologist"
        )
    return result


def register(mcp):
    mcp.tool(output_schema=None)(view_document)
    mcp.tool(output_schema=None)(analyze_labs)
    mcp.tool(output_schema=None)(compare_labs)
