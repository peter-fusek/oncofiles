"""FastMCP server for Oncofiles medical document management."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from oncofiles.config import (
    DASHBOARD_ALLOWED_EMAILS,
    DATABASE_PATH,
    GOOGLE_DRIVE_FOLDER_ID,
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    LOG_LEVEL,
    MAX_DOCUMENTS_PER_PATIENT,
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
from oncofiles.memory import (
    get_rss_mb,
    get_rss_trend,
    get_semaphore_status,
    init_rss_tracking,
    is_memory_pressure,
    periodic_memory_check,
)

logger = logging.getLogger(__name__)

CONF_MAX_DOCS = MAX_DOCUMENTS_PER_PATIENT

# Stats constants (single source of truth for values that can't be computed at runtime)
TESTS_COUNT = 627

# Global sync semaphore — limits concurrent sync operations (GDrive + Gmail + Calendar)
_sync_semaphore = asyncio.Semaphore(
    1
)  # serialize all patient-touching jobs (Turso single-connection)

# In-memory share codes: {code: {patient_id, bearer_token, patient_name, created_at}}
_share_codes: dict[str, dict] = {}

# Rate limiter: {endpoint_key: [timestamp, ...]}
_rate_limits: dict[str, list[float]] = {}
_RATE_WINDOW = 60  # 1 minute window
_RATE_LIMITS = {
    "share-link": 5,  # max 5 share codes per minute
    "patient-tokens": 10,  # max 10 token creations per minute
    "patients": 5,  # max 5 patient creations per minute
    "share-redeem": 20,  # max 20 redemption attempts per minute (brute-force protection)
}


def _check_rate_limit(key: str, *, request: Request | None = None) -> JSONResponse | None:
    """Check rate limit for a given key. Per-IP for share-redeem, global otherwise."""
    limit = _RATE_LIMITS.get(key, 60)
    # Per-IP scoping for brute-force-sensitive endpoints
    rate_key = key
    if request and key == "share-redeem":
        client_ip = request.client.host if request.client else "unknown"
        rate_key = f"{key}:{client_ip}"
    now = time.time()
    if rate_key not in _rate_limits:
        _rate_limits[rate_key] = []
    _rate_limits[rate_key] = [t for t in _rate_limits[rate_key] if now - t < _RATE_WINDOW]
    if len(_rate_limits[rate_key]) >= limit:
        return JSONResponse(
            {"error": "Rate limit exceeded. Try again in a minute."},
            status_code=429,
        )
    _rate_limits[rate_key].append(now)
    return None


def _check_bearer(request: Request) -> JSONResponse | None:
    """Validate bearer token from Authorization header. Returns error response or None if OK."""
    if not MCP_BEARER_TOKEN:
        return None
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    token = auth_header.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token.encode(), MCP_BEARER_TOKEN.encode()):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return None


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
            base_url="https://oncofiles.com",
            client_registration_options=ClientRegistrationOptions(enabled=True),
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
    init_rss_tracking()
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
        from oncofiles.config import TURSO_REPLICA_PATH

        db = Database(
            turso_url=TURSO_DATABASE_URL,
            turso_token=TURSO_AUTH_TOKEN,
            turso_replica_path=TURSO_REPLICA_PATH,
        )
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    # Load patient context (DB → JSON file → hardcoded default)
    from oncofiles import patient_context

    await patient_context.initialize(db.db, PATIENT_CONTEXT_PATH)
    # Load per-patient context for all active patients (populates _contexts dict)
    for _p in await db.list_patients(active_only=True):
        await patient_context.load_from_db(db.db, patient_id=_p.patient_id)
    files = FilesClient()
    # Restore MCP OAuth sessions from DB (survive deploys)
    if hasattr(auth, "set_db"):
        auth.set_db(db)
        await auth.restore_from_db()
    # Load owner_email from OAuth tokens (needed for service account permission sharing)
    oauth_folder_id = ""
    owner_email = ""
    token = None
    # Resolve erika's UUID from slug (post-migration, patient_id is a UUID)
    _erika = await db.get_patient_by_slug("erika")
    _erika_uuid = _erika.patient_id if _erika else ""
    try:
        token = await db.get_oauth_token(patient_id=_erika_uuid) if _erika_uuid else None
        if token:
            oauth_folder_id = token.gdrive_folder_id or ""
            owner_email = token.owner_email or ""
    except Exception:
        logger.warning(
            "Failed to load OAuth token at startup — GDrive OAuth unavailable",
            exc_info=True,
        )

    # Prefer OAuth (user has storage quota for uploads; service account does not)
    gdrive = None
    gmail_client = None
    calendar_client = None
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
        logger.warning("OAuth GDrive init failed: %s", e, exc_info=True)

    # Build Gmail/Calendar clients if scopes granted
    if token and GOOGLE_OAUTH_CLIENT_ID:
        try:
            granted = json.loads(token.granted_scopes) if token.granted_scopes else []
            from oncofiles.oauth import SCOPE_CALENDAR, SCOPE_GMAIL

            if SCOPE_GMAIL in granted:
                from oncofiles.gmail_client import GmailClient

                gmail_client = GmailClient.from_oauth(
                    access_token=access_token,
                    refresh_token=token.refresh_token,
                    client_id=GOOGLE_OAUTH_CLIENT_ID,
                    client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
                )
                logger.info("Gmail client initialized from OAuth tokens")
            if SCOPE_CALENDAR in granted:
                from oncofiles.calendar_client import CalendarClient

                calendar_client = CalendarClient.from_oauth(
                    access_token=access_token,
                    refresh_token=token.refresh_token,
                    client_id=GOOGLE_OAUTH_CLIENT_ID,
                    client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
                )
                logger.info("Calendar client initialized from OAuth tokens")
        except Exception:
            logger.warning("Gmail/Calendar client init failed", exc_info=True)

    # Fall back to service account if no OAuth
    if not gdrive:
        try:
            gdrive = create_gdrive_client(owner_email=owner_email)
        except Exception as e:
            logger.warning("GDrive client init failed: %s — fallback disabled", e)

    # Start background sync scheduler
    scheduler = None
    job_tracker: dict[str, dict] = {}
    if SYNC_ENABLED:
        sync_folder_id = _get_sync_folder_id_from(oauth_folder_id)
        scheduler, job_tracker = _start_sync_scheduler(
            db,
            files,
            gdrive,
            sync_folder_id,
            gmail_client,
            calendar_client,
            erika_uuid=_erika_uuid,
        )

    # ── Startup validation ─────────────────────────────────────────────
    tool_count = _count_tools()
    oauth_stats = getattr(auth, "_last_restore_stats", {})
    logger.info(
        "Startup complete — tools=%d, RSS=%.1f MB, oauth_clients=%d, oauth_tokens=%d",
        tool_count,
        get_rss_mb(),
        oauth_stats.get("clients", 0),
        oauth_stats.get("access_tokens", 0) + oauth_stats.get("refresh_tokens", 0),
    )
    if tool_count < 70:
        logger.warning(
            "Tool count %d is below expected minimum (70). "
            "Some tool modules may have failed to register silently.",
            tool_count,
        )

    try:
        yield {
            "db": db,
            "files": files,
            "gdrive": gdrive,
            "oauth_folder_id": oauth_folder_id,
            "gdrive_folder_id": _get_sync_folder_id_from(oauth_folder_id),
            "started_at": started_at,
            "gmail_client": gmail_client,
            "calendar_client": calendar_client,
            "job_tracker": job_tracker,
        }
    finally:
        if scheduler:
            scheduler.shutdown(wait=False)
            logger.info("Sync scheduler stopped")
        await db.close()


# Cache of per-patient API clients — avoids re-creating HTTP pools every 5 min.
# Key: patient_id, Value: (gdrive, gmail, calendar, folder_id, created_at)
_patient_clients_cache: dict[str, tuple] = {}
_CLIENT_CACHE_TTL = 1800  # 30 min — refresh token changes invalidate sooner

# Track consecutive GDrive folder 404 failures per patient.
# After _FOLDER_404_THRESHOLD consecutive 404s, skip sync until folder is re-set.
_folder_404_counts: dict[str, int] = {}
_FOLDER_404_THRESHOLD = 3

# Per-service last-sync timestamps for dashboard (#276)
_last_service_sync: dict[str, dict[str, str | None]] = {}


def _record_service_sync(pid: str, service: str):
    """Record that a service sync completed for a patient."""
    if pid not in _last_service_sync:
        _last_service_sync[pid] = {"gdrive": None, "gmail": None, "calendar": None}
    _last_service_sync[pid][service] = datetime.now(UTC).isoformat()


def _get_service_sync_times(pid: str) -> dict[str, str | None]:
    return _last_service_sync.get(pid, {"gdrive": None, "gmail": None, "calendar": None})


def _is_folder_not_found(exc: BaseException) -> bool:
    """Check if an exception is a GDrive 404 (folder not found)."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return True
    # Also check nested exceptions from asyncio.to_thread
    if exc.__cause__:
        return _is_folder_not_found(exc.__cause__)
    return False


async def _create_patient_clients(
    db,
    patient_id: str,
) -> tuple | None:
    """Load OAuth token, refresh if needed, create API clients for a patient.

    Returns ``(gdrive, gmail_client, calendar_client, folder_id)`` or
    ``None`` if the patient has no OAuth token or no folder configured.
    Caches clients for 30 min to avoid rebuilding HTTP pools every sync cycle.
    """
    import time as _time

    from oncofiles.oauth import SCOPE_CALENDAR, SCOPE_GMAIL, is_token_expired, refresh_access_token

    # Check cache first
    cached = _patient_clients_cache.get(patient_id)
    if cached and (_time.monotonic() - cached[4]) < _CLIENT_CACHE_TTL:
        return cached[:4]

    token = await db.get_oauth_token(patient_id=patient_id)
    if not token or not token.gdrive_folder_id:
        return None

    access_token = token.access_token
    if is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None):
        try:
            refreshed = refresh_access_token(token.refresh_token)
            access_token = refreshed["access_token"]
            new_expiry = datetime.now(UTC) + timedelta(seconds=refreshed.get("expires_in", 3600))
            token.access_token = access_token
            token.token_expiry = new_expiry
            await db.upsert_oauth_token(token)
        except Exception:
            logger.warning("Token refresh failed for patient %s", patient_id, exc_info=True)
            return None

    p_gdrive = GDriveClient.from_oauth(
        access_token=access_token,
        refresh_token=token.refresh_token,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        owner_email=token.owner_email or "",
    )

    granted = json.loads(token.granted_scopes) if token.granted_scopes else []

    p_gmail = None
    if SCOPE_GMAIL in granted:
        from oncofiles.gmail_client import GmailClient

        p_gmail = GmailClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )

    p_calendar = None
    if SCOPE_CALENDAR in granted:
        from oncofiles.calendar_client import CalendarClient

        p_calendar = CalendarClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )

    # Cache the clients
    _patient_clients_cache[patient_id] = (
        p_gdrive,
        p_gmail,
        p_calendar,
        token.gdrive_folder_id,
        _time.monotonic(),
    )

    return (p_gdrive, p_gmail, p_calendar, token.gdrive_folder_id)


