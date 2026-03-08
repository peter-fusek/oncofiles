"""Document management tools."""

from __future__ import annotations

import io
import json

from fastmcp import Context

from oncofiles.filename_parser import parse_filename
from oncofiles.models import Document, DocumentCategory, SearchQuery
from oncofiles.tools._helpers import _clamp_limit, _doc_to_dict, _get_db, _get_files, _parse_date


async def upload_document(
    ctx: Context,
    content: str,
    filename: str,
    mime_type: str = "application/pdf",
) -> str:
    """Upload a medical document to persistent storage.

    The filename should follow the YYYYMMDD_institution_category_description.ext
    convention for automatic metadata extraction.

    Args:
        content: Base64-encoded file content.
        filename: Document filename (e.g. 20240115_NOUonko_labs_krvnyObraz.pdf).
        mime_type: MIME type of the document.
    """
    import base64
    import binascii

    try:
        file_bytes = base64.b64decode(content)
    except binascii.Error:
        return json.dumps({"error": "Invalid base64 content. Ensure the file is properly encoded."})

    db = _get_db(ctx)
    files = _get_files(ctx)

    # Upload to Files API
    await ctx.info(f"Uploading {filename} ({len(file_bytes)} bytes)...")
    metadata = files.upload(io.BytesIO(file_bytes), filename, mime_type)

    # Parse filename for structured metadata
    parsed = parse_filename(filename)

    doc = Document(
        file_id=metadata.id,
        filename=filename,
        original_filename=filename,
        document_date=parsed.document_date,
        institution=parsed.institution,
        category=parsed.category,
        description=parsed.description,
        mime_type=metadata.mime_type,
        size_bytes=metadata.size_bytes,
    )

    doc = await db.insert_document(doc)
    return json.dumps(
        {
            "id": doc.id,
            "file_id": doc.file_id,
            "filename": doc.filename,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
            "institution": doc.institution,
            "category": doc.category.value,
        }
    )


async def list_documents(
    ctx: Context,
    limit: int = 50,
    offset: int = 0,
) -> str:
    """List all stored medical documents with metadata.

    Returns documents ordered by date (newest first).
    """
    db = _get_db(ctx)
    docs = await db.list_documents(limit=limit, offset=offset)
    return json.dumps({"documents": [_doc_to_dict(d) for d in docs], "total": len(docs)})


async def search_documents(
    ctx: Context,
    text: str | None = None,
    institution: str | None = None,
    category: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> str:
    """Search medical documents by text, institution, category, or date range.

    Args:
        text: Full-text search query (searches filename, institution, description).
        institution: Filter by institution code (e.g. NOUonko, OUSA).
        category: Filter by category (labs, report, imaging, imaging_ct, imaging_us,
                  pathology, genetics, surgery, surgical_report, prescription,
                  referral, discharge, discharge_summary, chemo_sheet, other).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return (max 200).
    """
    try:
        db = _get_db(ctx)
        query = SearchQuery(
            text=text,
            institution=institution,
            category=DocumentCategory(category) if category else None,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
        )
        docs = await db.search_documents(query)
        return json.dumps({"documents": [_doc_to_dict(d) for d in docs], "total": len(docs)})
    except ValueError as e:
        return json.dumps({"error": str(e)})


async def get_document(ctx: Context, file_id: str) -> str:
    """Get a document's metadata and file_id for Claude to analyze.

    Returns the file_id that can be used to reference the document in conversation.

    Args:
        file_id: The Anthropic Files API file_id.
    """
    db = _get_db(ctx)
    doc = await db.get_document_by_file_id(file_id)
    if not doc:
        return json.dumps({"error": f"Document not found: {file_id}"})
    return json.dumps(
        {
            "id": doc.id,
            "file_id": doc.file_id,
            "filename": doc.filename,
            "document_date": doc.document_date.isoformat() if doc.document_date else None,
            "institution": doc.institution,
            "category": doc.category.value,
            "description": doc.description,
            "mime_type": doc.mime_type,
            "size_bytes": doc.size_bytes,
            "content_block": doc.content_block,
        }
    )


async def delete_document(ctx: Context, file_id: str) -> str:
    """Soft-delete a document (moves to trash, recoverable for 30 days).

    The document is hidden from all listings and searches but can be restored
    using restore_document. Files API copy is also deleted.

    Args:
        file_id: The Anthropic Files API file_id to delete.
    """
    db = _get_db(ctx)
    files = _get_files(ctx)

    # Delete from Files API
    try:
        files.delete(file_id)
    except Exception as e:
        await ctx.warning(f"Files API deletion failed (may already be deleted): {e}")

    # Soft-delete in local database
    deleted = await db.delete_document_by_file_id(file_id)
    return json.dumps(
        {
            "deleted": deleted,
            "file_id": file_id,
            "message": "Moved to trash. Recoverable for 30 days via restore_document."
            if deleted
            else "Document not found or already deleted.",
        }
    )


async def restore_document(ctx: Context, doc_id: int) -> str:
    """Restore a soft-deleted document from trash.

    Args:
        doc_id: The local document ID to restore.
    """
    db = _get_db(ctx)
    restored = await db.restore_document(doc_id)
    if restored:
        doc = await db.get_document(doc_id)
        return json.dumps(
            {
                "restored": True,
                "doc_id": doc_id,
                "filename": doc.filename if doc else None,
                "message": "Document restored from trash.",
            }
        )
    return json.dumps(
        {
            "restored": False,
            "doc_id": doc_id,
            "message": "Document not found in trash.",
        }
    )


async def list_trash(ctx: Context, limit: int = 50) -> str:
    """List soft-deleted documents in trash.

    Args:
        limit: Maximum results to return (default 50, max 200).
    """
    db = _get_db(ctx)
    limit = min(max(limit, 1), 200)
    docs = await db.list_trash(limit=limit)
    return json.dumps(
        {
            "trash": [
                {
                    "id": d.id,
                    "file_id": d.file_id,
                    "filename": d.filename,
                    "category": d.category.value,
                    "deleted_at": d.deleted_at.isoformat() if d.deleted_at else None,
                }
                for d in docs
            ],
            "total": len(docs),
        }
    )


def register(mcp):
    mcp.tool()(upload_document)
    mcp.tool()(list_documents)
    mcp.tool()(search_documents)
    mcp.tool()(get_document)
    mcp.tool()(delete_document)
    mcp.tool()(restore_document)
    mcp.tool()(list_trash)
