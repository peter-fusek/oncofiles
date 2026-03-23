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
from contextlib import asynccontextmanager
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
from oncofiles.memory import get_rss_mb, is_memory_pressure

logger = logging.getLogger(__name__)

# Stats constants (single source of truth for values that can't be computed at runtime)
TESTS_COUNT = 584

# Global sync semaphore — limits concurrent sync operations (GDrive + Gmail + Calendar)
_sync_semaphore = asyncio.Semaphore(2)


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
    if SYNC_ENABLED and gdrive:
        scheduler, job_tracker = _start_sync_scheduler(
            db, files, gdrive, oauth_folder_id, gmail_client, calendar_client
        )

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
    logger.info("Startup complete — RSS: %.1f MB", get_rss_mb())

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


def _start_sync_scheduler(
    db, files, gdrive, oauth_folder_id, gmail_client=None, calendar_client=None
):
    """Start APScheduler for periodic GDrive sync, Gmail sync, and Calendar sync."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from oncofiles.sync import extract_all_metadata, sync

    sync_timeout = 300  # 5 minutes max for scheduled sync

    _last_sync_time: str | None = None

    async def _run_sync(trigger: str = "scheduled"):
        nonlocal _last_sync_time

        if is_memory_pressure("sync"):
            return

        folder_id = _get_sync_folder_id_from(oauth_folder_id)
        if not folder_id:
            logger.debug("Scheduled sync skipped — no folder ID")
            return

        # Lightweight pre-check: skip heavy sync if no GDrive changes
        if _last_sync_time and trigger == "scheduled" and gdrive:
            try:
                has_changes = await asyncio.to_thread(
                    gdrive.has_changes_since, folder_id, _last_sync_time
                )
                if not has_changes:
                    logger.debug("Sync skipped — no GDrive changes since %s", _last_sync_time)
                    return
            except Exception:
                pass  # On error, proceed with full sync

        async with _sync_semaphore:
            try:
                stats = await asyncio.wait_for(
                    sync(db, files, gdrive, folder_id, trigger=trigger),
                    timeout=sync_timeout,
                )
                _last_sync_time = datetime.now(UTC).isoformat()
                logger.info("Scheduled sync complete: %s (RSS: %.1f MB)", stats, get_rss_mb())
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
                        logger.error("Post-sync enhance failed", exc_info=True)
            except TimeoutError:
                logger.error("Scheduled sync timed out after %ds", sync_timeout)
            except Exception:
                logger.exception("Scheduled sync failed")
            finally:
                from oncofiles.memory import RESTART_THRESHOLD_MB, reclaim_memory

                rss = reclaim_memory("gdrive_sync")
                # Graceful restart if memory is stuck high after reclaim
                if rss > RESTART_THRESHOLD_MB and _sync_semaphore._value >= 1:
                    logger.warning(
                        "Graceful restart: RSS %.1f MB after sync — exiting for Railway restart",
                        rss,
                    )
                    sys.exit(0)

    metadata_timeout = 600  # 10 minutes max for metadata extraction

    async def _run_metadata_extraction():

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
            from oncofiles.memory import reclaim_memory

            reclaim_memory("metadata_extraction")

    housekeeping_timeout = 120  # 2 minutes max for lightweight housekeeping jobs

    async def _run_trash_cleanup():
        try:
            purged = await asyncio.wait_for(
                db.purge_expired_trash(days=30), timeout=housekeeping_timeout
            )
            if purged:
                logger.info("Trash cleanup: purged %d expired documents", purged)
        except TimeoutError:
            logger.error("Trash cleanup timed out after %ds", housekeeping_timeout)
        except Exception:
            logger.exception("Trash cleanup failed")

    async def _run_pipeline_integrity_check():
        """Scheduled check: find and fix any docs stuck in incomplete pipeline state."""
        try:
            all_docs = await db.list_documents(limit=500)
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
                    "Pipeline integrity: %d docs with gaps: %s",
                    len(gaps),
                    ", ".join(f"#{d.id}({'+'.join(g)})" for d, g in gaps[:10]),
                )
                # Auto-fix: trigger enhance for docs missing AI/metadata
                fixable = [d for d, g in gaps if "no_ai" in g or "no_metadata" in g]
                if fixable and gdrive:
                    try:
                        e_stats = await asyncio.wait_for(
                            extract_all_metadata(db, files, gdrive),
                            timeout=metadata_timeout,
                        )
                        if e_stats["processed"] > 0:
                            logger.info("Pipeline integrity auto-fix: %s", e_stats)
                    except Exception:
                        logger.warning("Pipeline integrity auto-fix failed", exc_info=True)
            else:
                logger.info("Pipeline integrity: all %d docs complete", len(all_docs))
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

    async def _log_rss():
        from oncofiles.memory import RESTART_THRESHOLD_MB, reclaim_memory

        rss_mb = get_rss_mb()
        logger.info(
            "Periodic RSS check: %.1f MB (semaphore available: %d/2)",
            rss_mb,
            _sync_semaphore._value,
        )
        # If RSS is elevated, try to reclaim first
        if rss_mb > RESTART_THRESHOLD_MB:
            rss_mb = reclaim_memory("periodic_rss_check")
        # If RSS is still high after reclaim and no syncs are running, restart
        if rss_mb > RESTART_THRESHOLD_MB and _sync_semaphore._value == 2:
            logger.warning(
                "Graceful restart: RSS %.1f MB > %d MB after reclaim — exiting",
                rss_mb,
                RESTART_THRESHOLD_MB,
            )
            sys.exit(0)

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
        _run_prompt_log_cleanup,
        CronTrigger(hour=4, minute=15),  # daily at 4:15 AM (after OAuth cleanup)
        id="prompt_log_cleanup",
        max_instances=1,
    )
    scheduler.add_job(
        _log_rss,
        CronTrigger(hour="*/6", minute=15),  # every 6 hours at :15
        id="rss_monitor",
        max_instances=1,
    )
    scheduler.add_job(
        _run_pipeline_integrity_check,
        CronTrigger(hour="*/6", minute=30),  # every 6 hours at :30
        id="pipeline_integrity",
        max_instances=1,
    )

    async def _run_category_validation():
        """Auto-correct document categories after metadata extraction."""

        async def _do_category_validation():
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

            if corrected:
                logger.info("Category validation: corrected %d documents", corrected)

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
        if not gdrive:
            return

        async def _do_folder_cleanup():
            folder_id = _get_sync_folder_id_from(oauth_folder_id)
            if not folder_id:
                return

            cleaned = 0
            merged = 0

            # Get category folder structure
            _, folder_map = await asyncio.to_thread(gdrive.list_folder_with_structure, folder_id)

            # For each category folder, check subfolders
            for cat_folder_id, cat_name in folder_map.items():
                try:
                    sub_folders_raw = await asyncio.to_thread(
                        lambda fid=cat_folder_id: (
                            gdrive._service.files()
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
                            # Merge: keep first, trash empty duplicates
                            keep = ids[0]
                            for dup in ids[1:]:
                                contents = await asyncio.to_thread(
                                    lambda did=dup: (
                                        gdrive._service.files()
                                        .list(
                                            q=f"'{did}' in parents and trashed = false",
                                            fields="files(id)",
                                            pageSize=1,
                                        )
                                        .execute()
                                    )
                                )
                                if not contents.get("files"):
                                    await asyncio.to_thread(gdrive.trash_file, dup)
                                    merged += 1
                                    logger.info(
                                        "Merged duplicate folder '%s/%s' (%s), keeping %s",
                                        cat_name,
                                        name,
                                        dup,
                                        keep,
                                    )

                    # Check for empty folders
                    for sf in sub_folders:
                        contents = await asyncio.to_thread(
                            lambda sid=sf["id"]: (
                                gdrive._service.files()
                                .list(
                                    q=f"'{sid}' in parents and trashed = false",
                                    fields="files(id)",
                                    pageSize=1,
                                )
                                .execute()
                            )
                        )
                        if not contents.get("files"):
                            await asyncio.to_thread(gdrive.trash_file, sf["id"])
                            cleaned += 1
                            logger.info("Trashed empty folder '%s/%s'", cat_name, sf["name"])

                except Exception:
                    logger.warning("Empty folder check failed for %s", cat_name, exc_info=True)

            if cleaned or merged:
                logger.info(
                    "Folder cleanup: %d empty trashed, %d duplicates merged", cleaned, merged
                )
            else:
                logger.info("Folder cleanup: all folders OK")

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

    # Startup: full sync + enhance + category validation 60s after boot
    from apscheduler.triggers.date import DateTrigger

    async def _startup_catchup():
        """Lightweight startup: import-only sync + category validation.

        Skips the heavy export phase (rename, organize, OCR export) to avoid
        blocking the event loop and causing health check timeouts. The regular
        30-min scheduled sync handles exports.
        """
        from oncofiles.sync import sync_from_gdrive as _sync_from

        folder_id = _get_sync_folder_id_from(oauth_folder_id)
        if not folder_id:
            logger.debug("Startup catchup skipped — no folder ID")
            return

        import time as _time

        sync_id = None
        start = _time.monotonic()
        try:
            sync_id = await db.insert_sync_history(trigger="startup")
        except Exception:
            logger.warning("startup: failed to record sync history", exc_info=True)

        try:
            import_stats = await asyncio.wait_for(
                _sync_from(db, files, gdrive, folder_id, enhance=True),
                timeout=180,  # 3 min max for import
            )
            logger.info("Startup import: %s", import_stats)
            if sync_id:
                await db.complete_sync_history(
                    sync_id,
                    status="completed",
                    duration_s=round(_time.monotonic() - start, 1),
                    from_new=import_stats.get("new", 0),
                    from_updated=import_stats.get("updated", 0),
                    from_errors=import_stats.get("errors", 0),
                    stats_json=str(import_stats),
                )
        except TimeoutError:
            logger.warning("Startup import timed out after 180s")
            if sync_id:
                await db.complete_sync_history(
                    sync_id,
                    status="failed",
                    duration_s=round(_time.monotonic() - start, 1),
                    error_message="Startup import timed out after 180s",
                )
        except Exception as exc:
            logger.exception("Startup import failed")
            if sync_id:
                await db.complete_sync_history(
                    sync_id,
                    status="failed",
                    duration_s=round(_time.monotonic() - start, 1),
                    error_message=str(exc)[:500],
                )

        await _run_category_validation()
        logger.info("Startup catchup complete: import + validate (RSS: %.1f MB)", get_rss_mb())

    startup_time = datetime.now() + timedelta(seconds=60)
    scheduler.add_job(
        _startup_catchup,
        DateTrigger(run_date=startup_time),
        id="startup_catchup",
        max_instances=1,
    )
    logger.info("Startup catchup scheduled for %s", startup_time.strftime("%H:%M:%S"))

    # ── Gmail sync jobs ───────────────────────────────────────────────────
    gmail_sync_timeout = 300  # 5 minutes max

    async def _run_gmail_sync(trigger: str = "scheduled"):
        if not gmail_client:
            return

        if is_memory_pressure("Gmail sync"):
            return

        from oncofiles.gmail_sync import gmail_sync as _gmail_sync

        async with _sync_semaphore:
            try:
                stats = await asyncio.wait_for(
                    _gmail_sync(db, files, gmail_client, initial=False),
                    timeout=gmail_sync_timeout,
                )
                if not stats.get("skipped"):
                    logger.info("Gmail sync (%s): %s", trigger, stats)
            except TimeoutError:
                logger.error("Gmail sync timed out after %ds", gmail_sync_timeout)
            except Exception:
                logger.exception("Gmail sync failed")
            finally:
                from oncofiles.memory import reclaim_memory

                reclaim_memory("gmail_sync")

    async def _startup_gmail_sync():
        if not gmail_client:
            return

        from oncofiles.gmail_sync import gmail_sync as _gmail_sync

        async with _sync_semaphore:
            try:
                stats = await asyncio.wait_for(
                    _gmail_sync(db, files, gmail_client, initial=True),
                    timeout=gmail_sync_timeout,
                )
                logger.info("Gmail initial sync: %s (RSS: %.1f MB)", stats, get_rss_mb())
            except TimeoutError:
                logger.error("Gmail initial sync timed out after %ds", gmail_sync_timeout)
            except Exception:
                logger.exception("Gmail initial sync failed")
            finally:
                from oncofiles.memory import reclaim_memory

                reclaim_memory("gmail_startup_sync")

    if gmail_client:
        scheduler.add_job(
            _run_gmail_sync,
            IntervalTrigger(
                minutes=SYNC_INTERVAL_MINUTES, start_date=datetime.now() + timedelta(minutes=2)
            ),
            id="gmail_sync",
            max_instances=1,
        )
        gmail_startup_time = datetime.now() + timedelta(seconds=90)
        scheduler.add_job(
            _startup_gmail_sync,
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
    _last_calendar_sync_time: str | None = None

    async def _run_calendar_sync(trigger: str = "scheduled"):
        nonlocal _last_calendar_sync_time
        if not calendar_client:
            return

        if is_memory_pressure("Calendar sync"):
            return

        # Lightweight pre-check: skip if no calendar changes since last sync
        if _last_calendar_sync_time and trigger == "scheduled":
            try:
                has_changes = await asyncio.to_thread(
                    calendar_client.has_changes_since, _last_calendar_sync_time
                )
                if not has_changes:
                    logger.debug(
                        "Calendar sync skipped — no changes since %s", _last_calendar_sync_time
                    )
                    return
            except Exception:
                pass  # On error, proceed with full sync

        from oncofiles.calendar_sync import calendar_sync as _calendar_sync

        async with _sync_semaphore:
            try:
                stats = await asyncio.wait_for(
                    _calendar_sync(db, calendar_client, initial=False),
                    timeout=calendar_sync_timeout,
                )
                if not stats.get("skipped"):
                    logger.info("Calendar sync (%s): %s", trigger, stats)
                _last_calendar_sync_time = datetime.now(UTC).isoformat()
            except TimeoutError:
                logger.error("Calendar sync timed out after %ds", calendar_sync_timeout)
            except Exception:
                logger.exception("Calendar sync failed")
            finally:
                from oncofiles.memory import reclaim_memory

                reclaim_memory("calendar_sync")

    async def _startup_calendar_sync():
        if not calendar_client:
            return

        from oncofiles.calendar_sync import calendar_sync as _calendar_sync

        async with _sync_semaphore:
            try:
                stats = await asyncio.wait_for(
                    _calendar_sync(db, calendar_client, initial=True),
                    timeout=calendar_sync_timeout,
                )
                logger.info("Calendar initial sync: %s (RSS: %.1f MB)", stats, get_rss_mb())
            except TimeoutError:
                logger.error("Calendar initial sync timed out after %ds", calendar_sync_timeout)
            except Exception:
                logger.exception("Calendar initial sync failed")
            finally:
                from oncofiles.memory import reclaim_memory

                reclaim_memory("calendar_startup_sync")

    if calendar_client:
        scheduler.add_job(
            _run_calendar_sync,
            IntervalTrigger(
                minutes=SYNC_INTERVAL_MINUTES, start_date=datetime.now() + timedelta(minutes=3)
            ),
            id="calendar_sync",
            max_instances=1,
        )
        calendar_startup_time = datetime.now() + timedelta(seconds=120)
        scheduler.add_job(
            _startup_calendar_sync,
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

    scheduler.start()
    logger.info("Sync scheduler started — every %d min", SYNC_INTERVAL_MINUTES)
    return scheduler, job_tracker


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


# ── Static assets ─────────────────────────────────────────────────────────────

_FAVICON_SVG: str | None = None


@mcp.custom_route("/favicon.svg", methods=["GET"])
async def favicon_svg(request: Request) -> HTMLResponse:
    global _FAVICON_SVG  # noqa: PLW0603
    if _FAVICON_SVG is None:
        _FAVICON_SVG = (Path(__file__).parent / "favicon.svg").read_text()
    return HTMLResponse(_FAVICON_SVG, media_type="image/svg+xml")


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
        f"- 15 medical document categories (labs, imaging, pathology, genetics, etc.)\n"
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
        f"- Repository: https://github.com/instarea-sk/oncofiles\n"
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
        f"- GitHub: https://github.com/instarea-sk/oncofiles\n",
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
    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db: Database = lifespan_ctx["db"]
        reconnected = await db.reconnect_if_stale()
        result: dict = {"status": "ok", "version": VERSION}
        commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")[:7]
        if commit:
            result["commit"] = commit
        started_at = lifespan_ctx.get("started_at")
        if started_at:
            result["uptime_s"] = int((datetime.now(UTC) - started_at).total_seconds())
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
        logger.exception("Health check failed")
        return JSONResponse({"status": "degraded", "version": VERSION}, status_code=503)


def _count_tools() -> int:
    """Count registered MCP tools dynamically."""
    try:
        return sum(1 for k in mcp._local_provider._components if k.startswith("tool:"))
    except Exception:
        return 65  # fallback


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    """Public project stats for landing page and llms.txt."""
    from oncofiles.models import DocumentCategory

    return JSONResponse(
        {
            "tools": _count_tools(),
            "categories": len(DocumentCategory),
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

        from oncofiles.filename_parser import is_standard_format

        doc_count = await db.count_documents()
        sync_stats = await db.get_sync_stats_summary()
        recent_syncs = await db.get_sync_history(limit=5)

        # Document health summary
        all_docs = await db.list_documents(limit=500)
        doc_health = {
            "total": len(all_docs),
            "with_ai": sum(1 for d in all_docs if d.ai_summary),
            "with_metadata": sum(
                1 for d in all_docs if d.structured_metadata and d.structured_metadata != ""
            ),
            "with_date": sum(1 for d in all_docs if d.document_date),
            "with_institution": sum(1 for d in all_docs if d.institution),
            "synced": sum(1 for d in all_docs if d.gdrive_id),
            "standard_named": sum(1 for d in all_docs if is_standard_format(d.filename)),
        }

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
                "document_health": doc_health,
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
    """Create an HMAC-signed session token: email.expiry.signature."""
    if not MCP_BEARER_TOKEN:
        raise ValueError("Cannot create session token: MCP_BEARER_TOKEN not configured")
    expiry = str(int(time.time()) + _SESSION_MAX_AGE)
    key = MCP_BEARER_TOKEN.encode()
    payload = f"{email}.{expiry}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def _verify_session_token(token: str) -> str | None:
    """Verify a session token. Returns email if valid, None otherwise."""
    if not token or not MCP_BEARER_TOKEN:
        return None
    parts = token.rsplit(".", 2)
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
    expected = hmac.new(key, f"{email}.{expiry_str}".encode(), hashlib.sha256).hexdigest()[:32]
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


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard page. Open to all — auth happens client-side via Google Sign-In."""
    return HTMLResponse(_load_dashboard_html())


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

    if email not in DASHBOARD_ALLOWED_EMAILS:
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
        filter_param = request.query_params.get("filter", "all")
        try:
            limit = min(int(request.query_params.get("limit", "200")), 200)
        except (ValueError, TypeError):
            limit = 200
        result = await _build_document_matrix(db, filter_param=filter_param, limit=limit)
        return JSONResponse(result)
    except Exception:
        logger.exception("API documents endpoint error")
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
        gdrive = lifespan_ctx.get("gdrive")
        folder_id = lifespan_ctx.get("gdrive_folder_id", "")
        if not gdrive or not folder_id:
            return JSONResponse({"error": "GDrive not configured"}, status_code=503)
        result = await _build_reconciliation_report(db, gdrive, folder_id)
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

        entries = await db_inst.search_prompt_log(query)
        stats = await db_inst.get_prompt_log_stats()

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
                "status": e.status,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in entries
        ]
        return JSONResponse({"entries": items, "stats": stats})
    except Exception:
        logger.exception("API prompt-log endpoint error")
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

        prompt_stats, tool_stats, pipeline_stats, latency = await asyncio.gather(
            db_inst.get_prompt_stats(days=days),
            db_inst.get_tool_usage_stats(days=days),
            db_inst.get_pipeline_stats(),
            db_inst.get_prompt_latency_percentiles(days=days),
        )

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
    scope_map = {
        "drive": SCOPES,
        "gmail": GMAIL_SCOPES,
        "calendar": CALENDAR_SCOPES,
        "all": ALL_SCOPES,
    }
    scopes = scope_map.get(service, SCOPES)
    auth_url = get_auth_url_for_scopes(scopes)
    return RedirectResponse(auth_url)


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

    from oncofiles.oauth import parse_granted_scopes

    expiry = datetime.now(UTC) + timedelta(seconds=tokens.get("expires_in", 3600))
    new_scopes = parse_granted_scopes(tokens)

    # Merge with existing scopes so incremental auth doesn't drop prior grants
    db = request.app.state.fastmcp_server._lifespan_result["db"]
    existing_token = await db.get_oauth_token()
    if existing_token and existing_token.granted_scopes:
        existing_scopes = json.loads(existing_token.granted_scopes)
        merged_scopes = sorted(set(existing_scopes) | set(new_scopes))
    else:
        merged_scopes = new_scopes

    oauth_token = OAuthToken(
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", ""),
        token_expiry=expiry,
        granted_scopes=json.dumps(merged_scopes),
    )

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
