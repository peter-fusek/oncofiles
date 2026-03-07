"""FastMCP server for Oncofiles medical document management."""

from __future__ import annotations

import io
import json
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date

from fastmcp import Context, FastMCP
from fastmcp.utilities.types import Image
from starlette.requests import Request
from starlette.responses import JSONResponse

from oncofiles.config import (
    DATABASE_PATH,
    GOOGLE_DRIVE_FOLDER_ID,
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    LOG_LEVEL,
    MCP_BEARER_TOKEN,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
    SYNC_ENABLED,
    SYNC_INTERVAL_MINUTES,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from oncofiles.database import Database
from oncofiles.filename_parser import parse_filename
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import GDriveClient, create_gdrive_client
from oncofiles.models import (
    ActivityLogEntry,
    ActivityLogQuery,
    AgentState,
    ConversationEntry,
    ConversationQuery,
    Document,
    DocumentCategory,
    ResearchEntry,
    ResearchQuery,
    SearchQuery,
    TreatmentEvent,
    TreatmentEventQuery,
)
from oncofiles.ocr import OCR_MODEL, extract_text_from_image

logger = logging.getLogger(__name__)

# ── Auth ──────────────────────────────────────────────────────────────────────


def _create_auth():
    """Create auth provider based on environment.

    - streamable-http + MCP_BEARER_TOKEN: OAuth + static bearer (dual auth)
    - streamable-http: InMemoryOAuthProvider (OAuth2 for Claude.ai)
    - MCP_BEARER_TOKEN set: StaticTokenVerifier (dev/testing)
    - otherwise: None (no auth)
    """
    if MCP_TRANSPORT == "streamable-http":
        from fastmcp.server.auth.auth import AccessToken, ClientRegistrationOptions
        from fastmcp.server.auth.providers.in_memory import InMemoryOAuthProvider

        class DualAuthProvider(InMemoryOAuthProvider):
            """OAuth + static bearer token for server-to-server auth."""

            def __init__(self, bearer_token: str | None = None, **kwargs):
                super().__init__(**kwargs)
                self._bearer_token = bearer_token

            async def verify_token(self, token: str) -> AccessToken | None:
                # Check static bearer token first (server-to-server)
                if self._bearer_token and token == self._bearer_token:
                    return AccessToken(
                        token=token,
                        client_id="oncoteam",
                        scopes=[],
                    )
                # Fall back to OAuth token verification
                return await super().verify_token(token)

        return DualAuthProvider(
            bearer_token=MCP_BEARER_TOKEN or None,
            base_url="https://aware-kindness-production.up.railway.app",
            client_registration_options=ClientRegistrationOptions(enabled=True),
        )

    if MCP_BEARER_TOKEN:
        from fastmcp.server.auth import StaticTokenVerifier

        return StaticTokenVerifier(
            tokens={MCP_BEARER_TOKEN: {"client_id": "claude-ai", "scopes": []}},
        )

    return None


auth = _create_auth()


# ── Lifespan ──────────────────────────────────────────────────────────────────


def _setup_logging() -> None:
    """Configure structured logging based on transport and LOG_LEVEL."""
    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)
    if MCP_TRANSPORT == "streamable-http":
        # JSON format for Railway / cloud
        fmt = (
            '{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}'
        )
    else:
        fmt = "%(asctime)s %(levelname)-8s %(name)s — %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr, force=True)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize database and Files API client on startup."""
    _setup_logging()
    logger.info("Starting Oncofiles MCP server (transport=%s)", MCP_TRANSPORT)
    if TURSO_DATABASE_URL:
        db = Database(turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    files = FilesClient()
    # Load owner_email from OAuth tokens (needed for service account permission sharing)
    oauth_folder_id = ""
    owner_email = ""
    try:
        token = await db.get_oauth_token()
        if token:
            oauth_folder_id = token.gdrive_folder_id or ""
            owner_email = token.owner_email or ""
    except Exception:
        pass

    # Prefer OAuth (user's storage quota) over service account (no upload quota)
    gdrive = None
    try:
        if token and GOOGLE_OAUTH_CLIENT_ID:
            from oncofiles.oauth import is_token_expired, refresh_access_token

            access_token = token.access_token
            if is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None):
                refreshed = refresh_access_token(token.refresh_token)
                access_token = refreshed["access_token"]
                from datetime import datetime, timedelta

                new_expiry = datetime.now(UTC) + timedelta(
                    seconds=refreshed.get("expires_in", 3600)
                )
                token.access_token = access_token
                token.token_expiry = new_expiry
                await db.upsert_oauth_token(token)

            gdrive = GDriveClient.from_oauth(
                access_token=access_token,
                refresh_token=token.refresh_token,
                client_id=GOOGLE_OAUTH_CLIENT_ID,
                client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
                owner_email=owner_email,
            )
            logger.info("GDrive client initialized from OAuth tokens")
    except Exception as e:
        logger.warning("OAuth GDrive init failed: %s", e)

    # Fall back to service account if OAuth not available
    if not gdrive:
        try:
            gdrive = create_gdrive_client(owner_email=owner_email)
        except Exception as e:
            logger.warning("GDrive client init failed: %s — fallback disabled", e)

    # Start background sync scheduler
    scheduler = None
    if SYNC_ENABLED and gdrive:
        scheduler = _start_sync_scheduler(db, files, gdrive, oauth_folder_id)

    try:
        yield {"db": db, "files": files, "gdrive": gdrive, "oauth_folder_id": oauth_folder_id}
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
            logger.info("Sync scheduler stopped")
        await db.close()