def _start_sync_scheduler(
    db,
    files,
    gdrive,
    oauth_folder_id,
    gmail_client=None,
    calendar_client=None,
    *,
    erika_uuid: str = "",
):
    """Start APScheduler for periodic GDrive sync, Gmail sync, and Calendar sync."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from oncofiles.sync import enhance_documents as _enhance_docs
    from oncofiles.sync import extract_all_metadata, sync

    sync_timeout = 300  # 5 minutes max for scheduled sync

    _last_calendar_sync_times: dict[str, str] = {}

    async def _get_patient_gdrive(pid: str) -> tuple | None:
        """Get GDrive client + folder_id for a patient. Uses shared lifespan clients for erika."""
        if pid == erika_uuid and gdrive and oauth_folder_id:
            return (gdrive, _get_sync_folder_id_from(oauth_folder_id))
        clients = await _create_patient_clients(db, pid)
        if not clients:
            return None
        return (clients[0], clients[3])

    async def _get_patient_gmail(pid: str):
        """Get Gmail client for a patient. Uses shared lifespan clients for erika."""
        if pid == erika_uuid and gmail_client:
            return gmail_client
        clients = await _create_patient_clients(db, pid)
        return clients[1] if clients else None

    async def _get_patient_calendar(pid: str):
        """Get Calendar client for a patient. Uses shared lifespan clients for erika."""
        if pid == erika_uuid and calendar_client:
            return calendar_client
        clients = await _create_patient_clients(db, pid)
        return clients[2] if clients else None

    async def _run_sync(trigger: str = "scheduled"):
        if is_memory_pressure("sync"):
            return

        patients = await db.list_patients(active_only=True)
        for p in patients:
            pid = p.patient_id
            if is_memory_pressure(f"sync:{pid}"):
                break

            gc = await _get_patient_gdrive(pid)
            if not gc:
                continue
            p_gdrive, folder_id = gc

            # Skip if folder has been flagged as invalid (consecutive 404s)
            if _folder_404_counts.get(pid, 0) >= _FOLDER_404_THRESHOLD:
                logger.debug(
                    "Sync [%s] skipped — folder marked invalid after %d consecutive 404s. "
                    "Re-set folder via gdrive_set_folder to resume.",
                    pid,
                    _folder_404_counts[pid],
                )
                continue

            # Note: has_changes_since pre-check was removed — it only checked
            # root folder children, missing files in category subfolders (#206).
            # The sync itself is cheap for unchanged files (md5+modifiedTime skip).

            async with _sync_semaphore:
                try:
                    stats = await asyncio.wait_for(
                        sync(db, files, p_gdrive, folder_id, trigger=trigger, patient_id=pid),
                        timeout=sync_timeout,
                    )
                    # Sync succeeded — reset 404 counter
                    _folder_404_counts.pop(pid, None)
                    _record_service_sync(pid, "gdrive")
                    logger.info("Sync [%s]: %s (RSS: %.1f MB)", pid, stats, get_rss_mb())
                    # Auto-enhance new docs after sync
                    from_stats = stats.get("from_gdrive", {})
                    if from_stats.get("new", 0) > 0 or from_stats.get("updated", 0) > 0:
                        try:
                            e_stats = await asyncio.wait_for(
                                extract_all_metadata(db, files, p_gdrive, patient_id=pid),
                                timeout=metadata_timeout,
                            )
                            if e_stats["processed"] > 0:
                                logger.info("Post-sync enhance [%s]: %s", pid, e_stats)
                        except Exception:
                            logger.error("Post-sync enhance [%s] failed", pid, exc_info=True)
                except TimeoutError:
                    logger.error("Sync [%s] timed out after %ds", pid, sync_timeout)
                except Exception as exc:
                    # Detect GDrive folder 404 — stop hammering a dead folder
                    if _is_folder_not_found(exc):
                        count = _folder_404_counts.get(pid, 0) + 1
                        _folder_404_counts[pid] = count
                        logger.warning(
                            "Sync [%s] folder 404 (%d/%d). %s",
                            pid,
                            count,
                            _FOLDER_404_THRESHOLD,
                            "Sync suspended — re-set folder to resume."
                            if count >= _FOLDER_404_THRESHOLD
                            else "Will retry next cycle.",
                        )
                    else:
                        logger.exception("Sync [%s] failed", pid)
                finally:
                    from oncofiles.memory import reclaim_memory

                    reclaim_memory(f"gdrive_sync:{pid}")

        # Post-sync reclaim (restart logic consolidated into periodic_memory_check)
        from oncofiles.memory import reclaim_memory as _reclaim

        _reclaim("gdrive_sync_all")

    metadata_timeout = 600  # 10 minutes max for metadata extraction

    async def _run_metadata_extraction():
        try:
            patients = await db.list_patients(active_only=True)
            for p in patients:
                gc = await _get_patient_gdrive(p.patient_id)
                if not gc:
                    continue
                p_gdrive, _ = gc
                stats = await asyncio.wait_for(
                    extract_all_metadata(db, files, p_gdrive, patient_id=p.patient_id),
                    timeout=metadata_timeout,
                )
                if stats["processed"] > 0:
                    logger.info("Metadata extraction [%s]: %s", p.patient_id, stats)
        except TimeoutError:
            logger.error("Metadata extraction timed out after %ds", metadata_timeout)
        except Exception:
            logger.exception("Metadata extraction failed")
        finally:
            from oncofiles.memory import reclaim_memory

            reclaim_memory("metadata_extraction")

    async def _run_enhancement_sweep():
        """Daily sweep: retry AI enhancement for docs stuck with ai_processed_at IS NULL."""
        try:
            patients = await db.list_patients(active_only=True)
            for p in patients:
                gc = await _get_patient_gdrive(p.patient_id)
                if not gc:
                    continue
                p_gdrive, _ = gc
                stats = await asyncio.wait_for(
                    _enhance_docs(db, files, p_gdrive, patient_id=p.patient_id),
                    timeout=metadata_timeout,
                )
                if stats["processed"] > 0:
                    logger.info("Enhancement sweep [%s]: %s", p.patient_id, stats)
        except TimeoutError:
            logger.error("Enhancement sweep timed out after %ds", metadata_timeout)
        except Exception:
            logger.exception("Enhancement sweep failed")
        finally:
            from oncofiles.memory import reclaim_memory

            reclaim_memory("enhancement_sweep")

    housekeeping_timeout = 120  # 2 minutes max for lightweight housekeeping jobs

    async def _run_trash_cleanup():
        try:
            patients = await db.list_patients(active_only=True)
            total_purged = 0
            for p in patients:
                purged = await asyncio.wait_for(
                    db.purge_expired_trash(days=30, patient_id=p.patient_id),
                    timeout=housekeeping_timeout,
                )
                total_purged += purged
            if total_purged:
                logger.info("Trash cleanup: purged %d expired documents", total_purged)
        except TimeoutError:
            logger.error("Trash cleanup timed out after %ds", housekeeping_timeout)
        except Exception:
            logger.exception("Trash cleanup failed")

    async def _run_pipeline_integrity_check():
        """Scheduled check: find and fix any docs stuck in incomplete pipeline state."""
        try:
            patients = await db.list_patients(active_only=True)
            for p in patients:
                pid = p.patient_id
                all_docs = await db.list_documents(limit=500, patient_id=pid)
                ocr_ids = await db.get_ocr_document_ids()
                gaps = []

                for doc in all_docs:
                    doc_gaps = []
                    if doc.id not in ocr_ids:
                        doc_gaps.append("no_ocr")
                    if not doc.ai_summary:
                        doc_gaps.append("no_ai")
                    if not doc.structured_metadata:
                        doc_gaps.append("no_metadata")
                    if not doc.document_date:
                        doc_gaps.append("no_date")
                    if not doc.gdrive_id:
                        doc_gaps.append("no_gdrive")
                    if doc_gaps:
                        gaps.append((doc, doc_gaps))

                if gaps:
                    logger.warning(
                        "Pipeline integrity [%s]: %d docs with gaps: %s",
                        pid,
                        len(gaps),
                        ", ".join(f"#{d.id}({'+'.join(g)})" for d, g in gaps[:10]),
                    )
                    no_ai_docs = [d for d, g in gaps if "no_ai" in g]
                    no_metadata_docs = [
                        d for d, g in gaps if "no_metadata" in g and "no_ai" not in g
                    ]
                    gc = (
                        await _get_patient_gdrive(pid) if (no_ai_docs or no_metadata_docs) else None
                    )
                    if gc:
                        if no_ai_docs:
                            try:
                                eh_stats = await asyncio.wait_for(
                                    _enhance_docs(
                                        db,
                                        files,
                                        gc[0],
                                        document_ids=[d.id for d in no_ai_docs],
                                        patient_id=pid,
                                    ),
                                    timeout=metadata_timeout,
                                )
                                if eh_stats["processed"] > 0:
                                    logger.info(
                                        "Pipeline integrity enhance [%s]: %s", pid, eh_stats
                                    )
                            except Exception:
                                logger.warning(
                                    "Pipeline integrity enhance [%s] failed",
                                    pid,
                                    exc_info=True,
                                )
                        if no_metadata_docs:
                            try:
                                e_stats = await asyncio.wait_for(
                                    extract_all_metadata(db, files, gc[0], patient_id=pid),
                                    timeout=metadata_timeout,
                                )
                                if e_stats["processed"] > 0:
                                    logger.info(
                                        "Pipeline integrity metadata [%s]: %s", pid, e_stats
                                    )
                            except Exception:
                                logger.warning(
                                    "Pipeline integrity metadata [%s] failed",
                                    pid,
                                    exc_info=True,
                                )

                    # Auto-trash unrecoverable docs: Files API returns 400/404 AND no GDrive backup
                    for doc, g in gaps:
                        if "no_ocr" in g and "no_ai" in g and not doc.gdrive_id:
                            try:
                                files.download(doc.file_id)
                            except Exception as dl_err:
                                err_msg = str(dl_err).lower()
                                if "not downloadable" in err_msg or "404" in err_msg:
                                    logger.warning(
                                        "Pipeline integrity [%s]: trashing doc #%d (%s) "
                                        "— content gone, no GDrive backup",
                                        pid,
                                        doc.id,
                                        doc.filename,
                                    )
                                    await db.delete_document(doc.id)
                                else:
                                    logger.info(
                                        "Pipeline integrity [%s]: doc #%d download check failed "
                                        "(transient?) — NOT deleting: %s",
                                        pid,
                                        doc.id,
                                        dl_err,
                                    )
                else:
                    logger.info("Pipeline integrity [%s]: all %d docs complete", pid, len(all_docs))
        except Exception:
            logger.exception("Pipeline integrity check failed")

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
        except TimeoutError:
            logger.error("OAuth token cleanup timed out")
        except Exception:
            logger.exception("OAuth token cleanup failed")

    async def _run_prompt_log_cleanup():
        """Remove prompt log entries older than 90 days to control DB size."""
        try:
            async with db.db.execute(
                "DELETE FROM prompt_log WHERE created_at < datetime('now', '-90 days')"
            ) as cursor:
                deleted = cursor.rowcount
            await db.db.commit()
            if deleted:
                logger.info("Prompt log cleanup: removed %d entries older than 90 days", deleted)
        except TimeoutError:
            logger.error("Prompt log cleanup timed out")
        except Exception:
            logger.exception("Prompt log cleanup failed")

    # _log_rss removed — restart logic consolidated into periodic_memory_check().
    # The old _log_rss reclaimed before checking, masking the threshold crossing,
    # and sys.exit(0) was caught by APScheduler as a job error (#213).

    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_sync,
        IntervalTrigger(minutes=SYNC_INTERVAL_MINUTES),
        id="gdrive_sync",
        max_instances=1,
    )
    scheduler.add_job(
        _run_enhancement_sweep,
        CronTrigger(hour=3, minute=20),  # daily at 3:20 AM — retry stuck unprocessed docs
        id="enhancement_sweep",
        max_instances=1,
    )
    scheduler.add_job(
        _run_metadata_extraction,
        CronTrigger(hour=3, minute=30),  # daily at 3:30 AM (after enhancement sweep)
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
        _run_prompt_log_cleanup,
        CronTrigger(hour=4, minute=15),  # daily at 4:15 AM (after OAuth cleanup)
        id="prompt_log_cleanup",
        max_instances=1,
    )
    scheduler.add_job(
        _run_pipeline_integrity_check,
        CronTrigger(hour="*/6", minute=30),  # every 6 hours at :30
        id="pipeline_integrity",
        max_instances=1,
    )

    # ── DB keepalive / replica sync ──

    async def _db_keepalive():
        """Ping Turso every 4 min to keep connection alive, or sync embedded replica."""
        try:
            if db.is_replica:
                await db.sync_replica()
            else:
                await db.reconnect_if_stale(timeout=10.0)
        except Exception:
            logger.warning("DB keepalive/sync failed — will recover on next query")

    scheduler.add_job(
        _db_keepalive,
        IntervalTrigger(minutes=4),
        id="db_keepalive",
        max_instances=1,
    )

    async def _run_category_validation():
        """Auto-correct document categories after metadata extraction."""

        async def _do_category_validation():
            from oncofiles.models import DocumentCategory as _DocCat  # noqa: N814
            from oncofiles.tools.hygiene import _DOCTYPE_TO_CATEGORY

            patients = await db.list_patients(active_only=True)
            all_docs = []
            for p in patients:
                all_docs.extend(await db.list_documents(limit=500, patient_id=p.patient_id))
            docs = all_docs
            import json as _json

            corrected = 0

            # Phase 0: Remap deprecated categories (#140)
            for doc in docs:
                if doc.category.value == "surgical_report":
                    await db.update_document_category(doc.id, "surgery")
                    corrected += 1
                    logger.info("Category remap: %s surgical_report → surgery", doc.filename)

            # Phase 1: AI metadata-based category correction
            for doc in docs:
                if not doc.structured_metadata or doc.category.value in ("advocate", "reference"):
                    continue
                try:
                    meta = _json.loads(doc.structured_metadata)
                except (ValueError, TypeError):
                    logger.info(
                        "Category validation: unparseable metadata for doc %d (%s)",
                        doc.id,
                        doc.filename,
                    )
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
            # Also auto-detect reference materials in "other" category
            ref_keywords = (
                "devita",
                "nccn",
                "modra_kniha",
                "modrakniha",
                "esmo",
                "guideline",
            )
            for doc in docs:
                if doc.category.value != "other":
                    continue
                fn = doc.filename.lower()
                combined = fn + " " + (doc.ai_summary or "").lower()
                # Match by keywords OR by VitalSource institution (DeVita page splits)
                is_reference = any(kw in combined for kw in ref_keywords) or (
                    doc.institution == "VitalSource"
                )
                if is_reference:
                    await db.update_document_category(doc.id, "reference")
                    corrected += 1
                    logger.info("Reference auto-detected: %s other → reference", doc.filename)

            # Phase 3: Auto-detect advocate files in "other" category (#141)
            advocate_keywords = ("advokat", "advocate", "pacientadvokat", "patient_advocate")
            for doc in docs:
                if doc.category.value != "other":
                    continue
                combined = doc.filename.lower() + " " + (doc.ai_summary or "").lower()
                if any(kw in combined for kw in advocate_keywords):
                    await db.update_document_category(doc.id, "advocate")
                    corrected += 1
                    logger.info("Advocate auto-detected: %s other → advocate", doc.filename)

            # Phase 4: Dedup scan — soft-delete duplicate files (#144)
            deduped = 0
            from collections import defaultdict

            groups: dict[tuple, list] = defaultdict(list)
            for doc in docs:
                if doc.deleted_at:
                    continue
                key = (doc.category.value, str(doc.document_date) if doc.document_date else "none")
                groups[key].append(doc)

            for _key, group_docs in groups.items():
                if len(group_docs) < 2:
                    continue
                # Within group, find docs with matching gdrive_md5
                by_md5: dict[str, list] = defaultdict(list)
                for doc in group_docs:
                    if doc.gdrive_md5:
                        by_md5[doc.gdrive_md5].append(doc)
                for md5, dups in by_md5.items():
                    if len(dups) < 2:
                        continue
                    # Keep oldest (lowest id), soft-delete rest
                    dups.sort(key=lambda d: d.id)
                    for dup in dups[1:]:
                        await db.delete_document(dup.id)
                        deduped += 1
                        logger.info(
                            "Dedup: soft-deleted #%d (%s) — duplicate of #%d (md5=%s)",
                            dup.id,
                            dup.filename,
                            dups[0].id,
                            md5[:12],
                        )

            if deduped:
                logger.info("Dedup scan: soft-deleted %d duplicates", deduped)

            # Phase 5: Flag undated docs for date extraction (#143)
            undated = 0
            for doc in docs:
                if doc.document_date or doc.deleted_at:
                    continue
                # Try to extract date from filename (YYYYMMDD prefix)
                import re as _re

                date_match = _re.match(r"(\d{8})", doc.filename)
                if date_match:
                    try:
                        from datetime import date as _date

                        extracted = _date(
                            int(date_match.group(1)[:4]),
                            int(date_match.group(1)[4:6]),
                            int(date_match.group(1)[6:8]),
                        )
                        await db.db.execute(
                            "UPDATE documents SET document_date = ?, "
                            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
                            "WHERE id = ?",
                            (extracted.isoformat(), doc.id),
                        )
                        await db.db.commit()
                        undated += 1
                        logger.info("Date extracted: %s → %s", doc.filename, extracted.isoformat())
                    except (ValueError, IndexError):
                        pass
            if undated:
                logger.info("Undated fix: extracted dates for %d documents", undated)

            if corrected or deduped or undated:
                logger.info(
                    "Category validation total: %d corrected, %d deduped, %d dates extracted",
                    corrected,
                    deduped,
                    undated,
                )

        try:
            await asyncio.wait_for(_do_category_validation(), timeout=metadata_timeout)
        except TimeoutError:
            logger.error("Category validation timed out after %ds", metadata_timeout)
        except Exception:
            logger.exception("Category validation failed")

    scheduler.add_job(
        _run_category_validation,
        CronTrigger(hour=3, minute=45),  # daily at 3:45 AM (after metadata extraction)
        id="category_validation",
        max_instances=1,
    )

    folder_cleanup_timeout = 300  # 5 minutes max for GDrive folder operations

    async def _run_empty_folder_cleanup():
        """Remove empty year-month subfolders and merge duplicates from GDrive."""

        async def _do_folder_cleanup():
            patients = await db.list_patients(active_only=True)
            for p in patients:
                gc = await _get_patient_gdrive(p.patient_id)
                if not gc:
                    continue
                p_gdrive, folder_id = gc
                await _cleanup_folders_for(p_gdrive, folder_id, p.patient_id)

        async def _cleanup_folders_for(p_gdrive, folder_id, pid):
            cleaned = 0
            merged = 0
            category_merged = 0

            # Get category folder structure
            _, folder_map = await asyncio.to_thread(
                p_gdrive.list_folder_with_structure,
                folder_id,
            )

            # Phase 0: Merge legacy category folders (e.g., surgical_report → surgery)
            from oncofiles.gdrive_folders import (
                CATEGORY_FOLDERS,
                CATEGORY_MERGES,
                METADATA_FOLDERS,
                bilingual_name,
                en_key_from_folder_name,
            )

            for legacy_cat, target_cat in CATEGORY_MERGES.items():
                # Find legacy folder by name
                legacy_folder_id = None
                target_folder_id = None
                for fid, fname in folder_map.items():
                    en_key = en_key_from_folder_name(fname) or fname
                    if en_key == legacy_cat:
                        legacy_folder_id = fid
                    if en_key == target_cat:
                        target_folder_id = fid
                if not legacy_folder_id or not target_folder_id:
                    continue
                # Move all files from legacy → target
                try:
                    legacy_contents = await asyncio.to_thread(
                        lambda lid=legacy_folder_id: (
                            p_gdrive._service.files()
                            .list(
                                q=f"'{lid}' in parents and trashed = false",
                                fields="files(id, name)",
                                pageSize=200,
                            )
                            .execute()
                        )
                    )
                    files_to_move = legacy_contents.get("files", [])
                    for f in files_to_move:
                        await asyncio.to_thread(
                            p_gdrive.move_file,
                            f["id"],
                            target_folder_id,
                        )
                    # Trash the legacy folder if now empty
                    if files_to_move or not legacy_contents.get("files"):
                        await asyncio.to_thread(
                            p_gdrive.trash_file,
                            legacy_folder_id,
                        )
                    category_merged += 1
                    logger.info(
                        "Merged legacy folder '%s' → '%s' (%d files moved) [%s]",
                        bilingual_name(legacy_cat),
                        bilingual_name(target_cat),
                        len(files_to_move),
                        pid,
                    )
                except Exception:
                    logger.warning(
                        "Failed to merge legacy folder %s → %s",
                        legacy_cat,
                        target_cat,
                        exc_info=True,
                    )

            # Phase 0c: Move root-level files into 'other' category folder
            other_folder_id = None
            for fid, fname in folder_map.items():
                if en_key_from_folder_name(fname) == "other":
                    other_folder_id = fid
                    break

            if other_folder_id:
                root_items_raw = await asyncio.to_thread(
                    lambda: (
                        p_gdrive._service.files()
                        .list(
                            q=(
                                f"'{folder_id}' in parents"
                                " and mimeType != 'application/vnd.google-apps.folder'"
                                " and trashed = false"
                            ),
                            fields="files(id, name, createdTime)",
                            pageSize=50,
                        )
                        .execute()
                    )
                )
                for rf in root_items_raw.get("files", []):
                    if rf["name"] == "_manifest.json":
                        continue  # Skip known metadata files
                    try:
                        await asyncio.to_thread(p_gdrive.move_file, rf["id"], other_folder_id)
                        cleaned += 1
                        logger.info(
                            "Moved root file '%s' → other/ [%s]",
                            rf["name"],
                            pid,
                        )
                    except Exception:
                        logger.warning("Failed to move root file '%s'", rf["name"], exc_info=True)

            # Phase 0d: Re-home files from stray root-level folders (#277)
            # Stray folders are direct children of root that aren't known categories
            # (e.g., year folders "2025-03" created by pre-fix code).
            root_folders_raw = await asyncio.to_thread(
                lambda: (
                    p_gdrive._service.files()
                    .list(
                        q=(
                            f"'{folder_id}' in parents"
                            " and mimeType = 'application/vnd.google-apps.folder'"
                            " and trashed = false"
                        ),
                        fields="files(id, name)",
                        pageSize=100,
                    )
                    .execute()
                )
            )
            # Build set of known category/metadata folder IDs
            known_cat_ids: dict[str, str] = {}  # {folder_id: en_key}
            for rf in root_folders_raw.get("files", []):
                en = en_key_from_folder_name(rf["name"])
                if en:
                    known_cat_ids[rf["id"]] = rf["name"]

            if other_folder_id:
                for rf in root_folders_raw.get("files", []):
                    if rf["id"] in known_cat_ids:
                        continue  # Known category folder — skip
                    # Stray folder — move its files to 'other', then trash if empty
                    try:
                        stray_contents = await asyncio.to_thread(
                            lambda sid=rf["id"]: (
                                p_gdrive._service.files()
                                .list(
                                    q=f"'{sid}' in parents and trashed = false",
                                    fields="files(id, name)",
                                    pageSize=200,
                                )
                                .execute()
                            )
                        )
                        stray_files = stray_contents.get("files", [])
                        for sf in stray_files:
                            await asyncio.to_thread(p_gdrive.move_file, sf["id"], other_folder_id)
                            logger.info(
                                "Moved '%s' from stray folder '%s' → other/ [%s]",
                                sf["name"],
                                rf["name"],
                                pid,
                            )
                            cleaned += 1
                        # Trash the stray folder (now empty or was already empty)
                        await asyncio.to_thread(p_gdrive.trash_file, rf["id"])
                        cleaned += 1
                        logger.info(
                            "Trashed stray root folder '%s' [%s]",
                            rf["name"],
                            pid,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to clean stray folder '%s' [%s]",
                            rf["name"],
                            pid,
                            exc_info=True,
                        )

            # For each category folder, check subfolders
            # Only iterate known category folders (direct children of root),
            # NOT the full recursive folder_map which includes year-month subfolders (#277)
            for cat_folder_id, cat_name in known_cat_ids.items():
                try:
                    sub_folders_raw = await asyncio.to_thread(
                        lambda fid=cat_folder_id: (
                            p_gdrive._service.files()
                            .list(
                                q=(
                                    f"'{fid}' in parents"
                                    f" and mimeType = 'application/vnd.google-apps.folder'"
                                    f" and trashed = false"
                                ),
                                fields="files(id, name)",
                                pageSize=100,
                            )
                            .execute()
                        )
                    )
                    sub_folders = sub_folders_raw.get("files", [])

                    # Check for duplicates (same name)
                    by_name: dict[str, list[str]] = {}
                    for sf in sub_folders:
                        by_name.setdefault(sf["name"], []).append(sf["id"])

                    for name, ids in by_name.items():
                        if len(ids) > 1:
                            # Merge: move files from duplicates → keep, then trash
                            keep = ids[0]
                            for dup in ids[1:]:
                                # List ALL files in the duplicate folder
                                dup_contents = await asyncio.to_thread(
                                    lambda did=dup: (
                                        p_gdrive._service.files()
                                        .list(
                                            q=f"'{did}' in parents and trashed = false",
                                            fields="files(id, name)",
                                            pageSize=200,
                                        )
                                        .execute()
                                    )
                                )
                                dup_files = dup_contents.get("files", [])
                                # Move files from duplicate to keep folder
                                for df in dup_files:
                                    try:
                                        await asyncio.to_thread(p_gdrive.move_file, df["id"], keep)
                                        logger.info(
                                            "Moved '%s' from dup %s → %s", df["name"], dup, keep
                                        )
                                    except Exception:
                                        logger.warning(
                                            "Failed to move '%s' from dup folder",
                                            df["name"],
                                            exc_info=True,
                                        )
                                # Trash the now-empty duplicate
                                await asyncio.to_thread(p_gdrive.trash_file, dup)
                                merged += 1
                                logger.info(
                                    "Merged duplicate folder '%s/%s' (%s → %s, %d files moved)",
                                    cat_name,
                                    name,
                                    dup,
                                    keep,
                                    len(dup_files),
                                )

                    # Check for empty subfolders (#172)
                    for sf in sub_folders:
                        contents = await asyncio.to_thread(
                            lambda sid=sf["id"]: (
                                p_gdrive._service.files()
                                .list(
                                    q=f"'{sid}' in parents and trashed = false",
                                    fields="files(id)",
                                    pageSize=1,
                                )
                                .execute()
                            )
                        )
                        if not contents.get("files"):
                            await asyncio.to_thread(p_gdrive.trash_file, sf["id"])
                            cleaned += 1
                            logger.info("Trashed empty folder '%s/%s'", cat_name, sf["name"])

                    # Move root-level files into subfolders (#174, #175)
                    root_files_raw = await asyncio.to_thread(
                        lambda fid=cat_folder_id: (
                            p_gdrive._service.files()
                            .list(
                                q=(
                                    f"'{fid}' in parents"
                                    " and mimeType != "
                                    "'application/vnd.google-apps.folder'"
                                    " and trashed = false"
                                ),
                                fields="files(id, name, createdTime)",
                                pageSize=50,
                            )
                            .execute()
                        )
                    )
                    root_files = root_files_raw.get("files", [])
                    en_cat = en_key_from_folder_name(cat_name) or cat_name
                    for rf in root_files:
                        name = rf["name"]
                        target_subfolder = None

                        # Reference category: organize by source (#175)
                        if en_cat == "reference":
                            nl = name.lower()
                            if "devita" in nl:
                                target_subfolder = "DeVita-12e"
                            elif "nccn" in nl:
                                target_subfolder = "NCCN"
                            elif "modra" in nl or "modrakniha" in nl:
                                target_subfolder = "ModraKniha"
                        # Default: year-month subfolder
                        if not target_subfolder:
                            if len(name) >= 8 and name[:8].isdigit():
                                target_subfolder = f"{name[:4]}-{name[4:6]}"
                            elif rf.get("createdTime"):
                                target_subfolder = rf["createdTime"][:7]

                        if target_subfolder:
                            # Find or create subfolder
                            sf_id = None
                            for sf in sub_folders:
                                if sf["name"] == target_subfolder:
                                    sf_id = sf["id"]
                                    break
                            if not sf_id:
                                sf_id = await asyncio.to_thread(
                                    p_gdrive.create_folder,
                                    target_subfolder,
                                    cat_folder_id,
                                )
                            await asyncio.to_thread(
                                p_gdrive.move_file,
                                rf["id"],
                                sf_id,
                            )
                            cleaned += 1
                            logger.info(
                                "Moved root file '%s' → '%s/%s/'",
                                name,
                                cat_name,
                                target_subfolder,
                            )

                except Exception:
                    logger.warning(
                        "Folder cleanup failed for %s",
                        cat_name,
                        exc_info=True,
                    )

            # Check for empty category folders (#172)
            # Refresh folder map after cleanup
            _, folder_map_2 = await asyncio.to_thread(
                p_gdrive.list_folder_with_structure,
                folder_id,
            )
            active_cats = set(CATEGORY_FOLDERS + METADATA_FOLDERS)
            for cfid, cname in folder_map_2.items():
                en = en_key_from_folder_name(cname) or cname
                if en in active_cats:
                    continue  # Don't trash active category folders
                # Check if empty
                cat_contents = await asyncio.to_thread(
                    lambda fid=cfid: (
                        p_gdrive._service.files()
                        .list(
                            q=f"'{fid}' in parents and trashed = false",
                            fields="files(id)",
                            pageSize=1,
                        )
                        .execute()
                    )
                )
                if not cat_contents.get("files"):
                    await asyncio.to_thread(p_gdrive.trash_file, cfid)
                    cleaned += 1
                    logger.info(
                        "Trashed empty category folder '%s' [%s]",
                        cname,
                        pid,
                    )

            if cleaned or merged or category_merged:
                logger.info(
                    "Folder cleanup [%s]: %d empty trashed, "
                    "%d duplicates merged, %d legacy categories merged",
                    pid,
                    cleaned,
                    merged,
                    category_merged,
                )
            else:
                logger.info("Folder cleanup [%s]: all folders OK", pid)

        try:
            await asyncio.wait_for(_do_folder_cleanup(), timeout=folder_cleanup_timeout)
        except TimeoutError:
            logger.error("Folder cleanup timed out after %ds", folder_cleanup_timeout)
        except Exception:
            logger.exception("Empty folder cleanup failed")

    scheduler.add_job(
        _run_empty_folder_cleanup,
        CronTrigger(hour=3, minute=15),  # daily at 3:15 AM (between trash and metadata)
        id="empty_folder_cleanup",
        max_instances=1,
    )

    # ── Weekly analytics aggregation ────────────────────────────────────
    async def _run_weekly_analytics():
        """Aggregate weekly usage stats and persist to agent_state."""
        import json

        try:
            # Sequential — Turso single-connection can't handle concurrent queries
            prompt_stats = await db.get_prompt_stats(days=7)
            tool_stats = await db.get_tool_usage_stats(days=7)
            pipeline_stats = await db.get_pipeline_stats()
            summary = {
                "week_ending": datetime.now(UTC).strftime("%Y-%m-%d"),
                "prompts": {
                    "total_calls": prompt_stats.total_calls,
                    "total_input_tokens": prompt_stats.total_input_tokens,
                    "total_output_tokens": prompt_stats.total_output_tokens,
                    "error_rate": prompt_stats.error_rate,
                },
                "tools": {
                    "unique_tools": tool_stats.unique_tools_used,
                    "total_calls": tool_stats.total_tool_calls,
                    "top_3": [
                        {"name": t.tool_name, "calls": t.call_count}
                        for t in tool_stats.top_tools[:3]
                    ],
                },
                "pipeline": {
                    "total_syncs": pipeline_stats.total_syncs,
                    "success_rate": (
                        round(pipeline_stats.successful_syncs / pipeline_stats.total_syncs, 2)
                        if pipeline_stats.total_syncs > 0
                        else 0
                    ),
                    "docs_imported": pipeline_stats.documents_imported,
                },
            }
            from oncofiles.models import AgentState

            await db.set_agent_state(
                AgentState(
                    agent_id="oncofiles",
                    key="analytics_weekly_summary",
                    value=json.dumps(summary),
                )
            )
            logger.info("Weekly analytics: %s", json.dumps(summary, indent=None)[:200])
        except Exception:
            logger.exception("Weekly analytics aggregation failed")

    scheduler.add_job(
        _run_weekly_analytics,
        CronTrigger(day_of_week="sun", hour=5, minute=0),  # Sunday 5:00 AM
        id="weekly_analytics",
        max_instances=1,
    )

    # Startup: full sync + enhance + category validation 60s after boot
    from apscheduler.triggers.date import DateTrigger

    async def _startup_catchup():
        """Lightweight startup: import-only sync + category validation for all patients.

        Skips the heavy export phase (rename, organize, OCR export) to avoid
        blocking the event loop and causing health check timeouts. The regular
        5-min scheduled sync handles exports.

        """
        import time as _time

        from oncofiles.memory import db_slot
        from oncofiles.sync import sync_from_gdrive as _sync_from

        patients = await db.list_patients(active_only=True)
        for p in patients:
            pid = p.patient_id
            gc = await _get_patient_gdrive(pid)
            if not gc:
                continue
            p_gdrive, folder_id = gc

            sync_id = None
            start = _time.monotonic()
            try:
                async with db_slot("startup_insert_sync", priority=False):
                    sync_id = await db.insert_sync_history(trigger="startup", patient_id=pid)
            except Exception:
                logger.warning("startup: failed to record sync history", exc_info=True)

            try:
                import_stats = await asyncio.wait_for(
                    _sync_from(db, files, p_gdrive, folder_id, enhance=True, patient_id=pid),
                    timeout=180,  # 3 min max for import
                )
                logger.info("Startup import [%s]: %s", pid, import_stats)
                if sync_id:
                    try:
                        async with db_slot("startup_complete_sync", priority=False):
                            await db.complete_sync_history(
                                sync_id,
                                status="completed",
                                duration_s=round(_time.monotonic() - start, 1),
                                from_new=import_stats.get("new", 0),
                                from_updated=import_stats.get("updated", 0),
                                from_errors=import_stats.get("errors", 0),
                                stats_json=str(import_stats),
                            )
                    except Exception:
                        logger.warning("startup: failed to record sync completion", exc_info=True)
            except TimeoutError:
                logger.warning("Startup import [%s] timed out after 180s", pid)
                if sync_id:
                    try:
                        async with db_slot("startup_complete_sync_timeout", priority=False):
                            await db.complete_sync_history(
                                sync_id,
                                status="failed",
                                duration_s=round(_time.monotonic() - start, 1),
                                error_message="Startup import timed out after 180s",
                            )
                    except Exception:
                        logger.warning("startup: failed to record sync timeout", exc_info=True)
            except Exception as exc:
                logger.exception("Startup import [%s] failed", pid)
                if sync_id:
                    try:
                        async with db_slot("startup_complete_sync_fail", priority=False):
                            await db.complete_sync_history(
                                sync_id,
                                status="failed",
                                duration_s=round(_time.monotonic() - start, 1),
                                error_message=str(exc)[:500],
                            )
                    except Exception:
                        logger.warning("startup: failed to record sync failure", exc_info=True)

        await _run_category_validation()
        logger.info("Startup catchup complete: import + validate (RSS: %.1f MB)", get_rss_mb())

    startup_time = datetime.now() + timedelta(seconds=90)
    scheduler.add_job(
        _startup_catchup,
        DateTrigger(run_date=startup_time),
        id="startup_catchup",
        max_instances=1,
    )
    logger.info("Startup catchup scheduled for %s", startup_time.strftime("%H:%M:%S"))

    # ── Gmail sync jobs ───────────────────────────────────────────────────
    gmail_sync_timeout = 300  # 5 minutes max

    async def _run_gmail_sync(trigger: str = "scheduled", *, initial: bool = False):
        if is_memory_pressure("Gmail sync"):
            return

        from oncofiles.gmail_sync import gmail_sync as _gmail_sync

        patients = await db.list_patients(active_only=True)
        for p in patients:
            pid = p.patient_id
            p_gmail = await _get_patient_gmail(pid)
            if not p_gmail:
                continue

            async with _sync_semaphore:
                try:
                    stats = await asyncio.wait_for(
                        _gmail_sync(db, files, p_gmail, initial=initial, patient_id=pid),
                        timeout=gmail_sync_timeout,
                    )
                    if not stats.get("skipped"):
                        logger.info("Gmail sync [%s] (%s): %s", pid, trigger, stats)
                    _record_service_sync(pid, "gmail")
                except TimeoutError:
                    logger.error("Gmail sync [%s] timed out after %ds", pid, gmail_sync_timeout)
                except Exception:
                    logger.exception("Gmail sync [%s] failed", pid)
                finally:
                    from oncofiles.memory import reclaim_memory

                    reclaim_memory(f"gmail_sync:{pid}")

    scheduler.add_job(
        _run_gmail_sync,
        IntervalTrigger(
            minutes=SYNC_INTERVAL_MINUTES, start_date=datetime.now() + timedelta(minutes=2)
        ),
        id="gmail_sync",
        max_instances=1,
    )
    gmail_startup_time = datetime.now() + timedelta(seconds=150)
    scheduler.add_job(
        lambda: _run_gmail_sync("startup", initial=True),
        DateTrigger(run_date=gmail_startup_time),
        id="gmail_startup_sync",
        max_instances=1,
    )
    logger.info(
        "Gmail sync scheduled — every %d min (offset +2m), startup at %s",
        SYNC_INTERVAL_MINUTES,
        gmail_startup_time.strftime("%H:%M:%S"),
    )

    # ── Calendar sync jobs ─────────────────────────────────────────────────
    calendar_sync_timeout = 300  # 5 minutes max

    async def _run_calendar_sync(trigger: str = "scheduled", *, initial: bool = False):
        if is_memory_pressure("Calendar sync"):
            return

        from oncofiles.calendar_sync import calendar_sync as _calendar_sync

        patients = await db.list_patients(active_only=True)
        for p in patients:
            pid = p.patient_id
            p_calendar = await _get_patient_calendar(pid)
            if not p_calendar:
                continue

            # Per-patient skip-if-unchanged optimization
            last_cal = _last_calendar_sync_times.get(pid)
            if last_cal and trigger == "scheduled":
                try:
                    has_changes = await asyncio.to_thread(p_calendar.has_changes_since, last_cal)
                    if not has_changes:
                        logger.debug(
                            "Calendar sync [%s] skipped — no changes since %s", pid, last_cal
                        )
                        continue
                except Exception:
                    pass  # On error, proceed with full sync

            async with _sync_semaphore:
                try:
                    stats = await asyncio.wait_for(
                        _calendar_sync(db, p_calendar, initial=initial, patient_id=pid),
                        timeout=calendar_sync_timeout,
                    )
                    if not stats.get("skipped"):
                        logger.info("Calendar sync [%s] (%s): %s", pid, trigger, stats)
                    _last_calendar_sync_times[pid] = datetime.now(UTC).isoformat()
                    _record_service_sync(pid, "calendar")
                except TimeoutError:
                    logger.error(
                        "Calendar sync [%s] timed out after %ds", pid, calendar_sync_timeout
                    )
                except Exception:
                    logger.exception("Calendar sync [%s] failed", pid)
                finally:
                    from oncofiles.memory import reclaim_memory

                    reclaim_memory(f"calendar_sync:{pid}")

    scheduler.add_job(
        _run_calendar_sync,
        IntervalTrigger(
            minutes=SYNC_INTERVAL_MINUTES, start_date=datetime.now() + timedelta(minutes=3)
        ),
        id="calendar_sync",
        max_instances=1,
    )
    calendar_startup_time = datetime.now() + timedelta(seconds=210)
    scheduler.add_job(
        lambda: _run_calendar_sync("startup", initial=True),
        DateTrigger(run_date=calendar_startup_time),
        id="calendar_startup_sync",
        max_instances=1,
    )
    logger.info(
        "Calendar sync scheduled — every %d min (offset +3m), startup at %s",
        SYNC_INTERVAL_MINUTES,
        calendar_startup_time.strftime("%H:%M:%S"),
    )

    # ── Job tracking for /health endpoint ────────────────────────────────
    job_tracker: dict[str, dict] = {}

    def _job_started(event):
        job_tracker[event.job_id] = {
            **job_tracker.get(event.job_id, {}),
            "started_at": datetime.now(UTC).isoformat(),
            "running": True,
        }

    def _job_executed(event):
        entry = job_tracker.get(event.job_id, {})
        entry.update(
            last_ok=datetime.now(UTC).isoformat(),
            running=False,
            last_error=None,
        )
        # Calculate duration if we have start time
        started = entry.get("started_at")
        if started:
            start_dt = datetime.fromisoformat(started)
            entry["last_duration_s"] = round((datetime.now(UTC) - start_dt).total_seconds(), 1)
        job_tracker[event.job_id] = entry
        logger.info("Scheduler job completed: %s", event.job_id)

    def _job_error(event):
        entry = job_tracker.get(event.job_id, {})
        entry.update(
            last_error=datetime.now(UTC).isoformat(),
            last_error_msg=str(event.exception)[:200],
            running=False,
        )
        job_tracker[event.job_id] = entry
        logger.error("Scheduler job failed: %s — %s", event.job_id, event.exception)
        # If the memory monitoring job itself fails, check RSS directly as fallback.
        # A broken safety net is worse than no safety net — force restart if needed.
        if event.job_id == "memory_check":
            from oncofiles.memory import MEMORY_RESTART_THRESHOLD_MB

            rss = get_rss_mb()
            if rss > MEMORY_RESTART_THRESHOLD_MB:
                logger.critical(
                    "memory_check job errored AND RSS %.1f MB > %d MB — forcing restart",
                    rss,
                    MEMORY_RESTART_THRESHOLD_MB,
                )
                import signal

                os.kill(os.getpid(), signal.SIGTERM)

    def _job_missed(event):
        entry = job_tracker.get(event.job_id, {})
        entry["last_missed"] = datetime.now(UTC).isoformat()
        job_tracker[event.job_id] = entry
        logger.warning("Scheduler job missed (previous still running): %s", event.job_id)

    from apscheduler.events import (
        EVENT_JOB_ERROR,
        EVENT_JOB_EXECUTED,
        EVENT_JOB_MISSED,
        EVENT_JOB_SUBMITTED,
    )

    scheduler.add_listener(_job_started, EVENT_JOB_SUBMITTED)
    scheduler.add_listener(_job_executed, EVENT_JOB_EXECUTED)
    scheduler.add_listener(_job_error, EVENT_JOB_ERROR)
    scheduler.add_listener(_job_missed, EVENT_JOB_MISSED)

    # OF-1/OF-2: Periodic memory check — reclaim memory + graceful restart
    scheduler.add_job(
        periodic_memory_check,
        IntervalTrigger(minutes=5),
        id="memory_check",
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.start()
    logger.info("Sync scheduler started — every %d min", SYNC_INTERVAL_MINUTES)
    return scheduler, job_tracker


def _get_sync_folder_id_from(oauth_folder_id: str) -> str:
    """Resolve GDrive folder ID from config or OAuth."""
    return GOOGLE_DRIVE_FOLDER_ID or oauth_folder_id


# ── MCP server ────────────────────────────────────────────────────────────────

from oncofiles.audit_middleware import AuditMiddleware  # noqa: E402

_MCP_INSTRUCTIONS = """\
Medical document management for oncology and general health patient records.

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

