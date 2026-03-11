"""FastMCP server for Oncofiles medical document management."""

from __future__ import annotations

import hmac
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

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
    PATIENT_CONTEXT_PATH,
    SYNC_ENABLED,
    SYNC_INTERVAL_MINUTES,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
    VERSION,
)
from oncofiles.database import Database
from oncofiles.gdrive_client import GDriveClient, create_gdrive_client

logger = logging.getLogger(__name__)

# ── Auth ──────────────────────────────────────────────────────────────────────


def _create_auth():
    """Create auth provider based on environment.

    - streamable-http: PersistentOAuthProvider (survives deploys) + optional bearer
    - MCP_BEARER_TOKEN set: StaticTokenVerifier (dev/testing)
    - otherwise: None (no auth)
    """
    if MCP_TRANSPORT == "streamable-http":
        from fastmcp.server.auth.auth import ClientRegistrationOptions

        from oncofiles.persistent_oauth import PersistentOAuthProvider

        return PersistentOAuthProvider(
            bearer_token=MCP_BEARER_TOKEN or None,
            base_url="https://aware-kindness-production.up.railway.app",
            client_registration_options=ClientRegistrationOptions(enabled=False),
        )

    if MCP_BEARER_TOKEN:
        from fastmcp.server.auth import StaticTokenVerifier

        return StaticTokenVerifier(
            tokens={MCP_BEARER_TOKEN: {"client_id": "claude-ai", "scopes": []}},
        )

    return None


auth = _create_auth()