def _start_sync_scheduler(db, files, gdrive, oauth_folder_id):
    """Start APScheduler for periodic GDrive sync."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from oncofiles.sync import sync

    async def _run_sync():
        folder_id = _get_sync_folder_id_from(oauth_folder_id)
        if not folder_id:
            logger.debug("Scheduled sync skipped — no folder ID")
            return
        try:
            stats = await sync(db, files, gdrive, folder_id)
            logger.info("Scheduled sync complete: %s", stats)
        except Exception:
            logger.exception("Scheduled sync failed")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_sync,
        IntervalTrigger(minutes=SYNC_INTERVAL_MINUTES),
        id="gdrive_sync",
        max_instances=1,
    )
    scheduler.start()
    logger.info("Sync scheduler started — every %d min", SYNC_INTERVAL_MINUTES)
    return scheduler


def _get_sync_folder_id_from(oauth_folder_id: str) -> str:
    """Resolve GDrive folder ID from config or OAuth."""
    return GOOGLE_DRIVE_FOLDER_ID or oauth_folder_id


mcp = FastMCP(
    "Oncofiles",
    instructions="Medical document management via Anthropic Files API",
    lifespan=lifespan,
    auth=auth,
)


# ── Health check ──────────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        doc_count = await db.count_documents()
        return JSONResponse(
            {"status": "ok", "database": "connected", "documents": doc_count, "version": "3.0.2"}
        )
    except Exception as e:
        return JSONResponse(
            {"status": "degraded", "database": f"error: {e}", "version": "3.0.2"}, status_code=503
        )


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> JSONResponse:
    """Handle Google OAuth 2.0 redirect callback."""
    from datetime import datetime, timedelta

    from oncofiles.models import OAuthToken
    from oncofiles.oauth import exchange_code

    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "Missing authorization code"}, status_code=400)

    try:
        tokens = exchange_code(code)
    except Exception as e:
        logger.exception("OAuth token exchange failed")
        return JSONResponse({"error": f"Token exchange failed: {e}"}, status_code=500)

    expiry = datetime.now(UTC) + timedelta(seconds=tokens.get("expires_in", 3600))
    oauth_token = OAuthToken(
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", ""),
        token_expiry=expiry,
    )

    db = request.app.state.fastmcp_server._lifespan_result["db"]
    await db.upsert_oauth_token(oauth_token)

    return JSONResponse(
        {
            "status": "ok",
            "message": (
                "Google Drive connected successfully. Use gdrive_set_folder to pick a sync folder."
            ),
        }
    )


def _get_db(ctx: Context) -> Database:
    return ctx.request_context.lifespan_context["db"]


def _get_files(ctx: Context) -> FilesClient:
    return ctx.request_context.lifespan_context["files"]


def _get_gdrive(ctx: Context) -> GDriveClient | None:
    return ctx.request_context.lifespan_context.get("gdrive")


def _parse_date(value: str | None) -> date | None:
    """Parse a YYYY-MM-DD date string, raising ValueError with a friendly message."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Invalid date format: '{value}'. Expected YYYY-MM-DD.") from None


def _clamp_limit(limit: int, max_val: int = 200) -> int:
    """Clamp limit to [1, max_val]."""
    return min(max(limit, 1), max_val)


def _doc_to_dict(d: Document) -> dict:
    """Convert a Document to a JSON-serializable dict for tool output."""
    result = {
        "id": d.id,
        "file_id": d.file_id,
        "filename": d.filename,
        "document_date": d.document_date.isoformat() if d.document_date else None,
        "institution": d.institution,
        "category": d.category.value,
        "description": d.description,
    }
    if d.ai_summary:
        result["ai_summary"] = d.ai_summary
    if d.ai_tags:
        result["ai_tags"] = d.ai_tags
    if d.structured_metadata:
        result["structured_metadata"] = json.loads(d.structured_metadata)
    return result


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

    try:
        file_bytes = base64.b64decode(content)
    except Exception:
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
    return json.dumps({"documents": [_doc_to_dict(d) for d in docs], "total": len(docs)})


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


@mcp.tool()
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


@mcp.tool()
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


# ── Patient context ──────────────────────────────────────────────────────────

PATIENT_CONTEXT = {
    "name": "Erika Fusekova",
    "diagnosis": "AdenoCa colon sigmoideum, G3, mCRC (C18.7)",
    "staging": "IV (liver mets, peritoneal carcinomatosis, LN, Krukenberg tumor l.dx.)",
    "histology": "Adenocarcinoma Grade 3",
    "tumor_site": "Sigmoid colon (left-sided)",
    "diagnosis_date": "2025-12-01",
    "biomarkers": {
        "KRAS": "mutant G12S (c.34G>A, p.(Gly12Ser))",
        "KRAS_G12C": False,
        "NRAS": "wild-type",
        "BRAF_V600E": "wild-type",
        "HER2": "negative (FISH ratio 1.3, avg copy 3)",
        "MSI": "pMMR / MSS",
        "anti_EGFR_eligible": False,
    },
    "treatment": {
        "regimen": "mFOLFOX6 90%",
        "current_cycle": 2,
        "institution": "NOU (Narodny onkologicky ustav), Bratislava",
    },
    "metastases": [
        "liver (C78.7)",
        "peritoneum (C78.6)",
        "retroperitoneum",
        "Krukenberg (ovary l.dx., C79.6)",
        "mediastinal LN",
        "hilar LN",
        "retrocrural LN",
        "portal LN (C77.8)",
        "pulmonary nodules (<=5mm, monitor)",
    ],
    "comorbidities": ["VJI thrombosis (active, Clexane 0.6ml SC 2x/day)"],
    "surgeries": [
        {
            "date": "2026-01-18",
            "institution": "Bory Nemocnica",
            "type": "palliative resection",
            "result": "AdenoCa G3",
        }
    ],
    "physicians": {
        "treating": "MUDr. Stefan Porsok, PhD., MPH — primar OKO G, NOU Bratislava",
        "admitting": "MUDr. Natalia Pazderova — NOU Bratislava",
    },
    "excluded_therapies": [
        "anti-EGFR (cetuximab, panitumumab) — KRAS G12S",
        "checkpoint monotherapy (pembrolizumab, nivolumab) — pMMR/MSS",
        "HER2-targeted (trastuzumab, pertuzumab) — HER2 negative",
        "BRAF inhibitors (encorafenib) — BRAF wild-type",
        "KRAS G12C-specific (sotorasib, adagrasib) — patient has G12S, not G12C",
    ],
    "note": (
        "Lab values should be interpreted considering active chemotherapy. "
        "Key markers: CEA, CA 19-9, liver (ALT, AST, bilirubin), "
        "renal (creatinine, urea), blood counts (WBC, neutrophils, Hb, platelets). "
        "Active VJI thrombosis on Clexane — bevacizumab is HIGH RISK."
    ),
}