PATIENT TYPES — the system supports two patient profiles:
- `oncology`: cancer patients with chemo protocols, biomarker tracking, pre-cycle checklists. \
Categories include pathology, genetics, chemo_sheet.
- `general`: healthy patients with preventive care screening, general health lab ranges. \
Categories include vaccination, dental, preventive. Use `get_preventive_care_status` to check \
which EU-recommended screenings are due/overdue.

Available categories: labs, report, imaging, pathology, genetics, \
surgery, surgical_report, prescription, referral, discharge, discharge_summary, chemo_sheet, \
reference, advocate, other, vaccination, dental, preventive.\
"""

mcp = FastMCP(
    "Oncofiles",
    instructions=_MCP_INSTRUCTIONS,
    lifespan=lifespan,
    auth=auth,
)
from oncofiles.patient_middleware import PatientResolutionMiddleware  # noqa: E402

mcp.add_middleware(AuditMiddleware())
mcp.add_middleware(PatientResolutionMiddleware())


# ── Landing page ─────────────────────────────────────────────────────────────

_LANDING_HTML: str | None = None


def _load_landing_html() -> str:
    global _LANDING_HTML  # noqa: PLW0603
    if _LANDING_HTML is None:
        from pathlib import Path

        html_path = Path(__file__).parent / "landing.html"
        _LANDING_HTML = html_path.read_text().replace("{{VERSION}}", VERSION)
    return _LANDING_HTML


@mcp.custom_route("/", methods=["GET"])
async def landing(request: Request) -> HTMLResponse:
    return HTMLResponse(_load_landing_html())


# ── Legal pages (required for Google OAuth verification) ─────────────────────


def _legal_page(title: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Oncofiles</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  max-width: 720px; margin: 2rem auto; padding: 0 1.5rem; line-height: 1.7; color: #1a1a1a; }}
h1 {{ font-size: 1.8rem; margin-bottom: 0.5rem; }}
h2 {{ font-size: 1.3rem; margin-top: 2rem; }}
.meta {{ color: #666; font-size: 0.9rem; margin-bottom: 2rem; }}
a {{ color: #2563eb; }}
footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #e5e5e5;
  font-size: 0.85rem; color: #666; }}
</style></head><body>
<h1>{title}</h1>
{body_html}
<footer><a href="/">← Oncofiles</a> · <a href="mailto:peter.fusek@instarea.sk">Contact</a>
 · Built by <a href="https://www.instarea.com">Instarea</a></footer>
</body></html>"""


