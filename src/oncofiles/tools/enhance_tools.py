"""Enhancement and metadata extraction tools."""

from __future__ import annotations

import json

from fastmcp import Context

from oncofiles.tools._helpers import (
    _ensure_ocr_text,
    _get_db,
    _get_files,
    _get_gdrive,
    _try_download,
)


async def enhance_documents(
    ctx: Context,
    document_ids: str | None = None,
) -> str:
    """Run AI enhancement (summary + tags) on documents.

    If document_ids is omitted, processes all documents that haven't been enhanced yet.

    Args:
        document_ids: Comma-separated document IDs to enhance. If omitted, enhances all unprocessed.
    """
    from oncofiles.sync import enhance_documents as _enhance_documents

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)

    parsed_ids = (
        [int(d.strip()) for d in document_ids.split(",") if d.strip()] if document_ids else None
    )

    stats = await _enhance_documents(db, files, gdrive, document_ids=parsed_ids)
    return json.dumps(stats)


async def extract_document_metadata(
    ctx: Context,
    document_id: int,
) -> str:
    """Extract and store structured medical metadata from a document.

    Uses AI to analyze the document text and extract findings, diagnoses,
    medications, providers, and a patient-friendly summary. Results are
    persisted in the structured_metadata column.

    Args:
        document_id: The local document ID to extract metadata from.
    """
    from oncofiles.enhance import extract_structured_metadata

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)

    doc = await db.get_document(document_id)
    if not doc:
        return json.dumps({"error": f"Document not found: {document_id}"})

    # Get document text
    ok, content, raw_bytes = _try_download(files, doc, gdrive)
    if not ok:
        return json.dumps({"error": "Cannot download document for text extraction"})

    texts = await _ensure_ocr_text(db, doc, content, raw_bytes)
    if not texts:
        return json.dumps({"error": "No text could be extracted from document"})

    full_text = "\n\n".join(texts)
    metadata = extract_structured_metadata(full_text)
    metadata_json = json.dumps(metadata)

    await db.update_structured_metadata(document_id, metadata_json)

    return json.dumps(
        {
            "document_id": document_id,
            "filename": doc.filename,
            "structured_metadata": metadata,
        }
    )


def register(mcp):
    mcp.tool()(enhance_documents)
    mcp.tool()(extract_document_metadata)
