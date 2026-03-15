"""FastMCP server for Oncofiles medical document management."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

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
    started_at = datetime.now(UTC)
    deploy_id = os.environ.get("RAILWAY_DEPLOYMENT_ID", "")
    git_sha = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    logger.info(
        "Starting Oncofiles MCP server (transport=%s, deploy=%s, commit=%s)",
        MCP_TRANSPORT,
        deploy_id[:12] or "local",
        git_sha[:7] or "dev",
    )
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

    # Warmup: ping Turso to ensure connection is fresh before accepting requests
    try:
        await db.reconnect_if_stale()
        logger.info("Startup warmup: DB connection verified")
    except Exception:
        logger.warning(
            "Startup warmup: DB ping failed, will reconnect on first request",
            exc_info=True,
        )

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
            "started_at": started_at,
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

    sync_timeout = 300  # 5 minutes max for scheduled sync

    async def _run_sync():
        import gc
        import resource

        # Memory guard: skip sync if RSS too high (prevents OOM spiral)
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
        if rss_mb > 500:
            logger.warning("Skipping sync — RSS %.1f MB exceeds 500 MB threshold", rss_mb)
            gc.collect()
            return

        folder_id = _get_sync_folder_id_from(oauth_folder_id)
        if not folder_id:
            logger.debug("Scheduled sync skipped — no folder ID")
            return
        try:
            stats = await asyncio.wait_for(
                sync(db, files, gdrive, folder_id),
                timeout=sync_timeout,
            )
            logger.info("Scheduled sync complete: %s", stats)
            # Auto-enhance new docs after sync (if any were imported)
            if stats.get("new", 0) > 0 or stats.get("updated", 0) > 0:
                try:
                    e_stats = await asyncio.wait_for(
                        extract_all_metadata(db, files, gdrive),
                        timeout=metadata_timeout,
                    )
                    if e_stats["processed"] > 0:
                        logger.info("Post-sync enhance: %s", e_stats)
                except Exception:
                    logger.warning("Post-sync enhance failed", exc_info=True)
        except TimeoutError:
            logger.error("Scheduled sync timed out after %ds", sync_timeout)
        except Exception:
            logger.exception("Scheduled sync failed")
        finally:
            gc.collect()

    metadata_timeout = 600  # 10 minutes max for metadata extraction

    async def _run_metadata_extraction():
        import gc

        try:
            stats = await asyncio.wait_for(
                extract_all_metadata(db, files, gdrive),
                timeout=metadata_timeout,
            )
            if stats["processed"] > 0:
                logger.info("Metadata extraction: %s", stats)
        except TimeoutError:
            logger.error("Metadata extraction timed out after %ds", metadata_timeout)
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

    async def _run_oauth_token_cleanup():
        """Remove expired MCP OAuth tokens older than 30 days."""
        try:
            async with db.db.execute(
                "DELETE FROM mcp_oauth_tokens WHERE expires_at IS NOT NULL "
                "AND expires_at < datetime('now', '-30 days')"
            ) as cursor:
                deleted = cursor.rowcount
            await db.db.commit()
            if deleted:
                logger.info("OAuth cleanup: removed %d expired tokens", deleted)
        except Exception:
            logger.exception("OAuth token cleanup failed")

    async def _log_rss():
        import resource

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        rss_mb = rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024
        logger.info("Periodic RSS check: %.1f MB", rss_mb)

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
    scheduler.add_job(
        _run_oauth_token_cleanup,
        CronTrigger(hour=4, minute=0),  # daily at 4 AM
        id="oauth_token_cleanup",
        max_instances=1,
    )
    scheduler.add_job(
        _log_rss,
        CronTrigger(hour="*/6", minute=15),  # every 6 hours at :15
        id="rss_monitor",
        max_instances=1,
    )

    async def _run_category_validation():
        """Auto-correct document categories after metadata extraction."""
        try:
            from oncofiles.models import DocumentCategory as _DocCat  # noqa: N814
            from oncofiles.tools.hygiene import _DOCTYPE_TO_CATEGORY

            docs = await db.list_documents(limit=500)
            import json as _json

            corrected = 0
            for doc in docs:
                if not doc.structured_metadata or doc.category.value in ("advocate", "reference"):
                    continue
                try:
                    meta = _json.loads(doc.structured_metadata)
                except (ValueError, TypeError):
                    continue
                doc_type = meta.get("document_type")
                if not doc_type:
                    continue
                expected = _DOCTYPE_TO_CATEGORY.get(doc_type)
                if not expected and doc_type in {c.value for c in _DocCat}:
                    expected = doc_type
                if expected and doc.category.value != expected:
                    await db.update_document_category(doc.id, expected)
                    corrected += 1
                    logger.info(
                        "Category auto-corrected: %s %s → %s",
                        doc.filename,
                        doc.category.value,
                        expected,
                    )
            if corrected:
                logger.info("Category validation: corrected %d documents", corrected)
        except Exception:
            logger.exception("Category validation failed")

    scheduler.add_job(
        _run_category_validation,
        CronTrigger(hour=3, minute=45),  # daily at 3:45 AM (after metadata extraction)
        id="category_validation",
        max_instances=1,
    )

    # Startup: full sync + enhance + category validation 60s after boot
    from apscheduler.triggers.date import DateTrigger

    async def _startup_catchup():
        """Run full sync, then enhance + validate to catch up after redeploy."""
        await _run_sync()
        await _run_metadata_extraction()
        await _run_category_validation()
        logger.info("Startup catchup complete: sync + enhance + validate")

    startup_time = datetime.now() + timedelta(seconds=60)
    scheduler.add_job(
        _startup_catchup,
        DateTrigger(run_date=startup_time),
        id="startup_catchup",
        max_instances=1,
    )
    logger.info("Startup catchup scheduled for %s", startup_time.strftime("%H:%M:%S"))

    # Log scheduler job outcomes for observability
    def _job_executed(event):
        logger.info("Scheduler job completed: %s", event.job_id)

    def _job_error(event):
        logger.error("Scheduler job failed: %s — %s", event.job_id, event.exception)

    def _job_missed(event):
        logger.warning("Scheduler job missed (previous still running): %s", event.job_id)

    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED

    scheduler.add_listener(_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(_job_error, EVENT_JOB_ERROR)
    scheduler.add_listener(_job_missed, EVENT_JOB_MISSED)

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

Available categories: labs, report, imaging, pathology, genetics, \
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
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db: Database = lifespan_ctx["db"]
        reconnected = await db.reconnect_if_stale()
        result = {"status": "ok", "version": VERSION}
        commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
        if commit:
            result["commit"] = commit
        started_at = lifespan_ctx.get("started_at")
        if started_at:
            result["uptime_s"] = int((datetime.now(UTC) - started_at).total_seconds())
        if reconnected:
            result["reconnected"] = True
        return JSONResponse(result)
    except Exception:
        return JSONResponse({"status": "degraded", "version": VERSION}, status_code=503)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> JSONResponse:
    """Return server metrics. Requires bearer token authentication."""
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

        return JSONResponse(
            {
                "memory_rss_mb": round(rss_mb, 1),
                "documents": doc_count,
                "version": VERSION,
                "pid": pid,
                "uptime_seconds": uptime_s,
            }
        )
    except Exception:
        logger.exception("Metrics endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> JSONResponse:
    """Handle Google OAuth 2.0 redirect callback."""
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
    db_query,
    documents,
    enhance_tools,
    export,
    gdrive,
    hygiene,
    lab_trends,
    naming,
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
naming.register(mcp)
patient.register(mcp)
hygiene.register(mcp)
db_query.register(mcp)
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
        mcp.run(
            transport=MCP_TRANSPORT,
            host=MCP_HOST,
            port=MCP_PORT,
            uvicorn_config={
                "timeout_keep_alive": 120,
                "limit_concurrency": 50,
            },
        )


if __name__ == "__main__":
    main()