@mcp.custom_route("/oncoteam", methods=["GET"])
async def oncoteam_page(request: Request) -> HTMLResponse:
    return HTMLResponse(
        _legal_page(
            "Oncoteam",
            """
<p class="meta">Sister project of Oncofiles</p>

<p><strong>Oncoteam</strong> is an AI agent that analyzes your
Oncofiles data. While Oncofiles organizes your medical records,
Oncoteam helps you understand them:</p>

<ul>
<li>Tracks lab value trends (CBC, tumor markers, liver enzymes)</li>
<li>Calculates clinical indices (SII, Ne/Ly ratio)</li>
<li>Searches PubMed and ClinicalTrials.gov for relevant research</li>
<li>Prepares questions for your oncologist</li>
<li>Checks pre-cycle lab safety for chemo protocols</li>
</ul>

<p>Oncoteam works through Claude.ai as an MCP connector,
reading data from your Oncofiles repository.</p>

<h2>For developers</h2>
<p>Source code:
<a href="https://github.com/peter-fusek/oncoteam">
github.com/peter-fusek/oncoteam</a></p>
""",
        )
    )


_PRIVACY_HTML: str | None = None


@mcp.custom_route("/privacy", methods=["GET"])
async def privacy_policy(request: Request) -> HTMLResponse:
    global _PRIVACY_HTML  # noqa: PLW0603
    if _PRIVACY_HTML is None:
        _PRIVACY_HTML = _legal_page(
            "Privacy Policy",
            """
<p class="meta">Effective: March 25, 2026 · Last updated: March 25, 2026</p>

<p>Oncofiles is operated by <strong>Instarea s.r.o.</strong> ("we", "us").
This policy describes how we handle your data when you use Oncofiles
(<a href="https://oncofiles.com">oncofiles.com</a>).</p>

<h2>1. What Data We Access</h2>
<p>When you connect your Google account, Oncofiles accesses:</p>
<ul>
<li><strong>Google Drive</strong> — files in your designated medical documents folder only</li>
<li><strong>Gmail</strong> (read-only) — emails matching medical keywords
to detect appointments and results</li>
<li><strong>Google Calendar</strong> (read-only) — events to identify medical appointments</li>
</ul>
<p>We only access data you explicitly authorize through Google's OAuth consent screen.
You can revoke access at any time via
<a href="https://myaccount.google.com/permissions">Google Account Permissions</a>.</p>

<h2>2. How We Use Your Data</h2>
<p>Your data is used exclusively to:</p>
<ul>
<li>Organize and categorize your medical documents</li>
<li>Extract metadata (dates, institutions, categories) using AI</li>
<li>Make your records searchable through AI chat (Claude, ChatGPT)</li>
<li>Track lab values and treatment timelines</li>
</ul>
<p>We do <strong>not</strong> use your data for advertising, profiling, or any purpose
unrelated to your medical document management.</p>

<h2>3. Where Data Is Stored</h2>
<ul>
<li><strong>Document metadata</strong> — stored in a Turso database
(SQLite-compatible, hosted in EU)</li>
<li><strong>Original files</strong> — remain in your Google Drive;
we do not copy them to separate storage</li>
<li><strong>OCR text</strong> — stored as companion files in your Google Drive folder</li>
<li><strong>AI-generated metadata</strong> — stored in the database alongside document records</li>
</ul>

<h2>4. Data Sharing</h2>
<p>We do <strong>not</strong> sell, share, or transfer your personal data
to third parties, except:</p>
<ul>
<li><strong>AI providers</strong> — document content may be sent to Anthropic (Claude) for metadata
extraction. This is governed by Anthropic's data processing terms.</li>
<li><strong>Infrastructure</strong> — Railway (hosting), Turso (database). Both process data
under their respective privacy policies and data processing agreements.</li>
</ul>

<h2>5. Data Retention</h2>
<p>Your data is retained as long as your account is active. You can request deletion
of all stored data by contacting us. Original files in Google Drive are never deleted
by Oncofiles — only metadata in our database.</p>

<h2>6. Your Rights (GDPR)</h2>
<p>If you are in the EU/EEA, you have the right to:</p>
<ul>
<li>Access your personal data</li>
<li>Request correction or deletion</li>
<li>Data portability (export your data)</li>
<li>Withdraw consent at any time</li>
<li>Lodge a complaint with your supervisory authority</li>
</ul>

<h2>7. Google API Services Disclosure</h2>
<p>Oncofiles' use and transfer of information received from Google APIs adheres to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API
Services User Data Policy</a>, including the Limited Use requirements.</p>

<h2>8. Contact</h2>
<p>For privacy questions: <a href="mailto:peter.fusek@instarea.sk">peter.fusek@instarea.sk</a></p>
<p>Instarea s.r.o. · Bratislava, Slovakia</p>
""",
        )
    return HTMLResponse(_PRIVACY_HTML)


_TERMS_HTML: str | None = None