def _patient_context_text() -> str:
    bio = PATIENT_CONTEXT["biomarkers"]
    biomarkers = "\n".join(f"  - {k}: {v}" for k, v in bio.items())
    mets = ", ".join(PATIENT_CONTEXT["metastases"])
    comorb = ", ".join(PATIENT_CONTEXT["comorbidities"])
    excluded = "\n".join(f"  - {t}" for t in PATIENT_CONTEXT["excluded_therapies"])
    tx = PATIENT_CONTEXT["treatment"]
    phys = PATIENT_CONTEXT["physicians"]
    return (
        f"**Patient:** {PATIENT_CONTEXT['name']}\n"
        f"**Diagnosis:** {PATIENT_CONTEXT['diagnosis']}\n"
        f"**Staging:** {PATIENT_CONTEXT['staging']}\n"
        f"**Histology:** {PATIENT_CONTEXT['histology']}\n"
        f"**Tumor site:** {PATIENT_CONTEXT['tumor_site']}\n"
        f"**Biomarkers:**\n{biomarkers}\n"
        f"**Treatment:** {tx['regimen']} (cycle {tx['current_cycle']}) at {tx['institution']}\n"
        f"**Metastases:** {mets}\n"
        f"**Comorbidities:** {comorb}\n"
        f"**Physicians:** {phys['treating']}; {phys['admitting']}\n"
        f"**Excluded therapies:**\n{excluded}\n"
        f"**Note:** {PATIENT_CONTEXT['note']}"
    )


def _doc_header(doc: Document) -> str:
    date_str = doc.document_date.isoformat() if doc.document_date else "unknown"
    return (
        f"**{doc.filename}** | {doc.category.value} | {date_str} | {doc.institution or 'unknown'}"
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
) -> tuple[bool, list[str | Image], bytes | None]:
    """Try to download file content. Falls back to Google Drive if available.

    Returns (success, content_items, raw_bytes).
    """
    # 1. Try Files API
    try:
        content_bytes = files.download(doc.file_id)
        return True, _inline_content(doc, content_bytes), content_bytes
    except Exception:
        pass

    # 2. Fallback: Google Drive
    if gdrive and doc.gdrive_id:
        try:
            content_bytes = gdrive.download(doc.gdrive_id)
            return True, _inline_content(doc, content_bytes), content_bytes
        except Exception as e:
            return False, [f"[GDrive download also failed: {e}]"], None

    if not doc.gdrive_id:
        return False, ["[Not downloadable. No gdrive_id for fallback — see #35]"], None
    return False, ["[Not downloadable. GDrive client not configured — see #35]"], None


def _extract_pdf_text(content_bytes: bytes) -> list[str] | None:
    """Try to extract embedded text from a PDF using pymupdf.

    Returns list of per-page text if PDF has substantial embedded text,
    or None if the PDF appears to be scanned (no text).
    """
    import pymupdf

    doc = pymupdf.open(stream=content_bytes, filetype="pdf")
    try:
        texts = []
        pages_with_text = 0
        for page in doc:
            text = page.get_text().strip()
            texts.append(text)
            if len(text) > 50:  # non-trivial text content
                pages_with_text += 1
        # If majority of pages have text, it's a text PDF
        if pages_with_text > len(texts) / 2:
            return texts
        return None
    finally:
        doc.close()


def _resize_image_if_needed(image: Image, max_b64_bytes: int = 5_200_000) -> Image:
    """Resize image if its base64 encoding would exceed API limit (5MB).

    JPEG recompression bloats sizes, so we scale aggressively to stay under limit.
    """
    import base64

    if len(base64.b64encode(image.data)) <= max_b64_bytes:
        return image
    import pymupdf

    pix = pymupdf.Pixmap(image.data)
    # Target 3MB raw JPEG (well under 5MB b64 even after recompression bloat)
    scale = min(0.7, (3_000_000 / len(image.data)) ** 0.5)
    new_w = int(pix.width * scale)
    new_h = int(pix.height * scale)
    pix2 = pymupdf.Pixmap(pix, new_w, new_h)
    return Image(data=pix2.tobytes("jpeg"), format="jpeg")


async def _ensure_ocr_text(
    db: Database,
    doc: Document,
    content_items: list[str | Image],
    content_bytes: bytes | None = None,
) -> list[str]:
    """Get text for a document: cache → PDF native text → Vision OCR.

    Returns a list of extracted text strings (one per page).
    """
    # 1. Check cache
    if await db.has_ocr_text(doc.id):
        pages = await db.get_ocr_pages(doc.id)
        return [p["extracted_text"] for p in pages]

    # 2. For PDFs, try native text extraction first (free, fast)
    if doc.mime_type == "application/pdf" and content_bytes:
        pdf_texts = _extract_pdf_text(content_bytes)
        if pdf_texts:
            for page_num, text in enumerate(pdf_texts, start=1):
                await db.save_ocr_page(doc.id, page_num, text, "pymupdf-native")
            return pdf_texts

    # 3. Fall back to Vision OCR for scanned docs / images
    images = [item for item in content_items if isinstance(item, Image)]
    if not images:
        return []

    texts = []
    for page_num, image in enumerate(images, start=1):
        resized = _resize_image_if_needed(image)
        text = extract_text_from_image(resized)
        await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
        texts.append(text)

    return texts


# ── Analysis helpers ─────────────────────────────────────────────────────────


async def _check_baseline_labs(db: Database) -> str | None:
    """Check if pre-treatment baseline labs exist. Returns warning if missing."""
    from oncofiles.models import TreatmentEventQuery

    events = await db.list_treatment_events(TreatmentEventQuery(event_type="chemo", limit=1))
    if not events:
        return None

    # Get earliest chemo event date
    all_chemo = await db.list_treatment_events(TreatmentEventQuery(event_type="chemo", limit=200))
    if not all_chemo:
        return None

    earliest = min(e.event_date for e in all_chemo)
    baseline_labs = await db.get_labs_before_date(earliest.isoformat())
    if not baseline_labs:
        return (
            f"**WARNING: BASELINE LABS MISSING** — No pre-treatment lab results found "
            f"before first chemo cycle ({earliest.isoformat()}). Baseline values are "
            f"essential for trend analysis and toxicity grading."
        )
    return None


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
            "vs mixed — relate to known hepatic metastases (C78.7).\n\n"
            "6. **PLT + thrombosis cross-check**: Patient has active VJI thrombosis on Clexane. "
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

    # Sort chronologically (oldest → newest)
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
            "4. **PLT + thrombosis cross-check**: Patient has active VJI thrombosis on Clexane. "
            "If PLT trending up or >400, FLAG IMMEDIATELY.\n\n"
            "5. **Liver enzyme pattern**: track hepatocellular vs cholestatic pattern changes "
            "across dates — relate to hepatic metastases (C78.7).\n\n"
            "6. **Tumor markers**: CEA, CA 19-9 direction and velocity of change.\n\n"
            "7. **Threshold crossings**: Flag any value that crossed normal/abnormal boundary.\n\n"
            "8. **Chemotherapy toxicity trends**: "
            "cumulative myelosuppression, renal/hepatic function.\n\n"
            "**Output sections:** Critical Trends / Improving / Worsening / Stable / "
            "Inflammatory Markers (SII, Ne/Ly) / Tumor Markers / Questions for Oncologist"
        )
    return result


