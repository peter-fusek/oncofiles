"""FastMCP server for Erika's medical document management."""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image
from starlette.requests import Request
from starlette.responses import JSONResponse

from erika_files_mcp.config import (
    DATABASE_PATH,
    MCP_BEARER_TOKEN,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from erika_files_mcp.database import Database
from erika_files_mcp.filename_parser import parse_filename
from erika_files_mcp.files_api import FilesClient
from erika_files_mcp.gdrive_client import GDriveClient, create_gdrive_client
from erika_files_mcp.models import Document, DocumentCategory, SearchQuery

# ── Auth ──────────────────────────────────────────────────────────────────────

auth = None
if MCP_BEARER_TOKEN:
    from fastmcp.server.auth import StaticTokenVerifier

    auth = StaticTokenVerifier(
        tokens={MCP_BEARER_TOKEN: {"client_id": "claude-ai", "scopes": []}},
    )


# ── Lifespan ──────────────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize database and Files API client on startup."""
    if TURSO_DATABASE_URL:
        db = Database(turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    files = FilesClient()
    try:
        gdrive = create_gdrive_client()
    except Exception as e:
        import logging

        logging.getLogger(__name__).warning("GDrive client init failed: %s — fallback disabled", e)
        gdrive = None
    try:
        yield {"db": db, "files": files, "gdrive": gdrive}
    finally:
        await db.close()


mcp = FastMCP(
    "Erika Files",
    instructions="Medical document management via Anthropic Files API",
    lifespan=lifespan,
    auth=auth,
)


# ── Health check ──────────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "0.6.0"})


def _get_db(ctx: Context) -> Database:
    return ctx.request_context.lifespan_context["db"]


def _get_files(ctx: Context) -> FilesClient:
    return ctx.request_context.lifespan_context["files"]


def _get_gdrive(ctx: Context) -> GDriveClient | None:
    return ctx.request_context.lifespan_context.get("gdrive")


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


# ── Patient context ──────────────────────────────────────────────────────────

PATIENT_CONTEXT = {
    "name": "Erika Fusekova",
    "diagnosis": "Oncology patient — colorectal cancer (mCRC)",
    "note": (
        "Lab values should be interpreted considering active chemotherapy. "
        "Key markers: CEA, CA 19-9, liver (ALT, AST, bilirubin), "
        "renal (creatinine, urea), blood counts (WBC, neutrophils, Hb, platelets)."
    ),
}


def _patient_context_text() -> str:
    return (
        f"**Patient:** {PATIENT_CONTEXT['name']}\n"
        f"**Diagnosis:** {PATIENT_CONTEXT['diagnosis']}\n"
        f"**Note:** {PATIENT_CONTEXT['note']}"
    )


def _doc_header(doc: Document) -> str:
    date_str = doc.document_date.isoformat() if doc.document_date else "unknown"
    return (
        f"**{doc.filename}** | {doc.category.value} | "
        f"{date_str} | {doc.institution or 'unknown'}"
    )


def _pdf_to_images(content_bytes: bytes) -> list[Image]:
    """Convert PDF pages to JPEG images using pymupdf."""
    import pymupdf

    images = []
    doc = pymupdf.open(stream=content_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=200)
            images.append(Image(data=pix.tobytes("jpeg"), format="jpeg"))
    finally:
        doc.close()
    return images


def _inline_content(doc: Document, content_bytes: bytes) -> list[str | Image]:
    """Return the appropriate inline content for a document.

    Returns a list of content items. PDFs are converted to per-page JPEG images
    since Claude.ai connectors don't support EmbeddedResource (PDF) content.
    """
    if doc.mime_type and doc.mime_type.startswith("image/"):
        fmt = doc.mime_type.split("/")[1]  # jpeg, png, etc.
        return [Image(data=content_bytes, format=fmt)]
    elif doc.mime_type == "application/pdf":
        return _pdf_to_images(content_bytes)
    else:
        return [content_bytes.decode("utf-8", errors="replace")]


def _try_download(
    files: FilesClient,
    doc: Document,
    gdrive: GDriveClient | None = None,
) -> tuple[bool, list[str | Image]]:
    """Try to download file content. Falls back to Google Drive if available."""
    # 1. Try Files API
    try:
        content_bytes = files.download(doc.file_id)
        return True, _inline_content(doc, content_bytes)
    except Exception:
        pass

    # 2. Fallback: Google Drive
    if gdrive and doc.gdrive_id:
        try:
            content_bytes = gdrive.download(doc.gdrive_id)
            return True, _inline_content(doc, content_bytes)
        except Exception as e:
            return False, [f"[GDrive download also failed: {e}]"]

    if not doc.gdrive_id:
        return False, ["[Not downloadable. No gdrive_id for fallback — see #35]"]
    return False, ["[Not downloadable. GDrive client not configured — see #35]"]


# ── Analysis tools ───────────────────────────────────────────────────────────


@mcp.tool(output_schema=None)
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

    ok, content = _try_download(files, doc, gdrive)
    return [_doc_header(doc), *content]


@mcp.tool(output_schema=None)
async def analyze_labs(
    ctx: Context,
    file_id: str | None = None,
    limit: int = 3,
) -> list:
    """Analyze recent lab results with oncology context.

    Downloads lab documents and returns them inline for Claude to read,
    along with patient context for interpreting results under chemotherapy.

    Note: Each lab document is 100KB–2MB. Keep limit low to avoid large responses.

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
    download_errors = 0
    for doc in labs:
        result.append(_doc_header(doc))
        ok, content = _try_download(files, doc, gdrive)
        if not ok:
            download_errors += 1
        result.extend(content)

    if download_errors == len(labs):
        result.append(
            "**Error:** All file downloads failed. Files uploaded via the Anthropic Files API "
            "cannot be downloaded back (see issue #35). Documents need to be re-imported "
            "with a content store that supports retrieval."
        )
    else:
        result.append(
            "**Instructions:** Focus on out-of-range values, chemotherapy side effects "
            "(myelosuppression, hepatotoxicity, nephrotoxicity), and tumor markers "
            "(CEA, CA 19-9). Flag any critical values requiring immediate attention."
        )
    return result


@mcp.tool(output_schema=None)
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

    Note: Each lab document is 100KB–2MB. Keep limit reasonable.

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
        query = SearchQuery(
            category=DocumentCategory.LABS,
            date_from=date.fromisoformat(date_from) if date_from else None,
            date_to=date.fromisoformat(date_to) if date_to else None,
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

    # Sort chronologically (oldest → newest)
    labs.sort(key=lambda d: d.document_date or date.min)

    result: list = [_patient_context_text()]
    download_errors = 0
    for doc in labs:
        result.append(_doc_header(doc))
        ok, content = _try_download(files, doc, gdrive)
        if not ok:
            download_errors += 1
        result.extend(content)

    if download_errors == len(labs):
        result.append(
            "**Error:** All file downloads failed. Files uploaded via the Anthropic Files API "
            "cannot be downloaded back (see issue #35). Documents need to be re-imported "
            "with a content store that supports retrieval."
        )
    else:
        result.append(
            "**Instructions:** Compare values across these lab results chronologically. "
            "Identify trends (improving/worsening), highlight significant changes, "
            "and flag any values that crossed normal/abnormal thresholds. "
            "Pay special attention to tumor markers and chemotherapy toxicity indicators."
        )
    return result


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


@mcp.resource(
    "files://treatment-timeline",
    description="Chronological timeline of all treatment documents",
)
async def treatment_timeline(ctx: Context) -> str:
    """Return a chronological markdown timeline of treatment documents (metadata only)."""
    db = _get_db(ctx)
    docs = await db.get_treatment_timeline()
    if not docs:
        return "No treatment documents found."

    lines = [f"# Treatment Timeline ({len(docs)} documents)\n"]
    current_date = None
    for d in docs:
        date_str = d.document_date.isoformat() if d.document_date else "unknown"
        if date_str != current_date:
            current_date = date_str
            lines.append(f"\n## {current_date}\n")
        lines.append(
            f"- [{d.category.value}] **{d.filename}** "
            f"({d.institution or 'unknown'}) file_id: `{d.file_id}`"
        )
    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    if MCP_TRANSPORT == "stdio":
        mcp.run()
    else:
        mcp.run(transport=MCP_TRANSPORT, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