@mcp.custom_route("/terms", methods=["GET"])
async def terms_of_service(request: Request) -> HTMLResponse:
    global _TERMS_HTML  # noqa: PLW0603
    if _TERMS_HTML is None:
        _TERMS_HTML = _legal_page(
            "Terms of Service",
            """
<p class="meta">Effective: March 25, 2026 · Last updated: March 25, 2026</p>

<p>These terms govern your use of Oncofiles (<a href="https://oncofiles.com">oncofiles.com</a>),
operated by <strong>Instarea s.r.o.</strong> ("we", "us").</p>

<h2>1. Service Description</h2>
<p>Oncofiles is a medical document management tool that connects to your Google Drive,
Gmail, and Calendar to organize health records and make them accessible through AI chat.
It is designed for cancer patients and their caregivers.</p>

<h2>2. Not Medical Advice</h2>
<p><strong>Oncofiles is not a medical device and does not provide medical advice.</strong>
It organizes and presents your existing medical documents. All medical decisions should be
made with your healthcare providers. AI-generated summaries and analyses are for
informational purposes only.</p>

<h2>3. Your Data</h2>
<p>You retain full ownership of your medical documents and data. Oncofiles accesses your
Google services only with your explicit consent, which you can revoke at any time.
See our <a href="/privacy">Privacy Policy</a> for details.</p>

<h2>4. Account and Access</h2>
<ul>
<li>You must have a Google account to use Oncofiles</li>
<li>You are responsible for maintaining the security of your bearer tokens and credentials</li>
<li>Free tier: up to 200 documents per patient</li>
</ul>

<h2>5. Acceptable Use</h2>
<p>You agree not to:</p>
<ul>
<li>Use Oncofiles for purposes other than personal medical document management</li>
<li>Attempt to access other users' data</li>
<li>Reverse engineer or abuse the API</li>
<li>Use the service for commercial medical data processing without authorization</li>
</ul>

<h2>6. Availability</h2>
<p>We aim for high availability but do not guarantee uninterrupted service. Oncofiles
is provided "as is" without warranties of any kind. We are not liable for data loss
beyond what is stored in your Google Drive (which we do not modify or delete).</p>

<h2>7. Changes</h2>
<p>We may update these terms. Continued use after changes constitutes acceptance.
Material changes will be communicated via the dashboard or email.</p>

<h2>8. Contact</h2>
<p><a href="mailto:peter.fusek@instarea.sk">peter.fusek@instarea.sk</a></p>
<p>Instarea s.r.o. · Bratislava, Slovakia</p>
""",
        )
    return HTMLResponse(_TERMS_HTML)


# ── Static assets ─────────────────────────────────────────────────────────────

_FAVICON_SVG: str | None = None


@mcp.custom_route("/favicon.svg", methods=["GET"])
async def favicon_svg(request: Request) -> HTMLResponse:
    global _FAVICON_SVG  # noqa: PLW0603
    if _FAVICON_SVG is None:
        _FAVICON_SVG = (Path(__file__).parent / "favicon.svg").read_text()
    return HTMLResponse(_FAVICON_SVG, media_type="image/svg+xml")


_OG_IMAGE_SVG: str | None = None


@mcp.custom_route("/og-image.svg", methods=["GET"])
async def og_image_svg(request: Request) -> HTMLResponse:
    global _OG_IMAGE_SVG  # noqa: PLW0603
    if _OG_IMAGE_SVG is None:
        _OG_IMAGE_SVG = (Path(__file__).parent / "og-image.svg").read_text()
    return HTMLResponse(_OG_IMAGE_SVG, media_type="image/svg+xml")


@mcp.custom_route("/apple-touch-icon.png", methods=["GET"])
async def apple_touch_icon(request: Request) -> HTMLResponse:
    """Serve SVG favicon for Apple devices (modern iOS supports SVG)."""
    from starlette.responses import RedirectResponse

    return RedirectResponse("/favicon.svg", status_code=301)


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon_ico(request: Request) -> HTMLResponse:
    """Redirect favicon.ico to SVG (avoids 404 in browser tabs)."""
    from starlette.responses import RedirectResponse

    return RedirectResponse("/favicon.svg", status_code=301)


@mcp.custom_route("/robots.txt", methods=["GET"])
async def robots_txt(request: Request) -> HTMLResponse:
    return HTMLResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /mcp\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard/verify\n"
        "Disallow: /status\n"
        "Disallow: /metrics\n"
        "\n"
        "# LLM crawlers — allow landing + llms.txt, block private endpoints\n"
        "User-agent: GPTBot\n"
        "Allow: /\n"
        "Allow: /llms.txt\n"
        "Disallow: /mcp\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard/verify\n"
        "\n"
        "User-agent: ClaudeBot\n"
        "Allow: /\n"
        "Allow: /llms.txt\n"
        "Disallow: /mcp\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard/verify\n"
        "\n"
        "User-agent: PerplexityBot\n"
        "Allow: /\n"
        "Allow: /llms.txt\n"
        "\n"
        "Sitemap: https://oncofiles.com/sitemap.xml\n",
        media_type="text/plain",
    )


@mcp.custom_route("/sitemap.xml", methods=["GET"])
async def sitemap_xml(request: Request) -> HTMLResponse:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return HTMLResponse(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"  <url><loc>https://oncofiles.com/</loc><lastmod>{today}</lastmod>"
        "<priority>1.0</priority><changefreq>weekly</changefreq></url>\n"
        f"  <url><loc>https://oncofiles.com/dashboard</loc><lastmod>{today}</lastmod>"
        "<priority>0.8</priority><changefreq>daily</changefreq></url>\n"
        f"  <url><loc>https://oncofiles.com/health</loc><lastmod>{today}</lastmod>"
        "<priority>0.3</priority><changefreq>always</changefreq></url>\n"
        f"  <url><loc>https://oncofiles.com/privacy</loc><lastmod>{today}</lastmod>"
        "<priority>0.5</priority><changefreq>monthly</changefreq></url>\n"
        f"  <url><loc>https://oncofiles.com/terms</loc><lastmod>{today}</lastmod>"
        "<priority>0.5</priority><changefreq>monthly</changefreq></url>\n"
        "</urlset>\n",
        media_type="application/xml",
    )


@mcp.custom_route("/llms.txt", methods=["GET"])
async def llms_txt(request: Request) -> HTMLResponse:
    """LLM-readable site description following the llms.txt standard."""
    tools_count = _count_tools()
    return HTMLResponse(
        f"# Oncofiles\n"
        f"> Patient-side MCP server for oncology document management\n"
        f"\n"
        f"## About\n"
        f"Oncofiles is an open-source MCP (Model Context Protocol) server that helps\n"
        f"cancer patients and caregivers organize, search, and understand their medical\n"
        f"documents using AI. It provides {tools_count} tools for document "
        f"management, lab tracking,\n"
        f"clinical trial search, and treatment event logging.\n"
        f"\n"
        f"## Key Features\n"
        f"- 14 medical document categories (labs, imaging, pathology, genetics, etc.)\n"
        f"- AI-powered OCR, summaries, tags, and structured metadata extraction\n"
        f"- Bidirectional Google Drive sync with auto-rename and folder organization\n"
        f"- Lab value tracking with trends, reference ranges, and pre-cycle safety checks\n"
        f"- Pipeline dashboard with Google Sign-In authentication\n"
        f"- PubMed and ClinicalTrials.gov search integration\n"
        f"- Treatment event and research entry management\n"
        f"\n"
        f"## Technical Details\n"
        f"- Version: {VERSION}\n"
        f"- Protocol: MCP (Model Context Protocol) via FastMCP\n"
        f"- Language: Python 3.12+\n"
        f"- Database: SQLite / Turso\n"
        f"- License: MIT\n"
        f"- Repository: https://github.com/peter-fusek/oncofiles\n"
        f"\n"
        f"## Integration\n"
        f"Oncofiles works with Claude and any MCP-compatible AI assistant.\n"
        f"Connect via streamable-http transport at the /mcp endpoint.\n"
        f"Authentication requires a bearer token (MCP_BEARER_TOKEN).\n"
        f"\n"
        f"## Links\n"
        f"- Homepage: https://oncofiles.com\n"
        f"- Dashboard: https://oncofiles.com/dashboard\n"
        f"- Health: https://oncofiles.com/health\n"
        f"- GitHub: https://github.com/peter-fusek/oncofiles\n",
        media_type="text/plain; charset=utf-8",
    )


@mcp.custom_route("/manifest.json", methods=["GET"])
async def manifest_json(request: Request) -> JSONResponse:
    return JSONResponse(
        {
            "name": "Oncofiles Dashboard",
            "short_name": "Oncofiles",
            "start_url": "/dashboard",
            "display": "standalone",
            "background_color": "#0f172a",
            "theme_color": "#14b8a6",
            "icons": [{"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"}],
        }
    )


# ── Health check ──────────────────────────────────────────────────────────────


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Liveness probe — instant response, no DB dependency.

    Used by Railway healthcheck and UptimeRobot.  Must never block on
    external I/O so the process is not killed during Turso reconnects.
    """
    # Hard RSS ceiling — last-resort failsafe independent of scheduler health.
    # sys.exit(1) makes Railway see a failed healthcheck → immediate replace.
    from oncofiles.memory import HARD_RSS_CEILING_MB

    _rss_now = get_rss_mb()
    if _rss_now > HARD_RSS_CEILING_MB:
        logger.critical(
            "Hard RSS ceiling: %.1f MB > %d MB — forcing restart via health probe",
            _rss_now,
            HARD_RSS_CEILING_MB,
        )
        sys.exit(1)

    result: dict = {"status": "ok", "version": VERSION}
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
    if commit:
        result["commit"] = commit
    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        started_at = lifespan_ctx.get("started_at")
        if started_at:
            result["uptime_s"] = int((datetime.now(UTC) - started_at).total_seconds())
        result["memory_rss_mb"] = round(_rss_now, 1)
        result["memory"] = get_rss_trend()
        result["semaphores"] = get_semaphore_status()
        # Folder sync status — surfaces patients with suspended sync
        if _folder_404_counts:
            result["folder_404_suspended"] = {
                pid: count
                for pid, count in _folder_404_counts.items()
                if count >= _FOLDER_404_THRESHOLD
            }
    except Exception:
        logger.debug("Health: lifespan context not yet available", exc_info=True)
    return JSONResponse(result)


@mcp.custom_route("/readiness", methods=["GET"])
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe — checks DB connectivity with a 5 s timeout.

    Suitable for dashboards and deeper monitoring; NOT used as Railway
    healthcheck so a slow Turso reconnect won't kill the process.
    """
    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db: Database = lifespan_ctx["db"]
        reconnected = await db.reconnect_if_stale(timeout=5.0)
        result: dict = {"status": "ok", "version": VERSION, "db": "connected"}
        if reconnected:
            result["reconnected"] = True
        result["memory_rss_mb"] = round(get_rss_mb(), 1)
        # Scheduler job status (lightweight summary)
        tracker = lifespan_ctx.get("job_tracker", {})
        if tracker:
            jobs: dict = {}
            for job_id, info in tracker.items():
                entry: dict = {}
                if info.get("last_ok"):
                    entry["last_ok"] = info["last_ok"]
                if info.get("last_error"):
                    entry["last_error"] = info["last_error"]
                if info.get("running"):
                    entry["running"] = True
                if entry:
                    jobs[job_id] = entry
            if jobs:
                result["jobs"] = jobs
        return JSONResponse(result)
    except Exception:
        logger.exception("Readiness check failed")
        return JSONResponse(
            {"status": "degraded", "version": VERSION, "db": "unavailable"},
            status_code=503,
        )


def _count_tools() -> int:
    """Count registered MCP tools dynamically."""
    try:
        return sum(1 for k in mcp._local_provider._components if k.startswith("tool:"))
    except Exception:
        logger.warning("_count_tools: introspection failed, using fallback", exc_info=True)
        return 76  # fallback — update when tool count changes


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    """Public project stats for landing page and llms.txt."""
    from oncofiles.models import DocumentCategory

    doc_count = 0
    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patients = await db.list_patients(active_only=True)
        for p in patients:
            doc_count += await db.count_documents(patient_id=p.patient_id)
    except Exception:
        logger.warning("api_stats: failed to count documents", exc_info=True)

    return JSONResponse(
        {
            "documents": doc_count,
            "tools": _count_tools(),
            "categories": len(DocumentCategory) - 2,  # exclude legacy aliases
            "tests": TESTS_COUNT,
            "version": VERSION,
        }
    )