# ── Conversation archive tools (#37) ─────────────────────────────────────────


@mcp.tool()
async def log_conversation(
    ctx: Context,
    title: str,
    content: str,
    entry_date: str | None = None,
    entry_type: str = "note",
    tags: str | None = None,
    document_ids: str | None = None,
    participant: str = "claude.ai",
) -> str:
    """Save a diary entry to the conversation archive.

    Use this to log summaries, decisions, progress notes, questions,
    or any narrative content from conversations about the oncology journey.

    Args:
        title: Short title for the entry.
        content: Markdown body with the full entry text.
        entry_date: Date the entry is about (YYYY-MM-DD). Defaults to today.
        entry_type: Type of entry: summary, decision, progress, question, note.
        tags: Comma-separated tags (e.g. "chemo,FOLFOX,cycle-3").
        document_ids: Comma-separated document IDs referenced (e.g. "3,15").
        participant: Who created this: claude.ai, claude-code, oncoteam.
    """
    try:
        parsed_date = _parse_date(entry_date) or date.today()
    except ValueError as e:
        return json.dumps({"error": str(e)})

    db = _get_db(ctx)

    parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    parsed_doc_ids = (
        [int(d.strip()) for d in document_ids.split(",") if d.strip()] if document_ids else None
    )

    # Try to capture session_id from context
    session_id = getattr(ctx, "session_id", None)

    entry = ConversationEntry(
        entry_date=parsed_date,
        entry_type=entry_type,
        title=title,
        content=content,
        participant=participant,
        session_id=session_id,
        tags=parsed_tags,
        document_ids=parsed_doc_ids,
        source="live",
    )
    entry = await db.insert_conversation_entry(entry)
    return json.dumps(
        {
            "id": entry.id,
            "entry_date": entry.entry_date.isoformat(),
            "entry_type": entry.entry_type,
            "title": entry.title,
            "tags": entry.tags,
        }
    )


@mcp.tool()
async def search_conversations(
    ctx: Context,
    text: str | None = None,
    entry_type: str | None = None,
    participant: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    tags: str | None = None,
    limit: int = 50,
) -> str:
    """Search the conversation archive by text, type, date, or tags.

    Returns entries with truncated content (500 chars). Use get_conversation
    for full text of a specific entry.

    Args:
        text: Full-text search query.
        entry_type: Filter by type: summary, decision, progress, question, note.
        participant: Filter by participant: claude.ai, claude-code, oncoteam.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        tags: Comma-separated tags to filter by (all must match).
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        parsed_tags = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
        query = ConversationQuery(
            text=text,
            entry_type=entry_type,
            participant=participant,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            tags=parsed_tags,
            limit=_clamp_limit(limit),
        )
        entries = await db.search_conversation_entries(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "entry_date": e.entry_date.isoformat(),
            "entry_type": e.entry_type,
            "title": e.title,
            "content": e.content[:500] + ("..." if len(e.content) > 500 else ""),
            "participant": e.participant,
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


@mcp.tool()
async def get_conversation(ctx: Context, entry_id: int) -> str:
    """Get the full content of a single conversation entry by ID.

    Args:
        entry_id: The conversation entry ID.
    """
    db = _get_db(ctx)
    entry = await db.get_conversation_entry(entry_id)
    if not entry:
        return json.dumps({"error": f"Conversation entry not found: {entry_id}"})
    return json.dumps(
        {
            "id": entry.id,
            "entry_date": entry.entry_date.isoformat(),
            "entry_type": entry.entry_type,
            "title": entry.title,
            "content": entry.content,
            "participant": entry.participant,
            "session_id": entry.session_id,
            "tags": entry.tags,
            "document_ids": entry.document_ids,
            "source": entry.source,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
        }
    )


@mcp.tool()
async def get_journey_timeline(
    ctx: Context,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 200,
) -> str:
    """Get a unified chronological timeline merging documents and conversation entries.

    This is the complete view of the oncology journey — documents and diary entries
    interleaved by date. Useful for book writing, doctor sharing, or reviewing history.

    Args:
        date_from: Start date (YYYY-MM-DD).
        date_to: End date (YYYY-MM-DD).
        limit: Maximum items per type (default 200).
    """
    try:
        parsed_from = _parse_date(date_from)
        parsed_to = _parse_date(date_to)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    db = _get_db(ctx)

    # Fetch documents
    doc_conditions: list[str] = []
    doc_params: list[str | int] = []
    if parsed_from:
        doc_conditions.append("document_date >= ?")
        doc_params.append(parsed_from.isoformat())
    if parsed_to:
        doc_conditions.append("document_date <= ?")
        doc_params.append(parsed_to.isoformat())
    doc_conditions.append("deleted_at IS NULL")
    doc_where = " AND ".join(doc_conditions)
    async with db.db.execute(
        f"SELECT * FROM documents WHERE {doc_where} ORDER BY document_date ASC LIMIT ?",
        (*doc_params, limit),
    ) as cursor:
        doc_rows = await cursor.fetchall()

    # Fetch conversation entries
    entries = await db.get_conversation_timeline(
        date_from=parsed_from, date_to=parsed_to, limit=limit
    )

    # Merge into unified timeline
    timeline: list[dict] = []
    for row in doc_rows:
        timeline.append(
            {
                "date": row["document_date"] or "",
                "type": "document",
                "subtype": row["category"],
                "title": row["filename"],
                "detail": f"{row['institution'] or 'unknown'} | {row['category']}",
                "id": row["id"],
            }
        )
    for e in entries:
        timeline.append(
            {
                "date": e.entry_date.isoformat(),
                "type": "conversation",
                "subtype": e.entry_type,
                "title": e.title,
                "detail": e.content[:200] + ("..." if len(e.content) > 200 else ""),
                "id": e.id,
            }
        )

    # Sort chronologically
    timeline.sort(key=lambda x: x["date"])
    return json.dumps(timeline)


# ── Agent state tools (#32) ──────────────────────────────────────────────────


@mcp.tool()
async def set_agent_state(
    ctx: Context,
    key: str,
    value: str,
    agent_id: str = "oncoteam",
) -> str:
    """Set a persistent key-value pair for an agent.

    Upserts: creates the key if new, updates if it already exists.

    Args:
        key: State key name (e.g. "last_briefing_date", "treatment_protocol").
        value: JSON string value to store.
        agent_id: Agent identifier (default: oncoteam).
    """
    db = _get_db(ctx)
    state = AgentState(agent_id=agent_id, key=key, value=value)
    saved = await db.set_agent_state(state)
    return json.dumps(
        {
            "id": saved.id,
            "agent_id": saved.agent_id,
            "key": saved.key,
            "value": saved.value,
            "updated_at": saved.updated_at.isoformat() if saved.updated_at else None,
        }
    )


@mcp.tool()
async def get_agent_state(
    ctx: Context,
    key: str,
    agent_id: str = "oncoteam",
) -> str:
    """Get a persistent state value by key.

    Returns {value: null} if the key does not exist.

    Args:
        key: State key name.
        agent_id: Agent identifier (default: oncoteam).
    """
    db = _get_db(ctx)
    state = await db.get_agent_state(key, agent_id)
    if not state:
        return json.dumps({"key": key, "agent_id": agent_id, "value": None})
    return json.dumps(
        {
            "id": state.id,
            "agent_id": state.agent_id,
            "key": state.key,
            "value": state.value,
            "updated_at": state.updated_at.isoformat() if state.updated_at else None,
        }
    )


@mcp.tool()
async def list_agent_states(
    ctx: Context,
    agent_id: str = "oncoteam",
) -> str:
    """List all persistent state keys for an agent.

    Args:
        agent_id: Agent identifier (default: oncoteam).
    """
    db = _get_db(ctx)
    states = await db.list_agent_states(agent_id)
    return json.dumps(
        [
            {
                "key": s.key,
                "value": s.value,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
            for s in states
        ]
    )


# ── Treatment event tools (#34) ─────────────────────────────────────────────


@mcp.tool()
async def add_treatment_event(
    ctx: Context,
    event_date: str,
    event_type: str,
    title: str,
    notes: str = "",
    metadata: str = "{}",
) -> str:
    """Record a treatment milestone (chemo cycle, surgery, scan result, etc.).

    Args:
        event_date: Date of the event (YYYY-MM-DD).
        event_type: Type of event (e.g. chemo, surgery, scan, consult, side_effect).
        title: Short title for the event.
        notes: Optional longer description or notes.
        metadata: Optional JSON string with extra structured data.
    """
    try:
        parsed_event_date = _parse_date(event_date)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    db = _get_db(ctx)
    event = TreatmentEvent(
        event_date=parsed_event_date,
        event_type=event_type,
        title=title,
        notes=notes,
        metadata=metadata,
    )
    saved = await db.insert_treatment_event(event)
    return json.dumps(
        {
            "id": saved.id,
            "event_date": saved.event_date.isoformat(),
            "event_type": saved.event_type,
            "title": saved.title,
        }
    )


@mcp.tool()
async def list_treatment_events(
    ctx: Context,
    event_type: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 50,
) -> str:
    """List treatment events, optionally filtered by type and date range.

    Returns events in reverse chronological order.

    Args:
        event_type: Filter by event type (e.g. chemo, surgery).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        query = TreatmentEventQuery(
            event_type=event_type,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            limit=_clamp_limit(limit),
        )
        events = await db.list_treatment_events(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "event_date": e.event_date.isoformat(),
            "event_type": e.event_type,
            "title": e.title,
            "notes": e.notes,
            "metadata": e.metadata,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in events
    ]
    return json.dumps({"events": items, "total": len(items)})


