"""FastMCP server for Erika's medical document management."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from fastmcp import Context, FastMCP

from erika_files_mcp.config import DATABASE_PATH
from erika_files_mcp.database import Database
from erika_files_mcp.filename_parser import parse_filename
from erika_files_mcp.files_api import FilesClient
from erika_files_mcp.models import Document, DocumentCategory, SearchQuery


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize database and Files API client on startup."""
    db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    files = FilesClient()
    try:
        yield {"db": db, "files": files}
    finally:
        await db.close()


mcp = FastMCP(
    "Erika Files",
    description="Medical document management via Anthropic Files API",
    lifespan=lifespan,
)


def _get_db(ctx: Context) -> Database:
    return ctx.request_context.lifespan_context["db"]


def _get_files(ctx: Context) -> FilesClient:
    return ctx.request_context.lifespan_context["files"]


# ── Tools ────────────────────────────────────────────────────────────────────


@mcp.tool()
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

    file_bytes = base64.b64decode(content)

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


@mcp.tool()
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
    return json.dumps(
        [
            {
                "id": d.id,
                "file_id": d.file_id,
                "filename": d.filename,
                "document_date": d.document_date.isoformat() if d.document_date else None,
                "institution": d.institution,
                "category": d.category.value,
                "description": d.description,
            }
            for d in docs
        ]
    )


@mcp.tool()
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
        category: Filter by category (labs, report, imaging, pathology, surgery,
                  prescription, referral, discharge, other).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return.
    """
    db = _get_db(ctx)
    query = SearchQuery(
        text=text,
        institution=institution,
        category=DocumentCategory(category) if category else None,
        date_from=date.fromisoformat(date_from) if date_from else None,
        date_to=date.fromisoformat(date_to) if date_to else None,
        limit=limit,
    )
    docs = await db.search_documents(query)
    return json.dumps(
        [
            {
                "id": d.id,
                "file_id": d.file_id,
                "filename": d.filename,
                "document_date": d.document_date.isoformat() if d.document_date else None,
                "institution": d.institution,
                "category": d.category.value,
                "description": d.description,
            }
            for d in docs
        ]
    )


@mcp.tool()
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


@mcp.tool()
async def delete_document(ctx: Context, file_id: str) -> str:
    """Delete a document from both the Files API and local database.

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

    # Delete from local database
    deleted = await db.delete_document_by_file_id(file_id)
    return json.dumps({"deleted": deleted, "file_id": file_id})


# ── Resources ────────────────────────────────────────────────────────────────


@mcp.resource("files://catalog", description="Full catalog of stored medical documents")
async def catalog(ctx: Context) -> str:
    """Return the complete document catalog as a formatted list."""
    db = _get_db(ctx)
    docs = await db.list_documents(limit=200)
    lines = [f"# Document Catalog ({len(docs)} documents)\n"]
    for d in docs:
        date_str = d.document_date.isoformat() if d.document_date else "unknown"
        lines.append(
            f"- **{d.filename}** [{d.category.value}] "
            f"({date_str}, {d.institution or 'unknown'}) "
            f"file_id: `{d.file_id}`"
        )
    return "\n".join(lines)


@mcp.resource("files://latest-labs", description="Most recent lab result documents")
async def latest_labs(ctx: Context) -> str:
    """Return the 5 most recent lab result documents."""
    db = _get_db(ctx)
    labs = await db.get_latest_labs(limit=5)
    if not labs:
        return "No lab results found."
    lines = ["# Latest Lab Results\n"]
    for d in labs:
        date_str = d.document_date.isoformat() if d.document_date else "unknown"
        lines.append(
            f"- **{d.filename}** ({date_str}, {d.institution or 'unknown'}) file_id: `{d.file_id}`"
        )
    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