@mcp.custom_route("/status", methods=["GET"])
async def status(request: Request) -> JSONResponse:
    """System status with sync history, doc counts, and resource usage.

    Requires bearer token or dashboard session authentication.
    """
    import resource

    # Require bearer or dashboard session token
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db: Database = lifespan_ctx["db"]
        patient_id = await _get_dashboard_patient_id(request)

        from oncofiles.memory import db_slot

        async with db_slot("status", priority=True):
            # Ensure DB connection is fresh before running dashboard queries
            with suppress(Exception):
                await db.reconnect_if_stale(timeout=2.0)

            from oncofiles.filename_parser import is_standard_format

            doc_count = await db.count_documents(patient_id=patient_id)
            sync_stats = await db.get_sync_stats_summary(patient_id=patient_id)
            recent_syncs = await db.get_sync_history(limit=5, patient_id=patient_id)

            # Document health summary — aggregate SQL to avoid deserializing all rows (#258)
            async with db.db.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN ai_summary IS NOT NULL THEN 1 ELSE 0 END) AS with_ai,
                    SUM(CASE WHEN structured_metadata IS NOT NULL
                         AND structured_metadata != '' THEN 1 ELSE 0 END) AS with_metadata,
                    SUM(CASE WHEN document_date IS NOT NULL THEN 1 ELSE 0 END) AS with_date,
                    SUM(CASE WHEN institution IS NOT NULL THEN 1 ELSE 0 END) AS with_institution,
                    SUM(CASE WHEN gdrive_id IS NOT NULL THEN 1 ELSE 0 END) AS synced
                FROM documents
                WHERE deleted_at IS NULL AND patient_id = ?
                """,
                (patient_id,),
            ) as cursor:
                h = await cursor.fetchone()

            # standard_named requires Python logic — fetch only filenames
            async with db.db.execute(
                "SELECT filename FROM documents WHERE deleted_at IS NULL AND patient_id = ?",
                (patient_id,),
            ) as cursor:
                fn_rows = await cursor.fetchall()
            standard_count = sum(
                1 for r in fn_rows if is_standard_format(r["filename"], patient_id=patient_id)
            )

            doc_health = {
                "total": h["total"] or 0,
                "with_ai": h["with_ai"] or 0,
                "with_metadata": h["with_metadata"] or 0,
                "with_date": h["with_date"] or 0,
                "with_institution": h["with_institution"] or 0,
                "synced": h["synced"] or 0,
                "standard_named": standard_count,
            }

            # Google service connection status
            from oncofiles.oauth import SCOPE_CALENDAR, SCOPE_DRIVE, SCOPE_GMAIL

            google_services = {"drive": False, "gmail": False, "calendar": False}
            try:
                oauth_token = await db.get_oauth_token(patient_id=patient_id)
                if oauth_token:
                    scopes = json.loads(oauth_token.granted_scopes or "[]")
                    google_services["drive"] = SCOPE_DRIVE in scopes
                    google_services["gmail"] = SCOPE_GMAIL in scopes
                    google_services["calendar"] = SCOPE_CALENDAR in scopes
                    google_services["folder_id"] = (
                        oauth_token.gdrive_folder_id or lifespan_ctx.get("gdrive_folder_id") or None
                    )
                    google_services["folder_name"] = oauth_token.gdrive_folder_name
                    google_services["owner_email"] = oauth_token.owner_email
            except Exception:
                logger.warning("Failed to load Google services status for %s", patient_id)

        # Memory
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == "darwin":
            rss_mb = rusage.ru_maxrss / (1024 * 1024)
        else:
            rss_mb = rusage.ru_maxrss / 1024

        # Uptime
        started_at = lifespan_ctx.get("started_at")
        uptime_s = int((datetime.now(UTC) - started_at).total_seconds()) if started_at else None

        return JSONResponse(
            {
                "status": "ok",
                "version": VERSION,
                "uptime_s": uptime_s,
                "documents": doc_count,
                "document_limit": CONF_MAX_DOCS,
                "document_health": doc_health,
                "google_services": google_services,
                "service_sync_times": _get_service_sync_times(patient_id),
                "memory_rss_mb": round(rss_mb, 1),
                "sync_7d": {
                    "total": sync_stats.get("total_syncs", 0),
                    "successful": sync_stats.get("successful", 0),
                    "failed": sync_stats.get("failed", 0),
                    "avg_duration_s": sync_stats.get("avg_duration_s"),
                    "total_imported": sync_stats.get("total_imported", 0),
                    "total_errors": sync_stats.get("total_errors", 0),
                    "last_sync_at": sync_stats.get("last_sync_at"),
                },
                "recent_syncs": [
                    {
                        "started_at": s.get("started_at"),
                        "status": s.get("status"),
                        "trigger": s.get("sync_trigger"),
                        "duration_s": s.get("duration_s"),
                        "new": s.get("from_gdrive_new", 0),
                        "errors": (s.get("from_gdrive_errors", 0) or 0)
                        + (s.get("to_gdrive_errors", 0) or 0),
                    }
                    for s in recent_syncs
                ],
            }
        )
    except Exception:
        logger.exception("Status endpoint error")
        return JSONResponse({"status": "error", "version": VERSION}, status_code=500)


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(request: Request) -> JSONResponse:
    """Return server metrics. Requires bearer token authentication."""
    import resource
    import time

    # Require bearer token for metrics
    err = _check_bearer(request)
    if err:
        return err
    if not MCP_BEARER_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patients = await db.list_patients(active_only=True)
        doc_count = 0
        for p in patients:
            doc_count += await db.count_documents(patient_id=p.patient_id)

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


# ── Dashboard ─────────────────────────────────────────────────────────────────

_DASHBOARD_HTML: str | None = None

# Session token validity (24 hours)
_SESSION_MAX_AGE = 86400


def _load_dashboard_html() -> str:
    global _DASHBOARD_HTML  # noqa: PLW0603
    if _DASHBOARD_HTML is None:
        _DASHBOARD_HTML = (Path(__file__).parent / "dashboard.html").read_text()
    return _DASHBOARD_HTML


def _make_session_token(email: str) -> str:
    """Create an HMAC-signed session token: email|expiry|signature."""
    if not MCP_BEARER_TOKEN:
        raise ValueError("Cannot create session token: MCP_BEARER_TOKEN not configured")
    expiry = str(int(time.time()) + _SESSION_MAX_AGE)
    key = MCP_BEARER_TOKEN.encode()
    payload = f"{email}|{expiry}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}|{sig}"


def _verify_session_token(token: str) -> str | None:
    """Verify a session token. Returns email if valid, None otherwise."""
    if not token or not MCP_BEARER_TOKEN:
        return None
    # Support both new "|" and legacy "." separators
    sep = "|" if "|" in token else "."
    parts = token.rsplit(sep, 2)
    if len(parts) != 3:
        return None
    email, expiry_str, sig = parts
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    if time.time() > expiry:
        logger.warning("Session token expired for %s", email)
        return None
    key = MCP_BEARER_TOKEN.encode()
    expected = hmac.new(key, f"{email}{sep}{expiry_str}".encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        logger.warning("Session token signature mismatch for %s", email)
        return None
    return email


def _check_dashboard_auth(request: Request) -> JSONResponse | None:
    """Check bearer token OR dashboard session token. Returns error response or None."""
    # Try standard bearer token first
    if _check_bearer(request) is None:
        return None
    # Try dashboard session token (Bearer session:xxx)
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer session:"):
        session_token = auth_header.removeprefix("Bearer session:").strip()
        if _verify_session_token(session_token):
            return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


async def _get_dashboard_patient_id(request: Request) -> str:
    """Extract patient_id from query params and resolve slug → UUID.

    Accepts either a UUID or a slug (e.g. 'erika').  Always returns
    the UUID patient_id suitable for DB queries.
    """
    raw = request.query_params.get("patient_id", "").strip().lower()
    if not raw:
        # Fall back to first active patient instead of hardcoded "erika"
        try:
            db_fb: Database = request.app.state.fastmcp_server._lifespan_result["db"]
            patients = await db_fb.list_patients(active_only=True)
            if patients:
                return patients[0].patient_id
        except Exception:
            pass
        return ""
    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        resolved = await db.resolve_patient_id(raw)
        if not resolved:
            logger.warning("_get_dashboard_patient_id: no patient found for '%s'", raw)
        return resolved or raw
    except Exception:
        logger.error("_get_dashboard_patient_id: resolution failed for '%s'", raw, exc_info=True)
        return raw


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard page. Open to all — auth happens client-side via Google Sign-In."""
    return HTMLResponse(_load_dashboard_html())


@mcp.custom_route("/demo", methods=["GET"])
async def demo_dashboard(request: Request) -> HTMLResponse:
    """Public demo dashboard with masked sample data. No auth required."""
    html = _load_dashboard_html()
    # Inject demo mode flag BEFORE the main dashboard script so init() sees it
    demo_inject = "<script>window.DEMO_MODE=true;</script>\n"
    html = html.replace("<script>", demo_inject + "<script>", 1)
    return HTMLResponse(html)


@mcp.custom_route("/api/demo-data", methods=["GET"])
async def api_demo_data(request: Request) -> JSONResponse:
    """Return masked sample data for demo dashboard. Public, no auth."""
    return JSONResponse(
        {
            "status": {
                "version": VERSION,
                "uptime_s": 86400,
                "memory_rss_mb": 145.2,
                "documents": 158,
                "document_health": {
                    "total": 158,
                    "with_ocr": 155,
                    "with_ai": 158,
                    "with_metadata": 158,
                    "with_date": 158,
                    "with_institution": 150,
                    "synced": 158,
                    "standard_named": 155,
                },
                "sync_7d": {
                    "total": 312,
                    "successful": 308,
                    "failed": 4,
                    "avg_duration_s": 14.2,
                    "total_imported": 6,
                    "total_errors": 0,
                    "last_sync_at": "2026-03-24T14:30:00Z",
                },
                "recent_syncs": [
                    {
                        "started_at": "2026-03-24T14:30:00Z",
                        "status": "completed",
                        "trigger": "scheduled",
                        "duration_s": 12,
                        "new": 0,
                        "errors": 0,
                    },
                    {
                        "started_at": "2026-03-24T14:25:00Z",
                        "status": "completed",
                        "trigger": "scheduled",
                        "duration_s": 8,
                        "new": 1,
                        "errors": 0,
                    },
                    {
                        "started_at": "2026-03-24T14:20:00Z",
                        "status": "completed",
                        "trigger": "scheduled",
                        "duration_s": 15,
                        "new": 0,
                        "errors": 0,
                    },
                ],
            },
            "documents": {
                "filter": "all",
                "matched": 15,
                "summary": {
                    "total": 158,
                    "with_ocr": 155,
                    "with_ai": 158,
                    "with_metadata": 158,
                    "synced": 158,
                    "standard_named": 155,
                    "with_date": 158,
                    "with_institution": 150,
                    "fully_complete": 150,
                },
                "documents": [
                    {
                        "id": 1,
                        "filename": "20260315_Patient_NOU_Labs_BloodCount.pdf",
                        "category": "Labs",
                        "date": "2026-03-15",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 2,
                        "filename": "20260310_Patient_FNsP_CT_AbdomenPelvis.pdf",
                        "category": "CT",
                        "date": "2026-03-10",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 3,
                        "filename": "20260301_Patient_NOU_Pathology_ColonBiopsy.pdf",
                        "category": "Pathology",
                        "date": "2026-03-01",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 4,
                        "filename": "20260225_Patient_Medirex_Labs_TumorMarkers.pdf",
                        "category": "Labs",
                        "date": "2026-02-25",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 5,
                        "filename": "20260220_Patient_NOU_Genetics_KRASPanel.pdf",
                        "category": "Genetics",
                        "date": "2026-02-20",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 6,
                        "filename": "20260215_Patient_NOU_ChemoSheet_FOLFOX_C3.pdf",
                        "category": "ChemoSheet",
                        "date": "2026-02-15",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": False,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 7,
                        "filename": "20260210_Patient_FNsP_USG_Liver.pdf",
                        "category": "USG",
                        "date": "2026-02-10",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 8,
                        "filename": "20260205_Patient_NOU_Prescription_[MEDICATION_REDACTED].pdf",
                        "category": "Prescription",
                        "date": "2026-02-05",
                        "has_ocr": True,
                        "has_ai": False,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 9,
                        "filename": "20260130_Patient_NOU_DischargeSummary_Chemo.pdf",
                        "category": "DischargeSummary",
                        "date": "2026-01-30",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 10,
                        "filename": "20260125_Patient_GP_Referral_Oncology.pdf",
                        "category": "Referral",
                        "date": "2026-01-25",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 11,
                        "filename": "20260320_Patient_GP_Vaccination_Covid19Booster.pdf",
                        "category": "Vaccination",
                        "date": "2026-03-20",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 12,
                        "filename": "20260305_Patient_DentalClinic_Dental_AnnualCheckup.pdf",
                        "category": "Dental",
                        "date": "2026-03-05",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                    {
                        "id": 13,
                        "filename": "20260228_Patient_GP_Preventive_ColorectalScreening.pdf",
                        "category": "Preventive",
                        "date": "2026-02-28",
                        "has_ocr": True,
                        "has_ai": True,
                        "has_metadata": True,
                        "is_synced": True,
                        "is_standard_name": True,
                        "gdrive_id": "demo",
                    },
                ],
            },
            "prompt_log": {
                "entries": [
                    {
                        "id": 1,
                        "call_type": "enhance",
                        "document_id": 42,
                        "model": "claude-haiku-4-5-20251001",
                        "input_tokens": 1250,
                        "output_tokens": 380,
                        "duration_ms": 2100,
                        "result_summary": "Extracted metadata: Labs, NOU, 2026-03-15",
                        "status": "ok",
                        "created_at": "2026-03-24T14:28:00Z",
                    },
                    {
                        "id": 2,
                        "call_type": "ocr",
                        "document_id": 41,
                        "model": "claude-haiku-4-5-20251001",
                        "input_tokens": 8400,
                        "output_tokens": 1200,
                        "duration_ms": 4500,
                        "result_summary": "OCR: 3 pages extracted",
                        "status": "ok",
                        "created_at": "2026-03-24T14:25:00Z",
                    },
                    {
                        "id": 3,
                        "call_type": "classify_email",
                        "document_id": None,
                        "model": "claude-haiku-4-5-20251001",
                        "input_tokens": 420,
                        "output_tokens": 85,
                        "duration_ms": 800,
                        "result_summary": "medical=true (0.94)",
                        "status": "ok",
                        "created_at": "2026-03-24T14:20:00Z",
                    },
                ],
                "stats": {
                    "total_calls": 847,
                    "total_input_tokens": 1240000,
                    "total_output_tokens": 320000,
                    "avg_duration_ms": 2800,
                },
            },
            "analytics": {
                "days": 30,
                "prompts": {
                    "total_calls": 847,
                    "total_input_tokens": 1240000,
                    "total_output_tokens": 320000,
                    "total_errors": 3,
                    "error_rate": 0.004,
                    "estimated_cost_usd": 2.27,
                    "by_call_type": {
                        "enhance": 312,
                        "ocr": 198,
                        "classify_email": 180,
                        "classify_event": 92,
                        "summarize": 65,
                    },
                    "calls_per_day": [],
                },
                "tools": {
                    "total_calls": 2840,
                    "unique_tools": 34,
                    "top_tools": [
                        {"tool": "search_documents", "count": 520},
                        {"tool": "get_patient_context", "count": 410},
                        {"tool": "analyze_labs", "count": 280},
                        {"tool": "search_conversations", "count": 195},
                        {"tool": "get_lab_trends", "count": 140},
                    ],
                    "calls_per_day": [],
                },
                "pipeline": {
                    "total_syncs": 312,
                    "successful_syncs": 308,
                    "failed_syncs": 0,
                    "total_docs_imported": 158,
                    "total_docs_exported": 155,
                    "avg_sync_duration_s": 14.2,
                    "docs_enhanced": 158,
                    "docs_pending": 3,
                },
                "latency": {
                    "overall": {"p50": 2100, "p95": 5800, "p99": 12000},
                    "by_type": {
                        "enhance": {"p50": 2400, "p95": 4200},
                        "ocr": {"p50": 4500, "p95": 8900},
                        "classify_email": {"p50": 800, "p95": 1500},
                    },
                },
            },
        }
    )


@mcp.custom_route("/dashboard/config", methods=["GET"])
async def dashboard_config(request: Request) -> JSONResponse:
    """Return public config needed by dashboard (Google OAuth client ID)."""
    return JSONResponse({"client_id": GOOGLE_OAUTH_CLIENT_ID or ""})


@mcp.custom_route("/dashboard/verify", methods=["POST"])
async def dashboard_verify(request: Request) -> JSONResponse:
    """Verify a Google ID token and return a dashboard session token.

    Expects JSON body: {"credential": "google_id_token_jwt"}
    Returns: {"session_token": "...", "email": "..."} or 401/403.
    """
    import httpx

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid request body"}, status_code=400)

    credential = body.get("credential", "")
    if not credential:
        return JSONResponse({"error": "missing credential"}, status_code=400)

    if not MCP_BEARER_TOKEN:
        return JSONResponse({"error": "server not configured for auth"}, status_code=500)

    if not GOOGLE_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "Google OAuth not configured"}, status_code=500)

    # Verify the ID token with Google
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://oauth2.googleapis.com/tokeninfo?id_token={quote(credential, safe='')}"
            )
        if resp.status_code != 200:
            logger.warning("Google tokeninfo returned %d", resp.status_code)
            return JSONResponse({"error": "invalid Google token"}, status_code=401)
        token_info = resp.json()
    except Exception:
        logger.exception("Google token verification failed")
        return JSONResponse({"error": "token verification failed"}, status_code=500)

    # Check audience matches our client ID (mandatory — prevents token from other apps)
    aud = token_info.get("aud", "")
    if aud != GOOGLE_OAUTH_CLIENT_ID:
        logger.warning("Token audience mismatch: %s != %s", aud, GOOGLE_OAUTH_CLIENT_ID)
        return JSONResponse({"error": "invalid token audience"}, status_code=401)

    # Check email is in allowed list
    email = token_info.get("email", "").lower()
    email_verified = token_info.get("email_verified", "false")
    if email_verified not in ("true", True):
        return JSONResponse({"error": "email not verified"}, status_code=401)

    allowed = set(DASHBOARD_ALLOWED_EMAILS)
    # Also allow caregiver emails from the patients table
    try:
        db_auth: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        all_patients = await db_auth.list_patients(active_only=True)
        for p in all_patients:
            if p.caregiver_email:
                allowed.add(p.caregiver_email.lower())
    except Exception:
        pass
    if email not in allowed:
        logger.warning("Dashboard login denied for: %s", email)
        return JSONResponse({"error": "access denied for this email"}, status_code=403)

    # Issue session token
    session_token = _make_session_token(email)
    logger.info("Dashboard session issued for: %s", email)
    return JSONResponse(
        {
            "session_token": session_token,
            "email": email,
            "name": token_info.get("name", ""),
        }
    )