@mcp.tool()
async def get_treatment_event(ctx: Context, event_id: int) -> str:
    """Get full details of a treatment event by ID.

    Args:
        event_id: The treatment event ID.
    """
    db = _get_db(ctx)
    event = await db.get_treatment_event(event_id)
    if not event:
        return json.dumps({"error": f"Treatment event not found: {event_id}"})
    return json.dumps(
        {
            "id": event.id,
            "event_date": event.event_date.isoformat(),
            "event_type": event.event_type,
            "title": event.title,
            "notes": event.notes,
            "metadata": event.metadata,
            "created_at": event.created_at.isoformat() if event.created_at else None,
        }
    )


# ── Research entry tools (#33) ──────────────────────────────────────────────


@mcp.tool()
async def add_research_entry(
    ctx: Context,
    source: str,
    external_id: str,
    title: str,
    summary: str = "",
    tags: str = "[]",
    raw_data: str = "",
) -> str:
    """Save a research article or clinical trial found by an agent.

    Deduplicates by source+external_id — if a duplicate is found, returns
    the existing entry without error.

    Args:
        source: Source name (e.g. pubmed, clinicaltrials).
        external_id: External identifier (e.g. PMID, NCT number).
        title: Article or trial title.
        summary: Brief summary or abstract excerpt.
        tags: JSON array of tags (e.g. '["FOLFOX","mCRC"]').
        raw_data: Full raw data (abstract, JSON, etc.) for reference.
    """
    db = _get_db(ctx)
    entry = ResearchEntry(
        source=source,
        external_id=external_id,
        title=title,
        summary=summary,
        tags=tags,
        raw_data=raw_data,
    )
    saved = await db.insert_research_entry(entry)
    return json.dumps(
        {
            "id": saved.id,
            "source": saved.source,
            "external_id": saved.external_id,
            "title": saved.title,
        }
    )