if auth is None and MCP_TRANSPORT != "stdio":
    logging.getLogger(__name__).warning(
        "No authentication configured for transport=%s. Set MCP_BEARER_TOKEN.", MCP_TRANSPORT
    )


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
    from oncofiles.files_api import FilesClient

    _setup_logging()
    logger.info("Starting Oncofiles MCP server (transport=%s)", MCP_TRANSPORT)
    if TURSO_DATABASE_URL:
        db = Database(turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    # Load patient context (DB → JSON file → hardcoded default)
    from oncofiles import patient_context

    await patient_context.initialize(db.db, PATIENT_CONTEXT_PATH)
    files = FilesClient()
    # Restore MCP OAuth sessions from DB (survive deploys)
    if hasattr(auth, "set_db"):
        auth.set_db(db)
        await auth.restore_from_db()
    # Load owner_email from OAuth tokens (needed for service account permission sharing)
    oauth_folder_id = ""
    owner_email = ""
    token = None
    try:
        token = await db.get_oauth_token()
        if token:
            oauth_folder_id = token.gdrive_folder_id or ""
            owner_email = token.owner_email or ""
    except Exception:
        logger.debug("Failed to load OAuth token at startup", exc_info=True)

    # Prefer OAuth (user has storage quota for uploads; service account does not)
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

    # Fall back to service account if no OAuth
    if not gdrive:
        try:
            gdrive = create_gdrive_client(owner_email=owner_email)
        except Exception as e:
            logger.warning("GDrive client init failed: %s — fallback disabled", e)

    # Start background sync scheduler
    scheduler = None
    if SYNC_ENABLED and gdrive:
        scheduler = _start_sync_scheduler(db, files, gdrive, oauth_folder_id)

    # Log memory usage after initialization
    import resource

    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
    logger.info("Startup complete — RSS: %.1f MB", rss_mb)

    try:
        yield {
            "db": db,
            "files": files,
            "gdrive": gdrive,
            "oauth_folder_id": oauth_folder_id,
            "gdrive_folder_id": _get_sync_folder_id_from(oauth_folder_id),
        }
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
            logger.info("Sync scheduler stopped")
        await db.close()


def _start_sync_scheduler(db, files, gdrive, oauth_folder_id):
    """Start APScheduler for periodic GDrive sync."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from oncofiles.sync import extract_all_metadata, sync

    async def _run_sync():
        import gc

        folder_id = _get_sync_folder_id_from(oauth_folder_id)
        if not folder_id:
            logger.debug("Scheduled sync skipped — no folder ID")
            return
        try:
            stats = await sync(db, files, gdrive, folder_id)
            logger.info("Scheduled sync complete: %s", stats)
        except Exception:
            logger.exception("Scheduled sync failed")
        finally:
            gc.collect()

    async def _run_metadata_extraction():
        import gc

        try:
            stats = await extract_all_metadata(db, files, gdrive)
            if stats["processed"] > 0:
                logger.info("Metadata extraction: %s", stats)
        except Exception:
            logger.exception("Metadata extraction failed")
        finally:
            gc.collect()

    async def _run_trash_cleanup():
        try:
            purged = await db.purge_expired_trash(days=30)
            if purged:
                logger.info("Trash cleanup: purged %d expired documents", purged)
        except Exception:
            logger.exception("Trash cleanup failed")

    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_sync,
        IntervalTrigger(minutes=SYNC_INTERVAL_MINUTES),
        id="gdrive_sync",
        max_instances=1,
    )
    scheduler.add_job(
        _run_metadata_extraction,
        CronTrigger(hour=3, minute=30),  # daily at 3:30 AM (after trash cleanup)
        id="metadata_extraction",
        max_instances=1,
    )
    scheduler.add_job(
        _run_trash_cleanup,
        CronTrigger(hour=3, minute=0),  # daily at 3 AM
        id="trash_cleanup",
        max_instances=1,
    )
    scheduler.start()
    logger.info("Sync scheduler started — every %d min", SYNC_INTERVAL_MINUTES)
    return scheduler


def _get_sync_folder_id_from(oauth_folder_id: str) -> str:
    """Resolve GDrive folder ID from config or OAuth."""
    return GOOGLE_DRIVE_FOLDER_ID or oauth_folder_id


# ── MCP server ────────────────────────────────────────────────────────────────

from oncofiles.audit_middleware import AuditMiddleware  # noqa: E402

_MCP_INSTRUCTIONS = """\
Medical document management for oncology patient records.

SOURCE ATTRIBUTION — every response includes verifiable source links:
1. Every document has a `gdrive_url` field linking to Google Drive. Always present this \
link to the user so they can view, verify, or share the original document.
2. Research entries include a `url` field linking to PubMed or ClinicalTrials.gov. \
Always display these when citing research findings.
3. When making clinical observations, cite the specific source document(s) by filename \
and `gdrive_url`. Never state medical facts without a traceable source.
4. Use `get_related_documents` to discover cross-referenced documents (same visit, \
shared diagnoses, follow-ups) for comprehensive context.
5. For lab trend analysis, reference the source `document_id` for each data point.

CROSS-REFERENCES — documents are automatically linked:
- `same_visit`: same date + institution (e.g., labs and imaging from one appointment)
- `related`: nearby dates or shared diagnoses/medications

RECOMMENDED WORKFLOW for chat clients:
- Show GDrive links as clickable "View original" buttons alongside document summaries.
- Show PubMed/ClinicalTrials.gov links alongside research citations.
- Use `get_related_documents` for drill-down into connected records.
- In export packages, all entries include `gdrive_url` for offline verification.

Available categories: labs, report, imaging, imaging_ct, imaging_us, pathology, genetics, \
surgery, surgical_report, prescription, referral, discharge, discharge_summary, chemo_sheet, \
reference, advocate, other.\
"""

mcp = FastMCP(
    "Oncofiles",
    instructions=_MCP_INSTRUCTIONS,
    lifespan=lifespan,
    auth=auth,
)
mcp.add_middleware(AuditMiddleware())


# ── Landing page ─────────────────────────────────────────────────────────────

_LANDING_HTML: str | None = None


def _load_landing_html() -> str:
    global _LANDING_HTML  # noqa: PLW0603
    if _LANDING_HTML is None:
        from pathlib import Path

        html_path = Path(__file__).parent / "landing.html"
        _LANDING_HTML = html_path.read_text()
    return _LANDING_HTML


@mcp.custom_route("/", methods=["GET"])
async def landing(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_landing_html())


# ── Health check ──────────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        reconnected = await db.reconnect_if_stale()
        result = {"status": "ok", "version": VERSION}
        if reconnected:
            result["reconnected"] = True
        return JSONResponse(result)
    except Exception:
        return JSONResponse({"status": "degraded", "version": VERSION}, status_code=503)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> JSONResponse:
    """Return server metrics. Requires bearer token authentication."""
    import os
    import resource
    import time

    # Require bearer token for metrics
    auth_header = request.headers.get("authorization", "")
    if not MCP_BEARER_TOKEN or not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token = auth_header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), MCP_BEARER_TOKEN.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        doc_count = await db.count_documents()

        # Memory usage (RSS in MB)
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        rss_mb = rusage.ru_maxrss / (1024 * 1024)  # macOS reports bytes
        if sys.platform == "linux":
            rss_mb = rusage.ru_maxrss / 1024  # Linux reports KB

        # Process uptime
        pid = os.getpid()
        try:
            create_time = os.path.getctime(f"/proc/{pid}")
            uptime_s = int(time.time() - create_time)
        except (OSError, FileNotFoundError):
            uptime_s = None

        return JSONResponse({
            "memory_rss_mb": round(rss_mb, 1),
            "documents": doc_count,
            "version": VERSION,
            "pid": pid,
            "uptime_seconds": uptime_s,
        })
    except Exception:
        logger.exception("Metrics endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> JSONResponse:
    """Handle Google OAuth 2.0 redirect callback."""
    from datetime import datetime, timedelta

    from oncofiles.models import OAuthToken
    from oncofiles.oauth import exchange_code, verify_state_token

    # Validate CSRF state parameter
    state = request.query_params.get("state", "")
    if not verify_state_token(state):
        return JSONResponse({"error": "Invalid or expired state parameter."}, status_code=400)

    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "Missing authorization code"}, status_code=400)

    try:
        tokens = exchange_code(code)
    except Exception:
        logger.exception("OAuth token exchange failed")
        return JSONResponse({"error": "Token exchange failed. Please try again."}, status_code=500)

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


# ── Register tools and resources ─────────────────────────────────────────────

from oncofiles import resources  # noqa: E402
from oncofiles.tools import (  # noqa: E402
    activity,
    agent_state,
    analysis,
    clinical,
    conversations,
    documents,
    enhance_tools,
    export,
    gdrive,
    lab_trends,
    patient,
    research,
    treatment,
)

documents.register(mcp)
analysis.register(mcp)
conversations.register(mcp)
agent_state.register(mcp)
treatment.register(mcp)
research.register(mcp)
activity.register(mcp)
gdrive.register(mcp)
enhance_tools.register(mcp)
clinical.register(mcp)
lab_trends.register(mcp)
export.register(mcp)
patient.register(mcp)
resources.register(mcp)

# ── Backward-compatible re-exports for tests ─────────────────────────────────
from oncofiles.tools._helpers import (  # noqa: E402, F401
    PATIENT_CONTEXT,
    _check_baseline_labs,
    _clamp_limit,
    _doc_header,
    _doc_to_dict,
    _ensure_ocr_text,
    _extract_pdf_text,
    _gdrive_url,
    _get_db,
    _get_files,
    _get_gdrive,
    _inline_content,
    _parse_date,
    _patient_context_text,
    _pdf_to_images,
    _research_source_url,
    _resize_image_if_needed,
    _try_download,
    extract_text_from_image,
)
from oncofiles.tools.analysis import analyze_labs, compare_labs, view_document  # noqa: E402, F401
from oncofiles.tools.conversations import (  # noqa: E402, F401
    get_conversation,
    get_journey_timeline,
    log_conversation,
    search_conversations,
)

# ── Entry point ──────────────────────────────────────────────────────────────


def main() -> None:
    if MCP_TRANSPORT == "stdio":
        mcp.run()
    else:
        mcp.run(transport=MCP_TRANSPORT, host=MCP_HOST, port=MCP_PORT)


if __name__ == "__main__":
    main()