@mcp.custom_route("/api/documents", methods=["GET"])
async def api_documents(request: Request) -> JSONResponse:
    """Document status matrix API. Requires bearer or dashboard session auth."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        from oncofiles.tools.hygiene import _build_document_matrix

        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await _get_dashboard_patient_id(request)
        filter_param = request.query_params.get("filter", "all")
        try:
            limit = min(int(request.query_params.get("limit", "200")), 200)
        except (ValueError, TypeError):
            limit = 200
        result = await _build_document_matrix(
            db, filter_param=filter_param, limit=limit, patient_id=patient_id
        )
        return JSONResponse(result)
    except Exception:
        logger.exception("API documents endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/doc-detail/{doc_id}", methods=["GET"])
async def api_doc_detail(request: Request) -> JSONResponse:
    """Document detail with OCR pages and structured metadata."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        doc_id_raw = request.path_params.get("doc_id", "")
        try:
            doc_id = int(doc_id_raw)
        except (ValueError, TypeError):
            return JSONResponse({"error": "invalid doc_id"}, status_code=400)

        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await _get_dashboard_patient_id(request)

        # Fetch document row
        async with db.db.execute(
            "SELECT id, filename, gdrive_id, category, document_date, institution, "
            "ai_summary, ai_tags, structured_metadata, patient_id "
            "FROM documents WHERE id = ? AND deleted_at IS NULL",
            (doc_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        if not rows:
            return JSONResponse({"error": "document not found"}, status_code=404)
        doc = rows[0]

        # Verify patient isolation
        if patient_id and doc["patient_id"] != patient_id:
            return JSONResponse({"error": "document not found"}, status_code=404)

        gdrive_id = doc["gdrive_id"] or ""
        gdrive_url = f"https://drive.google.com/file/d/{gdrive_id}/view" if gdrive_id else ""
        preview_url = f"https://drive.google.com/file/d/{gdrive_id}/preview" if gdrive_id else ""

        # Parse ai_tags (JSON string)
        ai_tags: list[str] = []
        if doc["ai_tags"]:
            try:
                ai_tags = json.loads(doc["ai_tags"])
            except Exception:
                ai_tags = []

        # Parse structured_metadata (JSON string)
        structured_metadata: dict = {}
        if doc["structured_metadata"]:
            try:
                structured_metadata = json.loads(doc["structured_metadata"])
            except Exception:
                structured_metadata = {}

        # Fetch OCR pages (serialized — single Turso connection)
        async with db.db.execute(
            "SELECT page_number, extracted_text FROM document_pages "
            "WHERE document_id = ? ORDER BY page_number",
            (doc_id,),
        ) as cursor:
            page_rows = await cursor.fetchall()
        pages = [
            {
                "page_number": p["page_number"],
                "text": p["extracted_text"] or "",
                "char_count": len(p["extracted_text"] or ""),
            }
            for p in page_rows
        ]

        return JSONResponse(
            {
                "id": doc["id"],
                "filename": doc["filename"],
                "gdrive_id": gdrive_id,
                "gdrive_url": gdrive_url,
                "preview_url": preview_url,
                "category": doc["category"] or "other",
                "document_date": doc["document_date"] or "",
                "institution": doc["institution"] or "",
                "ai_summary": doc["ai_summary"] or "",
                "ai_tags": ai_tags,
                "structured_metadata": structured_metadata,
                "pages": pages,
            }
        )
    except Exception:
        logger.exception("API doc-detail endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/reconciliation", methods=["GET"])
async def api_reconciliation(request: Request) -> JSONResponse:
    """DB vs GDrive reconciliation report. Requires bearer or dashboard session auth."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        from oncofiles.tools.hygiene import _build_reconciliation_report

        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db: Database = lifespan_ctx["db"]
        patient_id = await _get_dashboard_patient_id(request)
        # Use per-patient GDrive client instead of global
        gdrive = None
        folder_id = ""
        if patient_id:
            clients = await _create_patient_clients(db, patient_id)
            if clients:
                gdrive, _, _, folder_id = clients
        if not gdrive or not folder_id:
            # No GDrive configured for this patient — return empty
            return JSONResponse(
                {
                    "db_count": 0,
                    "gdrive_count": 0,
                    "in_db_not_gdrive": [],
                    "in_gdrive_not_db": [],
                    "filename_mismatches": [],
                }
            )
        result = await _build_reconciliation_report(
            db,
            gdrive,
            folder_id,
            patient_id=patient_id,
        )
        return JSONResponse(result)
    except Exception:
        logger.exception("API reconciliation endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/prompt-log", methods=["GET"])
async def api_prompt_log(request: Request) -> JSONResponse:
    """Prompt observability API. Returns prompt log entries and stats."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        from oncofiles.models import PromptLogQuery

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        query = PromptLogQuery(
            call_type=request.query_params.get("call_type"),
            document_id=(
                int(request.query_params["document_id"])
                if "document_id" in request.query_params
                else None
            ),
            text=request.query_params.get("text"),
        )
        try:
            query.limit = min(int(request.query_params.get("limit", "100")), 200)
        except (ValueError, TypeError):
            query.limit = 100

        pid = await _get_dashboard_patient_id(request)
        entries = await db_inst.search_prompt_log(query, patient_id=pid)
        stats = await db_inst.get_prompt_log_stats(patient_id=pid)

        items = [
            {
                "id": e.id,
                "call_type": e.call_type.value,
                "document_id": e.document_id,
                "model": e.model,
                "input_tokens": e.input_tokens,
                "output_tokens": e.output_tokens,
                "duration_ms": e.duration_ms,
                "result_summary": e.result_summary,
                "system_prompt": e.system_prompt,
                "user_prompt": e.user_prompt,
                "status": e.status,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
        return JSONResponse({"entries": items, "stats": stats})
    except Exception:
        logger.exception("API prompt-log endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/manifest", methods=["GET"])
async def api_manifest(request: Request) -> JSONResponse:
    """Return the manifest JSON for the current patient. Requires auth."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        from oncofiles.manifest import export_manifest, render_manifest_json

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        pid = await _get_dashboard_patient_id(request)
        manifest = await export_manifest(db_inst, patient_id=pid)
        manifest_json = render_manifest_json(manifest)
        return JSONResponse(json.loads(manifest_json))
    except Exception:
        logger.exception("API manifest endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/usage-analytics", methods=["GET"])
async def api_usage_analytics(request: Request) -> JSONResponse:
    """Usage analytics: prompt stats, tool usage, pipeline health, latency."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        days = min(int(request.query_params.get("days", "30")), 90)

        # Analytics aggregate across ALL patients (system-wide view).
        # Per-patient filtering misses unattributed calls (empty patient_id) (#283).
        prompt_stats = await db_inst.get_prompt_stats(days=days, patient_id=None)
        tool_stats = await db_inst.get_tool_usage_stats(days=days, patient_id=None)
        pipeline_stats = await db_inst.get_pipeline_stats(patient_id=None)
        latency = await db_inst.get_prompt_latency_percentiles(days=days, patient_id=None)

        from dataclasses import asdict

        return JSONResponse(
            {
                "days": days,
                "prompts": asdict(prompt_stats),
                "tools": asdict(tool_stats),
                "pipeline": asdict(pipeline_stats),
                "latency": latency,
            }
        )
    except Exception:
        logger.exception("API usage-analytics endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/bug-report", methods=["POST"])
async def api_bug_report(request: Request) -> JSONResponse:
    """Create a GitHub issue from dashboard bug report with full context."""
    from oncofiles.config import GITHUB_REPO, GITHUB_TOKEN

    err = _check_dashboard_auth(request)
    if err:
        return err

    if not GITHUB_TOKEN:
        return JSONResponse({"error": "GITHUB_TOKEN not configured"}, status_code=503)

    try:
        body = await request.json()
        title = body.get("title", "Dashboard bug report")[:120]
        description = body.get("description", "")[:1000]
        page_url = body.get("page_url", "")
        page_section = body.get("page_section", "")
        page_state = body.get("page_state", "")[:5000]
        console_errors = body.get("console_errors", "")[:2000]
        screenshot_b64 = body.get("screenshot", "")
        user_agent = body.get("user_agent", "")

        # Sanitize user inputs for markdown (prevent injection)
        def _esc(s: str) -> str:
            return s.replace("|", "\\|").replace("`", "\\`")

        # Build structured issue body for Claude Code
        issue_body = f"""## Bug Report (Dashboard)

### Description
```
{description}
```

### Context
| Field | Value |
|-------|-------|
| Page | `{_esc(page_url)}` |
| Section | `{_esc(page_section)}` |
| User Agent | `{_esc(user_agent[:100])}` |
| Reported | {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")} |

### Page State
```json
{page_state}
```

### Console Errors
```
{console_errors or "None"}
```
"""
        # Upload screenshot if provided
        screenshot_url = ""
        if screenshot_b64 and len(screenshot_b64) < 5_000_000:
            ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            img_path = f".github/screenshots/bug_{ts}.png"
            try:
                import httpx

                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.put(
                        f"https://api.github.com/repos/{GITHUB_REPO}/contents/{img_path}",
                        headers={
                            "Authorization": f"Bearer {GITHUB_TOKEN}",
                            "Accept": "application/vnd.github+json",
                        },
                        json={
                            "message": f"Bug screenshot {ts}",
                            "content": screenshot_b64,
                            "branch": "main",
                        },
                    )
                    if resp.status_code in (200, 201):
                        dl = resp.json().get("content", {}).get("download_url", "")
                        if dl:
                            screenshot_url = dl
            except Exception:
                logger.warning("Failed to upload bug screenshot", exc_info=True)

        if screenshot_url:
            issue_body += f"\n### Screenshot\n![screenshot]({screenshot_url})\n"

        issue_body += """
### For Claude Code
Fix file: `src/oncofiles/dashboard.html`
- Check the Page State JSON for the data that was displayed
- Check Console Errors for JS exceptions
- Reproduce by navigating to the reported Page/Section
"""

        # Create GitHub issue
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"https://api.github.com/repos/{GITHUB_REPO}/issues",
                headers={
                    "Authorization": f"Bearer {GITHUB_TOKEN}",
                    "Accept": "application/vnd.github+json",
                },
                json={
                    "title": f"[Dashboard Bug] {title}",
                    "body": issue_body,
                    "labels": ["bug", "dashboard"],
                },
            )
            if resp.status_code == 201:
                issue_url = resp.json().get("html_url", "")
                logger.info("Bug report created: %s", issue_url)
                return JSONResponse({"ok": True, "issue_url": issue_url})
            else:
                logger.error(
                    "GitHub issue creation failed: %d %s", resp.status_code, resp.text[:200]
                )
                return JSONResponse(
                    {"error": f"GitHub API error: {resp.status_code}"},
                    status_code=502,
                )

    except Exception:
        logger.exception("Bug report endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/patients", methods=["GET"])
async def api_list_patients(request: Request) -> JSONResponse:
    """List all patients. Requires bearer or dashboard session auth."""
    err = _check_dashboard_auth(request)
    if err:
        return err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patients = await db_inst.list_patients(active_only=False)
        return JSONResponse(
            [
                {
                    "patient_id": p.patient_id,
                    "display_name": p.display_name,
                    "caregiver_email": p.caregiver_email,
                    "diagnosis_summary": p.diagnosis_summary,
                    "is_active": p.is_active,
                    "preferred_lang": p.preferred_lang,
                    "created_at": p.created_at.isoformat() if p.created_at else None,
                }
                for p in patients
            ]
        )
    except Exception:
        logger.exception("API list-patients error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/patients", methods=["POST"])
async def api_create_patient(request: Request) -> JSONResponse:
    """Create a new patient. Requires bearer or dashboard session auth.

    Body: {patient_id, display_name, caregiver_email?, diagnosis_summary?, preferred_lang?}
    Returns the created patient + a generated bearer token (shown once).
    """
    from oncofiles.models import Patient

    err = _check_dashboard_auth(request)
    if err:
        return err
    rate_err = _check_rate_limit("patients")
    if rate_err:
        return rate_err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        body = await request.json()
        patient_id = body.get("patient_id", "").strip().lower()
        if not patient_id or len(patient_id) < 2:
            return JSONResponse({"error": "patient_id required (min 2 chars)"}, status_code=400)
        # Slug-safe check
        import re

        if not re.match(r"^[a-z0-9][a-z0-9_-]{1,49}$", patient_id):
            return JSONResponse(
                {"error": "patient_id must be lowercase alphanumeric with - or _ (2-50 chars)"},
                status_code=400,
            )
        existing = await db_inst.get_patient_by_slug(patient_id)
        if existing:
            return JSONResponse(
                {"error": f"Patient '{patient_id}' already exists"}, status_code=409
            )

        patient = Patient(
            patient_id=patient_id,
            display_name=body.get("display_name", patient_id),
            caregiver_email=body.get("caregiver_email"),
            diagnosis_summary=body.get("diagnosis_summary"),
            preferred_lang=body.get("preferred_lang", "sk"),
        )
        created = await db_inst.insert_patient(patient)
        # Generate initial bearer token
        token = await db_inst.create_patient_token(patient_id, label="initial")
        logger.info("New patient created: %s", patient_id)
        return JSONResponse(
            {
                "patient": {
                    "patient_id": created.patient_id,
                    "display_name": created.display_name,
                    "caregiver_email": created.caregiver_email,
                    "diagnosis_summary": created.diagnosis_summary,
                    "preferred_lang": created.preferred_lang,
                },
                "bearer_token": token,
                "warning": "Save this token — it will not be shown again.",
            },
            status_code=201,
        )
    except Exception:
        logger.exception("API create-patient error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/patients/{patient_id}", methods=["PATCH"])
async def api_update_patient(request: Request) -> JSONResponse:
    """Update patient fields (is_active, display_name, preferred_lang).

    Requires bearer or dashboard session auth.
    Body: {is_active?: bool, display_name?: str, preferred_lang?: str}
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        pid = request.path_params["patient_id"]
        patient = await db_inst.get_patient(pid)
        if not patient:
            return JSONResponse({"error": "Patient not found"}, status_code=404)
        body = await request.json()
        updated = await db_inst.update_patient(
            pid,
            display_name=body.get("display_name"),
            is_active=body.get("is_active"),
            preferred_lang=body.get("preferred_lang"),
        )
        if updated and not body.get("is_active", True):
            # Clear sync caches for deactivated patient
            _patient_clients_cache.pop(pid, None)
            _folder_404_counts.pop(pid, None)
            logger.info("Patient deactivated: %s", pid)
        return JSONResponse(
            {
                "patient_id": updated.patient_id if updated else pid,
                "display_name": updated.display_name if updated else None,
                "is_active": updated.is_active if updated else None,
            }
        )
    except Exception:
        logger.exception("API update-patient error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/patient-tokens", methods=["POST"])
async def api_create_patient_token(request: Request) -> JSONResponse:
    """Generate a new bearer token for a patient. Requires bearer or dashboard session auth.

    Body: {patient_id, label?}
    Returns the plaintext token (shown once).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    rate_err = _check_rate_limit("patient-tokens")
    if rate_err:
        return rate_err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        body = await request.json()
        patient_id = body.get("patient_id", "").strip()
        if not patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)
        patient = await db_inst.get_patient(patient_id)
        if not patient:
            return JSONResponse({"error": f"Patient '{patient_id}' not found"}, status_code=404)
        label = body.get("label", "")
        token = await db_inst.create_patient_token(patient_id, label=label)
        return JSONResponse(
            {
                "patient_id": patient_id,
                "bearer_token": token,
                "label": label,
                "warning": "Save this token — it will not be shown again.",
            },
            status_code=201,
        )
    except Exception:
        logger.exception("API create-patient-token error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/sync-trigger", methods=["POST"])
async def api_sync_trigger(request: Request) -> JSONResponse:
    """Trigger a GDrive sync for a specific patient. Requires dashboard auth.

    Body: {"patient_id": "..."}
    Returns sync stats on success.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        body = await request.json()
        raw_patient_id = body.get("patient_id", "").strip().lower()
        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)

        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db_inst: Database = lifespan_ctx["db"]
        files = lifespan_ctx["files"]

        # Resolve slug → UUID before DB queries
        patient_id = await db_inst.resolve_patient_id(raw_patient_id) or raw_patient_id

        # Verify patient exists
        pat = await db_inst.get_patient(patient_id)
        if not pat:
            return JSONResponse({"error": f"Patient '{patient_id}' not found"}, status_code=404)

        # Create GDrive client from patient's OAuth token
        clients = await _create_patient_clients(db_inst, patient_id)
        if not clients:
            return JSONResponse(
                {
                    "error": (
                        f"No OAuth token or folder for patient '{patient_id}'. "
                        "Connect Google Drive first."
                    )
                },
                status_code=400,
            )
        p_gdrive, _, _, folder_id = clients

        from oncofiles.sync import sync

        stats = await asyncio.wait_for(
            sync(db_inst, files, p_gdrive, folder_id, trigger="manual", patient_id=patient_id),
            timeout=120,
        )

        return JSONResponse({"status": "ok", "patient_id": patient_id, "stats": stats})
    except TimeoutError:
        return JSONResponse({"error": "Sync timed out after 120s"}, status_code=504)
    except Exception:
        logger.exception("API sync-trigger error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/enhance-trigger", methods=["POST"])
async def api_enhance_trigger(request: Request) -> JSONResponse:
    """Trigger AI enhancement for a specific patient's documents. Requires dashboard auth.

    Body: {"patient_id": "..."}
    Enhances all unprocessed documents (OCR + AI summary + metadata).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        body = await request.json()
        raw_patient_id = body.get("patient_id", "").strip().lower()
        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)

        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db_inst: Database = lifespan_ctx["db"]
        files = lifespan_ctx["files"]

        patient_id = await db_inst.resolve_patient_id(raw_patient_id) or raw_patient_id
        pat = await db_inst.get_patient(patient_id)
        if not pat:
            return JSONResponse({"error": f"Patient '{patient_id}' not found"}, status_code=404)

        from oncofiles.sync import enhance_documents

        stats = await asyncio.wait_for(
            enhance_documents(db_inst, files, patient_id=patient_id),
            timeout=300,
        )

        return JSONResponse({"status": "ok", "patient_id": patient_id, "stats": stats})
    except TimeoutError:
        return JSONResponse({"error": "Enhancement timed out after 300s"}, status_code=504)
    except Exception:
        logger.exception("API enhance-trigger error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/gdrive-folders", methods=["GET"])
async def api_gdrive_folders(request: Request) -> JSONResponse:
    """List Google Drive root folders for a patient. Requires dashboard auth."""
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        patient_id = await _get_dashboard_patient_id(request)
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]

        token = await db_inst.get_oauth_token(patient_id=patient_id)
        if not token:
            return JSONResponse(
                {"error": "No OAuth token. Connect Google Drive first."},
                status_code=400,
            )

        from oncofiles.oauth import is_token_expired, refresh_access_token

        access_token = token.access_token
        if is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None):
            refreshed = refresh_access_token(token.refresh_token)
            access_token = refreshed["access_token"]
            new_expiry = datetime.now(UTC) + timedelta(seconds=refreshed.get("expires_in", 3600))
            token.access_token = access_token
            token.token_expiry = new_expiry
            await db_inst.upsert_oauth_token(token)

        gdrive = GDriveClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )

        folders = await asyncio.to_thread(gdrive.list_root_folders)
        return JSONResponse(
            {
                "folders": folders,
                "current_folder_id": token.gdrive_folder_id,
            }
        )
    except Exception:
        logger.exception("API gdrive-folders error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/gdrive-set-folder", methods=["POST"])