@mcp.tool()
async def search_research(
    ctx: Context,
    text: str | None = None,
    source: str | None = None,
    limit: int = 20,
) -> str:
    """Search saved research entries by text and/or source.

    Args:
        text: Search in title, summary, and tags.
        source: Filter by source (e.g. pubmed, clinicaltrials).
        limit: Maximum results to return.
    """
    db = _get_db(ctx)
    query = ResearchQuery(text=text, source=source, limit=limit)
    entries = await db.search_research_entries(query)
    items = [
        {
            "id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "title": e.title,
            "summary": e.summary[:500] + ("..." if len(e.summary) > 500 else ""),
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


@mcp.tool()
async def list_research_entries(
    ctx: Context,
    source: str | None = None,
    limit: int = 50,
) -> str:
    """List saved research entries, optionally filtered by source.

    Args:
        source: Filter by source (e.g. pubmed, clinicaltrials).
        limit: Maximum results to return.
    """
    db = _get_db(ctx)
    entries = await db.list_research_entries(source=source, limit=limit)
    items = [
        {
            "id": e.id,
            "source": e.source,
            "external_id": e.external_id,
            "title": e.title,
            "summary": e.summary[:200] + ("..." if len(e.summary) > 200 else ""),
            "tags": e.tags,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


# ── Activity log tools (#38) ────────────────────────────────────────────────


@mcp.tool()
async def add_activity_log(
    ctx: Context,
    session_id: str,
    agent_id: str,
    tool_name: str,
    input_summary: str = "",
    output_summary: str = "",
    duration_ms: int | None = None,
    status: str = "ok",
    error_message: str | None = None,
    tags: str = "[]",
) -> str:
    """Log an agent tool call to the activity audit trail (append-only).

    Args:
        session_id: Session identifier.
        agent_id: Agent that made the call (e.g. oncoteam).
        tool_name: Name of the tool that was called.
        input_summary: Brief summary of the input parameters.
        output_summary: Brief summary of the output.
        duration_ms: How long the call took in milliseconds.
        status: Result status (ok, error, timeout).
        error_message: Error details if status is not ok.
        tags: JSON array of tags (e.g. '["research","pubmed"]').
    """
    db = _get_db(ctx)
    entry = ActivityLogEntry(
        session_id=session_id,
        agent_id=agent_id,
        tool_name=tool_name,
        input_summary=input_summary,
        output_summary=output_summary,
        duration_ms=duration_ms,
        status=status,
        error_message=error_message,
        tags=tags,
    )
    saved = await db.insert_activity_log(entry)
    return json.dumps({"id": saved.id, "status": saved.status})


@mcp.tool()
async def search_activity_log(
    ctx: Context,
    session_id: str | None = None,
    agent_id: str | None = None,
    tool_name: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    text: str | None = None,
    limit: int = 50,
) -> str:
    """Search the activity log with filters.

    Args:
        session_id: Filter by session.
        agent_id: Filter by agent.
        tool_name: Filter by tool name.
        status: Filter by status (ok, error, timeout).
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
        text: Search in input/output summaries.
        limit: Maximum results to return.
    """
    try:
        db = _get_db(ctx)
        query = ActivityLogQuery(
            session_id=session_id,
            agent_id=agent_id,
            tool_name=tool_name,
            status=status,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
            text=text,
            limit=_clamp_limit(limit),
        )
        entries = await db.search_activity_log(query)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    items = [
        {
            "id": e.id,
            "session_id": e.session_id,
            "agent_id": e.agent_id,
            "tool_name": e.tool_name,
            "input_summary": e.input_summary,
            "output_summary": e.output_summary,
            "status": e.status,
            "duration_ms": e.duration_ms,
            "error_message": e.error_message,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in entries
    ]
    return json.dumps({"entries": items, "total": len(items)})


@mcp.tool()
async def get_activity_stats(
    ctx: Context,
    session_id: str | None = None,
    agent_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> str:
    """Get aggregated activity statistics by tool and status.

    Args:
        session_id: Filter by session.
        agent_id: Filter by agent.
        date_from: Filter from this date (YYYY-MM-DD).
        date_to: Filter to this date (YYYY-MM-DD).
    """
    try:
        db = _get_db(ctx)
        stats = await db.get_activity_stats(
            session_id=session_id,
            agent_id=agent_id,
            date_from=_parse_date(date_from),
            date_to=_parse_date(date_to),
        )
    except ValueError as e:
        return json.dumps({"error": str(e)})
    total_calls = sum(s["count"] for s in stats)
    return json.dumps({"stats": stats, "total_calls": total_calls})


# ── OAuth tools (#12) ─────────────────────────────────────────────────────


@mcp.tool()
async def gdrive_auth_url(ctx: Context) -> str:
    """Get the Google OAuth authorization URL for the user to visit.

    Returns a URL that the user should open in their browser to authorize
    Google Drive access. After authorization, Google redirects to the callback
    URL which stores the tokens automatically.
    """
    from oncofiles.oauth import get_auth_url

    if not GOOGLE_OAUTH_CLIENT_ID:
        return json.dumps({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"})

    url = get_auth_url()
    return json.dumps(
        {
            "auth_url": url,
            "instructions": "Open this URL in your browser to connect Google Drive.",
        }
    )


@mcp.tool()
async def gdrive_auth_callback(ctx: Context, code: str) -> str:
    """Exchange an OAuth authorization code for tokens and store them.

    Args:
        code: The authorization code from the Google OAuth redirect.
    """
    from datetime import datetime, timedelta

    from oncofiles.models import OAuthToken
    from oncofiles.oauth import exchange_code

    if not GOOGLE_OAUTH_CLIENT_ID:
        return json.dumps({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"})

    try:
        tokens = exchange_code(code)
    except Exception as e:
        return json.dumps({"error": f"Token exchange failed: {e}"})

    expiry = datetime.now(UTC) + timedelta(seconds=tokens.get("expires_in", 3600))
    oauth_token = OAuthToken(
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", ""),
        token_expiry=expiry,
    )

    db = _get_db(ctx)
    await db.upsert_oauth_token(oauth_token)
    msg = "Google Drive connected. Use gdrive_set_folder to pick a sync folder."
    return json.dumps({"status": "ok", "message": msg})


@mcp.tool()
async def gdrive_auth_status(ctx: Context) -> str:
    """Check if the user has valid Google Drive OAuth tokens."""
    from oncofiles.oauth import is_token_expired

    db = _get_db(ctx)
    token = await db.get_oauth_token()

    if not token:
        msg = "No OAuth tokens found. Use gdrive_auth_url to connect."
        return json.dumps({"connected": False, "message": msg})

    expired = is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None)
    return json.dumps(
        {
            "connected": True,
            "expired": expired,
            "gdrive_folder_id": token.gdrive_folder_id,
            "message": (
                "Connected" if not expired else "Token expired — will auto-refresh on next sync."
            ),
        }
    )


@mcp.tool()
async def gdrive_set_folder(ctx: Context, folder_id: str) -> str:
    """Set the Google Drive folder to sync with.

    Detects the folder owner's email and stores it for automatic permission
    sharing. When the service account creates files/folders, it grants writer
    access to the original folder owner so they can see the files.

    Args:
        folder_id: The Google Drive folder ID to use as the sync root.
    """
    db = _get_db(ctx)
    token = await db.get_oauth_token()
    if not token:
        return json.dumps({"error": "No OAuth tokens found. Connect Google Drive first."})

    await db.update_oauth_folder(token.user_id, token.provider, folder_id)

    # Detect folder owner and store for permission sharing
    gdrive = _get_gdrive(ctx)
    owner_email = None
    if gdrive:
        owner_email = gdrive.get_folder_owner(folder_id)
        if owner_email:
            await db.update_oauth_owner_email(token.user_id, token.provider, owner_email)
            gdrive.owner_email = owner_email
            logger.info("Detected folder owner: %s", owner_email)

    result = {"status": "ok", "folder_id": folder_id}
    if owner_email:
        result["owner_email"] = owner_email
        result["message"] = (
            f"Folder set. Owner '{owner_email}' will get writer access on all new files."
        )
    else:
        result["message"] = (
            "Folder set. Could not detect owner — run gdrive_fix_permissions "
            "with an explicit email to grant access."
        )
    return json.dumps(result)


# ── Sync tools (#v1.0) ───────────────────────────────────────────────────────


def _get_sync_folder_id(ctx: Context) -> str:
    """Get the GDrive folder ID from config or OAuth tokens."""
    if GOOGLE_DRIVE_FOLDER_ID:
        return GOOGLE_DRIVE_FOLDER_ID
    return ctx.request_context.lifespan_context.get("oauth_folder_id", "")


@mcp.tool()
async def gdrive_sync(
    ctx: Context,
    dry_run: bool = False,
    enhance: bool = True,
) -> str:
    """Run full bidirectional Google Drive sync.

    1. Imports new/changed files from GDrive (GDrive wins on conflicts)
    2. Exports documents to organized category/year-month folders
    3. Exports manifest + metadata markdown files

    Args:
        dry_run: Preview changes without syncing.
        enhance: Run AI summary/tag generation on new files (default True).
    """
    from oncofiles.sync import sync as _sync

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)
    if not gdrive:
        msg = "GDrive client not configured. Use gdrive_auth_url to connect."
        return json.dumps({"error": msg})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set. Use gdrive_set_folder to pick one."})

    stats = await _sync(db, files, gdrive, folder_id, dry_run=dry_run, enhance=enhance)
    return json.dumps(stats)


@mcp.tool()
async def sync_from_gdrive(
    ctx: Context,
    dry_run: bool = False,
    enhance: bool = True,
) -> str:
    """Import files from Google Drive into oncofiles.

    Walks category/year-month subfolders, detects new and changed files,
    downloads them, uploads to Files API, and stores metadata.

    Args:
        dry_run: Preview changes without importing.
        enhance: Run AI summary/tag generation on new/changed files (default True).
    """
    from oncofiles.sync import sync_from_gdrive as _sync_from_gdrive

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)
    if not gdrive:
        return json.dumps({"error": "GDrive client not configured"})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set"})

    stats = await _sync_from_gdrive(
        db,
        files,
        gdrive,
        folder_id,
        dry_run=dry_run,
        enhance=enhance,
    )
    return json.dumps(stats)


@mcp.tool()
async def sync_to_gdrive(
    ctx: Context,
    dry_run: bool = False,
) -> str:
    """Export documents from oncofiles to Google Drive.

    Uploads documents to organized category/year-month folders with
    manifest and metadata markdown files.

    Args:
        dry_run: Preview changes without exporting.
    """
    from oncofiles.sync import sync_to_gdrive as _sync_to_gdrive

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = _get_gdrive(ctx)
    if not gdrive:
        return json.dumps({"error": "GDrive client not configured"})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set"})

    stats = await _sync_to_gdrive(
        db,
        files,
        gdrive,
        folder_id,
        dry_run=dry_run,
    )
    return json.dumps(stats)


@mcp.tool()
async def gdrive_fix_permissions(
    ctx: Context,
    email: str | None = None,
) -> str:
    """Grant writer access to all files/folders in the sync root (one-off fix).

    Use this after initial sync when files were created by the service account
    and are invisible to the folder owner. Also updates the stored owner_email
    for automatic sharing on future uploads.

    Args:
        email: Email to grant access to. If omitted, detects from folder owner.
    """
    db = _get_db(ctx)
    gdrive = _get_gdrive(ctx)
    if not gdrive:
        return json.dumps({"error": "GDrive client not configured."})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set."})

    # Resolve email
    target_email = email
    if not target_email:
        target_email = gdrive.get_folder_owner(folder_id)
    if not target_email:
        return json.dumps({"error": "Could not detect folder owner. Pass email explicitly."})

    # Store owner_email for future auto-sharing
    token = await db.get_oauth_token()
    if token:
        await db.update_oauth_owner_email(token.user_id, token.provider, target_email)
    gdrive.owner_email = target_email

    # Grant access recursively
    count = gdrive.grant_access_recursive(folder_id, target_email)

    return json.dumps(
        {
            "status": "ok",
            "email": target_email,
            "files_shared": count,
            "message": f"Granted writer access to {target_email} on {count} files/folders.",
        }
    )


@mcp.tool()
async def export_manifest(ctx: Context) -> str:
    """Export the full database as a JSON manifest (on-demand).

    Returns the manifest JSON with all documents, conversations,
    treatment events, research entries, and agent state.
    """
    from oncofiles.manifest import export_manifest as _export_manifest
    from oncofiles.manifest import render_manifest_json

    db = _get_db(ctx)
    manifest = await _export_manifest(db)
    return render_manifest_json(manifest)


@mcp.tool()
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


# ── Structured metadata extraction (#Phase 2) ─────────────────────────────────


@mcp.tool()
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


# ── Clinical trials (#Phase 3) ──────────────────────────────────────────────


@mcp.tool()
async def fetch_clinical_trials(
    ctx: Context,
    condition: str,
    keywords: str | None = None,
    status: str = "RECRUITING",
    location_country: str | None = None,
    phase: str | None = None,
    limit: int = 20,
) -> str:
    """Fetch clinical trials from ClinicalTrials.gov and store in research_entries.

    Searches the ClinicalTrials.gov API v2 for matching studies and saves
    them to the research_entries table (deduplicates by NCT number).

    Args:
        condition: Medical condition to search for (e.g. "colorectal cancer").
        keywords: Additional search terms (e.g. "FOLFOX", "immunotherapy").
        status: Trial status filter (RECRUITING, ACTIVE_NOT_RECRUITING, COMPLETED).
        location_country: Country filter (e.g. "United States", "Slovakia").
        phase: Phase filter (PHASE1, PHASE2, PHASE3, PHASE4).
        limit: Maximum number of trials to fetch (default 20).
    """
    from oncofiles.clinical_trials import search_trials, trial_to_research_entry

    try:
        trials = search_trials(
            condition=condition,
            keywords=keywords,
            status=status,
            location_country=location_country,
            phase=phase,
            page_size=limit,
        )
    except Exception as e:
        return json.dumps({"error": f"ClinicalTrials.gov API error: {e}"})

    db = _get_db(ctx)
    stored = []
    for trial in trials:
        entry_data = trial_to_research_entry(trial)
        entry = ResearchEntry(**entry_data)
        saved = await db.insert_research_entry(entry)
        stored.append(
            {
                "id": saved.id,
                "nct_id": trial["nct_id"],
                "title": trial["title"],
                "status": trial["status"],
                "phase": trial["phase"],
            }
        )

    return json.dumps(
        {
            "fetched": len(trials),
            "stored": len(stored),
            "trials": stored,
        }
    )


# ── Document export package (#Phase 4) ───────────────────────────────────────


@mcp.tool()
async def export_document_package(
    ctx: Context,
    include_metadata: bool = True,
    include_timeline: bool = True,
) -> str:
    """Export a structured document package for consultations or second opinions.

    Assembles all documents grouped by category with metadata, treatment
    events timeline, and structured metadata. Returns JSON that Oncoteam
    can render as PDF, email, or share link.

    Args:
        include_metadata: Include AI summaries and structured metadata (default True).
        include_timeline: Include treatment events timeline (default True).
    """
    db = _get_db(ctx)

    # Get all documents grouped by category
    docs = await db.list_documents(limit=200)

    # Group by category
    by_category: dict[str, list[dict]] = {}
    for d in docs:
        cat = d.category.value
        if cat not in by_category:
            by_category[cat] = []
        entry = {
            "id": d.id,
            "file_id": d.file_id,
            "filename": d.filename,
            "document_date": d.document_date.isoformat() if d.document_date else None,
            "institution": d.institution,
            "description": d.description,
        }
        if include_metadata:
            if d.ai_summary:
                entry["ai_summary"] = d.ai_summary
            if d.structured_metadata:
                entry["structured_metadata"] = json.loads(d.structured_metadata)
        by_category[cat].append(entry)

    result: dict = {
        "patient": PATIENT_CONTEXT,
        "total_documents": len(docs),
        "documents_by_category": by_category,
    }

    if include_timeline:
        events = await db.get_treatment_events_timeline()
        result["treatment_timeline"] = [
            {
                "id": e.id,
                "event_date": e.event_date.isoformat(),
                "event_type": e.event_type,
                "title": e.title,
                "notes": e.notes,
            }
            for e in events
        ]

    return json.dumps(result)


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
    description="Chronological timeline of treatment documents and events",
)
async def treatment_timeline(ctx: Context) -> str:
    """Return a chronological markdown timeline merging documents and treatment events."""
    db = _get_db(ctx)
    docs = await db.get_treatment_timeline()
    events = await db.get_treatment_events_timeline()

    if not docs and not events:
        return "No treatment documents or events found."

    # Build unified timeline items
    items: list[tuple[str, str]] = []
    for d in docs:
        date_str = d.document_date.isoformat() if d.document_date else "unknown"
        line = (
            f"- [doc/{d.category.value}] **{d.filename}** "
            f"({d.institution or 'unknown'}) file_id: `{d.file_id}`"
        )
        items.append((date_str, line))
    for e in events:
        date_str = e.event_date.isoformat()
        if len(e.notes) > 100:
            notes_preview = f" — {e.notes[:100]}..."
        elif e.notes:
            notes_preview = f" — {e.notes}"
        else:
            notes_preview = ""
        line = f"- [event/{e.event_type}] **{e.title}**{notes_preview}"
        items.append((date_str, line))

    items.sort(key=lambda x: x[0])
    total = len(docs) + len(events)
    lines = [f"# Treatment Timeline ({total} items: {len(docs)} docs, {len(events)} events)\n"]
    current_date = None
    for date_str, line in items:
        if date_str != current_date:
            current_date = date_str
            lines.append(f"\n## {current_date}\n")
        lines.append(line)
    return "\n".join(lines)


@mcp.resource(
    "files://conversation-archive",
    description="Last 30 days of conversation diary entries",
)
async def conversation_archive(ctx: Context) -> str:
    """Return the last 30 days of conversation entries as markdown."""
    from datetime import timedelta

    db = _get_db(ctx)
    since = date.today() - timedelta(days=30)
    entries = await db.get_conversation_timeline(date_from=since, limit=200)
    if not entries:
        return "No conversation entries in the last 30 days."

    lines = [f"# Conversation Archive (last 30 days, {len(entries)} entries)\n"]
    current_date = None
    for e in entries:
        date_str = e.entry_date.isoformat()
        if date_str != current_date:
            current_date = date_str
            lines.append(f"\n## {current_date}\n")
        tag_str = f" [{', '.join(e.tags)}]" if e.tags else ""
        lines.append(f"### [{e.entry_type}] {e.title}{tag_str}\n")
        lines.append(e.content)
        lines.append("")
    return "\n".join(lines)


@mcp.resource(
    "files://activity-timeline",
    description="Last 24 hours of agent tool calls",
)
async def activity_timeline(ctx: Context) -> str:
    """Return the last 24 hours of agent activity as markdown."""
    db = _get_db(ctx)
    entries = await db.get_activity_timeline(hours=24)
    if not entries:
        return "No agent activity in the last 24 hours."

    lines = [f"# Activity Timeline (last 24h, {len(entries)} calls)\n"]
    for e in entries:
        ts = e.created_at.strftime("%H:%M:%S") if e.created_at else "?"
        status_icon = "x" if e.status != "ok" else "v"
        duration = f" ({e.duration_ms}ms)" if e.duration_ms else ""
        lines.append(f"- [{ts}] [{status_icon}] {e.agent_id}/{e.tool_name}{duration}")
        if e.input_summary:
            lines.append(f"  in: {e.input_summary[:100]}")
        if e.error_message:
            lines.append(f"  err: {e.error_message[:200]}")
    return "\n".join(lines)


# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    if MCP_TRANSPORT == "stdio":
        mcp.run()
    else:
        mcp.run(transport=MCP_TRANSPORT, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