async def api_gdrive_set_folder(request: Request) -> JSONResponse:
    """Set or create a GDrive sync folder for a patient. Requires dashboard auth.

    Body: {patient_id, folder_id?} or {patient_id, folder_name?}
    If folder_name given without folder_id, creates a new folder in root.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        body = await request.json()
        raw_patient_id = body.get("patient_id", "").strip().lower()
        folder_id = body.get("folder_id", "").strip()
        folder_name = body.get("folder_name", "").strip()

        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)
        if not folder_id and not folder_name:
            return JSONResponse(
                {"error": "folder_id or folder_name required"},
                status_code=400,
            )

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await db_inst.resolve_patient_id(raw_patient_id) or raw_patient_id
        token = await db_inst.get_oauth_token(patient_id=patient_id)
        if not token:
            return JSONResponse(
                {"error": "No OAuth token. Connect Google Drive first."},
                status_code=400,
            )

        from oncofiles.oauth import is_token_expired, refresh_access_token

        access_token = token.access_token
        if is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None):
            refreshed = refresh_access_token(token.refresh_token)
            access_token = refreshed["access_token"]
            new_expiry = datetime.now(UTC) + timedelta(seconds=refreshed.get("expires_in", 3600))
            token.access_token = access_token
            token.token_expiry = new_expiry
            await db_inst.upsert_oauth_token(token)

        gdrive = GDriveClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )

        # Create new folder if needed
        if not folder_id and folder_name:
            folder_id = await asyncio.to_thread(gdrive.create_folder, folder_name, "root")

        # Validate folder exists before persisting
        try:
            meta = await asyncio.to_thread(
                lambda: (
                    gdrive._service.files()
                    .get(fileId=folder_id, fields="id,name,mimeType")
                    .execute()
                )
            )
            if meta.get("mimeType") != "application/vnd.google-apps.folder":
                return JSONResponse(
                    {"error": f"ID is not a folder (type: {meta.get('mimeType')})"},
                    status_code=400,
                )
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            if status == 404:
                return JSONResponse(
                    {"error": f"Folder '{folder_id}' not found. Check the ID."},
                    status_code=404,
                )
            return JSONResponse({"error": f"Cannot access folder: {exc}"}, status_code=400)

        # Persist folder choice (including display name from metadata)
        cached_folder_name = meta.get("name") if meta else None
        await db_inst.update_oauth_folder(
            patient_id, "google", folder_id, folder_name=cached_folder_name
        )

        # Clear folder-invalid flag so sync resumes immediately
        _folder_404_counts.pop(patient_id, None)
        _patient_clients_cache.pop(patient_id, None)

        # Detect owner email
        owner_email = await asyncio.to_thread(gdrive.get_folder_owner, folder_id)
        if owner_email:
            await db_inst.update_oauth_owner_email(patient_id, "google", owner_email)
            gdrive.owner_email = owner_email

        # Create category subfolders
        from oncofiles.gdrive_folders import ALL_FOLDERS, bilingual_name

        created = []
        skipped = []
        for en_key in ALL_FOLDERS:
            display = bilingual_name(en_key)
            existing = await asyncio.to_thread(gdrive.find_folder, display, folder_id)
            if existing:
                skipped.append(display)
                continue
            await asyncio.to_thread(gdrive.create_folder, display, folder_id)
            created.append(display)

        return JSONResponse(
            {
                "status": "ok",
                "folder_id": folder_id,
                "owner_email": owner_email,
                "subfolders_created": len(created),
                "subfolders_skipped": len(skipped),
            }
        )
    except Exception:
        logger.exception("API gdrive-set-folder error")
        return JSONResponse({"error": "internal error"}, status_code=500)


_SHARE_CODE_EXPIRY = 600  # 10 minutes


@mcp.custom_route("/api/share-link", methods=["POST"])
async def api_create_share_link(request: Request) -> JSONResponse:
    """Generate a one-time setup code for sharing MCP connection info.

    Body: {patient_id}
    Returns: {code, expires_in}
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    rate_err = _check_rate_limit("share-link")
    if rate_err:
        return rate_err

    try:
        body = await request.json()
        patient_id = body.get("patient_id", "").strip().lower()
        if not patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient = await db_inst.get_patient(patient_id)
        if not patient:
            return JSONResponse(
                {"error": f"Patient '{patient_id}' not found"},
                status_code=404,
            )

        # Get the latest active token for this patient
        tokens = await db_inst.list_patient_tokens(patient_id)
        if not tokens:
            return JSONResponse(
                {"error": "No tokens for this patient. Create one first."},
                status_code=400,
            )

        # Generate a fresh token so we can share the plaintext
        bearer_token = await db_inst.create_patient_token(patient_id, label="share-link")

        # Generate 6-char uppercase code
        import secrets

        code = secrets.token_hex(8).upper()  # 16 hex chars

        # Purge expired codes
        now = time.time()
        expired = [k for k, v in _share_codes.items() if now - v["created_at"] > _SHARE_CODE_EXPIRY]
        for k in expired:
            del _share_codes[k]

        _share_codes[code] = {
            "patient_id": patient_id,
            "bearer_token": bearer_token,
            "patient_name": patient.display_name,
            "created_at": now,
        }

        return JSONResponse(
            {
                "code": code,
                "expires_in": _SHARE_CODE_EXPIRY,
            }
        )
    except Exception:
        logger.exception("API share-link create error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/share-link/{code}", methods=["GET"])
async def api_redeem_share_link(request: Request) -> JSONResponse:
    """Redeem a one-time setup code. Public (no auth). Returns connection info."""
    rate_err = _check_rate_limit("share-redeem", request=request)
    if rate_err:
        return rate_err
    code = request.path_params.get("code", "").upper()
    if not code or code not in _share_codes:
        return JSONResponse({"error": "Invalid or expired setup code."}, status_code=404)

    entry = _share_codes[code]
    if time.time() - entry["created_at"] > _SHARE_CODE_EXPIRY:
        del _share_codes[code]
        return JSONResponse({"error": "Setup code has expired."}, status_code=410)

    # One-time use: delete after redemption
    del _share_codes[code]

    # Determine MCP URL
    mcp_url = "https://oncofiles.com/mcp"

    return JSONResponse(
        {
            "patient_name": entry["patient_name"],
            "mcp_url": mcp_url,
            "bearer_token": entry["bearer_token"],
            "instructions": {
                "claude_ai": (
                    "In Claude.ai: Project Settings > Connectors > "
                    "Add MCP server > paste the URL and token above."
                ),
                "chatgpt": (
                    "In ChatGPT: Settings > Developer Mode > MCP > "
                    "Add server > paste the URL and token above."
                ),
                "claude_desktop": {
                    "mcpServers": {
                        "oncofiles": {
                            "url": mcp_url,
                            "headers": {"Authorization": (f"Bearer {entry['bearer_token']}")},
                        }
                    }
                },
                "claude_code": (
                    f"claude mcp add oncofiles {mcp_url} "
                    f"--header 'Authorization: Bearer {entry['bearer_token']}'"
                ),
            },
        }
    )


@mcp.custom_route("/oauth/authorize/{service}", methods=["GET"])
async def oauth_authorize(request: Request) -> JSONResponse:
    """Redirect to Google OAuth for a specific service (drive, gmail, calendar)."""
    from starlette.responses import RedirectResponse

    from oncofiles.config import GOOGLE_OAUTH_CLIENT_ID
    from oncofiles.oauth import (
        ALL_SCOPES,
        CALENDAR_SCOPES,
        GMAIL_SCOPES,
        SCOPES,
        get_auth_url_for_scopes,
    )

    if not GOOGLE_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "OAuth not configured"}, status_code=500)

    service = request.path_params.get("service", "drive")
    raw_pid = request.query_params.get("patient_id", "").strip().lower()
    if not raw_pid:
        return JSONResponse({"error": "patient_id required"}, status_code=400)
    # Resolve slug → UUID (e.g. "erika" → UUID); pass through if already UUID
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await db_inst.resolve_patient_id(raw_pid) or raw_pid
    except Exception:
        logger.error("OAuth connect: patient resolution failed for '%s'", raw_pid, exc_info=True)
        patient_id = raw_pid
    scope_map = {
        "drive": SCOPES,
        "gmail": GMAIL_SCOPES,
        "calendar": CALENDAR_SCOPES,
        "all": ALL_SCOPES,
    }
    scopes = scope_map.get(service, SCOPES)

    from oncofiles.oauth import _make_state_token

    state = _make_state_token(patient_id=patient_id)
    auth_url = get_auth_url_for_scopes(scopes, state=state)
    return RedirectResponse(auth_url)


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> JSONResponse:
    """Handle Google OAuth 2.0 redirect callback."""
    from oncofiles.models import OAuthToken
    from oncofiles.oauth import exchange_code, verify_state_token

    # Validate CSRF state parameter and extract patient_id
    state = request.query_params.get("state", "")
    valid, patient_id = verify_state_token(state)
    if not valid or not patient_id:
        return JSONResponse({"error": "Invalid or expired state parameter."}, status_code=400)

    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "Missing authorization code"}, status_code=400)

    try:
        tokens = exchange_code(code)
    except Exception:
        logger.exception("OAuth token exchange failed")
        return JSONResponse({"error": "Token exchange failed. Please try again."}, status_code=500)

    from oncofiles.oauth import parse_granted_scopes

    expiry = datetime.now(UTC) + timedelta(seconds=tokens.get("expires_in", 3600))
    new_scopes = parse_granted_scopes(tokens)

    # Merge with existing scopes so incremental auth doesn't drop prior grants
    db = request.app.state.fastmcp_server._lifespan_result["db"]
    existing_token = await db.get_oauth_token(patient_id=patient_id)
    if existing_token and existing_token.granted_scopes:
        existing_scopes = json.loads(existing_token.granted_scopes)
        merged_scopes = sorted(set(existing_scopes) | set(new_scopes))
    else:
        merged_scopes = new_scopes

    oauth_token = OAuthToken(
        patient_id=patient_id,
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", ""),
        token_expiry=expiry,
        granted_scopes=json.dumps(merged_scopes),
        gdrive_folder_id=existing_token.gdrive_folder_id if existing_token else "",
        owner_email=existing_token.owner_email if existing_token else "",
    )

    await db.upsert_oauth_token(oauth_token)

    return HTMLResponse(
        """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Connected — Oncofiles</title>
<style>
  body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0;
         display: flex; align-items: center; justify-content: center; min-height: 100vh;
         margin: 0; }
  .card { text-align: center; max-width: 420px; padding: 2.5rem; }
  .icon { font-size: 3rem; margin-bottom: 1rem; }
  h1 { color: #14b8a6; font-size: 1.5rem; margin: 0 0 0.75rem; }
  p { color: #94a3b8; line-height: 1.6; margin: 0 0 1.5rem; }
  a { color: #14b8a6; text-decoration: none; padding: 0.6rem 1.5rem;
      border: 1px solid #14b8a6; border-radius: 6px; display: inline-block; }
  a:hover { background: #14b8a6; color: #0f172a; }
</style>
</head>
<body>
<div class="card">
  <div class="icon">&#x2705;</div>
  <h1>Google Drive Connected</h1>
  <p>Your account is linked. You can close this tab and return to your AI chat
     or WhatsApp conversation.</p>
  <a href="/dashboard">Open Dashboard</a>
</div>
</body>
</html>"""
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
    integrations,
    lab_trends,
    naming,
    patient,
    prompt_log,
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
prompt_log.register(mcp)
integrations.register(mcp)
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
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware

        mcp.run(
            transport=MCP_TRANSPORT,
            host=MCP_HOST,
            port=MCP_PORT,
            stateless_http=True,  # no server-side sessions — survives Railway deploys (#229)
            middleware=[
                Middleware(
                    CORSMiddleware,
                    allow_origins=[
                        "https://oncofiles.com",
                        "https://claude.ai",
                        "https://chatgpt.com",
                        "https://oncoteam.cloud",
                    ],
                    allow_credentials=True,
                    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
                    allow_headers=["Authorization", "Content-Type", "mcp-protocol-version"],
                ),
            ],
            uvicorn_config={
                "timeout_keep_alive": 120,
                "limit_concurrency": 50,
            },
        )


if __name__ == "__main__":
    main()
