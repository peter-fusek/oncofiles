"""FastMCP server for Oncofiles medical document management."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from oncofiles import mailer, scheduler_singleton, session_revocation
from oncofiles.cloud_guard import local_only_task
from oncofiles.config import (
    AI_NIGHT_WINDOW_START_UTC,
    AI_NIGHTLY_ONLY,
    AI_RUN_ON_STARTUP,
    DAILY_AI_DOC_CAP,
    DASHBOARD_ADMIN_EMAILS,
    DATABASE_PATH,
    DOC_QUERY_LIMIT,
    ENABLE_INTEGRITY_CHECK,
    GOOGLE_DRIVE_FOLDER_ID,
    GOOGLE_INTEGRATION_ENABLED,
    GOOGLE_OAUTH_CLIENT_ID,
    GOOGLE_OAUTH_CLIENT_SECRET,
    LOG_LEVEL,
    MAX_DOCUMENTS_PER_PATIENT,
    MCP_BEARER_TOKEN,
    MCP_HOST,
    MCP_PORT,
    MCP_TRANSPORT,
    SYNC_ENABLED,
    SYNC_INTERVAL_MINUTES,
    SYNC_STALE_ALERT_HOURS,
    VERSION,
    gmail_calendar_enabled,
)
from oncofiles.database import Database
from oncofiles.gdrive_client import GDriveClient, create_gdrive_client
from oncofiles.memory import (
    get_rss_mb,
    get_rss_trend,
    get_semaphore_status,
    init_rss_tracking,
    is_memory_pressure,
)
from oncofiles.network_errors import NETWORK_UNAVAILABLE, is_network_unavailable
from oncofiles.secrets_keys import dashboard_session_key

logger = logging.getLogger(__name__)

CONF_MAX_DOCS = MAX_DOCUMENTS_PER_PATIENT

# Stats constants (single source of truth for values that can't be computed at runtime)
TESTS_COUNT = 716


def _assert_local_database(environ: Mapping[str, str] | None = None) -> None:
    """Fail closed (#617): the master store is the local SQLite file and the
    Turso/libsql code is gone. A leftover ``TURSO_*`` variable means someone
    expects a cloud database; refuse to start rather than guess."""
    env = os.environ if environ is None else environ
    leftover = sorted(k for k, v in env.items() if k.startswith("TURSO_") and v.strip())
    if leftover:
        raise RuntimeError(
            f"{', '.join(leftover)} set, but Oncofiles runs on a local SQLite "
            "file only (Turso support was removed in #617). Unset every TURSO_* "
            "variable; the database path is DATABASE_PATH."
        )


# Global sync semaphore — limits concurrent sync operations (GDrive + Gmail + Calendar)
_sync_semaphore = asyncio.Semaphore(1)  # serialize all patient-touching jobs (single DB connection)

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
    "dashboard-verify": 10,  # max 10 Google sign-in attempts per minute
    "dashboard-logout": 30,  # max 30 logouts per minute (#510)
    "oauth-authorize": 10,  # max 10 OAuth starts per minute
    "oauth-callback": 10,  # max 10 OAuth callbacks per minute
}


def _check_rate_limit(key: str, *, request: Request | None = None) -> JSONResponse | None:
    """Check rate limit for a given key. Per-IP for share-redeem, global otherwise.

    The bucket holds the timestamps of allowed requests within the rolling
    window. Each call: filter out expired timestamps, then either 429 (bucket
    full) or record the new request and persist. The previous shape (#519)
    short-circuited on an empty bucket and returned BEFORE recording — that
    means the first request in any fresh window was never counted, so the
    bucket stayed empty forever and the limit never engaged. The fix is
    twofold: always record the allowed request, and persist the bucket on
    every code path so the next call sees the count.
    """
    limit = _RATE_LIMITS.get(key, 60)
    # Per-IP scoping for brute-force-sensitive endpoints
    rate_key = key
    if request and key == "share-redeem":
        client_ip = request.client.host if request.client else "unknown"
        rate_key = f"{key}:{client_ip}"
    now = time.time()
    bucket = [t for t in _rate_limits.get(rate_key, []) if now - t < _RATE_WINDOW]
    if len(bucket) >= limit:
        # Keep the full bucket so subsequent calls in the window also see 429.
        _rate_limits[rate_key] = bucket
        return JSONResponse(
            {"error": "Rate limit exceeded. Try again in a minute."},
            status_code=429,
        )
    bucket.append(now)
    _rate_limits[rate_key] = bucket
    return None


def _check_bearer(request: Request) -> JSONResponse | None:
    """Validate `Authorization: Bearer` header against MCP_BEARER_TOKEN.

    Fail-closed: on HTTP transports, returns 401 when either MCP_BEARER_TOKEN
    is unset OR the request header does not match. stdio is a local-IPC trust
    boundary and remains open with no bearer required.

    Returns None on success, JSONResponse(401) on failure.

    Security note (#484 / #485): prior to this fix, `not MCP_BEARER_TOKEN`
    returned None (authorized) — which cascaded through `_check_dashboard_auth`
    and `_get_dashboard_patient_id(is_bearer=True)` to bypass all per-caregiver
    patient scoping if the env var was ever missing on an HTTP deploy.
    """
    # stdio: local IPC trust boundary — no bearer required
    if MCP_TRANSPORT == "stdio":
        return None
    # HTTP: require both a configured server token AND a matching request header
    if not MCP_BEARER_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
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

# Fail-closed startup gate (#485): an HTTP deploy with no MCP_BEARER_TOKEN and
# no OAuth provider is effectively open — refuse to boot instead of logging a
# warning. streamable-http always gets PersistentOAuthProvider, but a future
# transport option could slip through; this assertion is the backstop.
if MCP_TRANSPORT != "stdio" and not MCP_BEARER_TOKEN and auth is None:
    raise RuntimeError(
        f"No authentication configured for transport={MCP_TRANSPORT}. "
        "Set MCP_BEARER_TOKEN or use an auth-providing transport (streamable-http)."
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
    # #706: survives container recreates when APP_LOG_DIR is set (off locally).
    # Every root handler (stderr and the file) carries the same redaction filter.
    from oncofiles.app_log import (
        attach_access_log_filter,
        attach_redaction,
        file_handler_from_env,
    )

    root = logging.getLogger()
    for h in root.handlers:
        attach_redaction(h)
    # #970 infra review: uvicorn's access log never carries a /q/<token> link
    # or a query string (masked on the logger, before any handler).
    attach_access_log_filter()
    handler = file_handler_from_env(fmt, level)
    if handler is not None:
        root.addHandler(handler)


@asynccontextmanager
async def lifespan(server: FastMCP) -> AsyncIterator[dict]:
    """Initialize database and Files API client on startup."""
    from oncofiles.files_api import FilesClient

    _setup_logging()
    init_rss_tracking()
    started_at = datetime.now(UTC)
    deploy_id = os.environ.get("RAILWAY_DEPLOYMENT_ID", "")
    git_sha = os.environ.get("ONCOFILES_GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    logger.info(
        "Starting Oncofiles MCP server (transport=%s, deploy=%s, commit=%s)",
        MCP_TRANSPORT,
        deploy_id[:12] or "local",
        git_sha[:7] or "dev",
    )
    # Where the data lives, stated at every start: PHI stays on this host (#617).
    _assert_local_database()
    logger.info("Database mode: local file")
    db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    # #510: rehydrate the in-memory dashboard-session revocation set from
    # the persisted table so revocations survive Railway restarts.
    try:
        await session_revocation.load_from_db(db.db)
        await session_revocation.purge_expired(db.db)
    except Exception:
        logger.warning("Session revocation rehydrate failed — continuing", exc_info=True)
    # Hydrate the per-patient context cache for all active patients, by pid.
    # There is no legacy/default patient any more (#733).
    from oncofiles import patient_context

    await patient_context.initialize(db)
    files = FilesClient()
    # Restore MCP OAuth sessions from DB (survive deploys)
    if hasattr(auth, "set_db"):
        auth.set_db(db)
        await auth.restore_from_db()
    # Load owner_email from OAuth tokens (needed for service account permission sharing)
    oauth_folder_id = ""
    owner_email = ""
    token = None
    # Resolve primary patient UUID from slug (post-migration, patient_id is a UUID)
    _primary = await db.get_patient_by_slug("q1b")
    _primary_uuid = _primary.patient_id if _primary else ""
    if not GOOGLE_INTEGRATION_ENABLED:
        # #552: leaving `token` as None short-circuits the OAuth GDrive block,
        # the Gmail/Calendar client builds (all guarded by `if token`) and,
        # together with the service-account guard below, every Google call.
        logger.warning(
            "GOOGLE_INTEGRATION_ENABLED=false — Drive/Gmail/Calendar disabled. "
            "No Google API calls will be attempted. MCP, dashboard and local "
            "housekeeping continue normally."
        )
    else:
        try:
            token = await db.get_oauth_token(patient_id=_primary_uuid) if _primary_uuid else None
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
                await db.update_oauth_access_token(
                    token.patient_id, token.provider, access_token, new_expiry
                )

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

    # Build Gmail/Calendar clients if scopes granted (never when the flag is off, #746)
    if token and GOOGLE_OAUTH_CLIENT_ID:
        try:
            gmail_client, calendar_client = _build_gmail_calendar_clients(token, access_token)
            if gmail_client:
                logger.info("Gmail client initialized from OAuth tokens")
            if calendar_client:
                logger.info("Calendar client initialized from OAuth tokens")
        except Exception:
            logger.warning("Gmail/Calendar client init failed", exc_info=True)

    # Fall back to service account if no OAuth. Skipped entirely when the
    # kill switch is off (#552) — the service account is the path that
    # produced the "Project #... has been deleted" 403 every night.
    if not gdrive and GOOGLE_INTEGRATION_ENABLED:
        try:
            gdrive = create_gdrive_client(owner_email=owner_email)
        except Exception as e:
            logger.warning("GDrive client init failed: %s — fallback disabled", e)

    # Curated folder (#562): block legacy writes on the default-patient client too.
    _startup_sync_folder = _get_sync_folder_id_from(oauth_folder_id)
    if gdrive is not None and _startup_sync_folder:
        try:
            await _mark_patient_curated(_primary_uuid, gdrive, _startup_sync_folder)
        except Exception:
            logger.warning("curated-folder check failed at startup", exc_info=True)

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
            primary_uuid=_primary_uuid,
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

    # Curated-folder nightly runs (#573): independent of SYNC_ENABLED/APScheduler.
    # Cloud guard for curated documents (#603): every AI path refuses their ids.
    from oncofiles.curated.indexer import (
        mark_guard_failed,
        mark_guard_ready,
        register_curated_ids,
    )
    from oncofiles.tools.curated import (
        continuous_settings,
        curated_nightly_loop,
        nightly_settings,
    )

    try:
        register_curated_ids(await db.curated_document_ids())
        mark_guard_ready()
    except Exception:
        # Fail closed: refuse every cloud-AI call on documents until it loads.
        mark_guard_failed()
        logger.exception("curated: guard not loaded — cloud AI refused for documents")

    # #856: erase copies older than 30 days go daily at 04:35 UTC. Independent
    # of SYNC_ENABLED/APScheduler (prod runs SYNC_ENABLED=false, #784) and of
    # Google; this loop is the job's only owner, one per process.
    from oncofiles.erase_retention import JOB_ID as ERASE_RETENTION_JOB
    from oncofiles.erase_retention import erase_retention_loop

    erase_retention_task, _ = scheduler_singleton.ensure_background_loop(
        ERASE_RETENTION_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== ERASE_RETENTION_JOB, tested)
            local_only_task("pre_store_erase_retention", erase_retention_loop)(_sync_semaphore)
        ),
    )

    # #999 / #950: the holding mover (phase three) and reader (phase two) were
    # APScheduler jobs, which never start in prod (SYNC_ENABLED=false). These
    # loops are now their only periodic owner (first run after the startup
    # grace); the event-driven kicks stay. One per process.
    from oncofiles.holding_loops import EXTRACT_JOB_ID as HOLDING_EXTRACT_JOB
    from oncofiles.holding_loops import MOVE_JOB_ID as HOLDING_MOVE_JOB
    from oncofiles.holding_loops import holding_extract_loop, holding_move_loop

    holding_move_task, _ = scheduler_singleton.ensure_background_loop(
        HOLDING_MOVE_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== HOLDING_MOVE_JOB, tested)
            local_only_task("holding_move", holding_move_loop)(db, _sync_semaphore)
        ),
    )
    holding_extract_task, _ = scheduler_singleton.ensure_background_loop(
        HOLDING_EXTRACT_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== HOLDING_EXTRACT_JOB, tested)
            local_only_task("holding_extract", holding_extract_loop)(db)
        ),
    )

    # #999 / #952: the inbound queue sweep (claimed-row DKIM verification, the
    # durable queue, then the quarantine expiry / purge / digests) was an
    # APScheduler job, which never starts in prod. This loop is its only
    # periodic owner (first run after the startup grace, under _sync_semaphore);
    # the Brevo route's kick after an enqueue stays. One per process.
    from oncofiles.inbound_loop import JOB_ID as INBOUND_QUEUE_JOB
    from oncofiles.inbound_loop import inbound_queue_loop

    inbound_queue_task, _ = scheduler_singleton.ensure_background_loop(
        INBOUND_QUEUE_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== INBOUND_QUEUE_JOB, tested)
            local_only_task("inbound_queue", inbound_queue_loop)(db, _sync_semaphore)
        ),
    )

    # #999 / #868: the access log retention (daily 04:25 UTC, 90-day access
    # telemetry) was an APScheduler job, which never starts in prod. This loop
    # is its only owner (startup grace, catch-up past the slot, once per UTC
    # day, under _sync_semaphore). One per process.
    from oncofiles.access_log_loop import JOB_ID as ACCESS_LOG_RETENTION_JOB
    from oncofiles.access_log_loop import access_log_retention_loop

    access_log_task, _ = scheduler_singleton.ensure_background_loop(
        ACCESS_LOG_RETENTION_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== ACCESS_LOG_RETENTION_JOB, tested)
            local_only_task("dashboard_access_log_retention", access_log_retention_loop)(
                db, _sync_semaphore
            )
        ),
    )

    # #999: the MCP OAuth token cleanup (daily 04:00 UTC, expired rows past a
    # 30-day grace) was an APScheduler job, which never starts in prod. This
    # loop is its only owner (startup grace, catch-up past the slot, once per
    # UTC day, under _sync_semaphore). One per process.
    from oncofiles.oauth_cleanup_loop import JOB_ID as OAUTH_TOKEN_CLEANUP_JOB
    from oncofiles.oauth_cleanup_loop import oauth_token_cleanup_loop

    oauth_cleanup_task, _ = scheduler_singleton.ensure_background_loop(
        OAUTH_TOKEN_CLEANUP_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== OAUTH_TOKEN_CLEANUP_JOB, tested)
            local_only_task("oauth_token_cleanup", oauth_token_cleanup_loop)(db, _sync_semaphore)
        ),
    )

    # #999: the memory check (every 5 min, graceful restart above the RSS
    # threshold) was an APScheduler job, which never starts in prod, leaving
    # only the /health hard ceiling. This loop is its only owner (first run
    # 7 min after start as before, #366; no DB, no semaphore). One per process.
    from oncofiles.memory_check_loop import JOB_ID as MEMORY_CHECK_JOB
    from oncofiles.memory_check_loop import memory_check_loop

    memory_check_task, _ = scheduler_singleton.ensure_background_loop(
        MEMORY_CHECK_JOB,
        lambda: asyncio.create_task(
            # literal job name: the #925 lock reads it (== MEMORY_CHECK_JOB, tested)
            local_only_task("memory_check", memory_check_loop)()
        ),
    )

    curated_nightly_task = None
    if nightly_settings()[0] and GOOGLE_INTEGRATION_ENABLED:
        # #784: one curated loop per process; a second lifespan gets the same task.
        curated_nightly_task, loop_started = scheduler_singleton.ensure_curated_loop(
            # #925: the loop (continuous turns + nightly sweep) is a local-only zone.
            lambda: asyncio.create_task(local_only_task("curated loop", curated_nightly_loop)(db))
        )
        if loop_started:
            cont, poll_s, settle_s, chunk = continuous_settings()
            logger.info(
                "Curated loop started (nightly sweep hour %s Europe/Bratislava; continuous %s)",
                nightly_settings()[1] if nightly_settings()[1] is not None else "every",
                f"on: probe {poll_s}s, settle {settle_s}s, {chunk} docs/run" if cont else "off",
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
            "transport": MCP_TRANSPORT,
        }
    finally:
        # #784: shared singletons stop only when their last holder exits.
        _release_curated_loop(curated_nightly_task)
        if scheduler_singleton.release_background_loop(ERASE_RETENTION_JOB, erase_retention_task):
            erase_retention_task.cancel()
        for _name, _task in (
            (HOLDING_MOVE_JOB, holding_move_task),
            (HOLDING_EXTRACT_JOB, holding_extract_task),
            (INBOUND_QUEUE_JOB, inbound_queue_task),
            (ACCESS_LOG_RETENTION_JOB, access_log_task),
            (OAUTH_TOKEN_CLEANUP_JOB, oauth_cleanup_task),
            (MEMORY_CHECK_JOB, memory_check_task),
        ):
            if scheduler_singleton.release_background_loop(_name, _task):
                _task.cancel()
        if scheduler and scheduler_singleton.release_scheduler(scheduler):
            scheduler.shutdown(wait=False)
            logger.info("Sync scheduler stopped")
        # Close AI client to release httpx connection pool
        import oncofiles.enhance

        if oncofiles.enhance._shared_client:
            oncofiles.enhance._shared_client.close()
            oncofiles.enhance._shared_client = None
        if files:
            files.close()
        await db.close()


# Cache of per-patient API clients — avoids re-creating HTTP pools every 5 min.
# Key: patient_id, Value: (gdrive, gmail, calendar, folder_id, created_at)
_patient_clients_cache: dict[str, tuple] = {}
_CLIENT_CACHE_TTL = 1800  # 30 min — refresh token changes invalidate sooner

# Track consecutive GDrive folder 404 failures per patient.
# After _FOLDER_404_THRESHOLD consecutive 404s, skip sync until folder is re-set.
_folder_404_counts: dict[str, int] = {}
_FOLDER_404_THRESHOLD = 3

# Track (patient_id, service) combinations with dead refresh tokens (#415).
# Once an invalid_grant is seen, log once, skip further syncs, and surface on
# /health so the dashboard / caregiver can trigger a re-auth. Cleared by
# successful OAuth callback for that patient+service.
_invalid_grant_tokens: set[tuple[str, str]] = set()


def _is_invalid_grant(exc: BaseException) -> bool:
    """Detect google.auth.exceptions.RefreshError('invalid_grant', ...)."""
    name = type(exc).__name__
    if name != "RefreshError":
        return False
    return "invalid_grant" in str(exc).lower()


# Track (patient_id, service) pairs whose Google credentials are *structurally*
# broken — the GCP project, the API enablement or the OAuth client itself is
# gone (#552). Distinct from _invalid_grant_tokens: a caregiver can fix an
# invalid_grant by re-authing, but nobody can re-auth their way out of a
# deleted project. These need operator action, so they get their own surface
# on /health (counts) and /readiness (detail).
_hard_auth_failures: dict[tuple[str, str], dict[str, str]] = {}

# #979: why the LAST _create_patient_clients call for a patient returned None
# after loading a token (``network_unavailable`` | ``refresh_failed``); cleared
# when a client is built, when the patient has no token and under the kill
# switch. Transient by design: never feeds needs_reauth or _hard_auth_failures.
# Counts per patient (operator /readiness only, never /health).
_client_unavailable: dict[str, str] = {}
_network_unavailable_stats: dict[str, dict] = {}


def _forget_patient_runtime(patient_id: str) -> None:
    """Drop every in-memory Google state of ONE patient (#856).

    After a Drive disconnect or an erasure: the cached Drive / Gmail / Calendar
    clients (they carry the access + refresh token), the folder 404 counter and
    the re-auth / hard-auth / unavailable markers of that patient.
    """
    _patient_clients_cache.pop(patient_id, None)
    _folder_404_counts.pop(patient_id, None)
    _client_unavailable.pop(patient_id, None)
    _network_unavailable_stats.pop(patient_id, None)
    _curated_patients.discard(patient_id)
    for key in [k for k in _invalid_grant_tokens if k[0] == patient_id]:
        _invalid_grant_tokens.discard(key)
    for key in [k for k in _hard_auth_failures if k[0] == patient_id]:
        _hard_auth_failures.pop(key, None)


def _note_client_unavailable(patient_id: str, code: str) -> None:
    _client_unavailable[patient_id] = code
    if code == NETWORK_UNAVAILABLE:
        st = _network_unavailable_stats.setdefault(patient_id, {"count": 0, "last_at": None})
        st["count"] += 1
        st["last_at"] = datetime.now(UTC).isoformat(timespec="seconds")


def patient_client_unavailable_reason(patient_id: str) -> str | None:
    """Code of the last failed client build for this patient, or None (#979)."""
    return _client_unavailable.get(patient_id)


def network_unavailable_stats() -> dict:
    """{patient_id: {count, last_at}} since process start (operator-only)."""
    return {pid: dict(st) for pid, st in _network_unavailable_stats.items()}


# #552: cached output of the sync-staleness watchdog. /health is a liveness
# probe and must never touch the DB, so the hourly `sync_watchdog` job does the
# querying and parks the result here for the endpoints to read.
# Patients whose Drive folder overlaps a curated tree (#562). Filled by
# _mark_patient_curated; the watchdog and the nightly/weekly pipelines skip them
# so a deliberate block never reads as an outage.
_curated_patients: set[str] = set()


async def _mark_patient_curated(patient_id: str, gdrive, folder_id: str) -> bool:
    """Run the (cached) curated check on a patient client; record the verdict."""
    from oncofiles.curated_folder import mark_if_curated, write_block_reason

    if gdrive is None or not folder_id:
        return False
    await asyncio.to_thread(mark_if_curated, gdrive, folder_id)
    blocked = write_block_reason(gdrive) is not None
    if blocked and patient_id:
        if patient_id not in _curated_patients:
            logger.info("patient %s: Drive folder is curated — legacy writes disabled", patient_id)
        _curated_patients.add(patient_id)
    return blocked


_sync_staleness: dict[str, object] = {
    "checked_at": None,
    "stale_count": 0,
    "patients": [],  # [{patient_slug, last_ok, age_h}] — stale ones only
}
# Deduplicate the admin alert to at most one per condition per day.
_pipeline_alert_sent: dict[str, str] = {}

# (needle, reason) — matched case-insensitively against str(exc).
_HARD_AUTH_MARKERS: tuple[tuple[str, str], ...] = (
    ("has been deleted", "project_deleted"),
    ("has not been used in project", "api_not_enabled"),
    ("accessnotconfigured", "api_not_enabled"),
    ("service_disabled", "api_not_enabled"),
    ("invalid_client", "oauth_client_invalid"),
    ("unauthorized_client", "oauth_client_invalid"),
)


def _hard_auth_reason(exc: BaseException, _depth: int = 0) -> str | None:
    """Classify a structural credential failure, or return None.

    Deliberately does NOT match ``insufficientFilePermissions`` — that is a
    per-file ownership problem (#445) that sync survives file-by-file, not a
    dead credential. Flagging it here would permanently light up the health
    endpoint for patients whose Drive files are merely shared read-only.

    Recurses into ``__cause__`` (bounded) because ``asyncio.to_thread`` and the
    googleapiclient wrappers frequently re-raise with the real error nested.
    """
    if _depth > 4:
        return None
    text = str(exc).lower()
    if "insufficientfilepermissions" not in text:
        for marker, reason in _HARD_AUTH_MARKERS:
            if marker in text:
                return reason
    cause = exc.__cause__
    if cause is not None and cause is not exc:
        return _hard_auth_reason(cause, _depth + 1)
    return None


def _handle_sync_exception(pid: str, service: str, exc: BaseException) -> None:
    """Log sync exceptions once per stale-token; full trace for unknown errors."""
    if _is_invalid_grant(exc):
        key = (pid, service)
        if key not in _invalid_grant_tokens:
            _invalid_grant_tokens.add(key)
            logger.warning(
                "%s sync [%s] suspended — refresh token invalid, re-auth required",
                service.capitalize(),
                pid,
            )
        return
    reason = _hard_auth_reason(exc)
    if reason:
        key = (pid, service)
        now = datetime.now(UTC).isoformat()
        entry = _hard_auth_failures.get(key)
        if entry is None:
            _hard_auth_failures[key] = entry = {
                "reason": reason,
                "first_seen": now,
                "detail": str(exc)[:200],
            }
            # ERROR, once per (pid, service): this halts ALL processing for the
            # patient and no caregiver action can clear it. #552 went unnoticed
            # for a month because this path used to be an unremarkable
            # logger.exception among thousands of access-log lines.
            logger.error(
                "%s sync [%s] BLOCKED — %s. Operator action required; "
                "no processing will run for this patient until credentials are repaired.",
                service.capitalize(),
                pid,
                reason,
            )
        entry["last_seen"] = now
        return
    logger.exception("%s sync [%s] failed", service.capitalize(), pid)


# Per-service last-sync timestamps for dashboard (#276)
_last_service_sync: dict[str, dict[str, str | None]] = {}


def _record_service_sync(pid: str, service: str):
    """Record that a service sync completed for a patient."""
    if pid not in _last_service_sync:
        _last_service_sync[pid] = {"gdrive": None, "gmail": None, "calendar": None}
    _last_service_sync[pid][service] = datetime.now(UTC).isoformat()
    # A successful call proves the credential is healthy again — clear any
    # structural-auth flag so /health stops reporting a repaired patient (#552).
    _hard_auth_failures.pop((pid, service), None)


def _get_service_sync_times(pid: str) -> dict[str, str | None]:
    return _last_service_sync.get(pid, {"gdrive": None, "gmail": None, "calendar": None})


async def _get_service_sync_times_async(db, pid: str) -> dict[str, str | None]:
    """Per-patient last-sync timestamps surviving Railway restarts (#463).

    In-memory `_last_service_sync` resets on every process restart (deploys,
    keepalive, memory ceiling). Before this helper, the dashboard Integration
    Status popover showed empty / stale values until the next sync tick fired
    for that patient — feeling "already ran but obsolete data there".

    Strategy: return in-memory value when present (cheap + matches
    sync-completion timing), fall back to MAX(created_at) from the
    arrival-side tables when a service has no in-memory record. Using
    MAX(created_at) means after a restart we show "when the last new item
    landed" which is arguably the stronger signal than "when the last
    poll happened".
    """
    cached = _last_service_sync.get(pid, {"gdrive": None, "gmail": None, "calendar": None})
    if all(v is not None for v in cached.values()):
        return cached

    result = dict(cached)
    queries = (
        ("gdrive", "SELECT MAX(created_at) AS t FROM documents WHERE patient_id = ?"),
        ("gmail", "SELECT MAX(created_at) AS t FROM email_entries WHERE patient_id = ?"),
        ("calendar", "SELECT MAX(created_at) AS t FROM calendar_entries WHERE patient_id = ?"),
    )
    for service, sql in queries:
        if result.get(service):
            continue
        try:
            async with db.db.execute(sql, (pid,)) as cursor:
                row = await cursor.fetchone()
            if row is None:
                continue
            t = dict(row).get("t") if row is not None else None
            if t:
                result[service] = t
        except Exception:
            # Table may be missing in older DBs or during migration — don't
            # let this break the popover.
            logger.debug("service_sync_times fallback query failed for %s", service, exc_info=True)
    return result


def _is_folder_not_found(exc: BaseException) -> bool:
    """Check if an exception is a GDrive 404 (folder not found)."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 404:
        return True
    # Also check nested exceptions from asyncio.to_thread
    if exc.__cause__:
        return _is_folder_not_found(exc.__cause__)
    return False


def _build_gmail_calendar_clients(token, access_token: str) -> tuple:
    """Return ``(gmail, calendar)`` clients for the scopes ``token`` grants.

    #746: with GMAIL_CALENDAR_ENABLED off this returns ``(None, None)`` before
    importing or constructing either client, even when the stored token holds
    the gmail/calendar scopes. Callers treat None as "not connected".
    """
    if not gmail_calendar_enabled():
        return None, None

    from oncofiles.oauth import SCOPE_CALENDAR, SCOPE_GMAIL

    granted = json.loads(token.granted_scopes) if token.granted_scopes else []

    gmail = None
    if SCOPE_GMAIL in granted:
        from oncofiles.gmail_client import GmailClient

        gmail = GmailClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )

    calendar = None
    if SCOPE_CALENDAR in granted:
        from oncofiles.calendar_client import CalendarClient

        calendar = CalendarClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )
    return gmail, calendar


async def _create_patient_clients(
    db,
    patient_id: str,
) -> tuple | None:
    """Load OAuth token, refresh if needed, create API clients for a patient.

    Returns ``(gdrive, gmail_client, calendar_client, folder_id)`` or
    ``None`` if the patient has no OAuth token or no folder configured.
    Caches clients for 30 min to avoid rebuilding HTTP pools every sync cycle.
    """
    # #552 kill switch. This is the single chokepoint for per-patient Google
    # clients, so returning None here is enough to stop every Drive/Gmail/
    # Calendar call downstream: callers already treat None as "not connected"
    # and skip cleanly. Deliberately placed before the token load so we do not
    # even attempt a refresh against a dead OAuth client.
    if not GOOGLE_INTEGRATION_ENABLED:
        _client_unavailable.pop(patient_id, None)  # the kill switch is its own state
        return None

    import time as _time

    from oncofiles.oauth import is_token_expired, refresh_access_token

    # Check cache first
    cached = _patient_clients_cache.get(patient_id)
    if cached and (_time.monotonic() - cached[4]) < _CLIENT_CACHE_TTL:
        # Re-check on cache hits too (cheap: detection is cached for 10 min) so a
        # folder that becomes curated is blocked without waiting for the 30-min TTL.
        await _mark_patient_curated(patient_id, cached[0], cached[3])
        if not gmail_calendar_enabled():  # #746: never hand out Gmail/Calendar clients
            return (cached[0], None, None, cached[3])
        return cached[:4]

    token = await db.get_oauth_token(patient_id=patient_id)
    if not token or not token.gdrive_folder_id:
        _client_unavailable.pop(patient_id, None)
        return None

    access_token = token.access_token
    if is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None):
        try:
            refreshed = refresh_access_token(token.refresh_token)
            access_token = refreshed["access_token"]
            new_expiry = datetime.now(UTC) + timedelta(seconds=refreshed.get("expires_in", 3600))
            token.access_token = access_token
            token.token_expiry = new_expiry
            await db.update_oauth_access_token(
                token.patient_id, token.provider, access_token, new_expiry
            )
        except Exception as exc:
            if is_network_unavailable(exc):
                # #979: a WAN/DNS outage is not a disconnect. Code + exception
                # class only (no trace, no URL), never needs_reauth / hard auth.
                _note_client_unavailable(patient_id, NETWORK_UNAVAILABLE)
                logger.warning(
                    "Token refresh for patient %s: %s (%s) — transient, will retry",
                    patient_id,
                    NETWORK_UNAVAILABLE,
                    type(exc).__name__,
                )
                return None
            _note_client_unavailable(patient_id, "refresh_failed")
            logger.warning("Token refresh failed for patient %s", patient_id, exc_info=True)
            return None

    p_gdrive = GDriveClient.from_oauth(
        access_token=access_token,
        refresh_token=token.refresh_token,
        client_id=GOOGLE_OAUTH_CLIENT_ID,
        client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        owner_email=token.owner_email or "",
    )

    p_gmail, p_calendar = _build_gmail_calendar_clients(token, access_token)

    # Curated folder (#562): block every legacy write on this client.
    await _mark_patient_curated(patient_id, p_gdrive, token.gdrive_folder_id)

    _client_unavailable.pop(patient_id, None)  # #979: built fine, nothing pending

    # Cache the clients
    _patient_clients_cache[patient_id] = (
        p_gdrive,
        p_gmail,
        p_calendar,
        token.gdrive_folder_id,
        _time.monotonic(),
    )

    return (p_gdrive, p_gmail, p_calendar, token.gdrive_folder_id)


def _release_curated_loop(task) -> bool:
    """Lifespan exit for the curated loop (#784). True when it stopped curated work.

    ``request_stop()`` is global (it also stops a run of a newer loop), so it
    fires only when ``scheduler_singleton.release_curated_loop`` says this
    holder owns the current loop (or no loop runs at all); a stale holder whose
    loop already ended is a no-op.
    """
    from oncofiles.tools.curated import request_stop

    if not scheduler_singleton.release_curated_loop(task):
        return False
    request_stop()  # a running curated run finishes its document and logs cleanly
    if task is not None:
        task.cancel()
    return True


def _start_sync_scheduler(
    db,
    files,
    gdrive,
    oauth_folder_id,
    gmail_client=None,
    calendar_client=None,
    *,
    primary_uuid: str = "",
):
    """Start APScheduler for periodic GDrive sync, Gmail sync, and Calendar sync.

    Idempotent within a process (#784): when a scheduler is already running,
    it is returned (logged once) instead of starting a second one that would
    fire every job twice.
    """
    if (existing := scheduler_singleton.existing_scheduler()) is not None:
        return existing

    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    from oncofiles.sync import enhance_documents as _enhance_docs
    from oncofiles.sync import extract_all_metadata, sync

    sync_timeout = 300  # 5 minutes max for scheduled sync

    _last_calendar_sync_times: dict[str, str] = {}

    async def _get_patient_gdrive(pid: str) -> tuple | None:
        """Get GDrive client + folder_id for a patient."""
        if pid == primary_uuid and gdrive and oauth_folder_id:
            # Check the folder actually synced, on every call (#562 review): the
            # lifespan client is long-lived and is never rebuilt on folder change.
            folder = _get_sync_folder_id_from(oauth_folder_id)
            await _mark_patient_curated(pid, gdrive, folder)
            return (gdrive, folder)
        clients = await _create_patient_clients(db, pid)
        if not clients:
            return None
        return (clients[0], clients[3])

    async def _get_patient_gmail(pid: str):
        """Get Gmail client for a patient. Uses shared lifespan clients for primary patient."""
        if pid == primary_uuid and gmail_client:
            return gmail_client
        clients = await _create_patient_clients(db, pid)
        return clients[1] if clients else None

    async def _get_patient_calendar(pid: str):
        """Get Calendar client for a patient. Uses shared lifespan clients for primary patient."""
        if pid == primary_uuid and calendar_client:
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
                    # Post-sync auto-enhance removed in #433 — enhancement now
                    # runs only inside _run_nightly_ai_pipeline (AI_NIGHTLY_ONLY=true)
                    # or via the separate enhancement_sweep / metadata_extraction
                    # cron jobs when AI_NIGHTLY_ONLY=false.
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
                        _handle_sync_exception(pid, "gdrive", exc)
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

    async def _run_pipeline_integrity_check():
        """Scheduled check: find and fix any docs stuck in incomplete pipeline state."""
        try:
            patients = await db.list_patients(active_only=True)
            for p in patients:
                pid = p.patient_id
                all_docs = await db.list_documents(limit=DOC_QUERY_LIMIT, patient_id=pid)
                ocr_ids = await db.get_ocr_document_ids(patient_id=pid)
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

                    # No auto-trash (#734): a doc whose content is gone stays listed,
                    # reported by the gap warning above. Only the user deletes it.
                else:
                    logger.info("Pipeline integrity [%s]: all %d docs complete", pid, len(all_docs))
        except Exception:
            logger.exception("Pipeline integrity check failed")

    async def _run_document_groups_backfill():
        """Sweep the backlog for multi-doc PDFs + multi-file logical docs.

        Without this job, detect_and_split_documents / detect_and_consolidate_documents
        only run during the every-5-min gdrive_sync and therefore only catch newly-ingested
        docs — backlog docs imported before auto-split shipped stay ungrouped forever (#396).

        Split + consolidate each call AI per doc (claude-haiku), so the job is serialized via
        _sync_semaphore so it never overlaps with gdrive_sync writes.
        """
        if is_memory_pressure("document_groups_backfill"):
            return

        try:
            from oncofiles.backfill_splits import (
                backfill_consolidation,
                backfill_multi_document_splits,
            )

            patients = await db.list_patients(active_only=True)
            for p in patients:
                pid = p.patient_id
                if is_memory_pressure(f"backfill:{pid}"):
                    break
                if _folder_404_counts.get(pid, 0) >= _FOLDER_404_THRESHOLD:
                    continue

                gc = await _get_patient_gdrive(pid)
                if not gc:
                    continue
                p_gdrive, folder_id = gc

                async with _sync_semaphore:
                    try:
                        split_stats = await asyncio.wait_for(
                            backfill_multi_document_splits(
                                db,
                                files,
                                p_gdrive,
                                patient_id=pid,
                                folder_id=folder_id,
                                dry_run=False,
                            ),
                            timeout=metadata_timeout,
                        )
                        if (
                            split_stats.get("splits_created", 0) > 0
                            or split_stats.get("multi_doc", 0) > 0
                        ):
                            logger.info("Backfill splits [%s]: %s", pid, split_stats)
                    except TimeoutError:
                        logger.error("Backfill splits [%s] timed out", pid)
                    except Exception:
                        logger.exception("Backfill splits [%s] failed", pid)

                    try:
                        consol_stats = await asyncio.wait_for(
                            backfill_consolidation(db, p_gdrive, patient_id=pid, dry_run=False),
                            timeout=metadata_timeout,
                        )
                        if (
                            consol_stats.get("consolidated", 0) > 0
                            or consol_stats.get("groups_found", 0) > 0
                        ):
                            logger.info("Backfill consolidate [%s]: %s", pid, consol_stats)
                    except TimeoutError:
                        logger.error("Backfill consolidate [%s] timed out", pid)
                    except Exception:
                        logger.exception("Backfill consolidate [%s] failed", pid)

                    # Robot unblock: patient-context institution fallback for stuck docs (#404)
                    try:
                        from oncofiles.enhance import (
                            backfill_institution_from_patient_context,
                        )

                        unblock_stats = await asyncio.wait_for(
                            backfill_institution_from_patient_context(
                                db.db, patient_id=pid, dry_run=False
                            ),
                            timeout=60,
                        )
                        if unblock_stats.get("updated", 0) > 0:
                            logger.info("Institution fallback [%s]: %s", pid, unblock_stats)
                    except TimeoutError:
                        logger.error("Institution fallback [%s] timed out", pid)
                    except Exception:
                        logger.exception("Institution fallback [%s] failed", pid)
        except Exception:
            logger.exception("Document groups backfill failed")
        finally:
            from oncofiles.memory import reclaim_memory

            reclaim_memory("document_groups_backfill")

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

    async def _patient_ai_cost_usd_today(patient_id: str) -> float:
        """Sum estimated_cost_usd from prompt_log for ``patient_id`` in the
        last 24h. Used to track per-job cost telemetry (#441 Layer 6).

        Returns 0.0 when the column is empty or DB is unavailable — never
        raises; this is observability, not a gate.
        """
        try:
            sql = """
                SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS usd
                  FROM prompt_log
                 WHERE patient_id = ?
                   AND status = 'ok'
                   AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 days')
            """
            async with db.db.execute(sql, (patient_id,)) as cursor:
                row = await cursor.fetchone()
                return float(row["usd"] or 0.0) if row else 0.0
        except Exception:
            return 0.0

    async def _total_ai_cost_usd_today() -> float:
        """Cluster-wide AI cost in the last 24h. Surfaces in /readiness."""
        try:
            sql = """
                SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS usd
                  FROM prompt_log
                 WHERE status = 'ok'
                   AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 days')
            """
            async with db.db.execute(sql) as cursor:
                row = await cursor.fetchone()
                return float(row["usd"] or 0.0) if row else 0.0
        except Exception:
            return 0.0

    async def _run_nightly_ai_pipeline():
        """Consolidated nightly AI pipeline — runs at AI_NIGHT_WINDOW_START_UTC.

        Tier T1+T2 (#441 Layer 3): classify (cheap) + enhance + structured
        metadata. Tier T3 (multi-doc split detection + consolidation) lives
        in `_run_weekly_discovery_pipeline` and runs Sunday 23:00 UTC, since
        relationship/split graphs change slowly and weekly is sufficient.

        Layer 2: no max_age_hours filter — backlog drains naturally as
        budget permits. `only_new=True` (= ai_processed_at IS NULL) is the
        only filter; per-patient EUR budget is the cost ceiling.
        """
        from oncofiles.cloud_guard import cloud_ai_disabled_reason
        from oncofiles.sync import enhance_documents as _enhance_docs_fn

        if is_memory_pressure("nightly_ai_pipeline"):
            return

        total_enhanced = 0
        total_metadata = 0
        # #552: a run where every patient's sync failed used to be
        # indistinguishable from a clean run — the per-patient `continue`
        # below swallowed the exception, the job returned normally, and
        # job_tracker["nightly_ai_pipeline"]["last_ok"] was refreshed. Count
        # outcomes so the tail can fail the job instead of reporting success.
        sync_attempted = 0
        sync_failed = 0
        # #708: the AI phases (2 enhance, 3 metadata) swallow per-document
        # errors into stats["errors"]; count them the same way. A phase that
        # raises as a whole counts as one failed attempt.
        enhance_attempted = 0
        enhance_failed = 0
        # #708: with cloud AI off (CLOUD_AI_ENABLED off or no key) every AI call
        # raises CloudAIDisabledError. Skip the AI phases for the whole run and
        # say so once: a deliberate skip, not N per-document failures.
        enhance_skipped = cloud_ai_disabled_reason()
        if enhance_skipped:
            logger.info("Nightly AI pipeline: AI phases skipped this run — %s", enhance_skipped)

        try:
            from oncofiles.budget import check_patient_budget

            cost_before = await _total_ai_cost_usd_today()

            patients = await db.list_patients(active_only=True)
            for p in patients:
                pid = p.patient_id
                if is_memory_pressure(f"nightly:{pid}"):
                    break
                if _folder_404_counts.get(pid, 0) >= _FOLDER_404_THRESHOLD:
                    continue

                # Budget gate (#442): skip AI phases for patients over their
                # rolling 30-day EUR cap. GDrive sync still runs (Phase 1
                # passes enhance=False — zero AI cost) so the Drive inventory
                # stays current; only Phases 2-4 (AI calls) are skipped. Admin
                # tier returns within_budget=True always (grandfathered).
                status = await check_patient_budget(db, pid)
                if not status.within_budget:
                    logger.info(
                        "Nightly AI skipped [%s] — budget exhausted "
                        "(tier=%s, used=€%.2f / cap=€%.2f)",
                        pid,
                        status.tier,
                        status.used_eur,
                        status.cap_eur or 0.0,
                    )
                    continue

                gc_pair = await _get_patient_gdrive(pid)
                if not gc_pair:
                    continue
                p_gdrive, folder_id = gc_pair
                if pid in _curated_patients:
                    # Curated folder (#562): owned by the contract pipeline, not the
                    # legacy phases. Not an attempt, not a failure.
                    logger.info("pipeline [%s]: curated folder — legacy phases skipped", pid)
                    continue

                async with _sync_semaphore:
                    # Phase 1: GDrive import (md5-gated, cheap for unchanged).
                    # enhance=False is the cap-enforcement contract — inline enhance
                    # during import would bypass DAILY_AI_DOC_CAP. Phase 2 below is
                    # the single authoritative AI-cost gate. See #433.
                    sync_attempted += 1
                    try:
                        sync_stats = await asyncio.wait_for(
                            sync(
                                db,
                                files,
                                p_gdrive,
                                folder_id,
                                trigger="nightly",
                                patient_id=pid,
                                enhance=False,
                            ),
                            timeout=sync_timeout,
                        )
                        _record_service_sync(pid, "gdrive")
                        _folder_404_counts.pop(pid, None)
                        logger.info("Nightly sync [%s]: %s", pid, sync_stats)
                    except TimeoutError:
                        sync_failed += 1
                        logger.error("Nightly sync [%s] timed out", pid)
                        continue
                    except Exception as exc:
                        sync_failed += 1
                        if _is_folder_not_found(exc):
                            count = _folder_404_counts.get(pid, 0) + 1
                            _folder_404_counts[pid] = count
                        else:
                            _handle_sync_exception(pid, "gdrive", exc)
                        continue

                    if enhance_skipped:
                        continue  # #708: cloud AI off — logged once above

                    # Phase 2: Enhance NEW only — no max_age filter (#441 Layer 2),
                    # rely on `only_new=True` (= ai_processed_at IS NULL) +
                    # per-patient EUR budget gate to bound cost.
                    try:
                        eh_stats = await asyncio.wait_for(
                            _enhance_docs_fn(
                                db,
                                files,
                                p_gdrive,
                                patient_id=pid,
                                only_new=True,
                                max_age_hours=None,
                                limit=DAILY_AI_DOC_CAP,
                            ),
                            timeout=metadata_timeout,
                        )
                        total_enhanced += eh_stats.get("processed", 0)
                        enhance_attempted += eh_stats.get("processed", 0) + eh_stats.get(
                            "errors", 0
                        )
                        enhance_failed += eh_stats.get("errors", 0)
                        if eh_stats.get("processed", 0) > 0:
                            logger.info("Nightly enhance [%s]: %s", pid, eh_stats)
                    except Exception:
                        enhance_attempted += 1
                        enhance_failed += 1
                        logger.exception("Nightly enhance [%s] failed", pid)

                    # Phase 3: Metadata extraction NEW only — same Layer 2 contract.
                    try:
                        md_stats = await asyncio.wait_for(
                            extract_all_metadata(
                                db,
                                files,
                                p_gdrive,
                                patient_id=pid,
                                only_new=True,
                                max_age_hours=None,
                                cap=DAILY_AI_DOC_CAP,
                            ),
                            timeout=metadata_timeout,
                        )
                        total_metadata += md_stats.get("processed", 0)
                        enhance_attempted += md_stats.get("processed", 0) + md_stats.get(
                            "errors", 0
                        )
                        enhance_failed += md_stats.get("errors", 0)
                        if md_stats.get("processed", 0) > 0:
                            logger.info("Nightly metadata [%s]: %s", pid, md_stats)
                    except Exception:
                        enhance_attempted += 1
                        enhance_failed += 1
                        logger.exception("Nightly metadata [%s] failed", pid)

                    # Phase 4 (split + consolidation) moved to weekly Sunday
                    # via `_run_weekly_discovery_pipeline` (#441 Layer 3).

            cost_after = await _total_ai_cost_usd_today()
            cost_delta = max(0.0, cost_after - cost_before)
            logger.info(
                "Nightly AI pipeline: patients_synced=%d/%d enhanced=%d metadata=%d "
                "ai_docs_failed=%d/%d ai_phases=%s cost_delta=$%.4f",
                sync_attempted - sync_failed,
                sync_attempted,
                total_enhanced,
                total_metadata,
                enhance_failed,
                enhance_attempted,
                "skipped" if enhance_skipped else "ran",
                cost_delta,
            )
            # #441 Layer 6: surface per-run cost in job_tracker → /readiness.
            # #552: also surface what the run actually accomplished, so
            # "job succeeded" can be checked against "job did something".
            try:
                entry = job_tracker.get("nightly_ai_pipeline", {})
                entry["last_cost_usd"] = round(cost_delta, 4)
                entry["last_run_summary"] = {
                    "sync_attempted": sync_attempted,
                    "sync_failed": sync_failed,
                    "enhanced": total_enhanced,
                    "metadata": total_metadata,
                    "enhance_attempted": enhance_attempted,
                    "enhance_failed": enhance_failed,
                    # #708: a deliberate skip is marked, never a silent last_ok.
                    "enhance_skipped": "cloud_ai_disabled" if enhance_skipped else None,
                }
                job_tracker["nightly_ai_pipeline"] = entry
            except Exception:
                pass

            # #552: total failure must not read as success. Raising hands the
            # run to the EVENT_JOB_ERROR listener, which records last_error /
            # last_error_msg on job_tracker and logs at ERROR — so /readiness
            # shows a failing nightly job instead of a fresh last_ok. Only
            # fires when we actually tried and every attempt failed; a night
            # with nothing to do (0 attempted) is legitimately a success.
            if sync_attempted > 0 and sync_failed == sync_attempted:
                raise RuntimeError(
                    f"Nightly pipeline: all {sync_attempted} patient sync(s) failed — "
                    "no documents imported, renamed or enhanced. "
                    "Check /readiness hard_auth_failures and the container log."
                )
            # #708: the same rule for the AI phases. 0 attempted (nothing new,
            # or cloud AI off and skipped) stays a success.
            if enhance_attempted > 0 and enhance_failed == enhance_attempted:
                raise RuntimeError(
                    f"Nightly pipeline: all {enhance_attempted} document enhancement(s) "
                    "failed — no AI summaries or metadata written. Check the container log."
                )
            if enhance_failed:
                logger.warning(
                    "Nightly AI pipeline: %d of %d document enhancement(s) failed",
                    enhance_failed,
                    enhance_attempted,
                )
        finally:
            from oncofiles.memory import reclaim_memory

            reclaim_memory("nightly_ai_pipeline")

    async def _run_weekly_discovery_pipeline():
        """Tier 3 discovery — multi-doc split detection + consolidation.

        Sunday 23:00 UTC. The relationship/split graph for any given week's
        documents changes slowly; running daily was wasteful. Weekly cadence
        cuts T3 cost by ~85% with no observable UX impact (the dashboard's
        group_id / part_number columns still update on Monday morning).
        """
        from oncofiles.backfill_splits import (
            backfill_consolidation,
            backfill_multi_document_splits,
        )

        if is_memory_pressure("weekly_discovery_pipeline"):
            return

        total_split = 0
        total_consolidated = 0

        try:
            from oncofiles.budget import check_patient_budget

            cost_before = await _total_ai_cost_usd_today()
            patients = await db.list_patients(active_only=True)
            for p in patients:
                pid = p.patient_id
                if is_memory_pressure(f"weekly_discovery:{pid}"):
                    break
                if _folder_404_counts.get(pid, 0) >= _FOLDER_404_THRESHOLD:
                    continue
                # Same budget gate as nightly — Layer 1.
                status = await check_patient_budget(db, pid)
                if not status.within_budget:
                    logger.info(
                        "Weekly discovery skipped [%s] — budget exhausted "
                        "(tier=%s, used=€%.2f / cap=€%.2f)",
                        pid,
                        status.tier,
                        status.used_eur,
                        status.cap_eur or 0.0,
                    )
                    continue
                gc_pair = await _get_patient_gdrive(pid)
                if not gc_pair:
                    continue
                p_gdrive, folder_id = gc_pair
                if pid in _curated_patients:
                    # Curated folder (#562): owned by the contract pipeline, not the
                    # legacy phases. Not an attempt, not a failure.
                    logger.info("pipeline [%s]: curated folder — legacy phases skipped", pid)
                    continue

                async with _sync_semaphore:
                    try:
                        split_stats = await asyncio.wait_for(
                            backfill_multi_document_splits(
                                db,
                                files,
                                p_gdrive,
                                patient_id=pid,
                                folder_id=folder_id,
                                dry_run=False,
                                only_new=True,
                                max_age_hours=None,
                            ),
                            timeout=metadata_timeout,
                        )
                        total_split += split_stats.get("splits_created", 0)
                        if split_stats.get("splits_created", 0) > 0:
                            logger.info("Weekly splits [%s]: %s", pid, split_stats)
                    except Exception:
                        logger.exception("Weekly splits [%s] failed", pid)

                    try:
                        cons_stats = await asyncio.wait_for(
                            backfill_consolidation(
                                db,
                                p_gdrive,
                                patient_id=pid,
                                dry_run=False,
                                only_new=True,
                                max_age_hours=None,
                            ),
                            timeout=metadata_timeout,
                        )
                        total_consolidated += cons_stats.get("consolidated", 0)
                        if cons_stats.get("consolidated", 0) > 0:
                            logger.info("Weekly consolidate [%s]: %s", pid, cons_stats)
                    except Exception:
                        logger.exception("Weekly consolidate [%s] failed", pid)

            cost_after = await _total_ai_cost_usd_today()
            cost_delta = max(0.0, cost_after - cost_before)
            logger.info(
                "Weekly discovery pipeline: splits=%d consolidated=%d cost_delta=$%.4f",
                total_split,
                total_consolidated,
                cost_delta,
            )
            try:
                entry = job_tracker.get("weekly_discovery_pipeline", {})
                entry["last_cost_usd"] = round(cost_delta, 4)
                job_tracker["weekly_discovery_pipeline"] = entry
            except Exception:
                pass
        finally:
            from oncofiles.memory import reclaim_memory

            reclaim_memory("weekly_discovery_pipeline")

    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    scheduler = AsyncIOScheduler()

    # ── LLM-touching jobs: nightly-only vs legacy (see #433) ───────────────
    if AI_NIGHTLY_ONLY:
        # misfire_grace_time=1800: absorb deploys within 30 min of the slot so
        #   a restart at 23:10 doesn't silently skip the 23:00 run (#440).
        # coalesce=True: if multiple misses accumulate, run once — never double-bill.
        scheduler.add_job(
            local_only_task("nightly_ai_pipeline", _run_nightly_ai_pipeline),
            CronTrigger(hour=AI_NIGHT_WINDOW_START_UTC, minute=0),
            id="nightly_ai_pipeline",
            max_instances=1,
            misfire_grace_time=1800,
            coalesce=True,
        )
        # #441 Layer 3: weekly Tier 3 discovery (split + consolidation) on
        # Sunday at AI_NIGHT_WINDOW_START_UTC + 30 min. Daily cadence was
        # wasteful — relationship/split graphs change slowly. The 30-min
        # offset from the nightly slot keeps the budget gate reading the
        # latest spend from the just-finished nightly run.
        scheduler.add_job(
            local_only_task("weekly_discovery_pipeline", _run_weekly_discovery_pipeline),
            CronTrigger(day_of_week="sun", hour=AI_NIGHT_WINDOW_START_UTC, minute=30),
            id="weekly_discovery_pipeline",
            max_instances=1,
            misfire_grace_time=1800,
            coalesce=True,
        )
        logger.info(
            "AI_NIGHTLY_ONLY=true — nightly AI pipeline at %02d:00 UTC, "
            "weekly discovery on Sun %02d:30 UTC (cap=%d docs/patient, grace=30min)",
            AI_NIGHT_WINDOW_START_UTC,
            AI_NIGHT_WINDOW_START_UTC,
            DAILY_AI_DOC_CAP,
        )
        # AI_RUN_ON_STARTUP: one-off validation fire 2 min after boot. Use after
        # cost-optimizer changes to validate without waiting 17h for 23:00 UTC.
        if AI_RUN_ON_STARTUP:
            _oneoff_at = datetime.now() + timedelta(minutes=2)
            scheduler.add_job(
                local_only_task("nightly_ai_oneoff", _run_nightly_ai_pipeline),
                DateTrigger(run_date=_oneoff_at),
                id="nightly_ai_oneoff",
                max_instances=1,
            )
            logger.info(
                "AI_RUN_ON_STARTUP=true — one-off nightly pipeline fires at %s UTC",
                _oneoff_at.strftime("%H:%M:%S"),
            )
    else:
        scheduler.add_job(
            local_only_task("gdrive_sync", _run_sync),
            IntervalTrigger(minutes=SYNC_INTERVAL_MINUTES),
            id="gdrive_sync",
            max_instances=1,
        )
        scheduler.add_job(
            local_only_task("enhancement_sweep", _run_enhancement_sweep),
            CronTrigger(hour=3, minute=20),
            id="enhancement_sweep",
            max_instances=1,
        )
        scheduler.add_job(
            local_only_task("metadata_extraction", _run_metadata_extraction),
            CronTrigger(hour=3, minute=30),
            id="metadata_extraction",
            max_instances=1,
        )
        scheduler.add_job(
            local_only_task("document_groups_backfill", _run_document_groups_backfill),
            CronTrigger(hour=3, minute=55),
            id="document_groups_backfill",
            max_instances=1,
        )
        logger.info("AI_NIGHTLY_ONLY=false — legacy 5-min sync + 4 nightly sweeps registered")

    # ── Zero-cost housekeeping (always on) ─────────────────────────────────

    async def _run_tier_transition():
        """Move patients from 'free_onboarding' to 'free' when their 30-day
        onboarding window expires. #442. Daily at 00:05 UTC — well clear of
        the 23:00 UTC AI pipeline. No AI cost, pure SQL."""
        try:
            async with db.db.execute(
                """
                UPDATE patients
                SET tier = 'free',
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE tier = 'free_onboarding'
                  AND onboarding_ends_at IS NOT NULL
                  AND onboarding_ends_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                """,
            ) as cursor:
                count = cursor.rowcount
            await db.db.commit()
            if count:
                logger.info(
                    "tier_transition: moved %d patient(s) from free_onboarding → free",
                    count,
                )
        except Exception:
            logger.exception("tier_transition job failed")

    scheduler.add_job(
        local_only_task("tier_transition", _run_tier_transition),
        CronTrigger(hour=0, minute=5),
        id="tier_transition",
        max_instances=1,
        misfire_grace_time=1800,
        coalesce=True,
    )

    # #999: the MCP OAuth token cleanup (04:00 UTC) is NOT an APScheduler job
    # any more: its only owner is the lifespan loop (oncofiles.oauth_cleanup_loop).
    scheduler.add_job(
        local_only_task("prompt_log_cleanup", _run_prompt_log_cleanup),
        CronTrigger(hour=4, minute=15),
        id="prompt_log_cleanup",
        max_instances=1,
    )
    # #868 / #999: the access log retention (04:25 UTC) is NOT an APScheduler
    # job any more: its only owner is the lifespan loop (oncofiles.access_log_loop).
    # #856: the erase copy retention (04:35 UTC) is NOT an APScheduler job: it
    # must run while SYNC_ENABLED=false, so its only owner is the lifespan loop
    # (oncofiles.erase_retention).

    # Pipeline integrity check: opt-in only (AI-touching; gated by ENABLE_INTEGRITY_CHECK)
    if ENABLE_INTEGRITY_CHECK:
        scheduler.add_job(
            local_only_task("pipeline_integrity", _run_pipeline_integrity_check),
            CronTrigger(hour="*/6", minute=30),
            id="pipeline_integrity",
            max_instances=1,
        )
        logger.info("ENABLE_INTEGRITY_CHECK=true — every-6h pipeline integrity check registered")

    # ── Sync staleness watchdog (#552) ──

    async def _run_sync_watchdog():
        """Detect a silently-dead pipeline and tell a human.

        Deliberately failure-mode agnostic: it does not care *why* sync stopped
        (dead credentials, deleted GCP project, revoked folder, wedged
        scheduler) — only that no run has *completed* for a patient who used to
        sync fine. That generality is the point; #552 was invisible precisely
        because every check we had enumerated specific known failures.

        Zero AI cost, one cheap indexed query per active patient.
        """
        try:
            threshold = timedelta(hours=SYNC_STALE_ALERT_HOURS)
            now = datetime.now(UTC)
            stale: list[dict] = []
            for p in await db.list_patients(active_only=True):
                if p.patient_id in _curated_patients:
                    # Legacy sync is deliberately off for curated folders (#562).
                    continue
                last_ok = await db.get_last_successful_sync(patient_id=p.patient_id)
                if not last_ok:
                    # Never synced successfully — not "stale", just not
                    # connected yet. Onboarding gaps are #468's job.
                    continue
                try:
                    last_dt = datetime.fromisoformat(last_ok.replace("Z", "+00:00"))
                except ValueError:
                    logger.warning("sync_watchdog: unparseable finished_at %r", last_ok)
                    continue
                age = now - last_dt
                if age > threshold:
                    stale.append(
                        {
                            "patient_slug": p.slug,
                            "last_ok": last_ok,
                            "age_h": round(age.total_seconds() / 3600, 1),
                        }
                    )

            _sync_staleness["checked_at"] = now.isoformat()
            _sync_staleness["stale_count"] = len(stale)
            _sync_staleness["patients"] = stale

            if stale and not GOOGLE_INTEGRATION_ENABLED:
                # Staleness is expected while the kill switch is off. Record it
                # for /readiness but do not page — alert fatigue during a known
                # outage is how the *next* real one gets ignored.
                logger.info(
                    "sync_watchdog: %d patient(s) stale, but GOOGLE_INTEGRATION_ENABLED=false "
                    "— intentional pause, not alerting",
                    len(stale),
                )
            elif stale:
                logger.error(
                    "sync_watchdog: %d patient(s) have had no successful sync in >%dh — %s",
                    len(stale),
                    SYNC_STALE_ALERT_HOURS,
                    ", ".join(f"{s['patient_slug']}={s['age_h']}h" for s in stale),
                )
                lines = "\n".join(
                    f"  {s['patient_slug']:<20} last OK {s['last_ok']}  ({s['age_h']}h ago)"
                    for s in stale
                )
                auth_note = ""
                if _hard_auth_failures:
                    reasons = ", ".join(
                        sorted({i.get("reason", "?") for i in _hard_auth_failures.values()})
                    )
                    auth_note = (
                        f"\nStructural credential failures detected: {reasons}\n"
                        "This needs operator action — caregivers cannot re-auth their way out.\n"
                    )
                await _notify_admin_pipeline_alert(
                    condition="sync_stale",
                    subject=f"[Oncofiles] Pipeline stalled — {len(stale)} patient(s) not syncing",
                    body=(
                        f"No successful sync in over {SYNC_STALE_ALERT_HOURS}h:\n\n"
                        f"{lines}\n{auth_note}\n"
                        f"Checked: {now.isoformat()}\n"
                        f"Detail:  https://oncofiles.com/readiness (operator bearer required)\n"
                    ),
                )
            else:
                # Condition cleared — allow a future alert to fire again.
                _pipeline_alert_sent.pop("sync_stale", None)
        except Exception:
            logger.exception("sync_watchdog failed")

    scheduler.add_job(
        local_only_task("sync_watchdog", _run_sync_watchdog),
        IntervalTrigger(hours=1, start_date=datetime.now() + timedelta(minutes=5)),
        id="sync_watchdog",
        max_instances=1,
        misfire_grace_time=600,
        coalesce=True,
    )

    async def _run_category_validation():
        """AI-powered document classification sweep (no dedup, #734)."""

        async def _do_category_validation():
            from oncofiles.models import DocumentCategory as _DocCat  # noqa: N814

            patients = await db.list_patients(active_only=True)
            all_docs = []
            for p in patients:
                all_docs.extend(
                    await db.list_documents(limit=DOC_QUERY_LIMIT, patient_id=p.patient_id)
                )
            docs = all_docs
            import json as _json

            corrected = 0

            # Phase 0: Remap deprecated categories (structural, keep)
            for doc in docs:
                if doc.category.value == "surgical_report":
                    await db.update_document_category(doc.id, "surgery")
                    corrected += 1
                    logger.info("Category remap: %s surgical_report → surgery", doc.filename)

            # Phase 1: AI category from structured_metadata.category field
            for doc in docs:
                if not doc.structured_metadata or doc.deleted_at:
                    continue
                if doc.category.value != "other":
                    continue  # only upgrade "other" → specific
                try:
                    meta = _json.loads(doc.structured_metadata)
                except (ValueError, TypeError):
                    continue
                ai_cat = meta.get("category")
                if ai_cat and ai_cat != "other":
                    try:
                        valid_cat = _DocCat(ai_cat)
                        await db.update_document_category(doc.id, valid_cat.value)
                        corrected += 1
                        logger.info(
                            "Category AI-corrected: %s other → %s",
                            doc.filename,
                            valid_cat.value,
                        )
                    except ValueError:
                        pass

            # No automatic dedup (#734): duplicates are reported by the
            # user-invoked find_duplicates tool and removed only by the user.
            if corrected:
                logger.info("Category validation total: %d corrected", corrected)

        try:
            await asyncio.wait_for(_do_category_validation(), timeout=metadata_timeout)
        except TimeoutError:
            logger.error("Category validation timed out after %ds", metadata_timeout)
        except Exception:
            logger.exception("Category validation failed")

    # Category validation only registered in legacy mode; nightly pipeline
    # handles category upgrades inline during phase 2/3 via structured_metadata. #433
    if not AI_NIGHTLY_ONLY:
        scheduler.add_job(
            local_only_task("category_validation", _run_category_validation),
            CronTrigger(hour=3, minute=45),
            id="category_validation",
            max_instances=1,
        )

    # ── Weekly analytics aggregation ────────────────────────────────────
    async def _run_weekly_analytics():
        """Aggregate weekly usage stats and persist to agent_state.

        No try/except here on purpose (#542): a failure must reach the
        EVENT_JOB_ERROR listener, which records it for /readiness. The old
        catch-all let four non-existent attribute names fail every run while
        the job was reported as executed.
        """
        from oncofiles.weekly_analytics import run_weekly_analytics

        await run_weekly_analytics(db)

    scheduler.add_job(
        local_only_task("weekly_analytics", _run_weekly_analytics),
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

            # In nightly mode (AI_NIGHTLY_ONLY=true) skip AI enhancement on startup —
            # the nightly pipeline at 23:00 UTC owns all LLM work. Import-only on
            # deploy prevents every Railway restart from firing Claude calls (#433).
            try:
                import_stats = await asyncio.wait_for(
                    _sync_from(
                        db,
                        files,
                        p_gdrive,
                        folder_id,
                        enhance=not AI_NIGHTLY_ONLY,
                        patient_id=pid,
                    ),
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

        # Category validation deferred to nightly pipeline under AI_NIGHTLY_ONLY (#433)
        if not AI_NIGHTLY_ONLY:
            await _run_category_validation()
        logger.info("Startup catchup complete (RSS: %.1f MB)", get_rss_mb())

    startup_time = datetime.now() + timedelta(seconds=90)
    scheduler.add_job(
        local_only_task("startup_catchup", _startup_catchup),
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
                except Exception as exc:
                    _handle_sync_exception(pid, "gmail", exc)
                finally:
                    from oncofiles.memory import reclaim_memory

                    reclaim_memory(f"gmail_sync:{pid}")

    if not gmail_calendar_enabled():
        logger.info("Gmail sync not scheduled — GMAIL_CALENDAR_ENABLED=false (#618/#699)")
    elif AI_NIGHTLY_ONLY:
        scheduler.add_job(
            local_only_task("gmail_sync", _run_gmail_sync),
            CronTrigger(hour=AI_NIGHT_WINDOW_START_UTC, minute=15),
            id="gmail_sync",
            max_instances=1,
            misfire_grace_time=1800,
            coalesce=True,
        )
        logger.info("Gmail sync scheduled — nightly at %02d:15 UTC", AI_NIGHT_WINDOW_START_UTC)
    else:
        scheduler.add_job(
            local_only_task("gmail_sync", _run_gmail_sync),
            IntervalTrigger(
                minutes=SYNC_INTERVAL_MINUTES, start_date=datetime.now() + timedelta(minutes=2)
            ),
            id="gmail_sync",
            max_instances=1,
        )
        gmail_startup_time = datetime.now() + timedelta(seconds=150)
        scheduler.add_job(
            local_only_task("gmail_startup_sync", lambda: _run_gmail_sync("startup", initial=True)),
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
                except Exception as exc:
                    _handle_sync_exception(pid, "calendar", exc)
                finally:
                    from oncofiles.memory import reclaim_memory

                    reclaim_memory(f"calendar_sync:{pid}")

    if not gmail_calendar_enabled():
        logger.info("Calendar sync not scheduled — GMAIL_CALENDAR_ENABLED=false (#618/#699)")
    elif AI_NIGHTLY_ONLY:
        scheduler.add_job(
            local_only_task("calendar_sync", _run_calendar_sync),
            CronTrigger(hour=AI_NIGHT_WINDOW_START_UTC, minute=20),
            id="calendar_sync",
            max_instances=1,
            misfire_grace_time=1800,
            coalesce=True,
        )
        logger.info("Calendar sync scheduled — nightly at %02d:20 UTC", AI_NIGHT_WINDOW_START_UTC)
    else:
        scheduler.add_job(
            local_only_task("calendar_sync", _run_calendar_sync),
            IntervalTrigger(
                minutes=SYNC_INTERVAL_MINUTES, start_date=datetime.now() + timedelta(minutes=3)
            ),
            id="calendar_sync",
            max_instances=1,
        )
        calendar_startup_time = datetime.now() + timedelta(seconds=210)
        scheduler.add_job(
            local_only_task(
                "calendar_startup_sync", lambda: _run_calendar_sync("startup", initial=True)
            ),
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
            last_error_type=type(event.exception).__name__,
            running=False,
        )
        job_tracker[event.job_id] = entry
        logger.error("Scheduler job failed: %s — %s", event.job_id, event.exception)
        # The memory check's failure fallback (RSS read, restart above the
        # threshold) lives in its lifespan loop now (oncofiles.memory_check_loop, #999).

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

    # #952: the durable inbound mail queue (and the #950 quarantine sweep it
    # runs) is NOT an APScheduler job (#999): it must run while
    # SYNC_ENABLED=false, so its only periodic owner is the lifespan loop
    # (oncofiles.inbound_loop); the Brevo route's kick after an enqueue is
    # unchanged. Claims are conditional updates, so the old container of a
    # rolling deploy and this one never take the same job.

    # #950 phases two and three: holding_extract and holding_move are NOT
    # APScheduler jobs (#999): they must run while SYNC_ENABLED=false, so their
    # only periodic owner is the lifespan loops (oncofiles.holding_loops); the
    # event-driven kicks (holding_*.kick) are unchanged.

    # OF-1/OF-2: the periodic memory check (reclaim + graceful restart) is NOT
    # an APScheduler job any more (#999): its only owner is the lifespan loop
    # (oncofiles.memory_check_loop), so it also runs with SYNC_ENABLED=false.

    scheduler.start()
    _defer_jobs_past_startup_grace(scheduler)
    scheduler_singleton.register_scheduler(scheduler, job_tracker)
    logger.info(
        "Sync scheduler started — every %d min, %d jobs, instance %s",
        SYNC_INTERVAL_MINUTES,
        len(scheduler.get_jobs()),
        scheduler_singleton.INSTANCE_ID,
    )
    return scheduler, job_tracker


def _defer_jobs_past_startup_grace(scheduler, grace_s: int | None = None) -> list[str]:
    """Move every job's first run to at least ``grace_s`` after start (#784).

    A rolling deploy runs the old and new container side by side for a few
    seconds. The curated loop waits ``STARTUP_GRACE_S`` before its first run;
    the scheduler's first slots must too, or a job that touches Drive or a
    model (``startup_catchup`` at +90 s, the legacy Gmail/Calendar startup
    syncs, ``AI_RUN_ON_STARTUP``, or a cron slot that happens to fall right
    after the deploy) could run while the old container is still alive.

    A slot inside the grace is DEFERRED to the end of the grace, not skipped:
    a deploy at 22:58 still runs the 23:00 nightly pipeline, at 23:03. Later
    slots follow the job's trigger as usual. Returns the deferred job ids.
    """
    if grace_s is None:
        from oncofiles.tools.curated import STARTUP_GRACE_S

        grace_s = STARTUP_GRACE_S
    grace_end = datetime.now(scheduler.timezone) + timedelta(seconds=grace_s)
    deferred: list[str] = []
    for job in scheduler.get_jobs():
        nrt = job.next_run_time
        if nrt is not None and nrt < grace_end:
            job.modify(next_run_time=grace_end)
            deferred.append(job.id)
    if deferred:
        logger.info(
            "Scheduler: first run of %s deferred to the startup grace end (%ds)",
            ", ".join(sorted(deferred)),
            grace_s,
        )
    return deferred


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
4. `get_related_documents` returns related documents when links exist; an empty result \
with a `note` is normal. Never present documents as linked unless the tool returns them.
5. For lab trend analysis, reference the source `document_id` for each data point.

CROSS-REFERENCES — links appear when available:
- `same_visit` (`source: computed`): the same document date AND the same known institution \
(e.g., labs and imaging from one appointment). It is a date-and-place rule, not a reading \
of the content; a document without a date or with an `Unknown` institution has none.
- Other link types (`related`, `follow_up`) are not created automatically today.
- When `get_related_documents` returns none, use `timeline` or `encounter_detail` for \
documents of the same day or visit instead.

RECOMMENDED WORKFLOW for chat clients:
- Show GDrive links as clickable "View original" buttons alongside document summaries.
- Show PubMed/ClinicalTrials.gov links alongside research citations.
- Use `get_related_documents` for drill-down into connected records when links exist.
- In export packages, all entries include `gdrive_url` for offline verification.

PATIENT TYPES — the system supports two patient profiles:
- `oncology`: cancer patients with chemo protocols, biomarker tracking. \
Categories include pathology, genetics, chemo_sheet.
- `general`: healthy patients with preventive care screening, general health lab ranges. \
Categories include vaccination, dental, preventive. `get_preventive_care_status` lists the \
screenings and vaccinations found in the records (with source documents) next to EU guideline \
intervals; the intervals are a general reference, not advice for the patient.

Available categories: labs, report, imaging, pathology, genetics, \
surgery, surgical_report, prescription, referral, discharge, discharge_summary, chemo_sheet, \
reference, advocate, other, vaccination, dental, preventive.

CURATED PATIENT FOLDERS — a Drive folder holding README.md + _index.json is curated:
- It is processed automatically by the curated pipeline on the server and mirrored into \
search/labs/timeline. Check its state with `curated_folder_status`.
- Re-runs, re-indexing (`curated_folder_run`, `curated_index`), re-OCR, backfills, audits \
and the legacy Drive sync tools are operator tools: they are not available in this \
connector. If documents are missing or a run looks stuck, suggest contacting the maintainer.

OPERATIONS — Oncofiles is self-hosted on its own server, not on a cloud PaaS. \
Users have no access to server or hosting logs, so never tell them to check logs; for a \
server-side problem suggest contacting the maintainer.\
"""


class _OncofilesMCP(FastMCP):
    """FastMCP whose streamable-http app also serves the operator surface on
    ``/ops/mcp`` (#887), behind the static operator bearer, in this process."""

    def http_app(self, path=None, *args, **kwargs):
        import fastmcp as _fastmcp

        from oncofiles.tool_surface import install_ops_route

        app = super().http_app(path, *args, **kwargs)
        install_ops_route(app, path if path is not None else _fastmcp.settings.streamable_http_path)
        return app


mcp = _OncofilesMCP(
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
    # Oncoteam was retired (#721). The route stays so old links get a notice, not a 404.
    return HTMLResponse(
        _legal_page(
            "Oncoteam",
            """
<p><strong>Oncoteam</strong>, the former sister project of Oncofiles, has been retired.
Your documents and records stay in Oncofiles.</p>
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
<p class="meta">Effective: March 25, 2026 · Last updated: November 26, 2026 ·
Consent version 2026-11-26</p>

<p>Oncofiles is operated by <strong>Instarea s.r.o.</strong> ("we", "us").
This policy describes how Oncofiles (<a href="https://oncofiles.com">oncofiles.com</a>)
handles your data as the service runs today.</p>

<h2 id="access">1. What Data We Access</h2>
<ul>
<li><strong>Google sign-in</strong> — your Google account e-mail address. We use it to decide
which patients you may see.</li>
<li><strong>Google Drive</strong> — Google's consent screen asks for access to your whole Google
Drive (read and write). A narrower Google permission would only cover files Oncofiles created
itself, so Oncofiles could not see a file you add to the folder yourself, such as a scan from your
phone. Oncofiles reads and changes <strong>only the folder you choose</strong> for a patient.
Inside that folder it may rename files to a standard name, move them into subfolders (for
example Labs or Reports), add a text transcript (<code>.md</code>) next to each file, and keep a
<code>README.md</code> with the folder's rules and a few files that record its own work.
<strong>Oncofiles never permanently deletes anything.</strong> Automatic processing does not
move your files to the trash.</li>
<li><strong>Gmail and Google Calendar</strong> — not used at the moment. If we add them later,
we will ask for your separate consent first.</li>
</ul>
<p>Oncofiles has not completed Google's app verification yet. Google therefore shows the
warning "Google hasn't verified this app" before the consent screen, and allows at most 100
users. The warning means that Google has not yet reviewed the app.</p>
<p>We only access data you explicitly authorize through Google's OAuth consent screen.
You can revoke access at any time via
<a href="https://myaccount.google.com/permissions">Google Account Permissions</a>.</p>

<h2 id="consent">2. Who Can Use Oncofiles, and Consent</h2>
<ul>
<li>New users cannot connect their own Google Drive yet. When this opens, it will open
by invitation first.</li>
<li>A patient can be someone with cancer or someone who keeps general health records.</li>
<li>You can create a patient for yourself, or, as a caregiver, for another person. When you
create a patient you confirm that you are this person, or that you have their consent or
another right (for example as a parent or legal guardian) to manage their health data, and you
agree to the processing of this health data as described here. Health data is a special
category of personal data, and we process it on the basis of this explicit consent
(GDPR Art. 9(2)(a)).</li>
<li>We record that confirmation: the version of this text, the time, and a keyed hash of your
e-mail address. The record does not contain the e-mail address itself.</li>
</ul>

<h2 id="processing">3. How We Process Your Data</h2>
<ul>
<li>Text recognition (OCR), document classification and the extraction of values run
<strong>on a local AI model on our own server in Slovakia (EU)</strong>. Background processing
does not send your documents to any cloud AI service. The server's cloud AI option is switched
off, and the indexing and extraction jobs refuse cloud AI calls in their code even if it were
switched on.</li>
<li>Lab values are extracted from the recognised text by rules. Diagnoses, treatments and
similar facts are read by the local model, and the server checks that their text is printed on
the page. Each value keeps a link to its source document, page and text, and a
verification state: machine-verified, read by the model with its text verified on the page,
unverified, or verified or corrected by a person.</li>
<li><strong>When you ask about your records in your own AI assistant</strong> (for example
Claude or ChatGPT, connected to Oncofiles), the assistant receives what it requests from
Oncofiles for your question (document text, values, summaries) as part of your conversation.
This happens only when you ask, and it is governed by your own agreement with that
assistant's provider, including their retention and training settings.</li>
</ul>
<h3 id="by-email">Documents sent by e-mail</h3>
<p>Each patient can have a private document e-mail address. Whoever sends documents to that
address agrees to the <a href="/terms">terms of service</a> and to the processing of these
documents as described in this policy; the sender receives an e-mail saying so, with the link to
the terms. Only attachments sent from the patient's caregiver address (with the sender
confirmed by the mail system) are added to the patient's folder, where they are processed like
any other document. The text of the caregiver's e-mail is not stored, and no e-mail text is
read by any AI model.
Documents sent to a patient's address by anyone other than the caregiver are held encrypted in
quarantine until the caregiver approves or rejects them; rejected or expired items stay in a
30-day trash and are then deleted (approved ones are filed normally), and the sender gets no
reply. So that the caregiver can decide, the plain text of such an e-mail is kept with them,
encrypted, for the same period and deleted together with the files, and the caregiver's
dashboard shows a small preview picture of each attachment. Documents removed after an
unclaimed trial can still be restored from encrypted backups for up to 6 months.</p>
<p lang="sk"><strong>Dokumenty poslané e-mailom.</strong> Každý pacient môže mať súkromnú
e-mailovú adresu pre dokumenty. Kto pošle dokumenty na túto adresu, súhlasí s
<a href="/terms">podmienkami používania</a> a so spracovaním týchto dokumentov podľa týchto
zásad; odosielateľ dostane e-mail, ktorý to uvádza, s odkazom na podmienky. Do priečinka
pacienta sa pridajú len prílohy poslané z adresy opatrovateľa pacienta (s odosielateľom
potvrdeným poštovým systémom) a spracujú sa ako každý iný dokument. Text e-mailu opatrovateľa
sa neukladá a žiadny text e-mailu nečíta AI model. Dokumenty, ktoré na adresu pacienta pošle
ktokoľvek iný než
opatrovateľ, čakajú zašifrované v karanténe, kým ich opatrovateľ neschváli alebo neodmietne;
odmietnuté alebo vypršané položky zostávajú 30 dní v koši a potom sa vymažú (schválené sa
zaradia ako zvyčajne) a odosielateľ nedostane žiadnu odpoveď. Aby sa opatrovateľ mohol
rozhodnúť, čistý text takého e-mailu sa uchováva spolu s nimi, zašifrovaný, rovnako dlho a
vymaže sa spolu so súbormi; na nástenke opatrovateľa sa zobrazí malý náhľad každej prílohy.
Dokumenty odstránené po nevyužitej skúšobnej dobe sa ešte najviac 6 mesiacov dajú obnoviť
zo šifrovaných záloh.</p>
<p>We do <strong>not</strong> use your data for advertising, profiling, or any purpose
unrelated to managing your medical documents.</p>

<h2 id="storage">4. Where Data Is Stored</h2>
<ul>
<li><strong>On our server in Slovakia (EU), on an encrypted disk</strong> — the patient profile
(name, date of birth, patient type, an optional diagnosis note and the caregiver's e-mail
address), the Google access token for Drive, the document index, the recognised text, extracted
values with their verification state, the consent record (section 2) and the access log
(section 6).</li>
<li><strong>Backups</strong> — local and encrypted, kept for a limited time. Before a bulk update
of extracted values, and before deleting a patient on request, the server also makes a safety
copy of the database; only the 5 newest safety copies are kept.</li>
<li><strong>Logs</strong> — the application's technical log masks e-mail addresses and tokens
and is kept for 7 days. The server sees IP addresses of requests, for security and to limit
abuse.</li>
<li><strong>Your Google Drive</strong> — your original files, plus the transcripts and
<code>README.md</code> described in section 1.</li>
</ul>

<h2 id="sharing">5. Data Sharing</h2>
<p>We do <strong>not</strong> sell, share, or transfer your personal data. Other services
involved:</p>
<ul>
<li><strong>Google</strong> — stores your files in your own Google Drive account.</li>
<li><strong>Your AI assistant</strong> (for example Anthropic's Claude or OpenAI's ChatGPT) —
only what it requests when you ask (section 3).</li>
<li><strong>Our e-mail provider</strong> (e-mail delivery, EU) — if you join the waitlist, your
e-mail address is used to notify us and to write to you about access. When a patient is
created, we receive a notification with the patient's short identifier and the caregiver's
e-mail address, never the patient's name. No medical documents or values are sent. The provider
may add a tracking pixel to our e-mails and rewrite their links through its own servers to count
opens and clicks; we do not add tracking of our own. To leave the list, reply to any of our
e-mails.</li>
<li><strong>Google Analytics</strong> — only on our public web pages (home page and demo), and
only after you accept analytics cookies in the cookie banner. It measures visits (pages viewed,
approximate location, device, where you came from). The logged-in app does not load Google
Analytics, and it never receives your medical documents or values.</li>
</ul>
<p>Hosting is our own server in Slovakia (EU); no third-party hosting or database provider
stores or processes your data.</p>

<h2 id="audit">6. Access Log</h2>
<p>Every request an AI assistant makes to Oncofiles is logged: the tool used, the time, the
patient, how the caller signed in, whether it succeeded, and a keyed hash of the caller's
e-mail address (never the address itself). Arguments are logged only when they cannot contain
health data (for example a document number or an option); everything else is stored only as
its length. We use this log to answer who accessed which records.</p>
<p>Administrators of the service can open any patient's records, for operation and
support.</p>
<p>The web dashboard keeps its own access log: each sign-in, each refused attempt to open a
patient, and each time an administrator opens a patient they are not the caregiver of. A row
holds the time, the event and a keyed hash of the caller's e-mail address (never the address
itself). These rows are deleted automatically after 90 days.</p>

<h2 id="retention">7. Data Retention</h2>
<p>Your data is retained as long as your account is active. Oncofiles does not delete your
documents, their text, extracted values or patient records automatically. Only technical
records expire on a schedule, for example the application log after 7 days (section 4) and the
dashboard access log after 90 days (section 6). You can ask us to delete all data stored about a
patient, including extracted values and the access log; see section 8.
Original files in your Google Drive are never deleted by Oncofiles.</p>

<h2 id="rights">8. Your Rights (GDPR)</h2>
<p>If you are in the EU/EEA, you have the right to:</p>
<ul>
<li>Access your personal data</li>
<li>Request correction or deletion</li>
<li>Data portability (export your data)</li>
<li>Withdraw consent at any time</li>
<li>Lodge a complaint with your supervisory authority (in Slovakia: the Office for Personal
Data Protection of the Slovak Republic)</li>
</ul>
<p>On the dashboard, the patient's caregiver can download an export of everything Oncofiles
stores about the patient (a ZIP of JSON files; the original files stay in your Google Drive),
and can disconnect Google Drive: Oncofiles then deletes its stored Google access token, asks
Google to revoke it when no other patient uses the same Google account, and stops processing
the folder; your Drive files are not changed. You can
also stop Oncofiles' access to your Google Drive at any time in your Google Account
(section 1).</p>
<p>To use the other rights, write to us by e-mail (section 10). Requests for access, export and
deletion are handled by hand, within 30 days. A deletion removes everything stored about the
patient in our database, including extracted values, the consent record (section 2) and the
access log (section 6); files in your Google Drive are never touched. Before the deletion the
server makes one safety copy of the database (section 4); deleted data can remain in that copy
until it is replaced by newer ones, and in the encrypted backups for up to 6 months.</p>

<h2 id="google-api">9. Google API Services Disclosure</h2>
<p>Oncofiles' use and transfer of information received from Google APIs adheres to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API
Services User Data Policy</a>, including the Limited Use requirements.</p>

<h2 id="contact">10. Contact</h2>
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
<p class="meta">Effective: March 25, 2026 · Last updated: November 26, 2026 ·
Consent version 2026-11-26</p>

<p>These terms govern your use of Oncofiles (<a href="https://oncofiles.com">oncofiles.com</a>),
operated by <strong>Instarea s.r.o.</strong> ("we", "us").</p>

<h2 id="service">1. Service Description</h2>
<p>Oncofiles keeps a Google Drive folder of medical documents organized. It recognizes their
text with a local AI model on its own server in Slovakia, extracts values with a link to their
source, and makes the records available to AI assistants such as Claude and ChatGPT when you
ask them. It is designed for cancer patients, people who keep general health records, and
their caregivers.</p>

<h2 id="not-medical-advice">2. Not Medical Advice</h2>
<p><strong>Oncofiles is not a medical device and does not provide medical advice.</strong>
It organizes and presents your existing medical documents. All medical decisions should be
made with your healthcare providers. Automatically recognized text and extracted values can be
wrong; check them against the source document, which every value links to. AI-generated
summaries and analyses are for informational purposes only.</p>

<h2 id="your-data">3. Your Data</h2>
<p>You retain full ownership of your medical documents and data. Oncofiles accesses your
Google services only with your explicit consent, which you can revoke at any time.
See our <a href="/privacy">Privacy Policy</a> for details.</p>
<p>When you create a patient, you confirm that you are this person, or that you have their
consent or another right (for example as a parent or legal guardian) to manage their health
data in Oncofiles. Do not create a patient for someone without that consent or right.</p>

<h3 id="by-email">Documents sent by e-mail</h3>
<p>Whoever sends documents to a patient's document e-mail address agrees to these terms and to
the processing of these documents as described in the <a href="/privacy">Privacy Policy</a>.
The documents are read right away, without a separate consent screen; the sender receives an
e-mail saying so, with the link to these terms. Only attachments from the patient's caregiver
address are added to the patient's folder. The address lets anyone who knows it send documents
to the patient, never read them: keep it private, and create a new one on the dashboard if it
gets into the wrong hands.</p>
<p lang="sk"><strong>Dokumenty poslané e-mailom.</strong> Kto pošle dokumenty na e-mailovú
adresu pacienta pre dokumenty, súhlasí s týmito podmienkami a so spracovaním týchto dokumentov
podľa <a href="/privacy">zásad ochrany súkromia</a>. Dokumenty sa čítajú hneď, bez osobitnej
obrazovky so súhlasom; odosielateľ dostane e-mail, ktorý to uvádza, s odkazom na tieto
podmienky. Do priečinka pacienta sa pridajú len prílohy z adresy opatrovateľa pacienta. Adresa
umožňuje každému, kto ju pozná, dokumenty pacientovi poslať, nikdy nie čítať: nezverejňujte ju,
a ak sa dostane do nesprávnych rúk, vytvorte si na prehľade novú.</p>

<h2 id="account">4. Account and Access</h2>
<ul>
<li>You must have a Google account to use Oncofiles</li>
<li>New users cannot connect their own Google Drive yet. When this opens, it will open
by invitation first</li>
<li>Oncofiles has not completed Google's app verification yet, so Google shows an "unverified
app" warning and allows at most 100 users</li>
<li>You are responsible for maintaining the security of your bearer tokens and credentials</li>
<li>Free tier: up to 500 documents per patient</li>
</ul>

<h2 id="acceptable-use">5. Acceptable Use</h2>
<p>You agree not to:</p>
<ul>
<li>Use Oncofiles for purposes other than personal medical document management</li>
<li>Attempt to access other users' data</li>
<li>Reverse engineer or abuse the API</li>
<li>Use the service for commercial medical data processing without authorization</li>
</ul>

<h2 id="availability">6. Availability</h2>
<p>We aim for high availability but do not guarantee uninterrupted service. Oncofiles
is provided "as is" without warranties of any kind. We are not liable for data loss
beyond what is stored in your Google Drive. Oncofiles never permanently deletes files: it
only renames and moves them inside the folder you chose.</p>

<h2 id="ending">7. Ending Your Use</h2>
<p>You can stop Oncofiles' access to your Google Drive at any time in your
<a href="https://myaccount.google.com/permissions">Google Account</a>, or as the patient's
caregiver with "Disconnect Google Drive" on the dashboard, where you can also download an
export of the data stored about the patient. To have that data deleted (or exported by us),
write to us by e-mail; we handle such requests by hand, within 30 days. Your files in Google
Drive stay where they are.</p>

<h2 id="changes">8. Changes</h2>
<p>We may update these terms. The date at the top of this page shows the latest version.
When we change what you agree to when creating a patient, the consent version changes.</p>

<h2 id="contact">9. Contact</h2>
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
_OG_IMAGE_PNG: bytes | None = None


@mcp.custom_route("/og-image.svg", methods=["GET"])
async def og_image_svg(request: Request) -> HTMLResponse:
    global _OG_IMAGE_SVG  # noqa: PLW0603
    if _OG_IMAGE_SVG is None:
        _OG_IMAGE_SVG = (Path(__file__).parent / "og-image.svg").read_text()
    return HTMLResponse(
        _OG_IMAGE_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@mcp.custom_route("/og-image.png", methods=["GET"])
async def og_image_png(request: Request) -> Response:
    global _OG_IMAGE_PNG  # noqa: PLW0603
    if _OG_IMAGE_PNG is None:
        _OG_IMAGE_PNG = (Path(__file__).parent / "og-image.png").read_bytes()
    # One day: social crawlers re-fetch rarely, and a refreshed image ships
    # with a deploy anyway.
    return Response(
        _OG_IMAGE_PNG,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


_ICON_PNG: bytes | None = None


@mcp.custom_route("/icon-256.png", methods=["GET"])
async def icon_png(request: Request) -> Response:
    """256 px PNG of the favicon. The MCP Registry entry (server.json `icons`)
    points here: registry clients must support PNG, SVG is optional."""
    global _ICON_PNG  # noqa: PLW0603
    if _ICON_PNG is None:
        _ICON_PNG = (Path(__file__).parent / "icon-256.png").read_bytes()
    return Response(
        _ICON_PNG,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# Self-hosted web fonts (#695): every page view used to send the visitor's IP
# to Google Fonts. The woff2 files, their @font-face sheet and the OFL licenses
# live in src/oncofiles/fonts/. Only files present in that folder at import
# time are served (allowlist, so no path tricks reach the filesystem). Font
# file names carry the font version, so they are cached `immutable`; the CSS
# and licence names are stable, so they get one day.
_FONTS_DIR = Path(__file__).parent / "fonts"
_FONT_TYPES = {
    ".woff2": ("font/woff2", "public, max-age=31536000, immutable"),
    ".css": ("text/css; charset=utf-8", "public, max-age=86400"),
    ".txt": ("text/plain; charset=utf-8", "public, max-age=86400"),
}
_FONT_FILES = frozenset(
    p.name for p in _FONTS_DIR.glob("*") if p.is_file() and p.suffix in _FONT_TYPES
)
_FONT_CACHE: dict[str, bytes] = {}


@mcp.custom_route("/fonts/{name}", methods=["GET"])
async def font_asset(request: Request) -> Response:
    name = request.path_params.get("name", "")
    if name not in _FONT_FILES:
        return Response("not found", status_code=404)
    body = _FONT_CACHE.get(name)
    if body is None:
        body = (_FONTS_DIR / name).read_bytes()
        _FONT_CACHE[name] = body
    media_type, cache_control = _FONT_TYPES[Path(name).suffix]
    return Response(body, media_type=media_type, headers={"Cache-Control": cache_control})


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


# Dashboard external assets — extracted from inline blocks to let CSP drop
# `'unsafe-inline'` from script-src-elem / style-src-elem (#501). The same
# read-once-and-cache pattern as the other static assets above.
_DASHBOARD_CSS: str | None = None
_DASHBOARD_JS: str | None = None


@mcp.custom_route("/dashboard.css", methods=["GET"])
async def dashboard_css(request: Request) -> Response:
    global _DASHBOARD_CSS  # noqa: PLW0603
    if _DASHBOARD_CSS is None:
        _DASHBOARD_CSS = (Path(__file__).parent / "dashboard.css").read_text()
    return Response(_DASHBOARD_CSS, media_type="text/css; charset=utf-8")


@mcp.custom_route("/dashboard.js", methods=["GET"])
async def dashboard_js(request: Request) -> Response:
    global _DASHBOARD_JS  # noqa: PLW0603
    if _DASHBOARD_JS is None:
        _DASHBOARD_JS = (Path(__file__).parent / "dashboard.js").read_text()
    return Response(_DASHBOARD_JS, media_type="application/javascript; charset=utf-8")


# Only the two photos the landing uses, as crops (Nov 2026): the full-size
# originals showed an ID-like number, a phone number and an e-mail on a
# contract (02) and a third person's name in a table row (04). The
# originals stay in the repo history but are no longer served.
_PILE_PHOTO_NAMES = {
    "02": "02-papers-pile-crop-web.jpg",
    "04": "04-handwritten-ocr-crop-web.jpg",
}
_PILE_PHOTO_DIR = (
    Path(__file__).parent.parent.parent
    / "assets"
    / "landing"
    / "pile-to-dashboard-2026-04-22"
    / "web"
)


@mcp.custom_route("/assets/pile/{slot}.jpg", methods=["GET"])
async def pile_photo(request: Request) -> Response:
    """Serve resized landing photos (1600px max) for the #470 pile→dashboard block."""
    from starlette.responses import FileResponse

    slot = request.path_params.get("slot", "")
    filename = _PILE_PHOTO_NAMES.get(slot)
    if filename is None:
        return Response("not found", status_code=404)
    path = _PILE_PHOTO_DIR / filename
    if not path.exists():
        return Response("not found", status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ── Movement-atlas showcase (#830): static pages + media, see atlas_showcase.py ──


@mcp.custom_route("/movement-atlas", methods=["GET"])
async def movement_atlas_page(request: Request) -> HTMLResponse:
    """The hosted interactive atlas (three.js). Own CSP: see _csp_for_path."""
    from oncofiles import atlas_showcase

    return HTMLResponse(atlas_showcase.atlas_page_html())


@mcp.custom_route("/movement-atlas/{name}", methods=["GET"])
async def movement_atlas_asset(request: Request) -> Response:
    """Atlas scripts, posters and the two small videos (fixed allowlist, Range → 206)."""
    from oncofiles import atlas_showcase

    return atlas_showcase.asset_response(request.path_params.get("name", ""))


@mcp.custom_route("/pouzitie/pohybovy-atlas", methods=["GET"])
async def movement_atlas_use_case_sk(request: Request) -> HTMLResponse:
    from oncofiles import atlas_showcase

    return HTMLResponse(atlas_showcase.use_case_page_html("sk"))


@mcp.custom_route("/use-cases/movement-atlas", methods=["GET"])
async def movement_atlas_use_case_en(request: Request) -> HTMLResponse:
    from oncofiles import atlas_showcase

    return HTMLResponse(atlas_showcase.use_case_page_html("en"))


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
        "Allow: /llms-sk.txt\n"
        "Disallow: /mcp\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard/verify\n"
        "\n"
        "User-agent: ClaudeBot\n"
        "Allow: /\n"
        "Allow: /llms.txt\n"
        "Allow: /llms-sk.txt\n"
        "Disallow: /mcp\n"
        "Disallow: /api/\n"
        "Disallow: /dashboard/verify\n"
        "\n"
        "User-agent: PerplexityBot\n"
        "Allow: /\n"
        "Allow: /llms.txt\n"
        "Allow: /llms-sk.txt\n"
        "\n"
        "Sitemap: https://oncofiles.com/sitemap.xml\n",
        media_type="text/plain",
    )


@mcp.custom_route("/sitemap.xml", methods=["GET"])
async def sitemap_xml(request: Request) -> HTMLResponse:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return HTMLResponse(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
        ' xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        f"  <url><loc>https://oncofiles.com/</loc><lastmod>{today}</lastmod>"
        "<priority>1.0</priority><changefreq>weekly</changefreq></url>\n"
        # /dashboard (sign-in) and /health (liveness probe) are not content
        # pages and stay out of the sitemap; /demo is public and indexable.
        f"  <url><loc>https://oncofiles.com/demo</loc><lastmod>{today}</lastmod>"
        "<priority>0.8</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="sk" href="https://oncofiles.com/demo"/>'
        '<xhtml:link rel="alternate" hreflang="en" href="https://oncofiles.com/demo?lang=en"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/privacy</loc><lastmod>{today}</lastmod>"
        "<priority>0.5</priority><changefreq>monthly</changefreq></url>\n"
        f"  <url><loc>https://oncofiles.com/terms</loc><lastmod>{today}</lastmod>"
        "<priority>0.5</priority><changefreq>monthly</changefreq></url>\n"
        f"  <url><loc>https://oncofiles.com/onkologia</loc><lastmod>{today}</lastmod>"
        "<priority>0.7</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="sk" href="https://oncofiles.com/onkologia"/>'
        '<xhtml:link rel="alternate" hreflang="en" href="https://oncofiles.com/oncology"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/oncology</loc><lastmod>{today}</lastmod>"
        "<priority>0.7</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="sk" href="https://oncofiles.com/onkologia"/>'
        '<xhtml:link rel="alternate" hreflang="en" href="https://oncofiles.com/oncology"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/eu</loc><lastmod>{today}</lastmod>"
        "<priority>0.8</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="en" href="https://oncofiles.com/eu"/>'
        '<xhtml:link rel="alternate" hreflang="sk" href="https://oncofiles.com/sk"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/sk</loc><lastmod>{today}</lastmod>"
        "<priority>0.8</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="en" href="https://oncofiles.com/eu"/>'
        '<xhtml:link rel="alternate" hreflang="sk" href="https://oncofiles.com/sk"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/pouzitie/pohybovy-atlas</loc><lastmod>{today}</lastmod>"
        "<priority>0.7</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="sk"'
        ' href="https://oncofiles.com/pouzitie/pohybovy-atlas"/>'
        '<xhtml:link rel="alternate" hreflang="en"'
        ' href="https://oncofiles.com/use-cases/movement-atlas"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/use-cases/movement-atlas</loc><lastmod>{today}</lastmod>"
        "<priority>0.7</priority><changefreq>monthly</changefreq>"
        '<xhtml:link rel="alternate" hreflang="sk"'
        ' href="https://oncofiles.com/pouzitie/pohybovy-atlas"/>'
        '<xhtml:link rel="alternate" hreflang="en"'
        ' href="https://oncofiles.com/use-cases/movement-atlas"/>'
        "</url>\n"
        f"  <url><loc>https://oncofiles.com/movement-atlas</loc><lastmod>{today}</lastmod>"
        "<priority>0.6</priority><changefreq>monthly</changefreq></url>\n"
        "</urlset>\n",
        media_type="application/xml",
    )


@mcp.custom_route("/llms.txt", methods=["GET"])
async def llms_txt(request: Request) -> HTMLResponse:
    """LLM-readable site description following the llms.txt standard."""
    tools_count = _count_tools()
    return HTMLResponse(
        f"# Oncofiles\n"
        f"> Medical records where every number has a source\n"
        f"\n"
        f"## About\n"
        f"Oncofiles is an open-source MCP (Model Context Protocol) server for cancer\n"
        f"patients, caregivers and people who want their everyday findings in order.\n"
        f"It reads the documents in the patient's own Google Drive folder, files them\n"
        f"and remembers where every value came from. It provides {tools_count} tools to an\n"
        f"AI assistant. It does not replace a doctor, does not diagnose and does not\n"
        f"propose treatment.\n"
        f"\n"
        f"## Key Features\n"
        f"- Documents stay in the patient's own Google Drive: originals under standard\n"
        f"  names and a .md transcript next to each file. The folder stays readable\n"
        f"  without Oncofiles (no lock-in). Nothing is deleted, at most moved to the trash.\n"
        f"- Documents by e-mail: each patient has a private address. Mail from the\n"
        f"  caregiver's own address lands unchanged in the Inbox of the Drive folder;\n"
        f"  attachments from anyone else wait for the caregiver's approval, the sender\n"
        f"  gets no reply, and no model reads them before approval.\n"
        f"- OCR and classification run on a local model on our own server in the EU\n"
        f"  (Slovakia). Document content is not sent to a cloud model in background\n"
        f"  processing.\n"
        f"- Every lab value carries its sample date, source document, page and line, and a\n"
        f"  verification state: machine-verified, unverified or human-verified.\n"
        f"- FHIR R4: read, search and export of a patient's record.\n"
        f"- Several patients per account; a caregiver sees only the patients assigned\n"
        f"  to them.\n"
        f"- Free up to 500 documents per patient.\n"
        f"- Live since November 2026. To try it, sign in with Google at\n"
        f"  https://oncofiles.com/dashboard and pick the Google Drive folder that\n"
        f"  already holds your documents; if signing in does not work or you have\n"
        f"  questions, write to us.\n"
        f"  The demo with an invented patient needs no sign-in:\n"
        f"  https://oncofiles.com/demo (how to start: https://oncofiles.com/#join).\n"
        f"\n"
        f"## Not only oncology\n"
        f"Oncofiles also has a general (non-oncology) patient profile. Use case: from four\n"
        f"orthopaedic documents and one MRI description read through the Oncofiles connector,\n"
        f"Claude built an\n"
        f"interactive 3D movement atlas (lower back, knee, shoulder) with exercise\n"
        f"suggestions for a physiotherapist to review. The model is schematic, built from\n"
        f"report text, not from scans; it is not medical advice.\n"
        f"- Use case: https://oncofiles.com/use-cases/movement-atlas\n"
        f"- Interactive atlas (Slovak interface): https://oncofiles.com/movement-atlas\n"
        f"\n"
        f"## Technical Details\n"
        f"- Version: {VERSION}\n"
        f"- Protocol: MCP (Model Context Protocol) via FastMCP\n"
        f"- Language: Python 3.12+\n"
        f"- Database: SQLite on our own server in the EU (Slovakia)\n"
        f"- License: MIT\n"
        f"- Repository: https://github.com/peter-fusek/oncofiles\n"
        f"\n"
        f"## Integration\n"
        f"Claude works today through MCP. ChatGPT and Gemini CLI are marked coming soon:\n"
        f"their connection has not been re-verified on the new server yet.\n"
        f"Connect via streamable-http transport at https://oncofiles.com/mcp.\n"
        f"Claude.ai signs in with Google; other MCP clients use an access token.\n"
        f"Only what the user asks about inside the assistant goes to that assistant.\n"
        f"\n"
        f"## Links\n"
        f"- Homepage: https://oncofiles.com\n"
        f"- Demo with an invented patient: https://oncofiles.com/demo\n"
        f"- Topic page: https://oncofiles.com/oncology\n"
        f"- Sign in: https://oncofiles.com/dashboard\n"
        f"- Slovak version: https://oncofiles.com/llms-sk.txt\n"
        f"- Health: https://oncofiles.com/health\n"
        f"- GitHub: https://github.com/peter-fusek/oncofiles\n",
        media_type="text/plain; charset=utf-8",
    )


@mcp.custom_route("/llms-sk.txt", methods=["GET"])
async def llms_sk_txt(request: Request) -> HTMLResponse:
    """Slovak LLM-readable site description (llms.txt standard)."""
    tools_count = _count_tools()
    return HTMLResponse(
        f"# Oncofiles\n"
        f"> Zdravotné záznamy, v ktorých má každé číslo svoj zdroj\n"
        f"\n"
        f"## O projekte\n"
        f"Oncofiles je open-source MCP (Model Context Protocol) server pre onkologických\n"
        f"pacientov, opatrovateľov a ľudí, ktorí chcú mať poriadok v bežných nálezoch.\n"
        f"Prečíta dokumenty v pacientovom vlastnom priečinku na Google Drive, zaradí ich\n"
        f"a pri každej hodnote si pamätá, odkiaľ pochádza. AI asistentovi poskytuje\n"
        f"{tools_count} nástrojov. Nenahrádza lekára, nediagnostikuje a nenavrhuje liečbu.\n"
        f"\n"
        f"## Pre koho\n"
        f"- Onkologickí pacienti a ich rodiny\n"
        f"- Opatrovatelia, ktorí vedú dokumentáciu za blízkeho\n"
        f"- Ľudia, ktorí chcú rozumieť vlastným bežným nálezom\n"
        f"\n"
        f"## Nielen onkológia\n"
        f"Oncofiles má aj všeobecný (neonkologický) profil pacienta. Príklad použitia: zo\n"
        f"štyroch ortopedických dokumentov a jedného popisu MR, prečítaných cez konektor\n"
        f"Oncofiles,\n"
        f"postavil Claude\n"
        f"interaktívny 3D pohybový atlas (chrbát, koleno, rameno) s návrhmi cvikov na\n"
        f"posúdenie fyzioterapeutom. Model je schematický, zostavený z textu správ, nie zo\n"
        f"snímok; nie je to lekárska rada.\n"
        f"- Príklad použitia: https://oncofiles.com/pouzitie/pohybovy-atlas\n"
        f"- Interaktívny atlas: https://oncofiles.com/movement-atlas\n"
        f"\n"
        f"## Kľúčové funkcie\n"
        f"- Dokumenty zostávajú vo vlastnom Google Drive pacienta: originály so\n"
        f"  štandardnými názvami a vedľa každého súboru prepis .md. Priečinok sa dá čítať\n"
        f"  aj bez Oncofiles (žiadne viazanie). Nič sa nemaže, nanajvýš sa presunie do koša.\n"
        f"- Dokumenty e-mailom: každý pacient má súkromnú adresu. Pošta z vlastnej adresy\n"
        f"  opatrovateľa príde nezmenená do priečinka Inbox v Drive; prílohy od kohokoľvek\n"
        f"  iného čakajú na jeho schválenie, odosielateľ nedostane odpoveď a pred\n"
        f"  schválením ich nečíta žiadny model.\n"
        f"- OCR a zatriedenie bežia na lokálnom modeli na našom serveri v EÚ (Slovensko).\n"
        f"  Obsah dokumentov sa pri spracovaní na pozadí neposiela do cloudového modelu.\n"
        f"- Každá laboratórna hodnota má dátum odberu, zdrojový dokument, stranu a riadok\n"
        f"  a stav overenia: overené strojom, neoverené alebo overené človekom.\n"
        f"- FHIR R4: čítanie, vyhľadávanie a export záznamov pacienta.\n"
        f"- Viac pacientov pod jedným účtom; opatrovateľ vidí len pacientov, ku ktorým\n"
        f"  má prístup.\n"
        f"- Zadarmo do 500 dokumentov na pacienta.\n"
        f"- V prevádzke od novembra 2026. Na vyskúšanie sa prihláste cez Google na\n"
        f"  https://oncofiles.com/dashboard a vyberte priečinok na Google Drive, v ktorom\n"
        f"  už dokumentáciu máte; ak sa vám nedarí prihlásiť alebo máte otázky,\n"
        f"  napíšte nám.\n"
        f"  Ukážka s vymyslenou pacientkou je bez prihlásenia:\n"
        f"  https://oncofiles.com/demo (ako začať: https://oncofiles.com/#join).\n"
        f"\n"
        f"## Ako sa líši od štátnych systémov (eZdravie / OnkoAsist / NCZI)\n"
        f"- Oncofiles je pacientsky nástroj: originály dokumentov zostávajú vo vašom\n"
        f"  Google Drive.\n"
        f"- Open-source pod licenciou MIT: kód je verejne auditovateľný.\n"
        f"- Dopĺňa, nenahrádza, oficiálnu zdravotnú dokumentáciu.\n"
        f"\n"
        f"## Bezpečnosť a súkromie\n"
        f"- Originály a prepisy sú vo vašom Google Drive. Index dokumentov, prečítaný\n"
        f"  text a hodnoty s ich pôvodom sú v databáze na našom serveri v EÚ (Slovensko),\n"
        f"  na šifrovanom disku.\n"
        f"- Pacienti sú v databáze od seba oddelení a každý prístup sa overuje.\n"
        f"- Do AI asistenta ide len to, na čo sa v ňom používateľ sám spýta.\n"
        f"\n"
        f"## Technické detaily\n"
        f"- Verzia: {VERSION}\n"
        f"- Protokol: MCP (Model Context Protocol) cez FastMCP\n"
        f"- Jazyk: Python 3.12+\n"
        f"- Databáza: SQLite na našom serveri v EÚ (Slovensko)\n"
        f"- Licencia: MIT\n"
        f"- Repozitár: https://github.com/peter-fusek/oncofiles\n"
        f"\n"
        f"## Integrácia\n"
        f"Claude funguje už dnes cez MCP. ChatGPT a Gemini CLI označujeme „pripravujeme“:\n"
        f"ich spojenie sme na novom serveri ešte znova neoverili.\n"
        f"Pripojenie cez streamable-http transport na https://oncofiles.com/mcp.\n"
        f"Claude.ai sa prihlasuje cez Google; ostatné MCP klienty používajú prístupový token.\n"
        f"\n"
        f"## Odkazy\n"
        f"- Domovská stránka: https://oncofiles.com\n"
        f"- Ukážka s vymyslenou pacientkou: https://oncofiles.com/demo\n"
        f"- Téma (SK): https://oncofiles.com/onkologia\n"
        f"- Téma (EN): https://oncofiles.com/oncology\n"
        f"- Prihlásenie: https://oncofiles.com/dashboard\n"
        f"- Anglická verzia: https://oncofiles.com/llms.txt\n"
        f"- GitHub: https://github.com/peter-fusek/oncofiles\n",
        media_type="text/plain; charset=utf-8",
    )


# ── SEO topic pages: /onkologia (SK) + /oncology (EN) ────────────────────────

_TOPIC_PAGE_CSS = """
  :root { color-scheme:light dark; --bg:#ffffff; --text:#000059; --text-muted:#475569;
    --accent:#000059; --border:#cbd5e1; --surface:#f8fafc; --navy:#000059; --coral:#ff4646;
    --turq:#33d6d6; --peach:#ffd0a8; }
  @media (prefers-color-scheme: dark) { :root { --bg:#000040; --text:#ffffff;
    --text-muted:#c9d1ee; --accent:#ffffff; --border:#2e2e90; --surface:#0c0c66; } }
  * { margin:0; padding:0; box-sizing:border-box; }
  html { overflow-x:hidden; }
  body { font-family:'DM Sans',-apple-system,BlinkMacSystemFont,sans-serif;
    background:var(--bg); color:var(--text); line-height:1.65;
    overflow-x:hidden; position:relative; }
  :focus-visible { outline:3px solid var(--accent); outline-offset:3px; }
  .container { max-width:760px; margin:0 auto; padding:2.5rem 1.5rem 4rem; }
  header.top { background:var(--navy); }
  header.top .container { padding:0 1.5rem; min-height:64px; display:flex;
    justify-content:space-between; align-items:center; }
  header.top :focus-visible { outline-color:var(--turq); }
  .logo { font-family:'Outfit','DM Sans',sans-serif; font-weight:700; font-size:1.55rem;
    letter-spacing:-0.035em; color:#ffffff; text-decoration:none; text-transform:lowercase; }
  .logo span { color:#ffffff; }
  .logo::after { content:'.'; color:var(--coral); }
  .lang-switch a { color:#d6dcf0; text-decoration:none; font-size:0.9rem; font-weight:700;
    display:inline-flex; align-items:center; justify-content:center; min-width:44px;
    min-height:44px; }
  .lang-switch a:hover { color:#ffffff; }
  h1 { font-family:'Outfit','DM Sans',sans-serif; font-weight:700; font-size:2.35rem;
    line-height:1.1; margin-bottom:1rem; letter-spacing:-0.02em; }
  h2 { font-family:'Outfit','DM Sans',sans-serif; font-weight:700; font-size:1.45rem;
    letter-spacing:-0.01em; margin:2.25rem 0 0.75rem; color:var(--text); }
  p { margin-bottom:1rem; color:var(--text-muted); }
  ul { margin:0 0 1.5rem 1.25rem; color:var(--text-muted); }
  ul li { margin-bottom:0.5rem; }
  a { color:var(--accent); text-underline-offset:3px; }
  .cta { display:inline-block; margin-top:1.5rem; padding:0.8rem 1.5rem;
    background:var(--coral); color:#000059; text-decoration:none; border-radius:10px;
    font-weight:700; }
  .cta:hover { background:#ff6b6b; }
  footer { margin-top:3rem; padding-top:1.5rem; border-top:1px solid var(--border);
    font-size:0.85rem; color:var(--text-muted); }
  .intro { font-size:1.075rem; color:var(--text); }
"""


def _topic_page_html(
    *,
    lang: str,
    title: str,
    description: str,
    canonical: str,
    alt_hreflang: str,
    alt_href: str,
    body_html: str,
) -> str:
    return f"""<!DOCTYPE html>
<html lang="{lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{canonical}">
<link rel="alternate" hreflang="{lang}" href="{canonical}">
<link rel="alternate" hreflang="{alt_hreflang}" href="{alt_href}">
<link rel="alternate" hreflang="x-default" href="https://oncofiles.com/">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{description}">
<meta property="og:type" content="article">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="https://oncofiles.com/og-image.png">
<link rel="stylesheet" href="/fonts/fonts.css">
<style>{_TOPIC_PAGE_CSS}</style>
</head>
<body>
<header class="top"><div class="container">
  <a class="logo" href="/">Onco<span>files</span></a>
  <span class="lang-switch">
    <a href="{alt_href}" hreflang="{alt_hreflang}">{alt_hreflang.upper()}</a>
  </span>
</div></header>
<main class="container">
{body_html}
</main>
<footer class="container">
<a href="/">&larr; oncofiles.com</a> &middot;
<a href="https://github.com/peter-fusek/oncofiles">GitHub</a> &middot;
<a href="mailto:peter.fusek@instarea.sk">peter.fusek@instarea.sk</a>
</footer>
</body>
</html>"""


@mcp.custom_route("/onkologia", methods=["GET"])
async def onkologia_page(request: Request) -> HTMLResponse:
    """SK topic page — AI pre onkologických pacientov."""
    body = """
<h1>AI pre onkologických pacientov: správa zdravotných dokumentov</h1>
<p class="intro">Onkologická dokumentácia sa hromadí rýchlo: laboratórne výsledky,
popisy CT a MR, lekárske správy, prílohy z mailov a fotky z mobilu. Oncofiles ich
prečíta, pomenuje a zaradí vo vašom Google Drive a pri každej hodnote si pamätá
stranu a riadok, odkiaľ pochádza. Potom sa pýtate v Claude a každá odpoveď ukáže
svoj zdroj.</p>

<h2>Čo Oncofiles rieši</h2>
<p>Keď v rodine niekto ochorie, stanete sa archivárom. Lekár má desať minút a vy
neviete nájsť posledný výsledok. Oncofiles drží priečinok usporiadaný podľa jeho
vlastných pravidiel v súbore README a z laboratórnych výsledkov vytiahne hodnoty,
ktoré si viete overiť v zdrojovom dokumente.</p>

<h2>Ako to funguje</h2>
<ul>
<li><strong>Dokumenty zostávajú vo vašom Google Drive.</strong> Originály so
štandardnými názvami a vedľa každého súboru prepis .md. Priečinok sa dá čítať aj
bez Oncofiles, takže môžete kedykoľvek odísť.</li>
<li><strong>Alebo ich pošlite e-mailom.</strong> Každý pacient má súkromnú adresu
na dokumenty. Pošta z vašej adresy príde nezmenená do priečinka Inbox vo vašom
Drive; prílohy od kohokoľvek iného čakajú na vaše schválenie a odosielateľ
nedostane žiadnu odpoveď.</li>
<li><strong>Číta ich lokálny model na našom serveri v EÚ (Slovensko).</strong>
Obsah dokumentov sa pri spracovaní na pozadí neposiela do cloudového modelu.</li>
<li><strong>Každá hodnota má svoj zdroj.</strong> Dátum odberu, dokument, strana
a riadok, z ktorého bola prečítaná, a stav overenia: overené strojom, neoverené
alebo overené človekom.</li>
<li><strong>Pýtate sa vo vlastnom AI asistentovi.</strong> Claude funguje už dnes
cez MCP; ChatGPT a Gemini CLI pripravujeme. Každá odpoveď ukáže svoj zdroj.</li>
<li><strong>Otvorené formáty.</strong> FHIR R4 na čítanie, vyhľadávanie a export
záznamov; kód je otvorený pod licenciou MIT.</li>
</ul>

<h2>Ako to dopĺňa eZdravie, OnkoAsist a NCZI</h2>
<p>Štátne systémy (eZdravie, OnkoAsist, NCZI) slúžia na oficiálnu komunikáciu
medzi zdravotníckymi zariadeniami. Oncofiles je pacientsky nástroj: neprepisuje
oficiálnu zdravotnú kartu, ale pomáha pacientovi rozumieť vlastným dokumentom a
pripraviť sa na ďalšiu konzultáciu.</p>

<h2>Pre koho je to určené</h2>
<ul>
<li>Onkologickí pacienti, ktorí chcú rozumieť vlastným výsledkom.</li>
<li>Opatrovatelia, ktorí vedú dokumentáciu za blízkeho: pod jedným účtom môže byť
viac pacientov a údaje sa medzi nimi nemiešajú.</li>
<li>Ľudia, ktorí chcú mať poriadok aj v bežných nálezoch.</li>
</ul>

<p>Oncofiles je zadarmo do 500 dokumentov na pacienta. Vyskúšať ho môžete hneď:
pozrite si ukážku s vymyslenou pacientkou, alebo sa prihláste cez Google a vyberte
priečinok na Google Drive, v ktorom už dokumentáciu máte (ak sa vám nedarí
prihlásiť alebo máte otázky, napíšte nám). Oncofiles nenahrádza lekára, nediagnostikuje a nenavrhuje
liečbu.</p>

<a class="cta" href="/#join">Vyskúšať &rarr;</a>
<p style="margin-top:1rem; font-size:0.875rem;"><a href="/demo">Pozrieť ukážku s
vymyslenou pacientkou</a> &middot; <a href="/pouzitie/pohybovy-atlas">Nielen
onkológia: pohybový atlas z vlastných správ</a></p>
"""
    html = _topic_page_html(
        lang="sk",
        title="AI pre onkologických pacientov — Oncofiles",
        description=(
            "Správa zdravotných dokumentov pre onkologických pacientov. "
            "Dokumenty zostávajú vo vašom Google Drive, číta ich lokálny model "
            "na našom serveri v EÚ a každá hodnota má svoj zdroj. Pýtate sa v Claude."
        ),
        canonical="https://oncofiles.com/onkologia",
        alt_hreflang="en",
        alt_href="https://oncofiles.com/oncology",
        body_html=body,
    )
    return HTMLResponse(html)


@mcp.custom_route("/oncology", methods=["GET"])
async def oncology_page(request: Request) -> HTMLResponse:
    """EN topic page — AI for cancer patients and caregivers."""
    body = """
<h1>AI for cancer patients: oncology document management</h1>
<p class="intro">Oncology paperwork piles up fast: lab results, CT and MRI
descriptions, doctors&rsquo; reports, email attachments and phone photos.
Oncofiles reads them, names them and files them in your own Google Drive, and
remembers the page and line every value came from. Then you ask in Claude, and
every answer shows its source.</p>

<h2>What Oncofiles solves</h2>
<p>When someone in the family is ill, you become the archivist. The doctor has
ten minutes and you cannot find the last result. Oncofiles keeps the folder in
order under its own rules in a README file, and takes the values out of lab
results so that you can check each one against its source document.</p>

<h2>How it works</h2>
<ul>
<li><strong>Your documents stay in your Google Drive.</strong> The originals
under standard names and a .md transcript next to each file. The folder stays
readable without Oncofiles, so you can leave at any time.</li>
<li><strong>Or send them by e-mail.</strong> Every patient has a private address
for documents. Mail from your own address arrives unchanged in the Inbox of your
Drive folder; attachments from anyone else wait for your approval, and the sender
gets no reply.</li>
<li><strong>A local model reads them on our own server in the EU (Slovakia).</strong>
Document content is not sent to a cloud model in background processing.</li>
<li><strong>Every value has its source.</strong> The sample date, the document,
the page and the line it was read from, and a verification state:
machine-verified, unverified or human-verified.</li>
<li><strong>You ask in your own AI assistant.</strong> Claude works today through
MCP; ChatGPT and Gemini CLI are coming soon. Every answer shows its source.</li>
<li><strong>Open formats.</strong> FHIR R4 for reading, searching and exporting
records; the code is open under the MIT license.</li>
</ul>

<h2>How it complements EHR and patient portals</h2>
<p>National health records and patient portals are built for provider-to-provider
handoffs. Oncofiles is a <em>patient-side</em> tool: it does not replace official
records but helps the patient understand their own documents and prepare for the
next consultation.</p>

<h2>Who it&rsquo;s for</h2>
<ul>
<li>Cancer patients who want to understand their own results.</li>
<li>Caregivers managing paperwork for someone close: one account can hold several
patients and their data is never mixed.</li>
<li>People who want their everyday findings in order too.</li>
</ul>

<p>Oncofiles is free up to 500 documents per patient. You can try it right away:
see the demo with an invented patient, or sign in with Google and pick the Google
Drive folder that already holds your documents (if signing in does not work or
you have questions, write to us). Oncofiles does not replace your doctor, does not diagnose and does
not propose treatment.</p>

<a class="cta" href="/#join">Try it &rarr;</a>
<p style="margin-top:1rem; font-size:0.875rem;"><a href="/demo">See the demo with
an invented patient</a> &middot; <a href="/use-cases/movement-atlas">Not only
oncology: a movement atlas from your own reports</a></p>
"""
    html = _topic_page_html(
        lang="en",
        title="AI for Cancer Patients — Oncology Document Management | Oncofiles",
        description=(
            "Oncology document management for cancer patients and caregivers. "
            "Documents stay in your Google Drive, a local model on our own EU server "
            "reads them, and every value has its source. Ask in Claude."
        ),
        canonical="https://oncofiles.com/oncology",
        alt_hreflang="sk",
        alt_href="https://oncofiles.com/onkologia",
        body_html=body,
    )
    return HTMLResponse(html)


# ── EU positioning pages: /eu (EN) + /sk (SK with press angles) ──────────────

_EU_PAGE_EXTRA_CSS = """
  .hero { padding:2.5rem 0 1.5rem; }
  .eyebrow { display:inline-block; font-size:0.85rem; color:#000059; font-weight:700;
    background:var(--turq); border-radius:999px; padding:0.15rem 0.8rem;
    margin-bottom:0.75rem; }
  h1 .accent { color:var(--text); }
  .compare { width:100%; border-collapse:collapse; margin:1.5rem 0 2rem;
    font-size:0.95rem; background:var(--bg); border:1px solid var(--border);
    border-radius:6px; overflow:hidden; }
  .compare th, .compare td { padding:0.75rem 0.9rem; text-align:left;
    border-bottom:1px solid var(--border); vertical-align:top; }
  .compare th { background:var(--surface); font-weight:700; color:var(--text);
    font-size:0.85rem; }
  .compare td:first-child { font-weight:500; color:var(--text); white-space:nowrap; }
  .compare td strong { color:var(--text); }
  .callout { background:var(--peach); padding:1rem 1.25rem; margin:1.5rem 0;
    border-radius:14px; }
  .callout p, .callout a, .callout strong { margin:0; color:#000059; }
  .press-angles { background:var(--surface); border:1px solid var(--border);
    padding:1.25rem 1.5rem; border-radius:14px; margin:1.5rem 0; }
  .press-angles h3 { font-size:1rem; margin-bottom:0.5rem; }
  .press-angles p { font-size:0.9rem; margin-bottom:0.5rem; }
"""


def _eu_comparison_schema_jsonld(canonical: str, name: str, description: str) -> str:
    """JSON-LD for Schema.org SoftwareApplication + ComparisonPage rich results."""
    return json.dumps(
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "SoftwareApplication",
                    "name": "Oncofiles",
                    "url": "https://oncofiles.com/",
                    "applicationCategory": "HealthApplication",
                    "operatingSystem": "Web",
                    "offers": {
                        "@type": "Offer",
                        "price": "0",
                        "priceCurrency": "EUR",
                        "description": "Free up to 500 documents per patient",
                    },
                    "availableOnDevice": [
                        "Desktop",
                        "Mobile",
                        "MCP clients (Claude)",
                    ],
                    "areaServed": [
                        {"@type": "AdministrativeArea", "name": "European Union"},
                        {"@type": "Country", "name": "Slovakia"},
                        {"@type": "Country", "name": "Czech Republic"},
                        {"@type": "Country", "name": "Germany"},
                        {"@type": "Country", "name": "Austria"},
                        {"@type": "Country", "name": "Switzerland"},
                        {"@type": "Country", "name": "United Kingdom"},
                    ],
                    "license": "https://github.com/peter-fusek/oncofiles/blob/main/LICENSE",
                    "isAccessibleForFree": True,
                },
                {
                    "@type": "WebPage",
                    "@id": canonical,
                    "url": canonical,
                    "name": name,
                    "description": description,
                    "inLanguage": "en" if canonical.endswith("/eu") else "sk",
                    "isPartOf": {
                        "@type": "WebSite",
                        "name": "Oncofiles",
                        "url": "https://oncofiles.com/",
                    },
                    "about": {
                        "@type": "MedicalAudience",
                        "audienceType": "Patient",
                        "healthCondition": {
                            "@type": "MedicalCondition",
                            "name": "Cancer",
                        },
                    },
                },
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


_EU_COMPARISON_TABLE_EN = """
<table class="compare" aria-label="Oncofiles vs ChatGPT Health vs HealthEx">
  <thead>
    <tr>
      <th>Feature</th>
      <th>Oncofiles</th>
      <th>ChatGPT Health</th>
      <th>HealthEx (Anthropic US)</th>
      <th>National EHR<br><small>(eZdravie / ELGA)</small></th>
    </tr>
  </thead>
  <tbody>
    <tr><td>Available in EU / CH / UK</td>
      <td><strong>Yes</strong></td>
      <td>No (excluded)</td>
      <td>No (US only)</td>
      <td>National-only</td></tr>
    <tr><td>Where documents are read</td>
      <td><strong>Local model on our own server in the EU (Slovakia)</strong></td>
      <td>N/A</td>
      <td>N/A</td>
      <td>Yes</td></tr>
    <tr><td>Works in your AI assistant</td>
      <td><strong>Claude today (MCP); ChatGPT coming soon</strong></td>
      <td>ChatGPT only</td>
      <td>Via US partnership</td>
      <td>No</td></tr>
    <tr><td>Source shown for every value</td>
      <td><strong>Yes &mdash; document, page, line, verification state</strong></td>
      <td>N/A</td>
      <td>N/A</td>
      <td>N/A</td></tr>
    <tr><td>Open-source (MIT)</td>
      <td><strong>Yes</strong></td>
      <td>No</td>
      <td>No</td>
      <td>N/A</td></tr>
    <tr><td>Price for patients</td>
      <td><strong>Free up to 500 documents per patient</strong></td>
      <td>Part of paid ChatGPT</td>
      <td>Enterprise only</td>
      <td>Free (state-funded)</td></tr>
  </tbody>
</table>
"""

_EU_COMPARISON_TABLE_SK = """
<table class="compare" aria-label="Oncofiles vs ChatGPT Health vs HealthEx">
  <thead>
    <tr>
      <th>Funkcia</th>
      <th>Oncofiles</th>
      <th>ChatGPT Health</th>
      <th>HealthEx (Anthropic US)</th>
      <th>Štátne systémy<br><small>(eZdravie / OnkoAsist)</small></th>
    </tr>
  </thead>
  <tbody>
    <tr><td>Dostupné v EÚ / CH / UK</td>
      <td><strong>Áno</strong></td>
      <td>Nie (vylúčené)</td>
      <td>Nie (iba US)</td>
      <td>Len národné</td></tr>
    <tr><td>Kde sa dokumenty čítajú</td>
      <td><strong>Lokálny model na našom serveri v EÚ (Slovensko)</strong></td>
      <td>N/A</td>
      <td>N/A</td>
      <td>Áno</td></tr>
    <tr><td>Funguje vo vašom AI asistentovi</td>
      <td><strong>Claude už dnes (MCP); ChatGPT pripravujeme</strong></td>
      <td>Len ChatGPT</td>
      <td>Cez US partnerstvo</td>
      <td>Nie</td></tr>
    <tr><td>Zdroj pri každej hodnote</td>
      <td><strong>Áno &mdash; dokument, strana, riadok, stav overenia</strong></td>
      <td>N/A</td>
      <td>N/A</td>
      <td>N/A</td></tr>
    <tr><td>Open-source (MIT)</td>
      <td><strong>Áno</strong></td>
      <td>Nie</td>
      <td>Nie</td>
      <td>N/A</td></tr>
    <tr><td>Cena pre pacienta</td>
      <td><strong>Zadarmo do 500 dokumentov na pacienta</strong></td>
      <td>Časť platenej ChatGPT</td>
      <td>Iba podnikové</td>
      <td>Zadarmo (štátne)</td></tr>
  </tbody>
</table>
"""


@mcp.custom_route("/eu", methods=["GET"])
async def eu_page(request: Request) -> HTMLResponse:
    """EN EU-positioning landing page — #447 Phase A."""
    canonical = "https://oncofiles.com/eu"
    title = "AI for EU Cancer Patients — Read on Our Own Server in the EU | Oncofiles"
    description = (
        "Oncofiles is an open-source medical record tool built for European "
        "cancer patients. Documents stay in your Google Drive, a local model on our "
        "own server in the EU reads them, and you ask in Claude. Built in the EEA, "
        "where ChatGPT Health is not offered."
    )
    body = f"""
<section class="hero">
  <span class="eyebrow">For EU cancer patients &amp; caregivers</span>
  <h1>AI for European cancer patients &mdash;
    <span class="accent">read on our own server in the EU</span></h1>
  <p class="intro">Oncofiles is a medical record tool built for patients in the
  EEA, Switzerland and the UK. Your documents stay in your own Google Drive, a
  local model on our own server in the EU (Slovakia) reads them, and you ask in
  Claude, where every answer shows its source.</p>
</section>

<div class="callout">
<p><strong>Why this matters now:</strong> ChatGPT Health launched without the EEA,
Switzerland and the UK, and HealthEx announced a partnership with Anthropic for
the US. Oncofiles is built and hosted in the EU.</p>
</div>

<h2>What makes Oncofiles different</h2>
<ul>
<li><strong>Your documents stay in your Google Drive.</strong> The originals
under standard names and a .md transcript next to each file. The folder stays
readable without Oncofiles, so you can leave at any time.</li>
<li><strong>Or send them by e-mail.</strong> Every patient has a private address
for documents. Mail from your own address arrives unchanged in the Inbox of your
Drive folder; attachments from anyone else wait for your approval, and the sender
gets no reply.</li>
<li><strong>A local model reads them on our own server in the EU (Slovakia).</strong>
Document content is not sent to a cloud model in background processing.</li>
<li><strong>Every value has its source.</strong> The sample date, the document,
the page and the line it was read from, and a verification state:
machine-verified, unverified or human-verified.</li>
<li><strong>You ask in your own AI assistant.</strong> Claude works today through
MCP; ChatGPT and Gemini CLI are coming soon. Every answer shows its source.</li>
<li><strong>Open formats.</strong> FHIR R4 for reading, searching and exporting
records; the code is open under the MIT license.</li>
</ul>

<h2>How Oncofiles compares</h2>
{_EU_COMPARISON_TABLE_EN}

<h2>Built for the European oncology reality</h2>
<p>Many EU cancer patients move between a public oncology centre, a private
laboratory, a second-opinion clinic and a GP, each with its own file names and
formats. Oncofiles is the <em>patient-side</em> layer that keeps those documents
in one folder without replacing the official records.</p>

<h2>Data governance</h2>
<ul>
<li>Original files and .md transcripts stay in <strong>your</strong> Google Drive.</li>
<li>The document index, the extracted text and the values with their provenance
are stored in a database on our own server in the EU (Slovakia), on an encrypted
disk.</li>
<li>Only what you ask about inside your AI assistant goes to that assistant.</li>
<li>Open-source: inspect what the code does with your data at any time.</li>
</ul>

<p>Oncofiles is free up to 500 documents per patient. You can try it right away:
see the demo with an invented patient, or sign in with Google and pick the Google
Drive folder that already holds your documents (if signing in does not work or
you have questions, write to us). Oncofiles does not replace your doctor, does not diagnose and does
not propose treatment.</p>

<a class="cta" href="/#join">Try it &rarr;</a>
<p style="margin-top:1rem; font-size:0.875rem;"><a href="/demo">See the demo with
an invented patient</a> &middot; <a href="/use-cases/movement-atlas">Not only
oncology: a movement atlas from your own reports</a></p>
<p style="margin-top:1rem; font-size:0.875rem;">Prefer the Slovak version?
<a href="/sk">Prejsť na slovenskú verziu &rarr;</a></p>
"""
    html = _topic_page_html(
        lang="en",
        title=title,
        description=description,
        canonical=canonical,
        alt_hreflang="sk",
        alt_href="https://oncofiles.com/sk",
        body_html=body,
    )
    # Inject comparison-table CSS + Schema.org JSON-LD before </head>.
    jsonld = _eu_comparison_schema_jsonld(canonical, title, description)
    injection = (
        f'<style>{_EU_PAGE_EXTRA_CSS}</style><script type="application/ld+json">{jsonld}</script>'
    )
    html = html.replace("</head>", f"{injection}</head>")
    return HTMLResponse(html)


@mcp.custom_route("/sk", methods=["GET"])
async def sk_page(request: Request) -> HTMLResponse:
    """SK mirror of /eu with Slovak-specific press angles — #447 Phase A."""
    canonical = "https://oncofiles.com/sk"
    title = "AI pre onkologických pacientov na Slovensku — Oncofiles"
    description = (
        "Oncofiles je open-source nástroj pre onkologických pacientov na "
        "Slovensku a v EÚ. Dokumenty zostávajú vo vašom Google Drive, číta ich "
        "lokálny model na našom serveri v EÚ a pýtate sa v Claude."
    )
    body = f"""
<section class="hero">
  <span class="eyebrow">Pre slovenských onkologických pacientov a opatrovateľov</span>
  <h1>AI pre onkologických pacientov &mdash;
    <span class="accent">číta sa na našom serveri v EÚ</span></h1>
  <p class="intro">Oncofiles je open-source nástroj na spravovanie zdravotnej
  dokumentácie pre pacientov na Slovensku a v ostatných krajinách EÚ. Dokumenty
  zostávajú vo vašom vlastnom Google Drive, číta ich lokálny model na našom
  serveri v EÚ (Slovensko) a pýtate sa v Claude, kde každá odpoveď ukáže svoj
  zdroj.</p>
</section>

<div class="callout">
<p><strong>Prečo teraz:</strong> ChatGPT Health bol spustený <em>bez</em> EHP,
Švajčiarska a UK a HealthEx ohlásil partnerstvo s Anthropic pre americký trh.
Oncofiles vzniká a beží v EÚ.</p>
</div>

<h2>Čo robí Oncofiles iným</h2>
<ul>
<li><strong>Dokumenty zostávajú vo vašom Google Drive.</strong> Originály so
štandardnými názvami a vedľa každého súboru prepis .md. Priečinok sa dá čítať aj
bez Oncofiles, takže môžete kedykoľvek odísť.</li>
<li><strong>Alebo ich pošlite e-mailom.</strong> Každý pacient má súkromnú adresu
na dokumenty. Pošta z vašej adresy príde nezmenená do priečinka Inbox vo vašom
Drive; prílohy od kohokoľvek iného čakajú na vaše schválenie a odosielateľ
nedostane žiadnu odpoveď.</li>
<li><strong>Číta ich lokálny model na našom serveri v EÚ (Slovensko).</strong>
Obsah dokumentov sa pri spracovaní na pozadí neposiela do cloudového modelu.</li>
<li><strong>Každá hodnota má svoj zdroj.</strong> Dátum odberu, dokument, strana
a riadok, z ktorého bola prečítaná, a stav overenia: overené strojom, neoverené
alebo overené človekom.</li>
<li><strong>Pýtate sa vo vlastnom AI asistentovi.</strong> Claude funguje už dnes
cez MCP; ChatGPT a Gemini CLI pripravujeme. Každá odpoveď ukáže svoj zdroj.</li>
<li><strong>Otvorené formáty.</strong> FHIR R4 na čítanie, vyhľadávanie a export
záznamov; kód je otvorený pod licenciou MIT.</li>
</ul>

<h2>Ako sa Oncofiles porovnáva</h2>
{_EU_COMPARISON_TABLE_SK}

<h2>Postavené pre slovenskú onkologickú realitu</h2>
<p>Mnohí slovenskí onkologickí pacienti chodia medzi Národným onkologickým
ústavom (NOU), Onkologickým ústavom sv. Alžbety (OÚSA), súkromnými laboratóriami,
druhým názorom a praktickým lekárom, a každé miesto má vlastné názvy súborov a
formáty. Tlačené slovenské správy čítame spoľahlivo, staré rukou písané záznamy
zatiaľ slabšie. Oncofiles je <em>pacientska vrstva</em>, ktorá tieto dokumenty
drží v jednom priečinku bez toho, aby nahradila oficiálnu zdravotnú kartu.</p>

<h2>Dopĺňa eZdravie, OnkoAsist a NCZI</h2>
<p>Štátne systémy slúžia na komunikáciu medzi zdravotníckymi zariadeniami.
Oncofiles je pacientsky nástroj: nekopíruje eZdravie, ale pomáha pacientovi
rozumieť vlastným dokumentom a prísť na ďalšiu konzultáciu pripravený.</p>

<h2>Ochrana dát</h2>
<ul>
<li>Originálne súbory a prepisy .md zostávajú vo <strong>vašom</strong> Google
Drive.</li>
<li>Index dokumentov, prečítaný text a hodnoty s ich pôvodom sú uložené v databáze
na našom serveri v EÚ (Slovensko), na šifrovanom disku.</li>
<li>Do AI asistenta ide len to, na čo sa v ňom sami spýtate.</li>
<li>Open-source: kedykoľvek si overíte, čo kód s vašimi dátami robí.</li>
</ul>

<div class="press-angles">
<h3>Pre novinárov a redakcie</h3>
<p><strong>Kľúčové body:</strong> Slovenský open-source projekt. Dokumenty číta
lokálny model na vlastnom serveri v EÚ a každá hodnota má dohľadateľný zdroj.
Zadarmo do 500 dokumentov na pacienta. Autor: Peter Fusek,
peter.fusek@instarea.sk.</p>
<p><strong>K dispozícii:</strong> rozhovor s autorom a technická ukážka.</p>
</div>

<p>Oncofiles je zadarmo do 500 dokumentov na pacienta. Vyskúšať ho môžete hneď:
pozrite si ukážku s vymyslenou pacientkou, alebo sa prihláste cez Google a vyberte
priečinok na Google Drive, v ktorom už dokumentáciu máte (ak sa vám nedarí
prihlásiť alebo máte otázky, napíšte nám). Oncofiles nenahrádza lekára, nediagnostikuje a nenavrhuje
liečbu.</p>

<a class="cta" href="/#join">Vyskúšať &rarr;</a>
<p style="margin-top:1rem; font-size:0.875rem;"><a href="/demo">Pozrieť ukážku s
vymyslenou pacientkou</a> &middot; <a href="/pouzitie/pohybovy-atlas">Nielen
onkológia: pohybový atlas z vlastných správ</a></p>
<p style="margin-top:1rem; font-size:0.875rem;">Prefer English?
<a href="/eu">Switch to the English version &rarr;</a></p>
"""
    html = _topic_page_html(
        lang="sk",
        title=title,
        description=description,
        canonical=canonical,
        alt_hreflang="en",
        alt_href="https://oncofiles.com/eu",
        body_html=body,
    )
    jsonld = _eu_comparison_schema_jsonld(canonical, title, description)
    injection = (
        f'<style>{_EU_PAGE_EXTRA_CSS}</style><script type="application/ld+json">{jsonld}</script>'
    )
    html = html.replace("</head>", f"{injection}</head>")
    return HTMLResponse(html)


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

    Used by the Coolify healthcheck and UptimeRobot.  Must never block on
    external I/O so the process is never killed by a slow dependency.
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
    # #708: config only (flag + key presence), no DB — the nightly AI phases
    # are skipped while this reads "disabled".
    from oncofiles.cloud_guard import cloud_ai_enabled

    result["cloud_ai"] = "enabled" if cloud_ai_enabled() else "disabled"
    # #852: onboarding gate mode, config only (never the invite list, no DB).
    from oncofiles.onboarding_gate import onboarding_mode

    result["onboarding"] = onboarding_mode()
    # Build-time commit (Dockerfile ARG GIT_SHA, #610); RAILWAY_* kept as fallback.
    commit = (os.environ.get("ONCOFILES_GIT_SHA") or os.environ.get("RAILWAY_GIT_COMMIT_SHA", ""))[
        :7
    ]
    if commit:
        result["commit"] = commit
    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        started_at = lifespan_ctx.get("started_at")
        if started_at:
            result["uptime_s"] = int((datetime.now(UTC) - started_at).total_seconds())
        result["memory_rss_mb"] = round(_rss_now, 1)
        result["memory"] = get_rss_trend()
        # CI waits for False before deploying (a deploy kills a run). In-memory,
        # no patient detail.
        from oncofiles.tools.curated import heartbeat, run_active

        result["curated_run_active"] = run_active()
        # Public endpoint: an age and a boolean only (detail: admin overview).
        result.update(heartbeat())
        result["semaphores"] = get_semaphore_status()
        # Folder sync status — surface COUNTS only, not patient IDs (#488).
        # /health is unauthenticated (Railway liveness probe) so per-patient
        # keys leaked active UUIDs + which caregivers were experiencing sync
        # or OAuth pain. Aggregated counts keep operator observability
        # without the enumeration vector. Authenticated admins can see the
        # per-patient detail on the dashboard / /readiness.
        suspended = sum(
            1 for count in _folder_404_counts.values() if count >= _FOLDER_404_THRESHOLD
        )
        if suspended:
            result["folder_404_suspended_count"] = suspended
        if _invalid_grant_tokens:
            result["needs_reauth_count"] = len(_invalid_grant_tokens)
        # #552: structural credential failure (deleted GCP project, disabled
        # API, dead OAuth client). Counts only — /health is unauthenticated, so
        # per-patient keys would leak active UUIDs (#488). Detail lives on the
        # operator-only /readiness payload.
        if _hard_auth_failures:
            result["hard_auth_failed_count"] = len(_hard_auth_failures)
        # #552: staleness read from the watchdog's cache — never a DB call.
        # /health is the liveness probe; blocking I/O here gets the process
        # killed at 120s.
        if _sync_staleness.get("stale_count"):
            result["stale_sync_count"] = _sync_staleness["stale_count"]
        # Make an intentional pause unmistakable, so a quiet pipeline is never
        # mistaken for a healthy one (or for a broken one).
        if not GOOGLE_INTEGRATION_ENABLED:
            result["google_integration"] = "disabled"
    except Exception:
        logger.debug("Health: lifespan context not yet available", exc_info=True)
    return JSONResponse(result)


def _readiness_caller_is_operator(request: Request) -> bool:
    """Decide whether the /readiness caller is allowed the full diagnostic
    payload (#512). Two paths qualify:

    - ``Authorization: Bearer <MCP_BEARER_TOKEN>`` matches — the operator
      bearer used by curl probes, Railway monitoring, ops scripts.
    - ``oncofiles_session`` cookie verifies AND the embedded email is in
      ``DASHBOARD_ADMIN_EMAILS`` — lets a logged-in admin read the full
      payload in the browser without exposing internals to logged-out
      visitors or non-admin caregivers.

    Anything else gets the redacted public-safe shape: status + version
    + db reachability only. No memory, no per-job last_error / running
    flags.
    """
    auth_header = request.headers.get("authorization", "")
    if (
        auth_header.startswith("Bearer ")
        and not auth_header.startswith("Bearer session:")
        and MCP_BEARER_TOKEN
    ):
        token = auth_header.removeprefix("Bearer ").strip()
        if hmac.compare_digest(token.encode(), MCP_BEARER_TOKEN.encode()):
            return True

    # Dashboard session cookie path. A same-origin `fetch('/readiness')`
    # sends the cookie automatically (default credentials).
    cookie_header = request.headers.get("cookie", "")
    if cookie_header:
        for pair in cookie_header.split(";"):
            if "=" not in pair:
                continue
            cname, _, cvalue = pair.strip().partition("=")
            if cname != "oncofiles_session":
                continue
            from urllib.parse import unquote as _urlunquote

            email = _verify_session_token(_urlunquote(cvalue.strip()))
            return _is_admin_email(email)
    return False


@mcp.custom_route("/readiness", methods=["GET"])
async def readiness(request: Request) -> JSONResponse:
    """Readiness probe — checks DB connectivity with a 2 s timeout (no retry).

    Suitable for dashboards and deeper monitoring; NOT used as the
    container healthcheck so a slow DB never gets the process killed.

    #512: the *minimal* shape (``status``, ``version``, ``db``) is public
    because external uptime monitors and the load balancer rely on it.
    The detailed operational telemetry (memory, per-job error/running
    flags, sync staleness, AI cost) is only returned when
    the caller is an operator — see ``_readiness_caller_is_operator``.
    """
    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db: Database = lifespan_ctx["db"]
        await db.ping(timeout=2.0)
        result: dict = {"status": "ok", "version": VERSION, "db": "connected"}
        # Public-safe payload ends here. Anything below is operator-only.
        if not _readiness_caller_is_operator(request):
            return JSONResponse(result)

        result["memory_rss_mb"] = round(get_rss_mb(), 1)
        from oncofiles.onboarding_gate import onboarding_mode

        result["onboarding"] = onboarding_mode()  # #852, mode only
        # #552: full detail for structural credential failures. Operator-only,
        # so patient IDs are safe here (unlike /health, which gets counts only).
        if _hard_auth_failures:
            result["hard_auth_failures"] = [
                {"patient_id": pid, "service": service, **info}
                for (pid, service), info in _hard_auth_failures.items()
            ]
        # #552: per-patient sync staleness, from the watchdog cache.
        result["sync_staleness"] = _sync_staleness
        # #665: per-patient continuous-mode counters (in-memory, no DB call).
        from oncofiles.tools.curated import nightly_retry_stats, probe_stats, startup_index_stats

        result["curated_probes"] = probe_stats()
        # #979: transient network/DNS failures building Google clients and the
        # curated nightly's same-night retries (codes, counts, times; no DB).
        result["network_unavailable"] = network_unavailable_stats()
        result["curated_nightly_retry"] = nightly_retry_stats()
        # #880: patients (re-)indexed at startup, incl. non-enrolled ones with a
        # stale INDEX_VERSION (in-memory counts, no DB call).
        result["curated_startup_index"] = startup_index_stats()
        # #868: dashboard sign-ins / ACL refusals / admin cross reads since
        # process start (in-memory, no DB; the rows are in dashboard_access_log).
        result["dashboard_access"] = dashboard_audit.counters()
        # #784: one scheduler per process, verifiable after a rolling deploy:
        # every answer must carry the same instance_id. In-memory, no DB.
        result["scheduler"] = scheduler_singleton.status()
        # Scheduler job status (lightweight summary)
        tracker = lifespan_ctx.get("job_tracker", {})
        jobs: dict = {}
        if tracker:
            for job_id, info in tracker.items():
                entry: dict = {}
                if info.get("last_ok"):
                    entry["last_ok"] = info["last_ok"]
                if info.get("last_error"):
                    entry["last_error"] = info["last_error"]
                if info.get("running"):
                    entry["running"] = True
                # #441 Layer 6: per-run AI cost (set by _run_*_pipeline tail).
                if info.get("last_cost_usd") is not None:
                    entry["last_cost_usd"] = info["last_cost_usd"]
                if entry:
                    jobs[job_id] = entry
        # #856: the erase copy retention is a lifespan loop, not an APScheduler
        # job, so it reports here whatever SYNC_ENABLED says (in-memory).
        from oncofiles import erase_retention

        erase = erase_retention.status()
        jobs[erase_retention.JOB_ID] = {
            "loop_running": scheduler_singleton.background_loop_running(erase_retention.JOB_ID),
            **{
                k: erase[k]
                for k in (
                    "schedule",
                    "next_run_at",
                    "last_run_at",
                    "last_ok",
                    "last_error",
                    "last_error_type",
                    "last_deleted",
                    "running",
                )
            },
        }
        # #999: the interval loops (holding mover and reader, inbound queue
        # sweep), in-memory, counts only.
        from oncofiles import scheduled_jobs

        for job_id, job in scheduled_jobs.INTERVAL_LOOPS.items():
            st = job.status()
            jobs[job_id] = {
                "loop_running": scheduler_singleton.background_loop_running(job_id),
                **{
                    k: st[k]
                    for k in (
                        "schedule",
                        "next_run_at",
                        "last_run_at",
                        "last_ok",
                        "last_error",
                        "last_error_type",
                        "running",
                        "runs",
                        "errors",
                        "last_summary",
                        "last_counts",
                    )
                },
            }
            if "last_deleted" in st:  # daily retention loops (#868 access log)
                jobs[job_id]["last_deleted"] = st["last_deleted"]
        result["jobs"] = jobs
        # #441 Layer 6: cluster-wide AI cost in the last 24h. Cheap aggregate
        # query — operator-only, like the rest of the readiness payload.
        try:
            ai_cost_24h = 0.0
            sql = (
                "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) AS usd "
                "FROM prompt_log WHERE status = 'ok' "
                "AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-1 days')"
            )
            async with db.db.execute(sql) as cursor:
                row = await cursor.fetchone()
                if row is not None:
                    ai_cost_24h = float(row["usd"] or 0.0)
            result["ai_cost_usd_24h"] = round(ai_cost_24h, 4)
        except Exception:
            # Never let cost telemetry block readiness response.
            pass
        # #952: inbound mail queue backlog, codes and counts only. One bounded
        # query: pending / processing / failed rows through the state index;
        # the ever-growing done rows are not read.
        try:
            result["inbound_jobs"] = await db.inbound_job_counts(include_done=False)
        except Exception:
            result["inbound_jobs"] = None
        # #950: inbound alert codes (in memory, counts only; never on /health)
        from oncofiles import inbound_email as _inbound_email

        result["inbound_alerts"] = _inbound_email.alerts_overview()
        # bad-token mails of the last hour, all addresses (enumeration watch)
        result["inbound_bad_tokens"] = _inbound_email.bad_tokens_overview()
        # #950 quarantine purge: switches + last run with work (in memory,
        # counts only)
        from oncofiles import inbound_quarantine as _inbound_quarantine

        result["inbound_quarantine_purge"] = _inbound_quarantine.purge_overview()
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


# #591: /api/stats is public, and the count costs 1 + N(patients) queries on the
# single DB connection. Cached, so an unauthenticated flood costs one count per TTL.
_API_STATS_TTL_S = 300
_api_stats_cache: dict = {"at": 0.0, "documents": None}


@mcp.custom_route("/api/stats", methods=["GET"])
async def api_stats(request: Request) -> JSONResponse:
    """Public project stats for landing page and llms.txt."""
    from oncofiles.models import DocumentCategory

    doc_count = _api_stats_cache["documents"]
    if doc_count is None or time.time() - _api_stats_cache["at"] > _API_STATS_TTL_S:
        doc_count = 0
        try:
            db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
            patients = await db.list_patients(active_only=True)
            for p in patients:
                doc_count += await db.count_documents(patient_id=p.patient_id)
            _api_stats_cache.update(at=time.time(), documents=doc_count)
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

            # standard_named requires Python logic. Which naming standard applies
            # depends on the row (document_stage.is_named: README contract for a
            # curated row, the legacy standard otherwise), so the metadata is
            # fetched only for the "gdrive:" rows that need it.
            from oncofiles import document_stage

            async with db.db.execute(
                "SELECT filename, file_id, mime_type, "
                "CASE WHEN file_id LIKE 'gdrive:%' THEN structured_metadata END "
                "AS structured_metadata "
                "FROM documents WHERE deleted_at IS NULL AND patient_id = ?",
                (patient_id,),
            ) as cursor:
                fn_rows = await cursor.fetchall()
            standard_count = sum(
                1
                for r in fn_rows
                if document_stage.is_named(
                    r["filename"],
                    file_id=r["file_id"],
                    structured_metadata=r["structured_metadata"],
                    patient_id=patient_id,
                    mime_type=r["mime_type"],
                )
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
                "service_sync_times": await _get_service_sync_times_async(db, patient_id),
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


@mcp.custom_route("/debug/memory-trace", methods=["GET"])
async def debug_memory_trace(request: Request) -> JSONResponse:
    """On-demand tracemalloc snapshot for diagnosing memory leaks (#426).

    Requires bearer token auth (operator-only, never exposed to MCP clients).
    Opt-in via ``MEMORY_TRACEMALLOC_ENABLED=true`` env var so we don't pay
    the ~15% per-alloc overhead in steady state. To use:

        1. Set ``MEMORY_TRACEMALLOC_ENABLED=true`` in the Coolify env + deploy.
        2. Wait for the suspect workload window (e.g. 23:00 UTC nightly AI run).
        3. curl -H "Authorization: Bearer <MCP_BEARER_TOKEN>"
              https://oncofiles.com/debug/memory-trace
        4. The response carries top-20 allocators by size + current RSS.
        5. Unset the env var after diagnosis — tracemalloc itself grows the
           heap (each snapshot costs ~10-20 MB) and is not a steady-state tool.

    Returns 503 when the flag is off so on/off state is obvious to the caller.
    """
    err = _check_bearer(request)
    if err:
        return err
    if not MCP_BEARER_TOKEN:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    if os.environ.get("MEMORY_TRACEMALLOC_ENABLED", "").lower() not in ("1", "true", "yes"):
        return JSONResponse(
            {
                "error": "tracemalloc disabled",
                "hint": (
                    "Set MEMORY_TRACEMALLOC_ENABLED=true in the deployment env "
                    "(Coolify) and redeploy to enable. Unset after diagnosis to avoid the "
                    "~15% per-alloc overhead."
                ),
            },
            status_code=503,
        )

    try:
        import tracemalloc

        if not tracemalloc.is_tracing():
            # Lazy-start on first hit so the env flag alone is enough — no
            # need for a separate startup hook. Frames kept small (5 levels)
            # to cap snapshot memory.
            tracemalloc.start(5)
            return JSONResponse(
                {
                    "status": "started",
                    "message": (
                        "tracemalloc just started — subsequent calls will "
                        "return real top-allocator data. Trigger the suspect "
                        "workload then call this endpoint again."
                    ),
                    "rss_mb": round(get_rss_mb(), 1),
                }
            )

        snap = tracemalloc.take_snapshot()
        top = snap.statistics("lineno")[:20]
        entries = []
        for stat in top:
            frame = stat.traceback[0]
            entries.append(
                {
                    "file": frame.filename.rsplit("/", 2)[-2]
                    + "/"
                    + frame.filename.rsplit("/", 1)[-1]
                    if "/" in frame.filename
                    else frame.filename,
                    "line": frame.lineno,
                    "size_mb": round(stat.size / (1024 * 1024), 3),
                    "count": stat.count,
                    "avg_bytes": int(stat.size / stat.count) if stat.count else 0,
                }
            )
        current_mb, peak_mb = tracemalloc.get_traced_memory()
        return JSONResponse(
            {
                "rss_mb": round(get_rss_mb(), 1),
                "traced_current_mb": round(current_mb / (1024 * 1024), 1),
                "traced_peak_mb": round(peak_mb / (1024 * 1024), 1),
                "top_allocators": entries,
            }
        )
    except Exception:
        logger.exception("memory-trace endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


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


_DEMO_DASHBOARD_HTML: str | None = None
# dashboard.html is one file for two pages: the signed-in app (/dashboard) and
# the public demo (/demo). Blocks wrapped in <!--app-only--> / <!--demo-only-->
# exist in one of them only; the other variant never ships them.
_VARIANT_MARKERS = ("<!--app-only-->", "<!--/app-only-->", "<!--demo-only-->", "<!--/demo-only-->")


def _dashboard_variant(html: str, *, demo: bool) -> str:
    drop = "app-only" if demo else "demo-only"
    html = re.sub(rf"[ \t]*<!--{drop}-->.*?<!--/{drop}-->[ \t]*\n?", "", html, flags=re.DOTALL)
    for marker in _VARIANT_MARKERS:
        html = re.sub(rf"[ \t]*{re.escape(marker)}[ \t]*\n?", "", html)
    return html


def _load_dashboard_html() -> str:
    """The signed-in app: no demo banner, no demo head tags."""
    global _DASHBOARD_HTML  # noqa: PLW0603
    if _DASHBOARD_HTML is None:
        raw = (Path(__file__).parent / "dashboard.html").read_text()
        _DASHBOARD_HTML = _dashboard_variant(raw, demo=False).replace("<!--demo-head-->\n", "")
    return _DASHBOARD_HTML


def _load_demo_dashboard_html() -> str:
    """The public demo: the same page in demo mode (``data-demo="1"``).

    No Google sign-in script, no sign-in gate, no admin markup, no bug report.
    The head gets the demo's own title, description and link tags, and the
    opt-in analytics loader the public pages use (#700); the app never does.
    """
    global _DEMO_DASHBOARD_HTML  # noqa: PLW0603
    if _DEMO_DASHBOARD_HTML is None:
        raw = (Path(__file__).parent / "dashboard.html").read_text()
        html = _dashboard_variant(raw, demo=True)
        html = html.replace('<html lang="sk">', '<html lang="sk" data-demo="1">', 1)
        html = html.replace(" <title>Oncofiles Dashboard</title>\n", "", 1)
        html = html.replace(" <!--demo-head-->\n", _demo_asset("demo_head.html"), 1)
        html = html.replace("</body>", _demo_asset("demo_cookie_banner.html") + "</body>", 1)
        _DEMO_DASHBOARD_HTML = html
    return _DEMO_DASHBOARD_HTML


def _parse_session_token(token: str) -> tuple[str, int, str, str, str] | None:
    """Single owner of the ``email|expiry|tid|sig`` format.

    Returns ``(email, expiry, tid, sig, sep)`` on a structurally-valid token
    (no HMAC verification, no expiry check) or ``None`` when unparseable.
    A 3-part legacy token returns ``tid=""``. ``sep`` is the separator
    actually used (current ``|`` or legacy ``.``) so signature
    re-construction stays consistent.
    """
    if not token:
        return None
    sep = "|" if "|" in token else "."
    parts = token.rsplit(sep, 3)
    if len(parts) == 4:
        email, expiry_str, tid, sig = parts
    elif len(parts) == 3:
        email, expiry_str, sig = parts
        tid = ""
    else:
        return None
    try:
        expiry = int(expiry_str)
    except ValueError:
        return None
    return email, expiry, tid, sig, sep


def _make_session_token(email: str) -> str:
    """Create an HMAC-signed session token: email|expiry|tid|signature.

    The `tid` (token id, 16 hex chars) is the handle by which a logged-out
    session is revoked server-side via /api/logout (#510).
    """
    if not MCP_BEARER_TOKEN:
        raise ValueError("Cannot create session token: MCP_BEARER_TOKEN not configured")
    expiry = str(int(time.time()) + _SESSION_MAX_AGE)
    tid = session_revocation.make_tid()
    key = dashboard_session_key(MCP_BEARER_TOKEN)
    payload = f"{email}|{expiry}|{tid}"
    sig = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}|{sig}"


def _session_cookie(request: Request) -> str:
    """Session token from the ``oncofiles_session`` cookie, percent-decoded.

    The cookie is written percent-encoded (#495) and Starlette's
    ``request.cookies`` does not decode it, so a raw read never verifies.
    """
    return unquote(request.cookies.get("oncofiles_session", ""))


def _verify_session_token(token: str, *, log: bool = True) -> str | None:
    """Verify a session token. Returns email if valid, None otherwise.

    Accepts both 4-part (email|expiry|tid|sig, current) and 3-part legacy
    formats. Legacy 3-part tokens cannot be revoked — they predate #510
    and were already invalidated by the #502 key rotation, so any still
    in circulation are stale.
    """
    if not MCP_BEARER_TOKEN:
        return None
    parsed = _parse_session_token(token)
    if parsed is None:
        return None
    email, expiry, tid, sig, sep = parsed
    if time.time() > expiry:
        if log:
            logger.warning("Session token expired for %s", email)
        return None
    key = dashboard_session_key(MCP_BEARER_TOKEN)
    signed_payload = f"{email}{sep}{expiry}{sep}{tid}" if tid else f"{email}{sep}{expiry}"
    expected = hmac.new(key, signed_payload.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        if log:
            logger.warning("Session token signature mismatch for %s", email)
        return None
    if tid and session_revocation.is_revoked(tid):
        if log:
            logger.info("Session token rejected (revoked) for %s", email)
        return None
    return email


def _extract_session_tid_and_expiry(token: str) -> tuple[str, int] | None:
    """Pull ``(tid, expiry)`` out of a session token for revocation purposes.

    Used by /api/logout, after the token verified (#591: a forged token is
    never written to the denylist); the original expiry says how long the
    revocation row must live.
    Returns None when there's no tid (legacy 3-part format) — those
    predate #510 and cannot be individually revoked.
    """
    parsed = _parse_session_token(token)
    if parsed is None:
        return None
    _email, expiry, tid, _sig, _sep = parsed
    if not tid:
        return None
    return tid, expiry


def _get_dashboard_email(request: Request) -> str | None:
    """Extract the authenticated user's email from the session token.

    Returns the email for session-authenticated users, None for bearer-token
    (admin) auth or unauthenticated requests.
    """
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer session:"):
        session_token = auth_header.removeprefix("Bearer session:").strip()
        return _verify_session_token(session_token)
    return None


def _is_admin_email(email: str | None) -> bool:
    """Check whether *email* belongs to an admin (``DASHBOARD_ADMIN_EMAILS``).

    Admin status grants the system-level endpoints AND, today, access to every
    patient: the MCP resolver (`_resolve_patient_id`) and
    `_require_body_patient_access` let admins through without a caregiver
    match. Narrowing admin PHI access to audited break-glass is #655.
    """
    return email is not None and email.lower() in DASHBOARD_ADMIN_EMAILS


def _email_matches_caregiver(email: str, caregiver_email: str | None) -> bool:
    """Comma-separated caregiver match; delegates to the single implementation (#651)."""
    from oncofiles.persistent_oauth import _email_matches_caregiver as _match

    return _match(email, caregiver_email)


async def _require_body_patient_access(
    request: Request, db: Database, patient_id: str
) -> JSONResponse | None:
    """ACL gate for dashboard POST endpoints that accept `patient_id` in the body.

    v5.15 Phase 3 closed the `?patient_id=` query-param path via
    `_get_dashboard_patient_id` (caregiver-email scoping). The body-param path
    was NOT in that scope — every `/api/*` POST that reads
    `body.get("patient_id")` previously treated any authenticated dashboard
    caller as authorized for any patient_id they typed. That's the same
    class as Felix Vítek's oncoteam#438 Bug 3 (#489 Part B).

    Routes call it through ``_dashboard_body_patient`` (#867), which first
    resolves the body value and refuses an unknown one. The static bearer
    (verified here, #867: no longer trusted on the header shape alone) is the
    operator; session callers must either be in DASHBOARD_ADMIN_EMAILS or
    match the target patient's caregiver_email.

    The helper runs ``_check_dashboard_auth`` itself (#867), so its answer
    never depends on a route having called that first.

    Returns None on success, JSONResponse(401) for an unauthenticated caller,
    JSONResponse(403) on denial. 404 is returned if the patient doesn't
    exist — before the caregiver check, as before.
    """
    auth_err = _check_dashboard_auth(request)
    if auth_err is not None:
        return auth_err
    auth_header = request.headers.get("authorization", "")
    # Static bearer token callers are admin-scoped (they had to know the secret).
    if auth_header.startswith("Bearer ") and not auth_header.startswith("Bearer session:"):
        if _check_bearer(request) is None:
            return None
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    email = _get_dashboard_email(request)
    if _is_admin_email(email):
        return None
    patient = await db.get_patient(patient_id)
    if not patient:
        return JSONResponse({"error": f"Patient '{patient_id}' not found"}, status_code=404)
    if email and _email_matches_caregiver(email, patient.caregiver_email):
        return None
    logger.warning(
        "patient-id body ACL denied: caller=%s tried patient=%r",
        _acl_caller_log_id(email),
        patient_id[:8],
    )
    return JSONResponse({"error": "forbidden: not authorized for this patient"}, status_code=403)


async def _resolve_body_patient_id(db: Database, raw: str) -> str | None:
    """Resolve a caller-supplied patient value (slug or UUID) to a UUID, or None.

    Replaces ``resolve_patient_id(raw) or raw`` (#867): an unknown value is a
    refusal at this point, never passed on. An archived patient still resolves
    by its exact UUID, as before (its slug did not, and still does not).
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    resolved = await db.resolve_patient_id(raw)
    if resolved:
        return resolved
    patient = await db.get_patient(raw)
    return patient.patient_id if patient else None


async def _dashboard_body_patient(
    request: Request, db: Database, raw: str
) -> tuple[str | None, JSONResponse | None]:
    """The single ACL path for dashboard routes with ``patient_id`` in the body (#867).

    Returns ``(patient_uuid, None)`` when the caller may act for the patient,
    else ``(None, response)``: 401 unauthenticated, otherwise the one
    ``_patient_not_found()`` answer for an unknown patient AND for a patient
    the caller may not act for (no existence oracle, #590). Only the log line
    (caller hash, never the e-mail) says which.
    """
    auth_err = _check_dashboard_auth(request)
    if auth_err is not None:
        return None, auth_err
    email, is_admin = _dashboard_caller(request)
    patient_id = await _resolve_body_patient_id(db, raw)
    if patient_id is None:
        logger.warning("patient-id body ACL: unknown patient caller=%s", _acl_caller_log_id(email))
        await _audit_body_refusal(request, db, email, is_admin, None)
        return None, _patient_not_found()
    acl_err = await _require_body_patient_access(request, db, patient_id)
    if acl_err is not None:
        if acl_err.status_code in (403, 404):
            # Logged as a denial inside _require_body_patient_access.
            await _audit_body_refusal(request, db, email, is_admin, patient_id)
            return None, _patient_not_found()
        return None, acl_err
    return patient_id, None


async def _dashboard_write_refusal(
    request: Request, db: Database, patient_id: str
) -> JSONResponse | None:
    """403 ``read_only`` unless the caller is this patient's own caregiver (#921).

    Runs AFTER ``_dashboard_body_patient`` on a route that writes documents for
    a patient. An admin passes that helper for every patient, but the dashboard
    shows other people's patients to an admin read-only (the "Admin
    (read-only)" group), so an admin who is not the caregiver is refused here;
    so is the operator bearer, which is not a dashboard session. Stricter only,
    never a second way in.
    """
    email = _get_dashboard_email(request)
    patient = await db.get_patient(patient_id) if email else None
    if email and patient and _email_matches_caregiver(email, patient.caregiver_email):
        return None
    return JSONResponse(
        {
            "error": "read_only",
            "message_sk": (
                "Tento pacient je pre vás len na čítanie; dokumenty pridáva jeho opatrovateľ."
            ),
            "message_en": (
                "This patient is read-only for you; documents are added by the caregiver."
            ),
        },
        status_code=403,
    )


async def _audit_body_refusal(
    request: Request, db: Database, email: str | None, is_admin: bool, patient_id: str | None
) -> None:
    """#868 ``acl_refused`` for a body-route refusal by a session caller (never raises).

    The operator bearer is not a dashboard session and is not recorded, as in
    ``_get_dashboard_patient_id``.
    """
    if _check_bearer(request) is None:
        return
    await dashboard_audit.record(
        db,
        "acl_refused",
        email=email,
        is_admin=is_admin,
        patient_id=patient_id,
        route=dashboard_audit.route_name(request),
    )


def _patient_not_found() -> JSONResponse:
    """The single answer for "unknown patient" and "not your patient" (#867, #590).

    Byte-identical for both cases (status, body, headers), so a non-admin
    caller cannot probe which patient ids or slugs exist. Logs and the #868
    audit may tell the cases apart; responses never do.

    ``Cache-Control: no-store`` (#976 follow-up): a refusal is per caller
    and per moment (a caregiver may be added a minute later), so no browser
    or proxy may keep it. The same header on every case keeps it identical.
    """
    return JSONResponse(
        {"error": "not_found"}, status_code=404, headers={"Cache-Control": "no-store"}
    )


def _check_dashboard_auth(request: Request) -> JSONResponse | None:
    """Check bearer token OR dashboard session token. Returns error response or None."""
    # Try standard bearer token first
    if _check_bearer(request) is None:
        return None
    # Try dashboard session token (Bearer session:xxx)
    if _get_dashboard_email(request) is not None:
        return None
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def _dashboard_caller(request: Request) -> tuple[str | None, bool]:
    """(email, is_admin) of a dashboard caller for the onboarding gate (#852).

    The static bearer (and stdio, which ``_check_bearer`` trusts) is the
    operator, so admin; a session caller is admin when its e-mail is in
    DASHBOARD_ADMIN_EMAILS.
    """
    if _check_bearer(request) is None:
        return None, True
    email = _get_dashboard_email(request)
    return email, _is_admin_email(email)


def _onboarding_refusal_response(action: str, email: str | None, code: str) -> JSONResponse:
    from oncofiles import onboarding_gate

    onboarding_gate.log_refusal(action, email, surface="dashboard", code=code)
    return JSONResponse(onboarding_gate.refusal_payload(code), status_code=403)


def _dashboard_onboarding_refusal(request: Request, action: str) -> JSONResponse | None:
    """403 ``onboarding_closed`` when this caller may not onboard (#852), else None.

    Call after ``_check_dashboard_auth`` on every route that creates a patient,
    starts a first Google link or sets a Drive folder.
    """
    from oncofiles import onboarding_gate

    email, is_admin = _dashboard_caller(request)
    if onboarding_gate.onboarding_allowed(email, is_admin=is_admin):
        return None
    return _onboarding_refusal_response(action, email, onboarding_gate.ONBOARDING_CLOSED)


def _dashboard_legacy_refusal(request: Request, action: str) -> JSONResponse | None:
    """403 for a legacy Drive sync / enhance trigger from a non-admin (#852).

    Admin-only in every ONBOARDING_MODE until #853 makes every new folder
    curated: new users must only ever get the curated path.
    """
    from oncofiles import onboarding_gate

    email, is_admin = _dashboard_caller(request)
    if onboarding_gate.legacy_allowed(is_admin=is_admin):
        return None
    return _onboarding_refusal_response(
        f"{onboarding_gate.ACTION_LEGACY_SYNC}:{action}", email, onboarding_gate.LEGACY_ADMIN_ONLY
    )


# Canonical sentinel lives in oncofiles.constants to avoid circular imports
# with persistent_oauth (which also needs it). Re-export here so existing
# `from oncofiles.server import NO_PATIENT_ACCESS_SENTINEL` call-sites keep
# working.
from oncofiles import dashboard_audit  # noqa: E402
from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL  # noqa: E402


def _acl_caller_log_id(email: str | None) -> str:
    """Short keyed hash of a dashboard caller for ACL logs (#531), never the e-mail."""
    try:
        from oncofiles.secrets_keys import caller_email_hash

        digest = caller_email_hash(email or "")
    except Exception:
        digest = None
    return digest[:12] if digest else "-"


def _dashboard_session_key(request: Request, email: str | None) -> str:
    """Dedupe key for one dashboard session (#868): the token's tid, else the caller hash."""
    try:
        header = request.headers.get("authorization", "")
        if isinstance(header, str) and header.startswith("Bearer session:"):
            parsed = _parse_session_token(header.removeprefix("Bearer session:").strip())
            if parsed is not None and parsed[2]:
                return f"tid:{parsed[2]}"
    except Exception:
        pass
    return f"caller:{dashboard_audit.caller_hash(email) or '-'}"


async def _get_dashboard_patient_id(request: Request, *, fallback_to_own: bool = True) -> str:
    """Extract patient_id from query params and resolve slug → UUID.

    The single ACL path for every dashboard GET route that reads a patient
    (#867, lock test ``tests/test_867_single_dashboard_acl.py``). Body-param
    POST routes use ``_dashboard_body_patient`` instead.

    ``fallback_to_own=False``: a session caller who names a patient they may
    not see gets the sentinel instead of their own first patient, so a route
    can refuse (``/api/patient-context``, ``/api/patient-budget``,
    ``/api/doc-detail``). With the default, a non-admin naming an UNKNOWN
    patient gets exactly what a FOREIGN one gets (own first patient, else the
    sentinel), both recorded as ``acl_refused`` (#867, #590).

    Accepts either a UUID or a slug (e.g. 'jana-testova').  Always returns
    the UUID patient_id suitable for DB queries, OR a non-empty sentinel
    that matches no real patient when the caller has no access.

    Authorization: a session user gets a patient when their email appears in
    its ``caregiver_email`` (comma-separated) or they are an admin
    (``DASHBOARD_ADMIN_EMAILS``, audit-logged). A non-caregiver falls back to
    their own first patient. Bearer token requests (MCP/system) bypass
    patient scoping. Unauthorized requests get the sentinel — NEVER empty
    string — so downstream `if patient_id:` truthy checks still apply the DB
    filter.

    Fail closed (#531): every error or miss on the way (no caller identity,
    slug resolution, patient listing, caregiver match) returns the sentinel,
    never the requested value and never a default patient.

    Audit (#868): access denials (no identity, unknown patient, not caregiver)
    and admin reads of a foreign patient go to ``dashboard_audit``, which
    never raises, so the audit write can never change the result. Errors and
    "no patient selected" are not denials and are not recorded.
    """
    email = _get_dashboard_email(request)
    is_bearer = _check_bearer(request) is None
    caller = _acl_caller_log_id(email)

    if not is_bearer and not email:
        dashboard_audit.count_anonymous_refusal()
        return NO_PATIENT_ACCESS_SENTINEL

    raw = request.query_params.get("patient_id", "").strip().lower()
    db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    if not raw:
        # Fall back to first active patient the caller is allowed to see
        try:
            patients = await db.list_patients(active_only=True)
            if not is_bearer:
                patients = [
                    p for p in patients if _email_matches_caregiver(email, p.caregiver_email)
                ]
            if patients:
                return patients[0].patient_id
        except Exception as exc:
            logger.warning(
                "dashboard patient ACL: default-patient lookup raised %s caller=%s — sentinel",
                type(exc).__name__,
                caller,
            )
            return NO_PATIENT_ACCESS_SENTINEL
        logger.warning(
            "dashboard patient ACL: no accessible patient caller=%s is_bearer=%s — sentinel",
            caller,
            is_bearer,
        )
        return NO_PATIENT_ACCESS_SENTINEL

    try:
        resolved = await db.resolve_patient_id(raw)
        if not resolved:
            # The dashboard lists archived patients too; they resolve here and
            # still go through the ACL below (before #531 they skipped it).
            resolved = await db.resolve_patient_id(raw, active_only=False)
    except Exception as exc:
        logger.warning(
            "dashboard patient ACL: resolve_patient_id raised %s caller=%s — sentinel",
            type(exc).__name__,
            caller,
        )
        return NO_PATIENT_ACCESS_SENTINEL
    if not resolved:
        if is_bearer:
            return NO_PATIENT_ACCESS_SENTINEL
        is_admin = _is_admin_email(email)
        await dashboard_audit.record(
            db,
            "acl_refused",
            email=email,
            is_admin=is_admin,
            patient_id=None,
            route=dashboard_audit.route_name(request),
        )
        if is_admin or not fallback_to_own:
            return NO_PATIENT_ACCESS_SENTINEL
        # #867/#590: for a non-admin an unknown patient answers exactly like a
        # foreign one (their own first patient, else the sentinel), so a list
        # route cannot tell whether a slug or UUID exists.
        try:
            patients = await db.list_patients(active_only=False)
            own = [p for p in patients if _email_matches_caregiver(email, p.caregiver_email)]
        except Exception as exc:
            logger.warning(
                "dashboard patient ACL: own-patient lookup raised %s caller=%s — sentinel",
                type(exc).__name__,
                caller,
            )
            return NO_PATIENT_ACCESS_SENTINEL
        return own[0].patient_id if own else NO_PATIENT_ACCESS_SENTINEL

    if is_bearer:
        return resolved

    # --- Authorization gate (session callers) ---
    try:
        patients = await db.list_patients(active_only=False)
        target = next((p for p in patients if p.patient_id == resolved), None)
        if target is not None and _email_matches_caregiver(email, target.caregiver_email):
            return resolved
        admin_read = target is not None and _is_admin_email(email)
        own = [p for p in patients if _email_matches_caregiver(email, p.caregiver_email)]
    except Exception as exc:
        logger.warning(
            "dashboard patient ACL: authorization check raised %s caller=%s — sentinel",
            type(exc).__name__,
            caller,
        )
        return NO_PATIENT_ACCESS_SENTINEL

    route = dashboard_audit.route_name(request)
    if admin_read:
        # Admin read-only access — allowed but audited (#868: once per
        # session + patient per hour in the DB, throttled in the app log).
        if dashboard_audit.should_record_read(_dashboard_session_key(request, email), resolved):
            await dashboard_audit.record(
                db, "patient_read", email=email, is_admin=True, patient_id=resolved, route=route
            )
        return resolved

    # Non-admin, non-caregiver — refused; fall back to own patient (or the
    # sentinel; or, for a route that refuses instead, always the sentinel,
    # #867). A requested patient missing from the listing has no target id.
    await dashboard_audit.record(
        db,
        "acl_refused",
        email=email,
        is_admin=_is_admin_email(email),
        patient_id=resolved if target is not None else None,
        route=route,
    )
    return own[0].patient_id if own and fallback_to_own else NO_PATIENT_ACCESS_SENTINEL


async def _dashboard_patient_or_refusal(request: Request) -> tuple[str, JSONResponse | None]:
    """``_get_dashboard_patient_id`` for routes that refuse instead of falling back (#867).

    Same ACL, without the "own first patient" fallback for a named patient:
    a sentinel answer becomes ``_patient_not_found()`` when the caller named a
    patient (foreign and unknown alike, #590; also when the check failed
    closed) and 400 when they named none and have no patient. A typed result
    that tells a DB error apart (for a 503, the #469 contract) is a follow-up.
    """
    pid = await _get_dashboard_patient_id(request, fallback_to_own=False)
    if pid != NO_PATIENT_ACCESS_SENTINEL:
        return pid, None
    if request.query_params.get("patient_id", "").strip():
        return pid, _patient_not_found()
    return pid, JSONResponse({"error": "patient_id required"}, status_code=400)


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request) -> HTMLResponse:
    """Dashboard page. Open to all — auth happens client-side via Google Sign-In."""
    return HTMLResponse(_load_dashboard_html())


_DEMO_ASSETS: dict[str, str] = {}


def _demo_asset(name: str) -> str:
    """Read demo.html / demo.css / demo.js once (same pattern as the dashboard assets)."""
    if name not in _DEMO_ASSETS:
        _DEMO_ASSETS[name] = (Path(__file__).parent / name).read_text()
    return _DEMO_ASSETS[name]


@mcp.custom_route("/demo", methods=["GET"])
async def demo_dashboard(request: Request) -> HTMLResponse:
    """Public demo: the dashboard itself, one to one, with one invented patient.

    Same dashboard.html / dashboard.js / dashboard.css as /dashboard, served in
    demo mode (``<html data-demo="1">``): no sign-in, no admin markup, and the
    script answers every API path from /api/demo-data, the only URL it may
    fetch (a ``window.fetch`` chokepoint in dashboard.js, locked by a test).
    Actions that would write only explain themselves. The earlier showcase of
    assistant answers lives on at /demo/assistant.
    """
    return HTMLResponse(_load_demo_dashboard_html())


@mcp.custom_route("/demo/assistant", methods=["GET"])
async def demo_assistant(request: Request) -> HTMLResponse:
    """Public showcase: how an assistant answers from the invented patient's
    documents (lab values with their source line, chat examples, document view).

    Its own small page (demo.html + demo.js), not the dashboard: it loads no
    dashboard.js and demo.js only fetches /api/demo-data (#605).
    """
    return HTMLResponse(_demo_asset("demo.html").replace("{{VERSION}}", VERSION))


@mcp.custom_route("/demo.css", methods=["GET"])
async def demo_css(request: Request) -> Response:
    return Response(_demo_asset("demo.css"), media_type="text/css; charset=utf-8")


@mcp.custom_route("/consent.js", methods=["GET"])
async def consent_js(request: Request) -> Response:
    """Opt-in analytics loader for the public pages (#700). Never used by /dashboard."""
    return Response(_demo_asset("consent.js"), media_type="application/javascript; charset=utf-8")


@mcp.custom_route("/demo.js", methods=["GET"])
async def demo_js(request: Request) -> Response:
    return Response(_demo_asset("demo.js"), media_type="application/javascript; charset=utf-8")


# ── Demo data: one invented patient (all names, values and dates are made up,
#    dates in 2027 per the synthetic-fixtures rule). ──────────────────────────

_DEMO_HOSPITAL = "Nemocnica Príkladov"
_DEMO_HOSPITAL_EN = "Example Hospital"
_DEMO_LAB_HEADER = "Vyšetrenie|Výsledok|Jednotky|Ref. rozsah"
_DEMO_LAB_HEADER_EN = "Test|Result|Units|Ref. range"
# The demo shows the same invented patient in both languages: Slovak names and
# Slovak report text in SK mode, English equivalents in EN mode (`*_en` keys).
# Filenames keep the standard YYYYMMDD_Patient_Institution_Category_Desc.ext
# shape; only the patient and institution tokens differ per language.
_DEMO_PATIENT = ("Jana Testová", "Jane Doe")
_DEMO_FILE_TOKENS = ("JanaTestova_NemPrikladov", "JaneDoe_ExampleHospital")
# (report date, sample date, sample time, {code: (value_text, state)}, cea_sample)
_DEMO_LAB_REPORTS = [
    (
        "2027-01-14",
        "2027-01-12",
        "07:10",
        {
            "WBC": ("6,1", "machine"),
            "HGB": ("138", "machine"),
            "PLT": ("262", "machine"),
            "CEA": ("3,1", "machine"),
        },
        None,
    ),
    (
        "2027-02-11",
        "2027-02-09",
        "07:05",
        {
            "WBC": ("4,4", "machine"),
            "HGB": ("124", "machine"),
            "PLT": ("198", "human"),
            "CEA": ("2,9", "machine"),
        },
        None,
    ),
    (
        "2027-03-10",
        "2027-03-09",
        "06:55",
        {"WBC": ("4,9", "machine"), "HGB": ("119", "machine"), "PLT": ("219", "machine")},
        None,
    ),
    (
        "2027-03-17",
        "2027-03-15",
        "06:40",
        {
            "WBC": ("6,3", "human"),
            "HGB": ("131", "machine"),
            "PLT": ("248", "machine"),
            "CEA": ("2,7", "unverified"),
        },
        "2027-03-02",
    ),
]
_DEMO_SERIES = [
    ("WBC", "Leukocyty", "White cells", "×10⁹/L", "4,0–10,0", 4.0, 10.0),
    ("HGB", "Hemoglobín", "Haemoglobin", "g/L", "120–160", 120, 160),
    ("PLT", "Trombocyty", "Platelets", "×10⁹/L", "150–400", 150, 400),
    ("CEA", "CEA", "CEA", "µg/L", "< 5,0", None, 5.0),
]
_DEMO_FIXED_ROWS = {"RBC": ("4,21", "×10¹²/L", "3,80–5,20"), "HCT": ("0,39", "L/L", "0,35–0,47")}


def _sk_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{d}.{m}.{y}"


_EN_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _en_date(iso: str) -> str:
    y, m, d = iso.split("-")
    return f"{int(d)} {_EN_MONTHS[int(m) - 1]} {y}"


def _en_num(text: str) -> str:
    return text.replace(",", ".")


def _demo_filename(date: str, slug: str, en: bool = False) -> str:
    return f"{date.replace('-', '')}_{_DEMO_FILE_TOKENS[1 if en else 0]}_{slug}.pdf"


def _demo_payload() -> dict:
    meta = {s[0]: s for s in _DEMO_SERIES}
    documents: list[dict] = []
    points: dict[str, list[dict]] = {s[0]: [] for s in _DEMO_SERIES}
    for idx, (reported, sampled, time_, values, cea_sample) in enumerate(_DEMO_LAB_REPORTS, 1):
        lines: list[dict] = [
            {
                "text": f"{_DEMO_HOSPITAL} – Oddelenie klinickej biochémie",
                "text_en": f"{_DEMO_HOSPITAL_EN} – Clinical Biochemistry",
                "kind": "head",
            },
            {
                "text": "Pacientka: Jana Testová (vymyslená)",
                "text_en": "Patient: Jane Doe (invented)",
            },
            {
                "text": "Odosielajúci lekár: MUDr. Ján Ukážkový",
                "text_en": "Referring physician: Dr John Sample",
            },
            {
                "text": f"Odber: {_sk_date(sampled)} {time_}",
                "text_en": f"Sampled: {_en_date(sampled)} {time_}",
            },
            {"text": f"Vydané: {_sk_date(reported)}", "text_en": f"Issued: {_en_date(reported)}"},
            {"text": ""},
            {"text": "Krvný obraz", "text_en": "Blood count", "kind": "sub"},
            {
                "cells": _DEMO_LAB_HEADER.split("|"),
                "cells_en": _DEMO_LAB_HEADER_EN.split("|"),
                "kind": "th",
            },
        ]
        order = ["WBC", "RBC", "HGB", "HCT", "PLT"]
        if "CEA" in values:
            order += ["", "Nádorové markery", "note", "CEA"]
        for code in order:
            if code == "":
                lines.append({"text": ""})
                continue
            if code == "Nádorové markery":
                lines.append({"text": code, "text_en": "Tumour markers", "kind": "sub"})
                continue
            if code == "note":
                if cea_sample:
                    note = f"Výsledok z odberu {_sk_date(cea_sample)}"
                    note_en = f"Result from the sample of {_en_date(cea_sample)}"
                else:
                    note, note_en = "Z rovnakého odberu", "From the same sample"
                lines.append({"text": note, "text_en": note_en, "kind": "note"})
                continue
            if code in _DEMO_FIXED_ROWS:
                v, unit, ref = _DEMO_FIXED_ROWS[code]
                lines.append(
                    {
                        "cells": [code, v, unit, ref],
                        "cells_en": [code, _en_num(v), unit, _en_num(ref)],
                    }
                )
                continue
            value_text, state = values[code]
            _, _sk, _en, unit, ref, _lo, _hi = meta[code]
            lines.append(
                {
                    "cells": [code, value_text, unit, ref],
                    "cells_en": [code, _en_num(value_text), unit, _en_num(ref)],
                }
            )
            line_no = len(lines)
            sample = cea_sample if (code == "CEA" and cea_sample) else sampled
            points[code].append(
                {
                    "date": sample,
                    "value": float(value_text.replace(",", ".")),
                    "value_text": value_text,
                    "state": state,
                    "doc": idx,
                    "page": 1,
                    "line": line_no,
                    "note": "date_mismatch" if state == "unverified" else "",
                }
            )
        lines += [
            {"text": ""},
            {"text": "Vyšetrila: RNDr. Eva Vzorová", "text_en": "Checked by: Eva Example, PhD"},
        ]
        for n, line in enumerate(lines, 1):
            line["n"] = n
        documents.append(
            {
                "id": idx,
                "date": reported,
                "category": "labs",
                "title_sk": "Laboratórny výsledok",
                "title_en": "Lab report",
                "filename": _demo_filename(reported, "Labs_BloodCount"),
                "filename_en": _demo_filename(reported, "Labs_BloodCount", en=True),
                "institution": _DEMO_HOSPITAL,
                "institution_en": _DEMO_HOSPITAL_EN,
                "pages": [{"n": 1, "lines": lines}],
            }
        )
    prose_docs = [
        (
            5,
            "2027-02-20",
            "imaging",
            "CT brucha a malej panvy",
            "CT of abdomen and pelvis",
            "CT_AbdomenPelvis",
            [
                (
                    f"{_DEMO_HOSPITAL} – Rádiologické oddelenie",
                    f"{_DEMO_HOSPITAL_EN} – Radiology",
                    "head",
                ),
                ("Pacientka: Jana Testová (vymyslená)", "Patient: Jane Doe (invented)", ""),
                (
                    "Vyšetrenie: CT brucha a malej panvy s kontrastnou látkou",
                    "Examination: contrast-enhanced CT of abdomen and pelvis",
                    "",
                ),
                ("Dátum vyšetrenia: 20.02.2027", "Date of examination: 20 Feb 2027", ""),
                ("", "", ""),
                ("Popis", "Findings", "sub"),
                (
                    "Pečeň primeranej veľkosti, bez ložiskových zmien.",
                    "Liver of normal size, no focal lesions.",
                    "",
                ),
                ("Lymfatické uzliny nezväčšené.", "Lymph nodes not enlarged.", ""),
                ("", "", ""),
                ("Záver", "Conclusion", "sub"),
                (
                    "Stacionárny nález oproti predchádzajúcemu vyšetreniu.",
                    "Stable compared with the previous examination.",
                    "",
                ),
                ("", "", ""),
                ("Vymyslený nález pre ukážku.", "Invented finding for the demo.", "note"),
            ],
        ),
        (
            6,
            "2027-04-02",
            "report",
            "Správa z onkologickej ambulancie",
            "Oncology clinic report",
            "Report_OncologyVisit",
            [
                (
                    f"{_DEMO_HOSPITAL} – Onkologická ambulancia",
                    f"{_DEMO_HOSPITAL_EN} – Oncology Clinic",
                    "head",
                ),
                ("Pacientka: Jana Testová (vymyslená)", "Patient: Jane Doe (invented)", ""),
                ("Kontrola: 02.04.2027", "Visit: 2 Apr 2027", ""),
                ("Ošetrujúci lekár: MUDr. Ján Ukážkový", "Physician: Dr John Sample", ""),
                ("", "", ""),
                ("Priebeh", "Course", "sub"),
                (
                    "Liečbu znáša dobre, bez nových ťažkostí.",
                    "Tolerating treatment well, no new complaints.",
                    "",
                ),
                (
                    "Krvný obraz z 15.03.2027 bez obmedzení pre ďalší cyklus.",
                    "Blood count of 15 Mar 2027 allows the next cycle.",
                    "",
                ),
                ("", "", ""),
                ("Odporúčanie", "Plan", "sub"),
                (
                    "Pokračovať podľa plánu, kontrola o 3 týždne.",
                    "Continue as planned, follow-up in 3 weeks.",
                    "",
                ),
                ("", "", ""),
                ("Vymyslená správa pre ukážku.", "Invented report for the demo.", "note"),
            ],
        ),
    ]
    for doc_id, date, cat, t_sk, t_en, slug, rows in prose_docs:
        lines = [
            {"n": n, "text": t_sk, "text_en": t_en_line, "kind": k}
            for n, (t_sk, t_en_line, k) in enumerate(rows, 1)
        ]
        documents.append(
            {
                "id": doc_id,
                "date": date,
                "category": cat,
                "title_sk": t_sk,
                "title_en": t_en,
                "filename": _demo_filename(date, slug),
                "filename_en": _demo_filename(date, slug, en=True),
                "institution": _DEMO_HOSPITAL,
                "institution_en": _DEMO_HOSPITAL_EN,
                "pages": [{"n": 1, "lines": lines}],
            }
        )
    documents.sort(key=lambda d: d["date"], reverse=True)
    series = [
        {
            "code": code,
            "name_sk": sk,
            "name_en": en,
            "unit": unit,
            "ref": ref,
            "low": lo,
            "high": hi,
            "points": points[code],
        }
        for code, sk, en, unit, ref, lo, hi in _DEMO_SERIES
    ]
    from oncofiles import demo_dashboard

    return {
        "version": VERSION,
        "patient": {"name": _DEMO_PATIENT[0], "name_en": _DEMO_PATIENT[1], "invented": True},
        "series": series,
        "documents": documents,
        # /demo (the dashboard in demo mode) reads this section; /demo/assistant the rest
        "dashboard": demo_dashboard.build(documents, VERSION),
    }


@mcp.custom_route("/api/demo-data", methods=["GET"])
async def api_demo_data(request: Request) -> JSONResponse:
    """Invented sample patient for the public demo. Public, no auth.

    No prompt log, sync history or cost data (#605): those described the old
    cloud pipeline and are not part of what a visitor needs to understand.
    """
    return JSONResponse(_demo_payload())


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

    rate_err = _check_rate_limit("dashboard-verify")
    if rate_err:
        return rate_err

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

    # Issue session token (open signup — any verified Google email can sign in)
    session_token = _make_session_token(email)
    logger.info("Dashboard session issued caller=%s", _acl_caller_log_id(email))
    try:
        audit_db = request.app.state.fastmcp_server._lifespan_result["db"]
    except Exception:  # no lifespan DB: telemetry only, never block a sign-in
        audit_db = None
    if audit_db is not None:
        await dashboard_audit.record(
            audit_db,
            "sign_in",
            email=email,
            is_admin=_is_admin_email(email),
            patient_id=None,
            route=dashboard_audit.route_name(request),
        )
        # #950 phase 3: first sign-in of a welcome@ trial (funnel; never raises)
        from oncofiles import trial_funnel

        await trial_funnel.note_sign_in(audit_db, email)
    resp = JSONResponse(
        {
            "session_token": session_token,
            "email": email,
            "name": token_info.get("name", ""),
        }
    )
    # Also set as HttpOnly cookie so the MCP /authorize flow (browser redirect
    # from claude.ai custom-connector) can identify the signed-in user without
    # an Authorization header. The dashboard JS still reads `session_token`
    # from the JSON body for its own XHR traffic; this cookie is purely so the
    # email-capture middleware at /authorize can bind the user to the MCP
    # token it's about to issue (#478 proper fix).
    #
    # URL-encode the token so SimpleCookie never wraps it in double quotes.
    # Email addresses contain '@' which is outside SimpleCookie's legal-value
    # char set, causing it to quote the entire value. Browsers echo the quotes
    # back in the Cookie header and the middleware's manual parser breaks.
    # `quote` is imported module-level (see line 17).
    resp.set_cookie(
        key="oncofiles_session",
        value=quote(session_token, safe=""),
        max_age=_SESSION_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return resp


@mcp.custom_route("/api/logout", methods=["POST"])
async def api_logout(request: Request) -> JSONResponse:
    """Revoke a dashboard session server-side and clear the cookie (#510).

    Pre-#510, dashboard logout was client-only — sessionStorage cleared, but
    the HMAC-signed token remained valid until natural 24h expiry. A
    captured token (proxy log, browser history, hostile extension) could
    keep authenticating until then.

    This endpoint:
    1. Reads the session token from `Authorization: Bearer session:…` OR
       the `oncofiles_session` cookie.
    2. Parses the tid + original expiry out of the token, only when the
       token still verifies (#591). A tampered or expired token already
       fails _verify_session_token, so there is nothing to revoke.
    3. Persists the tid to `session_revocations` and adds it to the
       in-memory denylist.
    4. Returns 200 with `{"ok": true}` and a `set_cookie` that expires
       `oncofiles_session`.

    Idempotent: a token without a tid (legacy 3-part format) returns 200
    with the cookie cleared but no DB write — those tokens were already
    invalidated by #502 key rotation.
    """
    rate_err = _check_rate_limit("dashboard-logout", request=request)
    if rate_err:
        return rate_err

    token = ""
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer session:"):
        token = auth_header.removeprefix("Bearer session:").strip()
    if not token:
        token = _session_cookie(request)

    # #591: only a token that still verifies goes on the denylist. A forged or
    # expired one authenticates nothing, and revoking it would let a caller with
    # no session insert rows with an expiry of its own choosing.
    parsed = (
        _extract_session_tid_and_expiry(token)
        if token and _verify_session_token(token) is not None
        else None
    )
    if parsed is not None:
        tid, expiry = parsed
        try:
            db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
            await session_revocation.revoke(db.db, tid, expiry)
            logger.info("Dashboard session revoked (tid=%s)", tid[:6] + "...")
        except Exception:
            logger.exception("Logout revoke failed")
            # Fall through — still clear the cookie even if DB write failed.

    response = JSONResponse({"ok": True}, status_code=200)
    response.set_cookie(
        key="oncofiles_session",
        value="",
        max_age=0,
        expires=0,
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )
    return response


@mcp.custom_route("/api/documents", methods=["GET"])
async def api_documents(request: Request) -> JSONResponse:
    """Document status matrix API. Requires bearer or dashboard session auth.

    Each row carries the user-facing lifecycle from ``document_stage``
    (``steps``, ``ready``, ``stage``, ``attention_reason``, ``curated``,
    ``uploaded_at``) and its full filename; ``?filter=`` also takes ``ready``,
    ``in_progress`` and ``attention``.

    Files still waiting in a curated inbox are rows too (``inbox: true``,
    ``id = "inbox:<drive id>"``, stage ``in_inbox`` or ``attention`` /
    ``review_needed``), read from the curated probe's in-memory snapshot of
    this patient: no Drive call and no DB write in this request.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        from oncofiles.tools.curated import inbox_snapshot
        from oncofiles.tools.hygiene import _build_document_matrix

        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await _get_dashboard_patient_id(request)
        filter_param = request.query_params.get("filter", "all")
        # Cap bumped from 200 → 2000 (#419). Dashboard currently renders the full
        # result so high limits are fine; heavy-pagination consumers should pass
        # offset + limit explicitly.
        try:
            limit = min(int(request.query_params.get("limit", "500")), 2000)
        except (ValueError, TypeError):
            limit = 500
        try:
            offset = max(int(request.query_params.get("offset", "0")), 0)
        except (ValueError, TypeError):
            offset = 0
        result = await _build_document_matrix(
            db,
            filter_param=filter_param,
            limit=limit,
            offset=offset,
            patient_id=patient_id,
            truncate_names=False,  # the dashboard shows whole filenames
            # files waiting in a curated inbox, from the probe's in-memory
            # snapshot of THIS patient (no Drive call here; #921 follow-up)
            inbox_items=inbox_snapshot(patient_id),
        )
        return JSONResponse(result)
    except Exception:
        logger.exception("API documents endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/activity", methods=["GET"])
async def api_activity(request: Request) -> JSONResponse:
    """One counts-only activity log for the dashboard patient.

    ``?limit=`` (default 30, max 100). Same auth and patient resolution as
    ``/api/documents``: a caller without a patient gets the sentinel, which
    matches no row, so the list is simply empty.

    ``events``, newest first, merged from the patient's real curated runs (no
    dry runs or previews, none of the non-run jobs) and the legacy Drive sync
    history. Each event is ``at``, ``kind``, ``status``, ``trigger``,
    ``duration_s`` and ``counts`` (``dashboard_activity``): codes and counts
    only, never a file name, a message, an error text or a document value.

    ``pending.inbox``: inbox files the next automatic run would process, as
    the last in-memory probe of this process saw them; null when unknown. No
    Drive call and no model call happens here.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        from oncofiles import dashboard_activity
        from oncofiles.tools.curated import probe_stats

        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await _get_dashboard_patient_id(request)
        try:
            limit = int(request.query_params.get("limit", "30"))
        except (ValueError, TypeError):
            limit = 30
        limit = min(max(limit, 1), 100)

        # One DB connection: the reads run one after the other.
        runs = await db.list_curated_run_activity(patient_id=patient_id, limit=limit)
        rereads = await db.list_reocr_run_activity(patient_id=patient_id, limit=limit)
        syncs = await db.get_sync_history(limit=limit, patient_id=patient_id)
        events = dashboard_activity.merge(runs, syncs, limit=limit, rereads=rereads)

        pending = (probe_stats().get(patient_id) or {}).get("pending_inbox_count")
        return JSONResponse(
            {
                "events": events,
                "pending": {"inbox": pending if isinstance(pending, int) else None},
            }
        )
    except Exception:
        logger.exception("API activity endpoint error")
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
        # #867: no own-patient fallback for a named patient, so an unknown and
        # a foreign ?patient_id= give the same answer (#590).
        patient_id = await _get_dashboard_patient_id(request, fallback_to_own=False)

        # Scoped in SQL (the #499 pattern): a foreign id, a soft-deleted
        # document, an unknown or foreign patient all answer the same 404.
        if patient_id == NO_PATIENT_ACCESS_SENTINEL:
            return _patient_not_found()
        doc = await db.get_document(doc_id, patient_id=patient_id)
        if doc is None or doc.deleted_at is not None:
            return _patient_not_found()

        gdrive_id = doc.gdrive_id or ""
        gdrive_url = f"https://drive.google.com/file/d/{gdrive_id}/view" if gdrive_id else ""
        preview_url = f"https://drive.google.com/file/d/{gdrive_id}/preview" if gdrive_id else ""

        # Parse ai_tags (JSON string)
        ai_tags: list[str] = []
        if doc.ai_tags:
            try:
                ai_tags = json.loads(doc.ai_tags)
            except Exception:
                ai_tags = []

        from oncofiles import document_stage

        # Parse structured_metadata (JSON string)
        structured_metadata: dict = {}
        if doc.structured_metadata:
            try:
                structured_metadata = json.loads(doc.structured_metadata)
            except Exception:
                structured_metadata = {}

        # Fetch OCR pages (serialized — single DB connection)
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
                "id": doc.id,
                "filename": doc.filename,
                "gdrive_id": gdrive_id,
                "gdrive_url": gdrive_url,
                "preview_url": preview_url,
                "category": doc.category.value if doc.category else "other",
                # #919: a curated document dated by month only → "YYYY-MM"
                "document_date": (
                    doc.document_date.isoformat()
                    if doc.document_date
                    else document_stage.document_month(
                        doc.file_id, structured_metadata, doc.filename
                    )
                    or ""
                ),
                "institution": doc.institution or "",
                "ai_summary": doc.ai_summary or "",
                "ai_tags": ai_tags,
                "structured_metadata": structured_metadata,
                "pages": pages,
            }
        )
    except Exception:
        logger.exception("API doc-detail endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/doc-thumbnails", methods=["GET"])
async def api_doc_thumbnails(request: Request) -> JSONResponse:
    """Previews of the patient's documents (#948, rules in ``doc_thumbnails``).

    ``?ids=12,15&size=s|l[&patient_id=<slug|uuid>]``: at most 24 ids, ``l``
    one id. 200 ``{"thumbnails": {"<id>": {"src": "data:image/..."} | null}}``
    in request order. Unknown, foreign, deleted, excluded and preview-less
    documents are all ``null`` (#590); so is everything while
    ``GOOGLE_INTEGRATION_ENABLED`` is off or the caller has no patient.
    400 ``{"error": <code>}`` for a malformed query. ``Cache-Control:
    private, no-store``. Drive is only read; no DB write.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    from oncofiles import doc_thumbnails

    no_store = {"Cache-Control": "private, no-store"}
    try:
        ids, size = doc_thumbnails.parse_query(
            request.query_params.get("ids"), request.query_params.get("size")
        )
    except doc_thumbnails.BadRequestError as bad:
        return JSONResponse({"error": bad.code}, status_code=400, headers=no_store)

    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        # #867: no own-patient fallback for a named patient; a sentinel makes
        # every id null, the same answer as unknown ids (#590).
        patient_id = await _get_dashboard_patient_id(request, fallback_to_own=False)
        thumbnails = await doc_thumbnails.thumbnails_for(
            db,
            patient_id,
            ids,
            size,
            google_enabled=GOOGLE_INTEGRATION_ENABLED,
            make_clients=_create_patient_clients,
        )
        return JSONResponse({"thumbnails": thumbnails}, headers=no_store)
    except Exception as exc:
        # The type only: a message could carry a URL or a name.
        logger.error("API doc-thumbnails error: %s", type(exc).__name__)
        return JSONResponse({"error": "internal error"}, status_code=500, headers=no_store)


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


def _schedules_payload(lifespan_tracker: dict | None) -> dict:
    """Jobs of the RUNNING scheduler + the curated loop (in-memory, no DB).

    Built by ``scheduled_jobs`` from ``scheduler_singleton`` (job id, trigger,
    next run) and the job tracker; known jobs this server does not run are
    listed under ``inactive`` with the switch that keeps them off.
    """
    from oncofiles import scheduled_jobs
    from oncofiles.tools.curated import continuous_settings, nightly_settings, probe_stats

    current = scheduler_singleton.current_scheduler()
    scheduler, tracker = current if current else (None, None)
    if tracker is None:
        tracker = lifespan_tracker or {}
    flags = {
        "scheduler_running": scheduler is not None,
        "sync_enabled": SYNC_ENABLED,
        "ai_nightly_only": AI_NIGHTLY_ONLY,
        "gmail_calendar_enabled": gmail_calendar_enabled(),
        "integrity_check": ENABLE_INTEGRITY_CHECK,
    }
    built = scheduled_jobs.build_jobs(scheduler, tracker, flags)
    # #856 / #999: lifespan loops (not APScheduler jobs) are shown in the same lists.
    for job_id in sorted(scheduled_jobs.BACKGROUND_LOOP_JOBS):
        loop_entry = scheduled_jobs.background_loop_entry(
            job_id,
            running=scheduler_singleton.background_loop_running(job_id),
            state=scheduled_jobs.background_loop_state(job_id),
        )
        if loop_entry.get("registered"):
            built["jobs"].append(loop_entry)
        else:
            built["inactive"].append(loop_entry)
    built["jobs"].sort(key=lambda j: (j["next_run_at"] is None, j["next_run_at"] or "", j["id"]))
    nightly_enabled, nightly_hour, _ = nightly_settings()
    curated = scheduled_jobs.curated_loop_entry(
        running=scheduler_singleton.curated_loop_running(),
        nightly_enabled=nightly_enabled,
        nightly_hour=nightly_hour,
        google_enabled=GOOGLE_INTEGRATION_ENABLED,
        continuous=continuous_settings(),
        stats=probe_stats(),
    )
    return {
        "scheduler": {
            "running": scheduler is not None,
            "started_at": scheduler_singleton.status().get("started_at"),
            "job_count": sum(
                1
                for j in built["jobs"]
                if j["registered"] and j["id"] not in scheduled_jobs.BACKGROUND_LOOP_JOBS
            ),
            "off_reason": None
            if scheduler is not None
            else scheduled_jobs.inactive_reason("", flags),
        },
        "curated_loop": curated,
        "jobs": built["jobs"],
        "inactive": built["inactive"],
    }


@mcp.custom_route("/api/schedules", methods=["GET"])
async def api_schedules(request: Request) -> JSONResponse:
    """Jobs of the running scheduler, the curated loop and prompt stats by call type.

    The job list comes from the live APScheduler (``_schedules_payload``), not
    a static catalogue; error details are the time and exception class only.
    Admin only (static bearer or a ``DASHBOARD_ADMIN_EMAILS`` session): the job
    internals and the prompt statistics are system-wide,
    not one patient's. Any other signed-in user gets 403; the patient-facing
    view of what happened is ``/api/activity``.
    """
    err = _require_admin(request)
    if err:
        return err

    try:
        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        payload = _schedules_payload(lifespan_ctx.get("job_tracker"))

        # Prompt stats by call type (last 30 days)
        db_inst: Database = lifespan_ctx["db"]
        prompt_stats = await db_inst.get_prompt_stats(days=30, patient_id=None)
        from dataclasses import asdict

        prompts = asdict(prompt_stats)

        return JSONResponse({**payload, "prompt_stats": prompts})
    except Exception:
        logger.exception("API schedules endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/usage-analytics", methods=["GET"])
async def api_usage_analytics(request: Request) -> JSONResponse:
    """Usage analytics: prompt stats, tool usage, pipeline health, latency.

    Scoping (#482):
      - Admin caller (DASHBOARD_ADMIN_EMAILS) → system-wide view by default
        (patient_id=None). May drill down by passing ?patient_id=<uuid|slug>.
      - Non-admin session user → ALWAYS scoped to the request's patient
        (resolved via _get_dashboard_patient_id, which enforces the
        caregiver_email ACL and returns the sentinel when access is denied).
      - Bearer-token caller (system/ops) → system-wide view (patient_id=None).
        Explicit ?patient_id= still honored for drilldown.

    Returns a `scope` field so the dashboard knows whether to render
    "všetci pacienti" vs "môj pacient" in the period label.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        days = min(int(request.query_params.get("days", "30")), 90)

        email = _get_dashboard_email(request)
        is_bearer = _check_bearer(request) is None
        is_admin = is_bearer or _is_admin_email(email)

        scope: str
        scoped_pid: str | None
        if is_admin:
            # Admin + bearer: unscoped by default, or drill-down if query param present.
            explicit = request.query_params.get("patient_id", "").strip()
            if explicit:
                scoped_pid = await _get_dashboard_patient_id(request)
                scope = "single_patient"
            else:
                scoped_pid = None
                scope = "all_patients"
        else:
            # Regular session user: always scoped via the dashboard helper, which
            # applies the caregiver_email ACL and returns NO_PATIENT_ACCESS_SENTINEL
            # when the caller has no authorized patient. DB helpers below use
            # `is not None` semantics so the sentinel string matches zero rows.
            scoped_pid = await _get_dashboard_patient_id(request)
            scope = "single_patient"

        prompt_stats = await db_inst.get_prompt_stats(days=days, patient_id=scoped_pid)
        tool_stats = await db_inst.get_tool_usage_stats(days=days, patient_id=scoped_pid)
        pipeline_stats = await db_inst.get_pipeline_stats(patient_id=scoped_pid)
        latency = await db_inst.get_prompt_latency_percentiles(days=days, patient_id=scoped_pid)

        from dataclasses import asdict

        return JSONResponse(
            {
                "days": days,
                "scope": scope,
                "patient_id": scoped_pid,
                "prompts": asdict(prompt_stats),
                "tools": asdict(tool_stats),
                "pipeline": asdict(pipeline_stats),
                "latency": latency,
            }
        )
    except Exception:
        logger.exception("API usage-analytics endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/cost-leaderboard", methods=["GET"])
async def api_cost_leaderboard(request: Request) -> JSONResponse:
    """Per-patient AI spend leaderboard (#411 Part A — admin-only).

    Returns one row per patient with non-zero AI activity in the rolling
    window, sorted by total cost descending. Designed to surface anomalous
    spenders for the admin dashboard's anomaly-review flow without requiring
    the operator to drill into each patient via `/api/usage-analytics`.

    Auth contract: admin-only. Static `MCP_BEARER_TOKEN` callers and OAuth
    callers whose Google email is in `DASHBOARD_ADMIN_EMAILS` are admitted;
    every other caller (including caregivers signed in to the dashboard)
    receives 403 here. Self-service per-patient stats already work via
    `/api/usage-analytics` which scopes to the caller's own patient — that
    path covers Part B; this endpoint is exclusively the admin overview.

    Query params:
        days: rolling window (default 30, capped at 365).
        limit: max patients to return (default 50, capped at 500).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err

    email = _get_dashboard_email(request)
    is_bearer = _check_bearer(request) is None
    if not (is_bearer or _is_admin_email(email)):
        return JSONResponse(
            {
                "error": "forbidden",
                "detail": (
                    "Cost leaderboard is admin-only. Per-patient self-service "
                    "stats are available via /api/usage-analytics."
                ),
            },
            status_code=403,
        )

    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        days = min(max(int(request.query_params.get("days", "30")), 1), 365)
        limit = min(max(int(request.query_params.get("limit", "50")), 1), 500)

        leaderboard = await db_inst.get_per_patient_cost_leaderboard(days=days, limit=limit)

        total_cost = round(sum(row["total_cost_usd"] for row in leaderboard), 6)

        return JSONResponse(
            {
                "days": days,
                "limit": limit,
                "patient_count": len(leaderboard),
                "total_cost_usd": total_cost,
                "leaderboard": leaderboard,
            }
        )
    except ValueError as exc:
        return JSONResponse({"error": "bad_request", "detail": str(exc)}, status_code=400)
    except Exception:
        logger.exception("API cost-leaderboard endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/bug-report", methods=["POST"])
async def api_bug_report(request: Request) -> JSONResponse:
    """File a dashboard bug report as an issue in a **private** repository.

    Privacy (2026-11-09, #621): the dashboard shows patient documents, so no
    screenshot and no page state ever leave the server. Reports go only to
    ``BUG_REPORT_REPO`` (no default), and only after GitHub confirms that the
    repository is private; otherwise the report is refused (fail closed).
    """
    from urllib.parse import urlsplit

    import httpx

    from oncofiles.config import BUG_REPORT_REPO, GITHUB_TOKEN

    err = _check_dashboard_auth(request)
    if err:
        return err
    if not (GITHUB_TOKEN and BUG_REPORT_REPO):
        return JSONResponse({"error": "bug reports are not configured"}, status_code=503)

    try:
        body = await request.json()
        title = str(body.get("title", "Dashboard bug report"))[:120]
        description = str(body.get("description", ""))[:1000]
        page_path = urlsplit(str(body.get("page_url", ""))).path[:200]  # no query / fragment
        page_section = str(body.get("page_section", ""))[:80]
        user_agent = str(body.get("user_agent", ""))[:100]
        console_errors = str(body.get("console_errors", ""))[:1000]
        # Deliberately ignored: body["screenshot"], body["page_state"] (patient data).

        def _esc(s: str) -> str:
            return s.replace("|", "\\|").replace("`", "\\`")

        issue_body = f"""## Bug Report (Dashboard)

### Description
```
{description}
```

### Context
| Field | Value |
|-------|-------|
| Page | `{_esc(page_path)}` |
| Section | `{_esc(page_section)}` |
| User Agent | `{_esc(user_agent)}` |
| Reported | {datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")} |

### Console Errors
```
{console_errors or "None"}
```

_No screenshot or page data is attached: the dashboard shows patient documents._
"""
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            repo = await client.get(
                f"https://api.github.com/repos/{BUG_REPORT_REPO}", headers=headers
            )
            if repo.status_code != 200 or repo.json().get("private") is not True:
                logger.error("Bug report refused: %s is not a private repository", BUG_REPORT_REPO)
                return JSONResponse(
                    {"error": "bug report target is not a private repository"}, status_code=503
                )
            resp = await client.post(
                f"https://api.github.com/repos/{BUG_REPORT_REPO}/issues",
                headers=headers,
                json={"title": f"[Dashboard Bug] {title}", "body": issue_body,
                      "labels": ["bug", "dashboard"]},
            )  # fmt: skip
        if resp.status_code == 201:
            issue_url = resp.json().get("html_url", "")
            logger.info("Bug report created: %s", issue_url)
            return JSONResponse({"ok": True, "issue_url": issue_url})
        logger.error("GitHub issue creation failed: %d %s", resp.status_code, resp.text[:200])
        return JSONResponse({"error": f"GitHub API error: {resp.status_code}"}, status_code=502)
    except Exception:
        logger.exception("Bug report endpoint error")
        return JSONResponse({"error": "internal error"}, status_code=500)


# ── Newsletter subscribe ─────────────────────────────────────────────

# Per-IP rate limiter: {ip: [timestamp, ...]}
_newsletter_rate: dict[str, list[float]] = {}
_NEWSLETTER_RATE_LIMIT = 5  # max signups per IP per hour
_NEWSLETTER_RATE_WINDOW = 3600  # 1 hour


async def _notify_new_subscriber(email: str, source: str) -> None:
    """Send the '[Lead]' note to the admin when someone joins the waitlist.

    Fire-and-forget over SMTP (`mailer.send_mail`, #834): failures are logged
    by the mailer without addresses and never propagate, so the subscribe
    response does not depend on delivery. The subscriber's address goes only
    into the body for the admin, never into a log line or the subject.
    """
    to = mailer.admin_recipients()
    if not to:
        logger.warning("Newsletter [Lead] note not sent: NOTIFY_TO_EMAIL is not set")
        return
    subject = f"[Lead] Oncofiles waitlist ({source})"
    text = (
        f"New waitlist subscriber:\n\n"
        f"  email:  {email}\n"
        f"  source: {source}\n"
        f"  time:   {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n\n"
        f"Stored in the oncofiles newsletter_subscribers table.\n"
    )
    await mailer.send_mail(to, subject, text, tags=["lead", "newsletter"])


_WELCOME_SUBJECT = "Oncofiles — ste na zozname / you're on the list"

_WELCOME_TEXT = (
    "Ďakujeme, že ste sa prihlásili na zoznam čakateľov Oncofiles.\n"
    "Napíšeme Vám, keď bude pre Vás pripravený prístup, a inak len vtedy, keď "
    "máme niečo podstatné.\n\n"
    "Odhlásiť sa môžete kedykoľvek: odpovedzte na tento e-mail a Vašu adresu "
    "odstránime.\n\n"
    "Peter Fusek, Oncofiles\n"
    "https://oncofiles.com\n\n"
    "— English —\n\n"
    "Thank you for joining the Oncofiles waitlist.\n"
    "We will write when access is ready for you, and otherwise only when we "
    "have something substantial to share.\n\n"
    "You can unsubscribe at any time: reply to this e-mail and we will remove "
    "your address.\n\n"
    "Peter Fusek, Oncofiles\n"
    "https://oncofiles.com\n"
)

_WELCOME_HTML = """<!doctype html>
<html><body style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5;color:#1f2933">
<p>Ďakujeme, že ste sa prihlásili na zoznam čakateľov Oncofiles.
Napíšeme Vám, keď bude pre Vás pripravený prístup, a inak len vtedy, keď máme niečo
podstatné.</p>
<p>Odhlásiť sa môžete kedykoľvek: odpovedzte na tento e-mail a Vašu adresu odstránime.</p>
<p>Peter Fusek, Oncofiles<br>oncofiles.com</p>
<hr style="border:0;border-top:1px solid #d9e2ec">
<p lang="en">Thank you for joining the Oncofiles waitlist.
We will write when access is ready for you, and otherwise only when we have something
substantial to share.</p>
<p lang="en">You can unsubscribe at any time: reply to this e-mail and we will remove your
address.</p>
<p lang="en">Peter Fusek, Oncofiles<br>oncofiles.com</p>
</body></html>
"""


async def _send_subscriber_welcome(email: str, source: str) -> bool:
    """Send the bilingual SK/EN welcome TO the subscriber (fire-and-forget).

    Plain text + simple HTML, no patient data, no signed links and no
    tracking parameters of ours; unsubscribe is a reply (Reply-To comes from
    `MAIL_REPLY_TO`). Returns whether the relay accepted it.
    """
    return await mailer.send_mail(
        [email],
        _WELCOME_SUBJECT,
        _WELCOME_TEXT,
        html=_WELCOME_HTML,
        tags=["subscriber_welcome", f"source-{source}"[:40]],
    )


async def _notify_admin_pipeline_alert(*, condition: str, subject: str, body: str) -> None:
    """Email the admin when the processing pipeline is broken (#552).

    Same fire-and-forget SMTP path as the other admin notes (`mailer`, #834).

    This exists because logs demonstrably did not work: the GCP-project
    deletion sat in the container log for a month while `/health` reported
    `status: ok`, and the AutoCheck monitor (#524) had been blind for 100+
    days. An email is the only channel that reaches a human who is not already
    looking.

    Deduplicated to at most one send per `condition` per UTC day so a
    persistent outage does not become an hourly mail flood.
    """
    today = datetime.now(UTC).date().isoformat()
    if _pipeline_alert_sent.get(condition) == today:
        return

    to = mailer.admin_recipients()
    configured = mailer.is_configured()
    if not to or not configured:
        logger.warning(
            "Pipeline alert SKIPPED (%s) — outgoing mail not configured "
            "(SMTP_HOST/MAIL_FROM=%s, NOTIFY_TO_EMAIL=%s)",
            condition,
            "set" if configured else "missing",
            "set" if to else "missing",
        )
        return

    # Mark before sending: a relay outage must not turn into a retry storm.
    _pipeline_alert_sent[condition] = today
    if await mailer.send_mail(to, subject, body, tags=["pipeline_alert", condition]):
        logger.info("Pipeline alert sent to admin (%s)", condition)


async def _notify_admin_onboarding_event(
    *,
    patient_slug: str,
    display_name: str,
    caregiver_email: str,
    event_type: str,
) -> None:
    """Send an admin-facing onboarding note over SMTP (#468, #834).

    Fire-and-forget: same shape as `_notify_new_subscriber`; the caller's
    request never fails because the note could not be sent. The patient's
    display name is deliberately left out (no patient data in e-mails, #834);
    the slug and the dashboard link identify the patient for the admin.
    `display_name` stays in the signature for the callers.
    """
    del display_name  # not sent: no patient data in e-mails (#834)
    to = mailer.admin_recipients()
    if not to:
        logger.warning(
            "Onboarding admin notification SKIPPED for patient=%s event=%s — "
            "NOTIFY_TO_EMAIL is not set",
            patient_slug,
            event_type,
        )
        return

    label_sk = {
        "created": "Nový pacient",
        "oauth_ok": "OAuth dokončený",
        "folder_set": "Drive priečinok nastavený",
        "first_sync": "Prvá synchronizácia",
        "first_ai": "Prvý AI dokument",
    }.get(event_type, event_type)
    subject = f"[Oncofiles] {label_sk}: {patient_slug}"
    text = (
        f"{label_sk}: {patient_slug}\n\n"
        f"  patient_slug:    {patient_slug}\n"
        f"  caregiver_email: {caregiver_email or '(unset)'}\n"
        f"  event_type:      {event_type}\n"
        f"  time:            {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        f"  dashboard:       https://oncofiles.com/dashboard?patient={patient_slug}\n"
    )
    await mailer.send_mail(to, subject, text, tags=["onboarding", event_type])


@mcp.custom_route("/api/newsletter/subscribe", methods=["POST"])
async def api_newsletter_subscribe(request: Request) -> JSONResponse:
    """Public newsletter signup — no auth required."""
    import re
    import uuid

    # Per-IP rate limiting
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    hits = _newsletter_rate.get(client_ip, [])
    hits = [t for t in hits if now - t < _NEWSLETTER_RATE_WINDOW]
    if len(hits) >= _NEWSLETTER_RATE_LIMIT:
        return JSONResponse(
            {"error": "Too many signups. Try again later."},
            status_code=429,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid JSON body"},
            status_code=400,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    email = (body.get("email") or "").strip().lower()
    if not email or not re.match(r"^[^@]+@[^@]+\.[^@]+$", email):
        return JSONResponse(
            {"error": "Invalid email address"},
            status_code=400,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    source = (body.get("source") or "landing").strip()[:50]

    is_new_row = False
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        subscriber_id = str(uuid.uuid4())
        cursor = await db_inst.db.execute(
            "INSERT OR IGNORE INTO newsletter_subscribers (id, email, source) VALUES (?, ?, ?)",
            (subscriber_id, email, source),
        )
        is_new_row = bool(cursor.rowcount)
        await db_inst.db.commit()
    except Exception:
        logger.exception("Newsletter subscribe DB error")
        return JSONResponse(
            {"error": "internal error"},
            status_code=500,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    # Record rate-limit hit after successful processing
    hits.append(now)
    _newsletter_rate[client_ip] = hits

    # Fire-and-forget notifications — only on genuinely new subscribers.
    # Admin gets the [Lead] alert; subscriber gets a welcome/confirmation
    # email so "I signed up but heard nothing" is no longer the default UX
    # (#461). Both are independent: one failing doesn't drop the other.
    if is_new_row:
        asyncio.create_task(_notify_new_subscriber(email, source))
        asyncio.create_task(_send_subscriber_welcome(email, source))

    # `email_queued` (#834) tells the landing whether to say "check your
    # e-mail": true when outgoing mail is configured. The welcome is queued
    # once per address; an address already on the list gets the same answer,
    # so this response is not a membership oracle.
    return JSONResponse(
        {"ok": True, "email_queued": mailer.is_configured()},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/api/onboarding-status", methods=["GET"])
async def api_onboarding_status(request: Request) -> JSONResponse:
    """Onboarding gate state for THIS caller (#852): mode + booleans only.

    The dashboard uses it to show an invitation notice instead of the new-patient
    wizard / Drive connection. Never returns the invite list; no DB call.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    from oncofiles import onboarding_gate

    email, is_admin = _dashboard_caller(request)
    return JSONResponse(onboarding_gate.status_for(email, is_admin=is_admin))


@mcp.custom_route("/api/public-config", methods=["GET"])
async def api_public_config(request: Request) -> JSONResponse:
    """Public landing flags: onboarding mode, whether the inbound welcome
    address is live, the address itself. No auth, no DB, env only (like
    /health); rate-limit class ``public``; cached briefly."""
    from oncofiles import public_config

    return JSONResponse(
        public_config.public_config(),
        headers={"Cache-Control": f"public, max-age={public_config.CACHE_SECONDS}"},
    )


@mcp.custom_route("/api/patients", methods=["GET"])
async def api_list_patients(request: Request) -> JSONResponse:
    """List patients visible to the authenticated user.

    Returns {own: [...], admin: [...], is_admin: bool}.
    - own: patients where caregiver_email matches the caller
    - admin: all OTHER patients (only for admin emails, read-only access)
    Bearer token requests (MCP/system) see all patients in 'own'.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        all_patients = await db_inst.list_patients(active_only=False)

        email = _get_dashboard_email(request)
        is_bearer = _check_bearer(request) is None
        is_admin = is_bearer or _is_admin_email(email)

        def _to_dict(p):
            return {
                "patient_id": p.patient_id,
                "display_name": p.display_name,
                "caregiver_email": p.caregiver_email,
                "diagnosis_summary": p.diagnosis_summary,
                "is_active": p.is_active,
                "preferred_lang": p.preferred_lang,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }

        if is_bearer:
            # MCP/system: all patients as own
            return JSONResponse(
                {"own": [_to_dict(p) for p in all_patients], "admin": [], "is_admin": False}
            )

        own = [p for p in all_patients if _email_matches_caregiver(email or "", p.caregiver_email)]
        admin_patients = []
        if is_admin:
            own_ids = {p.patient_id for p in own}
            admin_patients = [p for p in all_patients if p.patient_id not in own_ids]

        return JSONResponse(
            {
                "own": [_to_dict(p) for p in own],
                "admin": [_to_dict(p) for p in admin_patients],
                "is_admin": is_admin,
            }
        )
    except Exception:
        logger.exception("API list-patients error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/patient-context", methods=["GET"])
async def api_patient_context(request: Request) -> JSONResponse:
    """Return the clinical context (biomarkers + germline_findings + diagnosis) for a patient.

    Query params:
        patient_id: Target patient. If omitted, uses the caller's default patient
                    (own/caregiver) or errors.

    Feeds the dashboard germline risk banner (#385 Session 3). Never exposes
    documents or PII beyond the patient's own clinical summary fields.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]

        # #867: the single dashboard ACL path (slug or UUID; foreign → 403).
        target_pid, refusal = await _dashboard_patient_or_refusal(request)
        if refusal is not None:
            return refusal

        from oncofiles import patient_context as _pctx

        ctx = await _pctx.load_from_db(db_inst.db, patient_id=target_pid)
        if not ctx:
            ctx = _pctx.get_context(target_pid) or {}

        # Narrow the response to clinically-relevant fields — do not leak notes/
        # physician names by default (caller can request full via MCP get_patient_context).
        response = {
            "patient_id": target_pid,
            "patient_type": ctx.get("patient_type", "oncology"),
            "diagnosis": ctx.get("diagnosis") or "",
            "staging": ctx.get("staging") or "",
            "biomarkers": ctx.get("biomarkers") or {},
            "germline_findings": ctx.get("germline_findings") or {},
            "excluded_therapies": ctx.get("excluded_therapies") or [],
            "disclaimer": {
                "sk": (
                    "Informatívny súhrn pacienta. Nenahrádza ošetrujúceho lekára. "
                    "Pred akýmkoľvek rozhodnutím o liečbe overte s onkológom."
                ),
                "en": (
                    "Informational patient summary. Does not replace the treating "
                    "physician. Verify with your oncologist before any treatment decision."
                ),
            },
        }
        return JSONResponse(response)
    except Exception:
        logger.exception("API patient-context error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/patient-budget", methods=["GET"])
async def api_patient_budget(request: Request) -> JSONResponse:
    """Return the rolling 30-day AI spend + tier cap for a patient (#442).

    Drives the dashboard budget pill. Returns 204 No Content for tier='admin'
    (grandfathered prod patients) and for paid_* tiers — the UI hides the pill
    for those.

    Query params:
        patient_id: Target patient. If omitted, uses the caller's first patient.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]

        # #867: the single dashboard ACL path (slug or UUID; foreign → 403).
        target_pid, refusal = await _dashboard_patient_or_refusal(request)
        if refusal is not None:
            return refusal

        from oncofiles.budget import check_patient_budget

        status = await check_patient_budget(db_inst, target_pid)
        patient = await db_inst.get_patient(target_pid)
        onboarding_ends_at = (
            patient.onboarding_ends_at.isoformat()
            if patient and patient.onboarding_ends_at
            else None
        )

        return JSONResponse(
            {
                **status.as_dict(),
                "onboarding_ends_at": onboarding_ends_at,
                # Pill should hide for admin + paid_* (no cap or paid for it).
                "show_pill": status.tier in ("free_onboarding", "free"),
            }
        )
    except Exception:
        logger.exception("API patient-budget error")
        return JSONResponse({"error": "internal error"}, status_code=500)


NEW_PATIENT_TYPES = ("oncology", "general")


def _parse_new_patient_context_fields(body: dict) -> tuple[dict, str | None]:
    """Validate ``patient_type`` / ``date_of_birth`` of a new-patient request (#824).

    Returns (context updates, None) or ({}, error message). The type defaults
    to oncology; the birth date is optional, must be a real ISO calendar date
    (YYYY-MM-DD) and must not be in the future.
    """
    import re
    from datetime import date

    raw_type = body.get("patient_type")
    patient_type = "oncology" if raw_type in (None, "") else raw_type
    if patient_type not in NEW_PATIENT_TYPES:
        return {}, "patient_type must be one of: " + ", ".join(NEW_PATIENT_TYPES)
    fields: dict = {"patient_type": patient_type}

    raw_dob = body.get("date_of_birth")
    if raw_dob not in (None, ""):
        dob_error = "date_of_birth must be a valid date (YYYY-MM-DD), not in the future"
        if not isinstance(raw_dob, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_dob.strip()):
            return {}, dob_error
        try:
            dob = date.fromisoformat(raw_dob.strip())
        except ValueError:
            return {}, dob_error
        if dob > date.today() or dob.year < 1900:
            return {}, dob_error
        fields["date_of_birth"] = dob.isoformat()
    return fields, None


@mcp.custom_route("/api/patients", methods=["POST"])
async def api_create_patient(request: Request) -> JSONResponse:
    """Create a new patient. Requires bearer or dashboard session auth.

    Body: {patient_id, display_name, consent, consent_version,
    caregiver_email?, diagnosis_summary?, preferred_lang?, patient_type?,
    date_of_birth?}

    ``consent`` (JSON ``true``) and ``consent_version`` (equal to
    ``patient_consent.CONSENT_VERSION``) are required from every caller,
    the operator and admins included (#857); without them nothing is written
    (400). The consent row (version, time, keyed caller hash, never the
    e-mail) is written in the same transaction as the patient.

    ``patient_type`` (oncology | general, default oncology) and the optional
    ``date_of_birth`` (ISO date, not in the future) are validated and stored
    in the patient context at creation (#824). No bearer token is minted
    here: a token is created only on request via ``/api/patient-tokens``.
    """
    from oncofiles.models import Patient

    err = _check_dashboard_auth(request)
    if err:
        return err
    gate_err = _dashboard_onboarding_refusal(request, "create_patient")
    if gate_err:
        return gate_err
    rate_err = _check_rate_limit("patients")
    if rate_err:
        return rate_err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        from oncofiles import patient_consent
        from oncofiles.secrets_keys import caller_email_hash

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON object required"}, status_code=400)
        # #857: no patient without recorded consent, checked before any write.
        consent_err = patient_consent.consent_error(body)
        if consent_err:
            return JSONResponse(patient_consent.refusal_payload(consent_err), status_code=400)
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
        # Validate the context fields before anything is written (#824).
        context_fields, field_error = _parse_new_patient_context_fields(body)
        if field_error:
            return JSONResponse({"error": field_error}, status_code=400)
        existing = await db_inst.get_patient_by_slug(patient_id)
        if existing:
            return JSONResponse(
                {"error": f"Patient '{patient_id}' already exists"}, status_code=409
            )

        # Default caregiver_email to the signed-in user so they can see the patient
        caregiver_email = body.get("caregiver_email") or _get_dashboard_email(request)

        patient = Patient(
            patient_id=patient_id,
            display_name=body.get("display_name", patient_id),
            caregiver_email=caregiver_email,
            diagnosis_summary=body.get("diagnosis_summary"),
            preferred_lang=body.get("preferred_lang", "sk"),
        )
        # The consent is the signed-in caller's: hash the session e-mail (None
        # for the operator's static bearer, which has no e-mail).
        _caller, caller_is_admin = _dashboard_caller(request)
        created = await db_inst.insert_patient(
            patient,
            consent_version=patient_consent.CONSENT_VERSION,
            consent_caller_hash=caller_email_hash(_get_dashboard_email(request) or ""),
            consent_by_admin=caller_is_admin,
        )
        # No bearer token is minted here (#824): tokens are opt-in via
        # /api/patient-tokens. NB: `created.patient_id` is the UUID; the
        # request-body `patient_id` is the slug.
        #
        # Patient type and birth date go into the patient context, the single
        # source for both (`patients.profile` is a legacy, unused column).
        # Same path as the `update_patient_context` tool. Best-effort: the
        # patient row already exists, so a failure is reported, not raised.
        context_saved = True
        try:
            from oncofiles import patient_context

            await patient_context.ensure_loaded(db_inst.db, created.patient_id)
            # #835: the context's `name` was left empty; it is what tools and the
            # curated contract (#853) show as the patient's name.
            context_fields = {**context_fields, "name": created.display_name or patient_id}
            new_ctx = patient_context.update_context(context_fields, patient_id=created.patient_id)
            await patient_context.save_to_db(db_inst.db, new_ctx, patient_id=created.patient_id)
        except Exception:
            context_saved = False
            logger.exception("Patient context save failed for new patient %s", created.patient_id)
        logger.info("New patient created: %s", patient_id)

        # Onboarding signal T0 (#468): record event row + fire-and-forget admin email.
        # Both calls are best-effort and must NEVER fail patient creation.
        # Note: `patient_id` (request body) is the slug; `created.patient_id` is the UUID.
        try:
            await db_inst.insert_onboarding_event(
                created.patient_id,
                "created",
                meta={
                    "slug": patient_id,
                    "display_name": created.display_name,
                    "caregiver_email": created.caregiver_email,
                    "preferred_lang": created.preferred_lang,
                },
            )
        except Exception:
            logger.exception("Onboarding T0 event insert failed for %s", created.patient_id)
        asyncio.create_task(
            _notify_admin_onboarding_event(
                patient_slug=patient_id,
                display_name=created.display_name,
                caregiver_email=created.caregiver_email or "",
                event_type="created",
            )
        )

        return JSONResponse(
            {
                "patient": {
                    "patient_id": created.patient_id,
                    "display_name": created.display_name,
                    "caregiver_email": created.caregiver_email,
                    "diagnosis_summary": created.diagnosis_summary,
                    "preferred_lang": created.preferred_lang,
                    "patient_type": context_fields["patient_type"],
                    "date_of_birth": context_fields.get("date_of_birth", ""),
                },
                "context_saved": context_saved,
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

        # Ownership check: session users can only modify patients they're caregiver for
        email = _get_dashboard_email(request)
        is_bearer = _check_bearer(request) is None
        if not is_bearer and not _email_matches_caregiver(email or "", patient.caregiver_email):
            return JSONResponse({"error": "Not authorized to modify this patient"}, status_code=403)

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
        raw_patient_id = body.get("patient_id", "").strip()
        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)
        # Token rows key on the patient UUID: resolve a slug first (#824), so a
        # slug can never reach create_patient_token (an unknown value → 404).
        patient_id, acl_err = await _dashboard_body_patient(request, db_inst, raw_patient_id)
        # #489 Part B: ACL gate — body-supplied patient_id must belong to caller.
        # Without this, any session-authenticated user could mint bearer tokens
        # for any patient (cross-patient privilege escalation; mirror of Felix's
        # oncoteam#438 Bug 3).
        if acl_err is not None:
            return acl_err
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

    Legacy sync: admin-only in every ONBOARDING_MODE until #853 (#852).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    gate_err = _dashboard_legacy_refusal(request, "sync_trigger")
    if gate_err:
        return gate_err

    try:
        body = await request.json()
        raw_patient_id = body.get("patient_id", "").strip().lower()
        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)

        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db_inst: Database = lifespan_ctx["db"]
        files = lifespan_ctx["files"]

        # Resolve slug → UUID before DB queries
        patient_id, acl_err = await _dashboard_body_patient(request, db_inst, raw_patient_id)

        # #489 Part B: ACL gate (body-supplied patient_id → caregiver_email match).
        if acl_err is not None:
            return acl_err

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

    Legacy pipeline: admin-only in every ONBOARDING_MODE until #853 (#852).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    gate_err = _dashboard_legacy_refusal(request, "enhance_trigger")
    if gate_err:
        return gate_err

    try:
        body = await request.json()
        raw_patient_id = body.get("patient_id", "").strip().lower()
        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)

        lifespan_ctx = request.app.state.fastmcp_server._lifespan_result
        db_inst: Database = lifespan_ctx["db"]
        files = lifespan_ctx["files"]

        patient_id, acl_err = await _dashboard_body_patient(request, db_inst, raw_patient_id)

        # #489 Part B: ACL gate (body-supplied patient_id → caregiver_email match).
        if acl_err is not None:
            return acl_err

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
            await db_inst.update_oauth_access_token(
                token.patient_id, token.provider, access_token, new_expiry
            )

        gdrive = GDriveClient.from_oauth(
            access_token=access_token,
            refresh_token=token.refresh_token,
            client_id=GOOGLE_OAUTH_CLIENT_ID,
            client_secret=GOOGLE_OAUTH_CLIENT_SECRET,
        )

        # #563: browse into subfolders (e.g. Health/<patient>), not only My Drive root.
        from oncofiles.curated_folder import parse_drive_folder_ref

        raw_parent = request.query_params.get("parent", "").strip()
        parent_id = "root"
        if raw_parent and raw_parent != "root":
            parent_id = parse_drive_folder_ref(raw_parent)
            if not parent_id:
                return JSONResponse({"error": "invalid parent folder id"}, status_code=400)

        folders = await asyncio.to_thread(gdrive.list_child_folders, parent_id)
        parent_name = "My Drive"
        grandparent_id = None
        if parent_id != "root":
            try:
                meta = await asyncio.to_thread(
                    gdrive.get_file_metadata, parent_id, "id,name,parents,mimeType"
                )
            except FileNotFoundError:
                return JSONResponse({"error": "folder not found"}, status_code=404)
            parent_name = meta.get("name") or parent_id
            parents = meta.get("parents") or []
            grandparent_id = parents[0] if parents else "root"
        # #835: tell the picker what the folder being browsed is, so it can ask
        # before a parent of patient folders is connected. Read-only.
        from oncofiles.curated_folder import folder_overview

        overview = await asyncio.to_thread(folder_overview, gdrive, parent_id, folders)
        return JSONResponse(
            {
                "folders": folders,
                "parent_id": parent_id,
                "parent_name": parent_name,
                "grandparent_id": grandparent_id,
                "current_folder_id": token.gdrive_folder_id,
                "current": overview,
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

    #835: an existing folder is refused with 409 ``code: container_folder`` when
    a subfolder is a curated patient folder (``reason: curated_children``),
    when it has too many subfolders to check (``too_many_to_check``) or when
    the check could not run (``check_failed``). None of them can be confirmed
    away; there is no ``confirm_container`` field any more.

    #853: a folder without a README contract is made curated here
    (``layout: "curated"``, the default): the contract and the fixed folder
    set are written from the patient-type template (``curated/bootstrap.py``),
    the folder is connected and the patient is enrolled in curated
    processing. Refusals are 409 ``code: bootstrap_refused`` with a
    ``reason``; nothing is persisted or written before the checks pass. The
    legacy category folders are created only for ``layout: "legacy"``, which
    is admin-only.

    #852: refused (403 ``onboarding_closed``) for a caller the onboarding gate
    does not admit, before any Drive call.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    gate_err = _dashboard_onboarding_refusal(request, "set_folder")
    if gate_err:
        return gate_err

    try:
        body = await request.json()
        raw_patient_id = body.get("patient_id", "").strip().lower()
        folder_id = body.get("folder_id", "").strip()
        folder_name = body.get("folder_name", "").strip()
        if folder_id:
            # #563: accept a pasted Drive folder link as well as a bare id.
            from oncofiles.curated_folder import parse_drive_folder_ref

            parsed = parse_drive_folder_ref(folder_id)
            if not parsed:
                return JSONResponse(
                    {"error": "folder_id must be a Google Drive folder link or id"},
                    status_code=400,
                )
            folder_id = parsed

        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)
        layout = str(body.get("layout") or "curated")
        if layout not in ("curated", "legacy"):
            return JSONResponse({"error": "layout must be curated or legacy"}, status_code=400)
        if layout == "legacy" and _require_admin(request) is not None:
            return JSONResponse(
                {"error": "The legacy folder layout is admin-only (#853)."}, status_code=403
            )
        if not folder_id and not folder_name:
            return JSONResponse(
                {"error": "folder_id or folder_name required"},
                status_code=400,
            )

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id, acl_err = await _dashboard_body_patient(request, db_inst, raw_patient_id)

        # #489 Part B: ACL gate (body-supplied patient_id → caregiver_email match).
        if acl_err is not None:
            return acl_err

        # #839: one decision path shared with the MCP tool `gdrive_set_folder`
        # (token, optional new folder, "is it a folder", the #835 container
        # guard, persist, owner, curated detection, category subfolders).
        from oncofiles.drive_folder_link import connect_folder

        outcome = await connect_folder(
            db_inst,
            patient_id,
            folder_id=folder_id,
            folder_name=folder_name,
            create_parent_raw=str(body.get("parent_id", "") or ""),
            # #853: the default (curated) layout writes the README contract;
            # only the admin-only legacy layout creates category folders.
            create_subfolders=layout == "legacy",
            bootstrap=layout == "curated",
            date_of_birth=body.get("date_of_birth"),
        )
        return JSONResponse(outcome.payload, status_code=outcome.status)
    except Exception:
        logger.exception("API gdrive-set-folder error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/upload", methods=["POST"])
async def api_upload(request: Request) -> JSONResponse:
    """Add documents from the dashboard (#921). Multipart field ``files``.

    ``?patient_id=`` (slug or UUID) is checked BEFORE the body is read: the
    single body ACL (``_dashboard_body_patient``: unknown and foreign are the
    same 404), then ``_dashboard_write_refusal`` (an admin's read-only view of
    someone else's patient, and the operator bearer, get 403 ``read_only``).
    The rate limiter refuses a body over ``RATE_LIMIT_UPLOAD_MAX_BODY_BYTES``
    with 413 before it is read. Everything else (types, sizes, count, cap,
    curated inbox vs Drive-only) is ``dashboard_upload.handle_upload``.
    No #852 gate: adding documents to a patient that already exists and has
    Drive connected is not onboarding.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    raw_patient_id = str(request.query_params.get("patient_id") or "").strip()
    if not raw_patient_id:
        return JSONResponse({"error": "patient_id required"}, status_code=400)
    patient_id, acl_err = await _dashboard_body_patient(request, db_inst, raw_patient_id)
    if acl_err is not None:
        return acl_err
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err

    from starlette.datastructures import UploadFile

    from oncofiles import dashboard_upload

    try:
        form = await request.form(max_files=dashboard_upload.MAX_FILES + 1, max_fields=5)
    except Exception as exc:
        logger.warning("API upload: unreadable multipart body (%s)", type(exc).__name__)
        return JSONResponse({"error": "bad_request"}, status_code=400)
    try:
        uploads = [f for f in form.getlist("files") if isinstance(f, UploadFile)]
        outcome = await dashboard_upload.handle_upload(db_inst, patient_id, uploads)
        return JSONResponse(outcome.payload, status_code=outcome.status)
    except Exception:
        logger.exception("API upload error")
        return JSONResponse({"error": "internal error"}, status_code=500)
    finally:
        await form.close()


# ── Documents by e-mail (#950 phase 1) ───────────────────────────────────────


@mcp.custom_route("/api/inbound-address", methods=["GET"])
async def api_inbound_address(request: Request) -> JSONResponse:
    """The patient's private document e-mail address (#950).

    The #867 ACL (``_dashboard_patient_or_refusal``: unknown and foreign are
    the same 404), then the #921 write refusal: the address is a credential to
    write into the patient's inbox, so only the caregiver sees it (an admin's
    read-only view and the operator bearer get 403 ``read_only``). The row is
    created on first view. While neither adapter is configured
    (``INBOUND_MIME_HMAC_SECRET`` and ``INBOUND_BREVO_TOKEN`` both unset)
    nothing is created and the answer is ``{"enabled": false}``: no address is
    shown that could not receive mail.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    patient_id, refusal = await _dashboard_patient_or_refusal(request)
    if refusal is not None:
        return refusal
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err
    from oncofiles import inbound_email

    if not inbound_email.is_enabled():
        return JSONResponse({"enabled": False})
    try:
        return JSONResponse(await inbound_email.ensure_address(db_inst, patient_id))
    except Exception:
        logger.exception("API inbound-address error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/inbound-address/rotate", methods=["POST"])
async def api_inbound_address_rotate(request: Request) -> JSONResponse:
    """A new document address for the patient; the old one stops routing (#950).

    Body ``{"patient_id": …}``: ``_dashboard_body_patient`` (#867) then the
    #921 write refusal, as for the upload. The dashboard asks for a second,
    armed click first (no browser dialog).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    patient_id, acl_err = await _dashboard_body_patient(
        request, db_inst, str(body.get("patient_id") or "")
    )
    if acl_err is not None:
        return acl_err
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err
    from oncofiles import inbound_email

    if not inbound_email.is_enabled():
        return JSONResponse({"enabled": False}, status_code=409)
    try:
        return JSONResponse(await inbound_email.rotate_address(db_inst, patient_id))
    except Exception:
        logger.exception("API inbound-address rotate error")
        return JSONResponse({"error": "internal error"}, status_code=500)


# ── Data rights (#856): Drive disconnect and export ──────────────────────────


@mcp.custom_route("/api/drive-disconnect", methods=["POST"])
async def api_drive_disconnect(request: Request) -> JSONResponse:
    """The caregiver disconnects the patient's Google Drive (#856).

    Body ``{"patient_id": …}``: ``_dashboard_body_patient`` (#867) then the
    #921 write refusal (only the patient's own caregiver; an admin's
    read-only view and the operator bearer get 403 ``read_only``). The
    dashboard asks for a second, armed click first (no browser dialog).
    ``data_rights.disconnect_drive`` revokes the grant at Google (unless
    another patient uses it), deletes the stored token and folder link and
    switches curated processing off; 409 ``busy`` + code while curated work of
    the patient runs. Drive files are untouched.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    patient_id, acl_err = await _dashboard_body_patient(
        request, db_inst, str(body.get("patient_id") or "")
    )
    if acl_err is not None:
        return acl_err
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err
    from oncofiles import data_rights

    try:
        result = await data_rights.disconnect_drive(
            db_inst, patient_id, caller_email=_get_dashboard_email(request)
        )
    except Exception:
        logger.exception("API drive-disconnect error")
        return JSONResponse({"error": "internal error"}, status_code=500)
    if result.get("status") == "refused":
        return JSONResponse({"error": "busy", **result}, status_code=409)
    _forget_patient_runtime(patient_id)
    return JSONResponse(result)


# #856: one running export per principal (session e-mail, or the operator).
# Value = monotonic start; an entry older than _EXPORT_STALE_S is ignored (a
# stream the server never saw end). In memory only, never logged.
_exports_running: dict[str, float] = {}
_EXPORT_STALE_S = 900
_EXPORT_RETRY_AFTER_S = 30


async def _export_guarded(principal: str, stream):
    """Yield the export stream; free the principal's slot however it ends."""
    try:
        async for chunk in stream:
            yield chunk
    finally:
        _exports_running.pop(principal, None)


@mcp.custom_route("/api/patient-export", methods=["GET"])
async def api_patient_export(request: Request) -> Response:
    """ZIP of everything Oncofiles stores about ONE patient (#856).

    ``?patient_id=``: ``_dashboard_patient_or_refusal`` (#867; the caregiver,
    or an admin / the operator for a named patient; unknown and foreign are
    the same 404). Streamed in chunks (``data_rights.export_stream``: bounded
    memory, the registry's SQL-filtered rows, no credentials or hashes), with
    ``Cache-Control: no-store``. One ``activity_log`` row per request (the
    caller's keyed hash, no content). One running export per principal: a
    second one gets 429 ``export_busy`` + ``Retry-After`` until the first
    stream ends (or is older than ``_EXPORT_STALE_S``).
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    patient_id, refusal = await _dashboard_patient_or_refusal(request)
    if refusal is not None:
        return refusal
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    from starlette.responses import StreamingResponse

    from oncofiles import data_rights

    patient = await db_inst.get_patient(patient_id)
    if patient is None:
        return _patient_not_found()
    email, is_admin = _dashboard_caller(request)
    principal = (email or "").strip().lower() or "operator"
    started = _exports_running.get(principal)
    if started is not None and time.monotonic() - started < _EXPORT_STALE_S:
        return JSONResponse(
            {
                "error": "export_busy",
                "message_sk": "Jeden export už prebieha. Skúste to o chvíľu znova.",
                "message_en": "An export is already running. Try again in a moment.",
            },
            status_code=429,
            headers={"Retry-After": str(_EXPORT_RETRY_AFTER_S), "Cache-Control": "no-store"},
        )
    _exports_running[principal] = time.monotonic()
    await data_rights.audit(
        db_inst,
        patient_id=patient_id,
        tool="patient_export",
        caller_email=email,
        data={"status": "started", "is_admin": bool(is_admin)},
    )
    slug = re.sub(r"[^a-z0-9-]+", "-", (patient.slug or "patient").lower()).strip("-")
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return StreamingResponse(
        _export_guarded(principal, data_rights.export_stream(db_inst, patient_id)),
        media_type="application/zip",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": (
                f'attachment; filename="oncofiles-export-{slug or "patient"}-{stamp}.zip"'
            ),
        },
    )


@mcp.custom_route("/api/inbound-quarantine", methods=["GET"])
async def api_inbound_quarantine(request: Request) -> JSONResponse:
    """Attachments from other senders waiting for the caregiver (#950 quarantine).

    The #867 ACL (``_dashboard_patient_or_refusal``: unknown and foreign are
    the same 404). The caregiver sees each pending item (sender, time, file
    names, types, sizes); anyone else who passes the ACL (an admin's
    read-only view, the operator bearer) sees only the count.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    patient_id, refusal = await _dashboard_patient_or_refusal(request)
    if refusal is not None:
        return refusal
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    caregiver = await _dashboard_write_refusal(request, db_inst, patient_id) is None
    from oncofiles import inbound_quarantine

    try:
        return JSONResponse(
            await inbound_quarantine.dashboard_view(db_inst, patient_id, caregiver=caregiver)
        )
    except Exception:
        logger.exception("API inbound-quarantine error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/inbound-quarantine/decide", methods=["POST"])
async def api_inbound_quarantine_decide(request: Request) -> JSONResponse:
    """Approve, reject or restore (trash → pending) one quarantined item (#950).

    Body ``{"patient_id", "item_id", "action": "approve" | "reject" | "restore"}``:
    ``_dashboard_body_patient`` (#867) then the #921 write refusal (only the
    caregiver decides). A foreign or unknown item id is the same 404.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    patient_id, acl_err = await _dashboard_body_patient(
        request, db_inst, str(body.get("patient_id") or "")
    )
    if acl_err is not None:
        return acl_err
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err
    action = body.get("action")
    try:
        item_id = int(body.get("item_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if action not in ("approve", "reject", "restore"):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    from oncofiles import inbound_quarantine
    from oncofiles.secrets_keys import caller_email_hash

    if not inbound_quarantine.configured():
        return JSONResponse({"enabled": False}, status_code=409)
    decided_by = caller_email_hash(_get_dashboard_email(request) or "") or "dashboard"
    try:
        if action == "restore":
            # trash → pending: dashboard only, never an e-mail link
            result = await inbound_quarantine.restore(
                db_inst, patient_id, item_id, decided_by=decided_by
            )
        else:
            decide = (
                inbound_quarantine.approve if action == "approve" else inbound_quarantine.reject
            )
            result = await decide(
                db_inst, patient_id, item_id, decided_by=decided_by, via="dashboard"
            )
    except Exception:
        logger.exception("API inbound-quarantine decide error")
        return JSONResponse({"error": "internal error"}, status_code=500)
    if result.get("result") == "not_found":
        return _patient_not_found()
    ok_codes = ("approved", "rejected", "restored", "already")
    status = 200 if result.get("result") in ok_codes else 409
    return JSONResponse(result, status_code=status)


# #976: headers of every quarantine preview answer (image or refusal)
QUARANTINE_PREVIEW_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; sandbox",
    "Referrer-Policy": "no-referrer",
}


@mcp.custom_route("/api/inbound-quarantine/preview", methods=["GET"])
async def api_inbound_quarantine_preview(request: Request) -> Response:
    """A raster preview of one quarantined attachment (#976, rules in
    ``quarantine_preview``).

    ``?patient_id=…&item_id=…&n=…&size=thumb|large``. The card's ACL (#867
    ``_dashboard_patient_or_refusal``: unknown and foreign are the same 404),
    then the #921 write refusal: only the patient's caregiver (an admin's
    read-only view and the operator bearer get 403 ``read_only``). A foreign
    or unknown item id is the same 404. 200 = a re-encoded JPEG (never the
    original bytes); 415 = no safe preview for this type (the card shows the
    type mark); 409 = the file fails its manifest sha256; 422 = rendering
    failed or timed out; 429 ``preview_busy`` + ``Retry-After`` = a render of
    this patient is still running (one per patient). Always
    ``Cache-Control: no-store``.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    patient_id, refusal = await _dashboard_patient_or_refusal(request)
    if refusal is not None:
        return refusal
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err
    from oncofiles import inbound_quarantine, quarantine_preview

    headers = dict(QUARANTINE_PREVIEW_HEADERS)
    size = quarantine_preview.parse_size(request.query_params.get("size"))
    try:
        item_id = int(request.query_params.get("item_id", ""))
        index = int(request.query_params.get("n", ""))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_request"}, status_code=400, headers=headers)
    if size is None or item_id < 1 or not 0 <= index < 1000:
        return JSONResponse({"error": "bad_request"}, status_code=400, headers=headers)
    if not inbound_quarantine.configured():
        return JSONResponse({"enabled": False}, status_code=409, headers=headers)
    try:
        image = await quarantine_preview.render(db_inst, patient_id, item_id, index, size)
    except quarantine_preview.PreviewRefusedError as refused:
        if refused.code == quarantine_preview.NOT_FOUND:
            return _patient_not_found()
        if refused.code == quarantine_preview.BUSY:
            headers["Retry-After"] = str(quarantine_preview.BUSY_RETRY_AFTER_S)
            return JSONResponse({"error": refused.code}, status_code=429, headers=headers)
        status = {
            quarantine_preview.UNSUPPORTED: 415,
            quarantine_preview.INTEGRITY: 409,
        }.get(refused.code, 422)
        return JSONResponse({"error": refused.code}, status_code=status, headers=headers)
    except Exception as exc:
        logger.error("API inbound-quarantine preview error (%s)", type(exc).__name__)
        return JSONResponse({"error": "internal error"}, status_code=500, headers=headers)
    headers["Content-Disposition"] = 'inline; filename="preview.jpg"'
    return Response(image, media_type="image/jpeg", headers=headers)


# ── /q/<token>: the e-mail buttons of the quarantine (#950, #834 exception) ──

_QUARANTINE_PAGE_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
_QUARANTINE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": _QUARANTINE_PAGE_CSP,
}
_QUARANTINE_FORM_MAX_BYTES = 4096


def _quarantine_page(inner: str) -> HTMLResponse:
    page = (
        '<!doctype html><html lang="sk"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        "<title>Oncofiles</title><style>"
        "body{font-family:system-ui,sans-serif;max-width:34rem;margin:2rem auto;"
        "padding:0 16px;color:#1d2a44;background:#fff;line-height:1.5}"
        "h1{font-size:1.3rem}.sender{font-family:monospace;word-break:break-all}"
        "button{font-size:1.1rem;padding:.8rem 1.6rem;border:0;border-radius:6px;"
        "background:#ff8a65;color:#1d2a44;font-weight:600;cursor:pointer}"
        ".warn{border-left:4px solid #1d2a44;padding-left:.6rem}"
        "</style></head><body>" + inner + "</body></html>"
    )
    return HTMLResponse(page, headers=dict(_QUARANTINE_HEADERS))


def _quarantine_neutral() -> HTMLResponse:
    """The ONE answer for a bad, used or expired link (and a wrong CSRF value)."""
    return _quarantine_page(
        "<h1>Odkaz už neplatí / This link is no longer valid</h1>"
        "<p>Prílohy, ktoré čakajú na schválenie, nájdete po prihlásení v Oncofiles.</p>"
        "<p>Attachments waiting for approval are in Oncofiles after you sign in.</p>"
        '<p><a href="/dashboard">oncofiles.com/dashboard</a></p>'
    )


@mcp.custom_route("/q/{token}", methods=["GET"])
async def quarantine_link_page(request: Request) -> HTMLResponse:
    """Confirm page of an e-mail button. A GET NEVER changes anything (link
    scanners prefetch): it shows the sender (escaped), counts, types and sizes
    and one confirm button that POSTs the token in the body."""
    import html as _html

    from oncofiles import inbound_quarantine

    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    token = request.path_params.get("token", "")
    try:
        view = await inbound_quarantine.link_view(db_inst, token)
    except Exception as exc:
        logger.warning("quarantine link page failed (%s)", type(exc).__name__)
        view = None
    if view is None:
        return _quarantine_neutral()
    esc = _html.escape
    files = ", ".join(f"{esc(t)} {esc(s)}" for t, s in view["files"])
    if view["action"] == "approve":
        head = "Schváliť prílohy? / Approve the attachments?"
        what = (
            "<p>Prílohy pôjdu do dokumentov pacienta a spracujú sa automaticky.</p>"
            "<p>The attachments go into the patient's documents and are processed "
            "automatically.</p>"
        )
        label = "Schváliť / Approve"
    else:
        head = "Odmietnuť prílohy? / Reject the attachments?"
        what = "<p>Prílohy sa nespracujú.</p><p>The attachments are not processed.</p>"
        label = "Odmietnuť / Reject"
    warn = ""
    if view["reason"] == "auth_failed":
        warn = (
            '<p class="warn">Odosielateľa sa nepodarilo overiť. / '
            "The sender could not be verified.</p>"
        )
    inner = (
        f"<h1>{head}</h1>"
        f'<p>Odosielateľ / Sender: <span class="sender">{esc(view["sender"])}</span></p>'
        f"<p>Príloh / Attachments: {int(view['count'])} ({files})</p>"
        f"{warn}{what}"
        '<form method="post" action="/q">'
        f'<input type="hidden" name="token" value="{esc(token)}">'
        f'<input type="hidden" name="csrf" value="{esc(view["csrf"])}">'
        f'<button type="submit">{label}</button></form>'
    )
    return _quarantine_page(inner)


@mcp.custom_route("/q", methods=["POST"])
async def quarantine_link_action(request: Request) -> HTMLResponse:
    """The decision of an e-mail button: token and CSRF value in the form
    BODY (never the URL). Single use; a bad, used or expired token, a wrong
    CSRF value or a cross-site request gets the one neutral answer."""
    from urllib.parse import parse_qs

    from oncofiles import inbound_quarantine

    site = request.headers.get("sec-fetch-site", "").lower()
    origin = request.headers.get("origin", "")
    if site == "cross-site" or (
        origin
        and origin not in ("https://oncofiles.com", f"{request.url.scheme}://{request.url.netloc}")
    ):
        logger.info("quarantine link: refused reason=cross_site")
        return _quarantine_neutral()
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > _QUARANTINE_FORM_MAX_BYTES:
            return _quarantine_neutral()
    try:
        form = parse_qs(raw.decode("ascii"), max_num_fields=4, strict_parsing=False)
    except (UnicodeDecodeError, ValueError):
        return _quarantine_neutral()
    token = (form.get("token") or [""])[0]
    csrf = (form.get("csrf") or [""])[0]
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        outcome = await inbound_quarantine.act_on_link(db_inst, token, csrf)
    except Exception as exc:
        logger.warning("quarantine link action failed (%s)", type(exc).__name__)
        outcome = "retry_later"
    logger.info("quarantine link: outcome=%s", outcome)
    if outcome == "approved":
        return _quarantine_page(
            "<h1>Schválené / Approved</h1>"
            "<p>Prílohy sú v dokumentoch pacienta.</p>"
            "<p>The attachments are in the patient's documents.</p>"
        )
    if outcome == "rejected":
        return _quarantine_page(
            "<h1>Odmietnuté / Rejected</h1>"
            "<p>Prílohy sa nespracujú.</p><p>The attachments are not processed.</p>"
        )
    if outcome == "retry_later":
        return _quarantine_page(
            "<h1>Teraz sa to nepodarilo / This did not work right now</h1>"
            "<p>Skúste to o chvíľu znova, alebo rozhodnite po prihlásení v Oncofiles.</p>"
            "<p>Try again in a while, or decide after signing in to Oncofiles.</p>"
        )
    return _quarantine_neutral()


@mcp.custom_route("/api/curated/confirm-review", methods=["POST"])
async def api_curated_confirm_review(request: Request) -> JSONResponse:
    """The caregiver confirms a filed curated document in review "as is" (#919).

    Body ``{"patient_id": …, "document_id": …}``. ``_dashboard_body_patient``
    (#867: unknown and foreign are the same 404), then the #921 write refusal
    (an admin who is not this patient's caregiver, and the operator bearer,
    get 403 ``read_only``), and the Origin check of the dashboard POSTs (auth
    is an Authorization header, never a cookie). The rest is
    ``tools.curated.confirm_review_for``: the #900 override line in the
    sidecar only, then a re-index of that document. The dashboard asks for a
    second, armed click first. No #852 gate: a caregiver acting on an existing
    patient is not onboarding.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    bad_origin = _cross_origin(request)
    if bad_origin is not None:
        return bad_origin
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    patient_id, acl_err = await _dashboard_body_patient(
        request, db_inst, str(body.get("patient_id") or "")
    )
    if acl_err is not None:
        return acl_err
    ro_err = await _dashboard_write_refusal(request, db_inst, patient_id)
    if ro_err is not None:
        return ro_err
    raw_id = body.get("document_id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int | str):
        return JSONResponse({"error": "bad_request"}, status_code=400)
    try:
        document_id = int(raw_id)
    except ValueError:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    if document_id <= 0:
        return JSONResponse({"error": "bad_request"}, status_code=400)
    from oncofiles.secrets_keys import caller_email_hash
    from oncofiles.tools import curated as curated_tools

    try:
        status, payload = await curated_tools.confirm_review_for(
            db_inst,
            patient_id,
            document_id,
            caller_hash=caller_email_hash(_get_dashboard_email(request) or ""),
        )
        return JSONResponse(payload, status_code=status)
    except Exception:
        logger.exception("API curated confirm-review error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/inbound-unmatched", methods=["GET"])
async def api_inbound_unmatched(request: Request) -> JSONResponse:
    """Admin zone: mail to patient addresses that was not filed (#950).

    In-memory, counts only: a keyed hash of the sender (never the address),
    the patient id prefix, mails, files, reason codes and times, plus totals,
    and the durable queue's counts per state / failure reason (#952).
    Go-live diagnostics: ``auth_refusals`` (counter per refusal code) and
    ``authserv_candidates`` (hostname-shaped authserv-ids only, counts, times);
    ``header_names`` / ``item_keys`` / ``payload_keys`` (field NAMES of the
    Brevo payload as ``{name, items}``, never values), ``auth_evidence``
    (presence-flag counts) and ``field_names_stats``.
    """
    err = _require_admin(request)
    if err:
        return err
    from oncofiles import inbound_email

    out = inbound_email.unmatched_overview()
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        # #952: durable queue counts (per state; failed per reason code, e.g.
        # download_expired), so a lost mail is visible after a restart too.
        out["jobs"] = await db_inst.inbound_job_counts()
    except Exception as exc:
        logger.warning("API inbound-unmatched: job counts failed (%s)", type(exc).__name__)
        out["jobs"] = None
    try:
        # #950 DKIM mode: the claimed bucket (mail waiting for its verification)
        out["claimed_jobs"] = await db_inst.inbound_claimed_counts()
    except Exception as exc:
        logger.warning("API inbound-unmatched: claimed counts failed (%s)", type(exc).__name__)
        out["claimed_jobs"] = None
    # #950 phase 2: why the trial reader last waited (codes and counts only)
    from oncofiles import holding_extract

    out["holding_reader"] = holding_extract.reader_status()
    # #950 phase 3: time-to-value funnel of the welcome@ trials (counts and
    # median seconds only; no ids, addresses, names or files)
    from oncofiles import trial_funnel

    try:
        out["trial_funnel"] = trial_funnel.funnel(await db_inst.trial_funnel_rows())
    except Exception as exc:
        logger.warning("API inbound-unmatched: trial funnel failed (%s)", type(exc).__name__)
        out["trial_funnel"] = None
    # #950 quarantine: items / files / bytes per state; pending bytes against
    # the quarantine's own budget, the retained bytes of rejected / expired
    # items and the last purge run (purged_items, purged_bytes). Counts only.
    from oncofiles import inbound_quarantine

    try:
        out["quarantine"] = await inbound_quarantine.admin_overview(db_inst)
    except Exception as exc:
        logger.warning("API inbound-unmatched: quarantine overview failed (%s)", type(exc).__name__)
        out["quarantine"] = None
    return JSONResponse(out)


@mcp.custom_route("/api/trial-status", methods=["GET"])
async def api_trial_status(request: Request) -> JSONResponse:
    """The welcome@ trial of the dashboard patient (#950 phase 2), counts only.

    Same auth and patient resolution as ``/api/activity``
    (``_get_dashboard_patient_id``; a caller without a patient gets the
    sentinel, which has no trial). ``{"trial": false}`` for every patient that
    did not start through welcome@. Otherwise ``trial_until`` (the 30-day end),
    ``days_left`` and the holding documents per reading state (``documents``,
    ``reading``, ``ready``, ``attention``) for the first-steps checklist, and
    ``drive_connected``. No name, file name or address.
    """
    err = _check_dashboard_auth(request)
    if err:
        return err
    try:
        db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await _get_dashboard_patient_id(request)
        trial = await db.get_trial(patient_id=patient_id)
        if trial is None:
            return JSONResponse({"trial": False})
        from oncofiles import holding_store

        counts = await db.holding_document_counts(patient_id=patient_id)
        ends = datetime.strptime(trial["trial_ends_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        days_left = max(0, -(-int((ends - datetime.now(UTC)).total_seconds()) // 86400))
        return JSONResponse(
            {
                "trial": True,
                "trial_until": trial["trial_ends_at"],
                "days_left": days_left,
                "documents": sum(counts.values()),
                "reading": counts.get("pending", 0) + counts.get("reading", 0),
                "ready": counts.get("done", 0),
                "attention": counts.get("failed", 0) + counts.get("excluded", 0),
                "drive_connected": not await holding_store.holding_mode(db, patient_id),
                # #950 phase 3: held files with a verified copy in Drive, and
                # whether every one has one (the trial is converted)
                "moved_to_drive": await db.holding_moved_count(patient_id=patient_id),
                "converted": bool(trial.get("converted_at")),
            }
        )
    except Exception:
        logger.exception("API trial-status error")
        return JSONResponse({"error": "internal error"}, status_code=500)


# Google's cap for an unverified OAuth client with sensitive/restricted scopes.
GOOGLE_UNVERIFIED_USER_CAP = 100
GOOGLE_OAUTH_BASIS = "distinct_link_account_hashes_plus_unaudited_token_rows"


@mcp.custom_route("/api/admin/oauth-users", methods=["GET"])
async def api_admin_oauth_users(request: Request) -> JSONResponse:
    """Admin zone: Google accounts that completed the Drive/Gmail/Calendar OAuth.

    The unverified OAuth client is capped by Google at 100 users who granted
    its restricted scope. Counted (``basis``): distinct keyed account hashes of
    every stored Google link (append-only audit, so ever-granted) plus stored
    tokens of patients linked before that audit existed (one each, account
    unknown), a conservative upper bound. Dashboard sign-in (Google Identity
    Services, ``openid``/``email`` only) and MCP OAuth (our own server) grant no
    sensitive scope and are not counted. Counts only, never an e-mail.
    """
    err = _require_admin(request)
    if err:
        return err
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        counts = await db_inst.count_google_oauth_accounts()
    except Exception as exc:
        logger.warning("API admin oauth-users: count failed (%s)", type(exc).__name__)
        return JSONResponse({"error": "internal error"}, status_code=500)
    return JSONResponse(
        {
            "google_oauth_users": counts["accounts"] + counts["unaudited_links"],
            "cap": GOOGLE_UNVERIFIED_USER_CAP,
            "basis": GOOGLE_OAUTH_BASIS,
            "accounts": counts["accounts"],
            "unaudited_links": counts["unaudited_links"],
        }
    )


async def _read_capped_body(request: Request, limit: int) -> bytearray | None:
    """The request body in ONE buffer, or None as soon as it exceeds ``limit``."""
    buf = bytearray()
    async for chunk in request.stream():
        if len(buf) + len(chunk) > limit:
            return None
        buf.extend(chunk)
    return buf


async def _read_capped_body_in_time(request: Request, limit: int) -> bytearray | None | bool:
    """``_read_capped_body`` under ``inbound_email.READ_DEADLINE_S``; False on timeout."""
    from oncofiles import inbound_email

    try:
        async with asyncio.timeout(inbound_email.READ_DEADLINE_S):
            return await _read_capped_body(request, limit)
    except TimeoutError:
        return False


def _inbound_read_timeout() -> JSONResponse:
    return JSONResponse({"error": "request timeout"}, status_code=408)


def _declared_too_large(request: Request, limit: int) -> bool:
    try:
        return int(request.headers.get("content-length") or 0) > limit
    except ValueError:
        return False


def _inbound_busy() -> JSONResponse:
    return JSONResponse({"error": "busy"}, status_code=503, headers={"Retry-After": "30"})


@mcp.custom_route("/inbound/email", methods=["POST"])
async def inbound_email_webhook(request: Request) -> Response:
    """Inbound mail webhook, provider-agnostic raw MIME (#950 phase 1).

    Not a dashboard route: it is authenticated by the HMAC signature of the
    provider (contract in ``oncofiles.inbound_email``), keyed with its own
    secret ``INBOUND_MIME_HMAC_SECRET``. Order (infra review): FIRST the
    adapter must be enabled, else the app's ordinary 404 before a single body
    byte is read; then a read slot of its own (``MIME_READ_SLOTS``, two at a
    time, read deadline 408; full = 503, nothing read); then the capped read
    (413); then the signature (it needs the body), a missing or wrong one is
    the same 404. After a valid signature always 202, whatever happened to
    the mail. Nothing from the request content is logged.
    """
    from starlette.exceptions import HTTPException

    from oncofiles import inbound_email

    secret = inbound_email.mime_secret()
    if not secret:
        raise HTTPException(status_code=404)
    if inbound_email.MIME_READ_SLOTS.locked():
        return _inbound_busy()
    async with inbound_email.MIME_READ_SLOTS:
        if _declared_too_large(request, inbound_email.MAX_BODY_BYTES):
            return JSONResponse({"error": "request body too large"}, status_code=413)
        raw = await _read_capped_body_in_time(request, inbound_email.MAX_BODY_BYTES)
        if raw is False:
            return _inbound_read_timeout()
        if raw is None:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        original_to = request.headers.get(inbound_email.RECIPIENT_HEADER, "")
        auth = request.headers.get(inbound_email.AUTH_HEADER, "")
        signature = request.headers.get(inbound_email.SIGNATURE_HEADER, "")
        if not inbound_email.signature_ok(secret, signature, original_to, auth, raw):
            logger.info("inbound: refused request (signature)")
            raise HTTPException(status_code=404)
        try:
            message = inbound_email.parse_raw_mime(raw, original_to=original_to, auth=auth)
        except inbound_email.UnparseableMessageError:
            logger.info("inbound: dropped reason=unparseable")
            return JSONResponse({"status": "accepted"}, status_code=202)
        finally:
            raw = None
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    try:
        await inbound_email.process_message(db_inst, message)
    except Exception as exc:
        logger.error("inbound: processing failed (%s)", type(exc).__name__)
        return JSONResponse({"error": "internal error"}, status_code=500)
    return JSONResponse({"status": "accepted"}, status_code=202)


@mcp.custom_route("/inbound/brevo", methods=["POST"])
async def inbound_brevo_webhook(request: Request) -> Response:
    """Brevo inbound-parsing webhook (#950, D3 = Brevo).

    Brevo does not sign its calls: the webhook is created with the custom
    header ``X-Inbound-Token: <INBOUND_BREVO_TOKEN>`` (its own secret, never
    the raw-MIME HMAC key). Order (infra review): FIRST the adapter must be
    enabled, else the app's ordinary 404; then the token with
    ``hmac.compare_digest`` (missing or wrong = the same 404); both before a
    single body byte is read. Without the mandatory ``INBOUND_JOBS_SECRET``
    (#952) the answer is then 503 + Retry-After, nothing read or queued, so
    Brevo retries. Then a read slot of its own
    (``BREVO_READ_SLOTS``; full = 503; read deadline 408), the capped read
    (413) and the strict JSON parse (duplicate keys refused).
    Then every routable mail is written to the durable queue ``inbound_jobs``
    (#952, ``inbound_queue.enqueue``: encrypted minimal payload, one
    transaction) BEFORE the 202; if that write fails the answer is 500, so
    Brevo retries (an already queued mail is not queued twice); over a queue
    cap (``MAX_OPEN_JOBS_PER_TOKEN`` / ``MAX_OPEN_JOBS_TOTAL``) it is 503 +
    Retry-After with nothing written. Checks,
    attachment downloads, filing and the receipt run in the queue worker
    (``inbound_queue.kick`` now, plus the startup and periodic runs),
    serialized under ``PROCESS_LOCK``. Nothing from the request content is
    logged.
    """
    from starlette.exceptions import HTTPException

    from oncofiles import inbound_email

    secret = inbound_email.brevo_token()
    if not secret:
        raise HTTPException(status_code=404)
    token = request.headers.get(inbound_email.BREVO_TOKEN_HEADER, "")
    if not inbound_email.brevo_token_ok(secret, token):
        logger.info("inbound: refused request (token)")
        raise HTTPException(status_code=404)
    from oncofiles import inbound_queue

    if not inbound_queue.configured():
        # #952: INBOUND_JOBS_SECRET is mandatory; without it nothing can be
        # queued safely, so Brevo is asked to retry later (fail closed).
        inbound_queue._warn_unconfigured_once()
        return _inbound_busy()
    if inbound_email.BREVO_READ_SLOTS.locked():
        return _inbound_busy()
    async with inbound_email.BREVO_READ_SLOTS:
        if _declared_too_large(request, inbound_email.BREVO_MAX_BODY_BYTES):
            return JSONResponse({"error": "request body too large"}, status_code=413)
        raw = await _read_capped_body_in_time(request, inbound_email.BREVO_MAX_BODY_BYTES)
        if raw is False:
            return _inbound_read_timeout()
        if raw is None:
            return JSONResponse({"error": "request body too large"}, status_code=413)
        try:
            messages = inbound_email.parse_brevo_payload(inbound_email.parse_json_strict(raw))
        except inbound_email.UnparseableMessageError:
            logger.info("inbound: dropped reason=unparseable")
            return JSONResponse({"status": "accepted"}, status_code=202)
        finally:
            raw = None
    db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    if messages:
        try:
            await inbound_queue.enqueue(db_inst, messages)
        except inbound_queue.InboundQueueFullError:
            # #952 caps (nothing written), or #950 claimed address rows over
            # their bounds (the rest of the batch is then committed and the
            # periodic worker picks it up): Brevo retries after Retry-After
            return _inbound_busy()
        except Exception as exc:
            logger.error("inbound: enqueue failed (%s)", type(exc).__name__)
            return JSONResponse({"error": "internal error"}, status_code=500)
        inbound_queue.kick(db_inst)
    return JSONResponse({"status": "accepted"}, status_code=202)


def _require_admin(request: Request) -> JSONResponse | None:
    """Admin-only operations (#573): static bearer or an admin dashboard session.

    Fails closed — any session that is not in DASHBOARD_ADMIN_EMAILS gets 403.
    """
    if _check_bearer(request) is None:
        return None
    email = _get_dashboard_email(request)
    if email is None:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not _is_admin_email(email):
        return JSONResponse({"error": "admin only"}, status_code=403)
    return None


async def _admin_patient_id(db_inst: Database, raw: str) -> str | None:
    raw = (raw or "").strip().lower()
    if not raw:
        return None
    return await db_inst.resolve_patient_id(raw)


def _run_summary(run: dict | None) -> dict | None:
    if not run:
        return None
    report = run.get("report") or {}
    docs = report.get("documents") or []
    return {
        "id": run.get("id"),
        "dry_run": bool(run.get("dry_run")),
        "trigger": run.get("trigger"),
        "status": run.get("status"),
        "message": run.get("message"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "documents": len(docs),
        "filed": sum(1 for d in docs if d.get("result") == "filed"),
        "review": sum(1 for d in docs if d.get("result") == "review_needed"),
        "not_processed": len(report.get("not_processed") or []),
    }


@mcp.custom_route("/api/admin/curated/overview", methods=["GET"])
async def api_admin_curated_overview(request: Request) -> JSONResponse:
    """Admin operations panel: one card per patient (#573)."""
    err = _require_admin(request)
    if err:
        return err
    try:
        from oncofiles.tools.curated import (
            _runs,
            continuous_settings,
            heartbeat_detail,
            nightly_settings,
            preview_gate,
        )

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        from oncofiles.patient_context import load_from_db as _load_ctx

        enabled, hour, max_runs = nightly_settings()
        cards = []
        for p in await db_inst.list_patients(active_only=True):
            pid = p.patient_id
            token = await db_inst.get_oauth_token(patient_id=pid)
            running = _runs.get(pid)
            cards.append(
                {
                    "patient_id": pid,
                    "slug": p.slug,
                    "name": p.display_name,
                    # the type lives in patient_context, not on Patient (#650 test)
                    "patient_type": (await _load_ctx(db_inst.db, pid)).get("patient_type")
                    or "oncology",
                    "folder_name": token.gdrive_folder_name if token else None,
                    "drive_connected": bool(token and token.gdrive_folder_id),
                    "nightly_enabled": await db_inst.get_curated_nightly(patient_id=pid),
                    "running": {k: v for k, v in running.items() if k != "state"}
                    if running
                    else None,
                    "last_run": _run_summary(await db_inst.get_last_curated_run(patient_id=pid)),
                    "real_run_blocked": await preview_gate(db_inst, pid),
                }
            )
        return JSONResponse(
            {
                "nightly": {"enabled": enabled, "hour": hour, "max_runs": max_runs},
                "continuous": dict(
                    zip(
                        ("enabled", "poll_s", "settle_s", "chunk_docs"),
                        continuous_settings(),
                        strict=True,
                    )
                ),
                "heartbeat": heartbeat_detail(),
                "patients": cards,
            }
        )
    except Exception:
        logger.exception("API admin curated overview error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/admin/curated/run", methods=["POST"])
async def api_admin_curated_run(request: Request) -> JSONResponse:
    """Start a curated run (dry by default). A real run needs a clean preview < 24 h."""
    err = _require_admin(request)
    if err:
        return err
    try:
        from oncofiles.tools.curated import start_run

        body = await request.json()
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        pid = await _admin_patient_id(db_inst, str(body.get("patient_id", "")))
        if not pid:
            return JSONResponse({"error": "unknown patient"}, status_code=404)
        dry_run = body.get("dry_run", True) is not False  # anything but explicit false = dry
        result = await start_run(db_inst, pid, dry_run=dry_run, trigger="dashboard")
        return JSONResponse(result, status_code=409 if "error" in result else 202)
    except Exception:
        logger.exception("API admin curated run error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/admin/curated/report", methods=["GET"])
async def api_admin_curated_report(request: Request) -> JSONResponse:
    """Full report of a run (default: the latest) plus the run history."""
    err = _require_admin(request)
    if err:
        return err
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        pid = await _admin_patient_id(db_inst, request.query_params.get("patient_id", ""))
        if not pid:
            return JSONResponse({"error": "unknown patient"}, status_code=404)
        raw_id = request.query_params.get("run_id", "")
        run = (
            await db_inst.get_curated_run(int(raw_id), patient_id=pid)
            if raw_id.isdigit()
            else await db_inst.get_last_curated_run(patient_id=pid)
        )
        history = await db_inst.list_curated_runs(patient_id=pid, limit=20)
        return JSONResponse({"run": run, "history": history})
    except Exception:
        logger.exception("API admin curated report error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/admin/curated/nightly", methods=["POST"])
async def api_admin_curated_nightly(request: Request) -> JSONResponse:
    """Turn automatic processing on/off for one patient (#665).

    The flag (``curated_settings.nightly_enabled``; path and field names kept
    for compatibility) enrols the patient in the startup index, continuous
    processing and the nightly sweep, not only the nightly run.
    """
    err = _require_admin(request)
    if err:
        return err
    try:
        body = await request.json()
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        pid = await _admin_patient_id(db_inst, str(body.get("patient_id", "")))
        if not pid:
            return JSONResponse({"error": "unknown patient"}, status_code=404)
        enabled = body.get("enabled") is True
        who = _get_dashboard_email(request) or "bearer"
        await db_inst.set_curated_nightly(patient_id=pid, enabled=enabled, updated_by=who)
        return JSONResponse({"patient_id": pid, "nightly_enabled": enabled})
    except Exception:
        logger.exception("API admin curated nightly error")
        return JSONResponse({"error": "internal error"}, status_code=500)


# ── #573 admin: verify-only re-read and re-OCR from the dashboard ─────────

_ADMIN_MAX_DOCS = 10


def _cross_origin(request: Request) -> JSONResponse | None:
    """Defence in depth for admin POSTs: auth is an Authorization header (never
    a cookie), so a cross-site form cannot carry it; still refuse a request
    whose Origin names another host."""
    origin = request.headers.get("origin") or ""
    if not origin:
        return None
    from urllib.parse import urlsplit

    host = (request.headers.get("host") or "").lower()
    if urlsplit(origin).netloc.lower() != host:
        return JSONResponse({"error": "bad origin"}, status_code=403)
    return None


async def _admin_job_target(request: Request, db_inst: Database, body: dict):
    """Resolve patient and validate ``document_ids`` for a re-read/re-OCR POST.

    Returns ``(pid, ids, dry_run)`` or a JSONResponse error. Every id must be
    a curated document of that patient; at most 10; ``dry_run`` defaults to
    true (anything but an explicit ``false`` is a dry run). A real run needs
    an explicit selection: only dry runs may auto-pick documents.
    """
    raw = body.get("patient_slug") or body.get("patient_id") or ""
    pid = await _admin_patient_id(db_inst, str(raw))
    if not pid:
        return JSONResponse({"error": "unknown patient"}, status_code=404)
    raw_ids = body.get("document_ids")
    if raw_ids is None:
        raw_ids = []
    if not isinstance(raw_ids, list) or any(
        isinstance(i, bool) or not isinstance(i, int) for i in raw_ids
    ):
        return JSONResponse({"error": "document_ids must be a list of integers"}, status_code=400)
    ids = list(dict.fromkeys(raw_ids))
    if len(ids) > _ADMIN_MAX_DOCS:
        return JSONResponse(
            {"error": f"at most {_ADMIN_MAX_DOCS} documents per job"}, status_code=400
        )
    if ids:
        own = {r["id"] for r in await db_inst.curated_reocr_candidates(patient_id=pid)}
        foreign = [i for i in ids if i not in own]
        if foreign:
            return JSONResponse(
                {"error": "document_ids not found for this patient", "document_ids": foreign},
                status_code=400,
            )
    dry_run = body.get("dry_run", True) is not False
    if not dry_run and not ids:  # only dry runs may auto-pick documents
        return JSONResponse(
            {"error": "a real run requires an explicit selection of document_ids"},
            status_code=400,
        )
    return pid, ids, dry_run


def _admin_job_response(result: dict) -> JSONResponse:
    from oncofiles.tools.curated import admin_job_view

    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=409)
    if result.get("state") != "running":  # "nothing to do"
        return JSONResponse({k: v for k, v in result.items() if k in ("status", "candidates")})
    return JSONResponse(admin_job_view(result), status_code=202)


@mcp.custom_route("/api/admin/reread", methods=["POST"])
async def api_admin_reread(request: Request) -> JSONResponse:
    """Start a verify-only re-read (#678) for ≤ 10 documents. Dry run by default."""
    err = _require_admin(request) or _cross_origin(request)
    if err:
        return err
    try:
        from oncofiles.tools.curated import start_reread

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid body"}, status_code=400)
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        target = await _admin_job_target(request, db_inst, body)
        if isinstance(target, JSONResponse):
            return target
        pid, ids, dry_run = target
        if not ids:
            return JSONResponse({"error": "document_ids is required"}, status_code=400)
        logger.info(
            "admin re-read: by=%s patient=%s docs=%d dry_run=%s",
            (_get_dashboard_email(request) or "bearer")[:40],
            pid[:8],
            len(ids),
            dry_run,
        )
        actor = _get_dashboard_email(request) or "bearer"
        return _admin_job_response(
            await start_reread(db_inst, pid, ids, dry_run=dry_run, actor=actor)
        )
    except Exception:
        logger.exception("API admin reread error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/admin/reocr", methods=["POST"])
async def api_admin_reocr(request: Request) -> JSONResponse:
    """Start a re-OCR (#660). Dry run by default; a dry run without ids picks
    up to 3 documents with unverified facts, as curated_reocr does. A real
    run requires explicit ``document_ids`` (400 otherwise)."""
    err = _require_admin(request) or _cross_origin(request)
    if err:
        return err
    try:
        from oncofiles.tools.curated import start_reocr

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "invalid body"}, status_code=400)
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        target = await _admin_job_target(request, db_inst, body)
        if isinstance(target, JSONResponse):
            return target
        pid, ids, dry_run = target
        logger.info(
            "admin re-OCR: by=%s patient=%s docs=%d dry_run=%s",
            (_get_dashboard_email(request) or "bearer")[:40],
            pid[:8],
            len(ids),
            dry_run,
        )
        result = await start_reocr(
            db_inst,
            pid,
            dry_run=dry_run,
            document_ids=ids or None,
            limit=len(ids) or 3,
            actor=_get_dashboard_email(request) or "bearer",
        )
        return _admin_job_response(result)
    except Exception:
        logger.exception("API admin reocr error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/admin/reocr-status", methods=["GET"])
async def api_admin_reocr_status(request: Request) -> JSONResponse:
    """The patient's current or last re-read / re-OCR job: states and counts only."""
    err = _require_admin(request)
    if err:
        return err
    try:
        from oncofiles.tools.curated import _dry_runs, admin_job_view, reocr_status

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        raw = request.query_params.get("patient_slug") or request.query_params.get("patient_id", "")
        pid = await _admin_patient_id(db_inst, raw)
        if not pid:
            return JSONResponse({"error": "unknown patient"}, status_code=404)
        view = admin_job_view(await reocr_status(db_inst, pid))
        view["dry_runs_since_start"] = dict(_dry_runs)  # counted, not logged (#573)
        return JSONResponse(view)
    except Exception:
        logger.exception("API admin reocr-status error")
        return JSONResponse({"error": "internal error"}, status_code=500)


@mcp.custom_route("/api/admin/reread-candidates", methods=["GET"])
async def api_admin_reread_candidates(request: Request) -> JSONResponse:
    """Documents with Cowork-era unverified facts: ids, category, counts."""
    err = _require_admin(request)
    if err:
        return err
    try:
        from oncofiles.tools.curated import reread_candidates

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        raw = request.query_params.get("patient_slug") or request.query_params.get("patient_id", "")
        pid = await _admin_patient_id(db_inst, raw)
        if not pid:
            return JSONResponse({"error": "unknown patient"}, status_code=404)
        docs = await reread_candidates(db_inst, pid)
        return JSONResponse({"patient_id": pid, "max_per_job": _ADMIN_MAX_DOCS, "documents": docs})
    except Exception:
        logger.exception("API admin reread-candidates error")
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
        raw_patient_id = body.get("patient_id", "").strip().lower()
        if not raw_patient_id:
            return JSONResponse({"error": "patient_id required"}, status_code=400)

        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        # Token rows key on the patient UUID: resolve a slug first (#824), so a
        # slug can never reach create_patient_token (an unknown value → 404).
        patient_id, acl_err = await _dashboard_body_patient(request, db_inst, raw_patient_id)

        # #489 Part B: ACL gate — share-link creation mints cross-session access.
        # Body-supplied patient_id must belong to caller (or caller is admin).
        if acl_err is not None:
            return acl_err

        patient = await db_inst.get_patient(patient_id)
        if not patient:
            return JSONResponse(
                {"error": f"Patient '{patient_id}' not found"},
                status_code=404,
            )

        # No pre-existing token is required (#824: patient creation no longer
        # mints one). Asking for a share code is the explicit user action.
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

    rate_err = _check_rate_limit("oauth-authorize")
    if rate_err:
        return rate_err

    if not GOOGLE_OAUTH_CLIENT_ID:
        return JSONResponse({"error": "OAuth not configured"}, status_code=500)

    service = request.path_params.get("service", "drive")
    if service in ("gmail", "calendar") and not gmail_calendar_enabled():
        from oncofiles.config import GMAIL_CALENDAR_DISABLED_MESSAGE

        return JSONResponse({"error": GMAIL_CALENDAR_DISABLED_MESSAGE}, status_code=403)
    raw_pid = request.query_params.get("patient_id", "").strip().lower()
    if not raw_pid:
        return JSONResponse({"error": "patient_id required"}, status_code=400)
    # #591: the callback stores Google tokens for this patient, so only the
    # operator bearer, an admin or the patient's caregiver may start the link.
    # The dashboard opens this URL in a new tab, so its session is the cookie.
    bearer_ok = _check_bearer(request) is None
    email = None
    principal: dict = {"k": "o"}
    if not bearer_ok:
        auth_header = request.headers.get("authorization", "")
        session_token = (
            auth_header.removeprefix("Bearer session:").strip()
            if auth_header.startswith("Bearer session:")
            else _session_cookie(request)
        )
        email = _verify_session_token(session_token) if session_token else None
        parsed_session = _parse_session_token(session_token) if email else None
        if email is None or parsed_session is None or not parsed_session[2]:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # The state is bound to this session; /oauth/callback checks its cookie.
        principal = {"k": "s", "tid": parsed_session[2]}
    if service not in {"drive", "gmail", "calendar", "all"}:
        return JSONResponse({"error": "unknown service"}, status_code=400)
    # Resolve slug → UUID (e.g. "jana-testova" → UUID); an unknown value is refused
    # here (#867), never passed on.
    try:
        db_inst: Database = request.app.state.fastmcp_server._lifespan_result["db"]
        patient_id = await _resolve_body_patient_id(db_inst, raw_pid)
        patient = await db_inst.get_patient(patient_id) if patient_id else None
    except Exception:
        logger.error("OAuth connect: patient resolution failed for '%s'", raw_pid, exc_info=True)
        return JSONResponse({"error": "internal error"}, status_code=500)
    # #867/#590: unknown and not-your-patient give the same answer; only the
    # log line says which.
    if patient is None:
        logger.warning("OAuth connect: unknown patient caller=%s", _acl_caller_log_id(email))
        if not bearer_ok:
            await dashboard_audit.record(
                db_inst,
                "acl_refused",
                email=email,
                is_admin=_is_admin_email(email),
                patient_id=None,
                route=dashboard_audit.route_name(request),
            )
        return _patient_not_found()
    if not (
        bearer_ok
        or _is_admin_email(email)
        or _email_matches_caregiver(email, patient.caregiver_email)
    ):
        logger.warning("OAuth connect: not caregiver caller=%s", _acl_caller_log_id(email))
        await dashboard_audit.record(
            db_inst,
            "acl_refused",
            email=email,
            is_admin=False,
            patient_id=patient.patient_id,
            route=dashboard_audit.route_name(request),
        )
        return _patient_not_found()
    # #852 onboarding gate: a FIRST Google link for this patient needs a caller
    # the gate admits. A patient that already has a link may re-consent.
    from oncofiles import onboarding_gate

    if not onboarding_gate.onboarding_allowed(
        email, is_admin=bearer_ok or _is_admin_email(email)
    ) and not await onboarding_gate.has_google_link(db_inst, patient_id):
        return _onboarding_refusal_response(
            onboarding_gate.ACTION_GOOGLE_CONNECT, email, onboarding_gate.ONBOARDING_CLOSED
        )
    scope_map = {
        "drive": SCOPES,
        "gmail": GMAIL_SCOPES,
        "calendar": CALENDAR_SCOPES,
        "all": ALL_SCOPES,
    }
    scopes = scope_map.get(service, SCOPES)

    from oncofiles.oauth import _make_state_token

    state = _make_state_token(patient_id=patient_id, service=service, principal=principal)
    auth_url = get_auth_url_for_scopes(scopes, state=state)
    return RedirectResponse(auth_url)


@mcp.custom_route("/oauth/callback", methods=["GET"])
async def oauth_callback(request: Request) -> JSONResponse:
    """Handle Google OAuth 2.0 redirect callback.

    #591: the state (v2) names the patient, the service and the initiating
    principal. A missing / expired / replayed / tampered state, or a session
    state arriving without that session's cookie, is refused before any write.
    ``store_google_link`` then refuses a Google account that is neither the
    initiator's nor the caregiver's (login CSRF; an operator state completes
    only with the caregiver's account), refuses to replace a different
    account's token unless the initiator is the operator, an admin or the
    patient's caregiver, and logs every link / re-link / refusal to
    activity_log (hashes only).
    """
    from oncofiles.google_link import (
        allowed_accounts,
        log_link_refusal,
        mcp_principal_privileged,
        store_google_link,
    )
    from oncofiles.oauth import exchange_code, verify_link_state

    rate_err = _check_rate_limit("oauth-callback")
    if rate_err:
        return rate_err

    # Validate CSRF state parameter and extract patient_id
    payload = verify_link_state(request.query_params.get("state", ""))
    if payload is None:
        return JSONResponse({"error": "Invalid or expired state parameter."}, status_code=400)
    patient_id = payload["pid"]
    principal = payload["pr"]

    # Validate that the patient still exists (#345)
    db: Database = request.app.state.fastmcp_server._lifespan_result["db"]
    patient = await db.get_patient(patient_id)
    if not patient:
        logger.warning("OAuth callback for deleted patient: %s", patient_id)
        return JSONResponse(
            {"error": "Patient no longer exists. Please create a new patient from the dashboard."},
            status_code=404,
        )

    # Bind to the initiating principal.
    kind = principal.get("k")
    session_email = None
    if kind == "o":
        privileged = True
    elif kind == "s":
        session_token = _session_cookie(request)
        session_email = _verify_session_token(session_token) if session_token else None
        parsed_session = _parse_session_token(session_token) if session_email else None
        if parsed_session is None or parsed_session[2] != principal.get("tid"):
            await log_link_refusal(
                db,
                patient_id,
                service=payload["svc"],
                principal_kind=kind,
                reason="started in another session",
                source="oauth_callback",
            )
            return JSONResponse(
                {"error": "This link was started in another session."}, status_code=403
            )
        privileged = _is_admin_email(session_email) or _email_matches_caregiver(
            session_email, patient.caregiver_email
        )
    elif kind == "m":
        privileged = mcp_principal_privileged(principal, patient.caregiver_email)
    else:
        return JSONResponse({"error": "Invalid or expired state parameter."}, status_code=400)

    # #852 onboarding gate, re-checked here so a state minted before the gate
    # closed (or by a caller it no longer admits) cannot complete a FIRST link.
    # The operator ("o") is always admitted; an existing link may re-consent.
    from oncofiles import onboarding_gate

    if kind == "s":
        gate_ok = onboarding_gate.onboarding_allowed(
            session_email, is_admin=_is_admin_email(session_email)
        )
    elif kind == "m":
        gate_ok = onboarding_gate.onboarding_allowed_for_hash(
            principal.get("eh"), is_admin=bool(principal.get("adm"))
        )
    else:
        gate_ok = True
    if not gate_ok and not await onboarding_gate.has_google_link(db, patient_id):
        return _onboarding_refusal_response(
            onboarding_gate.ACTION_GOOGLE_CONNECT, session_email, onboarding_gate.ONBOARDING_CLOSED
        )

    code = request.query_params.get("code")
    if not code:
        return JSONResponse({"error": "Missing authorization code"}, status_code=400)

    try:
        tokens = exchange_code(code)
    except Exception:
        logger.exception("OAuth token exchange failed")
        return JSONResponse({"error": "Token exchange failed. Please try again."}, status_code=500)

    stored, reason = await store_google_link(
        db,
        patient_id=patient_id,
        tokens=tokens,
        service=payload["svc"],
        principal_kind=kind,
        privileged=privileged,
        allowed_account_hashes=allowed_accounts(principal, patient.caregiver_email, session_email),
        source="oauth_callback",
    )
    if not stored:
        return JSONResponse({"error": f"Google account not linked: {reason}."}, status_code=403)

    # Clear any "needs_reauth" flags for this patient now that fresh tokens landed (#415)
    for key in [k for k in _invalid_grant_tokens if k[0] == patient_id]:
        _invalid_grant_tokens.discard(key)

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


# ── Register tools ───────────────────────────────────────────────────────────
#
# No MCP resources are registered (#717). The five legacy `files://` resources
# were removed: they took no patient argument, so four were dead (TypeError on
# the required `patient_id`, #551) and `files://activity-timeline` returned every
# patient's activity log to any caller. The facts tools (`patient_overview`,
# `timeline`, `fact_series`, ...) replace them and resolve the patient through
# `_resolve_patient_id`. Locked by tests/test_activity_log_scoping.py.

from oncofiles.tools import (  # noqa: E402
    activity,
    agent_state,
    analysis,
    clinical,
    clinical_facts,
    clinical_records,
    conversations,
    curated,
    db_query,
    documents,
    enhance_tools,
    export,
    fact_review,
    fhir,
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
curated.register(mcp)
enhance_tools.register(mcp)
clinical.register(mcp)
clinical_records.register(mcp)
clinical_facts.register(mcp)
fact_review.register(mcp)
fhir.register(mcp)
lab_trends.register(mcp)
export.register(mcp)
naming.register(mcp)
patient.register(mcp)
hygiene.register(mcp)
db_query.register(mcp)
prompt_log.register(mcp)
integrations.register(mcp)

# #608: readOnlyHint / destructiveHint from the declarative classification.
from oncofiles.tool_annotations import ToolAnnotationTransform  # noqa: E402

mcp.add_transform(ToolAnnotationTransform())

# #887: /mcp serves only user tools; operator tools only on /ops/mcp.
from oncofiles.tool_surface import ToolSurfaceTransform  # noqa: E402

mcp.add_transform(ToolSurfaceTransform())

# ── Backward-compatible re-exports for tests ─────────────────────────────────
from oncofiles.tools._helpers import (  # noqa: E402, F401
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

# #898: the one validator for the `next` URL that /authorize hands the sign-in
# page (dashboard.js `safeAuthorizeNext` mirrors it).
AUTHORIZE_NEXT_MAX_LEN = 4096


def _safe_authorize_next(value: object) -> str | None:
    """Return ``value`` when it is a same-origin ``/authorize`` URL, else None.

    Accepted: exactly ``/authorize`` or ``/authorize?<query>``, at most
    ``AUTHORIZE_NEXT_MAX_LEN`` characters, printable ASCII only (no backslash,
    whitespace, control characters or fragment). Anything else (another
    path, an absolute or protocol-relative URL) is refused, so the sign-in
    page can never become an open redirect.
    """
    if not isinstance(value, str) or not value or len(value) > AUTHORIZE_NEXT_MAX_LEN:
        return None
    if value != "/authorize" and not value.startswith("/authorize?"):
        return None
    if any(c in "\\#" or ord(c) <= 0x20 or ord(c) >= 0x7F for c in value):
        return None
    return value


# #898 security review: parameters that must appear at most once on
# /authorize, so our parse and FastMCP's parse can never disagree.
_AUTHORIZE_SINGLE_PARAMS = (
    "client_id",
    "code_challenge",
    "code_challenge_method",
    "redirect_uri",
    "response_type",
    "state",
    "scope",
    "resource",
)
_AUTHORIZE_REQUIRED_PARAMS = ("client_id", "code_challenge", "redirect_uri", "response_type")
CONSENT_PATH = "/authorize/consent"
CONSENT_NONCE_TTL_S = 300
CONSENT_BODY_MAX = 8192
# nonce id → (expiry unix s, session tid), in insertion order. Every nonce gets
# the same TTL, so insertion order is (almost) expiry order: pruning pops
# expired ids from the front and stops at the first live one (no full scan).
# Caps never evict a live id (that would make a consumed nonce replayable);
# they refuse new consents instead:
# - per session tid: CONSENT_NONCE_PER_TID live consumed nonces, so one heavy
#   session only blocks itself (``consent_busy`` for that tid);
# - global: CONSENT_NONCE_CAP, a memory ceiling far above normal use.
_consumed_consent_nonces: OrderedDict[str, tuple[int, str]] = OrderedDict()
_consent_nonces_per_tid: dict[str, int] = {}
CONSENT_NONCE_PER_TID = 20
CONSENT_NONCE_CAP = 50_000


def _prune_consent_nonces(now: int) -> None:
    while _consumed_consent_nonces:
        _, (exp, tid) = next(iter(_consumed_consent_nonces.items()))
        if exp >= now:
            break
        _consumed_consent_nonces.popitem(last=False)
        left = _consent_nonces_per_tid.get(tid, 1) - 1
        if left <= 0:
            _consent_nonces_per_tid.pop(tid, None)
        else:
            _consent_nonces_per_tid[tid] = left


def _authorize_params(qs: str) -> tuple[dict[str, str] | None, str]:
    """Parse an /authorize query: ``(params, "")`` or ``(None, error_code)``."""
    from urllib.parse import parse_qsl

    if _safe_authorize_next("/authorize" + (f"?{qs}" if qs else "")) is None:
        return None, "unsafe_query"
    pairs = parse_qsl(qs, keep_blank_values=True)
    seen: dict[str, str] = {}
    for key, value in pairs:
        if key in _AUTHORIZE_SINGLE_PARAMS and key in seen:
            return None, "duplicate_param"
        seen.setdefault(key, value)
    if any(not seen.get(k) for k in _AUTHORIZE_REQUIRED_PARAMS):
        return None, "missing_param"
    return seen, ""


def _session_from_scope(scope) -> tuple[str, str] | None:
    """``(account, tid)`` of the request's valid dashboard session, or None."""
    from urllib.parse import unquote as _urlunquote

    for name, value in scope.get("headers", []):
        if name.decode("latin-1").lower() != "cookie":
            continue
        cookie_header = value.decode("latin-1", errors="replace")
        for pair in cookie_header.split(";"):
            if "=" not in pair:
                continue
            cname, _, cvalue = pair.strip().partition("=")
            if cname != "oncofiles_session":
                continue
            # URL-decode: /dashboard/verify URL-encodes the token so
            # SimpleCookie never wraps it in double quotes ('@' is outside
            # SimpleCookie's legal-value char set).
            token = _urlunquote(cvalue.strip())
            found = _verify_session_token(token, log=False)
            if found:
                parsed = _parse_session_token(token)
                return found, (parsed[2] if parsed else "")
    return None


def _consent_sig(parts: list[str]) -> str:
    from oncofiles.secrets_keys import mcp_consent_key

    key = mcp_consent_key(MCP_BEARER_TOKEN)
    return hmac.new(key, "\x1f".join(parts).encode("utf-8"), hashlib.sha256).hexdigest()


def _q_digest(q: str) -> str:
    """SHA-256 of the full forwarded query, so state/scope/resource are signed too."""
    return hashlib.sha256(q.encode("utf-8")).hexdigest()


def _make_consent_nonce(
    account: str, tid: str, client_id: str, challenge: str, redirect_uri: str, q: str
) -> str:
    """A CSRF nonce bound to the session, the flow tuple and the whole query (#898)."""
    import secrets as _secrets

    exp = int(time.time()) + CONSENT_NONCE_TTL_S
    nid = _secrets.token_urlsafe(16)
    sig = _consent_sig(
        [account, tid, client_id, challenge, redirect_uri, _q_digest(q), str(exp), nid]
    )
    return f"{exp}.{nid}.{sig}"


def _check_consent_nonce(
    nonce: str,
    account: str,
    tid: str,
    client_id: str,
    challenge: str,
    redirect_uri: str,
    q: str,
) -> str:
    """'' when valid (and consumes it), else an error code."""
    parts = (nonce or "").split(".")
    if len(parts) != 3:
        return "nonce_malformed"
    exp_s, nid, sig = parts
    try:
        exp = int(exp_s)
    except ValueError:
        return "nonce_malformed"
    expected = _consent_sig(
        [account, tid, client_id, challenge, redirect_uri, _q_digest(q), exp_s, nid]
    )
    if not hmac.compare_digest(sig, expected):
        return "nonce_invalid"
    now = int(time.time())
    if exp < now:
        return "nonce_expired"
    _prune_consent_nonces(now)
    if nid in _consumed_consent_nonces:
        return "nonce_replayed"
    if _consent_nonces_per_tid.get(tid, 0) >= CONSENT_NONCE_PER_TID:
        return "consent_busy"
    if len(_consumed_consent_nonces) >= CONSENT_NONCE_CAP:
        return "consent_busy"
    _consumed_consent_nonces[nid] = (exp, tid)
    _consent_nonces_per_tid[tid] = _consent_nonces_per_tid.get(tid, 0) + 1
    return ""


async def _consent_client(params: dict[str, str]):
    """``(client, normalised redirect_uri, "")`` or ``(None, "", error_code)``."""
    from oncofiles.persistent_oauth import (
        client_name_refusal,
        normalize_redirect_uri,
        redirect_uri_allowed,
    )

    provider = auth
    if provider is None or not hasattr(provider, "get_client"):
        return None, "", "no_oauth_provider"
    client = await provider.get_client(params["client_id"])
    if client is None:
        return None, "", "unknown_client"
    try:
        wanted = normalize_redirect_uri(params["redirect_uri"])
        registered = {normalize_redirect_uri(u) for u in (client.redirect_uris or [])}
    except Exception:
        return None, "", "invalid_redirect_uri"
    if wanted not in registered:
        return None, "", "redirect_uri_not_registered"
    if not redirect_uri_allowed(wanted):
        return None, "", "redirect_uri_not_allowed"
    name_code = client_name_refusal(client.client_name, client.redirect_uris)
    if name_code:
        return None, "", name_code
    return client, wanted, ""


_CONSENT_HEADERS = [
    (b"cache-control", b"no-store"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"x-content-type-options", b"nosniff"),
    (
        b"content-security-policy",
        b"default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'",
    ),
]


async def _send_simple(send, status: int, body: bytes, content_type: bytes, extra=()) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type),
                (b"content-length", str(len(body)).encode("ascii")),
                *_CONSENT_HEADERS,
                *extra,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_authorize_refusal(send, code: str, status: int = 400) -> None:
    body = (
        f"This connection request was refused ({code}). Sign in at "
        "https://oncofiles.com/dashboard, then connect again from Claude.ai or ChatGPT."
    ).encode("ascii")
    await _send_simple(send, status, body, b"text/plain; charset=utf-8")


async def _send_authorize_to_sign_in(qs: str, send) -> None:
    """#898: GET /authorize without a session: 302 to sign-in, or 400."""
    target = _safe_authorize_next("/authorize" + (f"?{qs}" if qs else ""))
    if target is None:
        logger.info("MCP /authorize refused: code=unsafe_query (no session)")
        await _send_authorize_refusal(send, "unsafe_query")
        return
    logger.info("MCP /authorize: no session, redirecting to sign-in (#898)")
    location = "/dashboard?next=" + quote(target, safe="")
    await _send_simple(send, 302, b"", b"text/plain", ((b"location", location.encode("ascii")),))


def _consent_page(client, redirect_uri: str, account: str, qs: str, nonce: str) -> bytes:
    from html import escape

    from oncofiles.persistent_oauth import is_loopback_redirect

    name = escape((client.client_name or client.client_id or "?")[:120])
    # The full callback URI for a web client (the host alone is ambiguous on
    # shared vendor hosts); loopback is a local app on this computer.
    if is_loopback_redirect(redirect_uri):
        target = "an app on this computer / aplikácia na tomto počítači"
    else:
        target = redirect_uri
    target = escape(target)
    who = escape(account)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>Connect Oncofiles</title>
<style>
body{{font-family:system-ui,sans-serif;background:#f6f7fb;color:#14213d;margin:0;padding:16px}}
main{{max-width:520px;margin:40px auto;background:#fff;border-radius:12px;padding:24px}}
dt{{font-weight:600;margin-top:12px}} dd{{margin:2px 0 0;word-break:break-all}}
button{{background:#ff7f6b;color:#14213d;border:0;border-radius:8px;padding:12px 20px;
font-size:16px;font-weight:600;cursor:pointer}}
a{{color:#14213d;margin-left:16px}}
p.sk{{color:#475569}}
</style></head><body><main>
<h1>Connect Oncofiles?</h1>
<p>An application asks to read and write your Oncofiles records as this account.
Allow it only if you started this connection yourself.</p>
<p class="sk" lang="sk">Aplikácia žiada prístup k vašim záznamom v Oncofiles pod týmto účtom.
Povoľte to, len ak ste pripojenie spustili vy.</p>
<p><strong>Ak vám tento odkaz niekto poslal, kliknite Zrušiť. /
If someone sent you this link, click Cancel.</strong></p>
<dl>
<dt>Application name, as supplied by the app / Názov, ktorý uvádza aplikácia</dt>
<dd>&ldquo;{name}&rdquo;</dd>
<dt>Returns to / Vráti sa na</dt><dd><code>{target}</code></dd>
<dt>Your account / Váš účet</dt><dd>{who}</dd>
</dl>
<form method="post" action="{CONSENT_PATH}">
<input type="hidden" name="q" value="{escape(qs)}">
<input type="hidden" name="nonce" value="{escape(nonce)}">
<p><button type="submit">Allow / Povoliť</button><a href="/dashboard">Cancel / Zrušiť</a></p>
</form>
</main></body></html>
""".encode()


async def _read_small_body(receive, limit: int) -> bytes | None:
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            return None
        chunk = message.get("body", b"")
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


class MCPAuthorizeEmailCaptureMiddleware:
    """Bind the signed-in Google account to an MCP OAuth flow only after
    explicit consent (#478, #898).

    FastMCP's AuthorizationHandler doesn't know which Google user drives the
    flow, so this middleware sits in front of it:

    - **GET /authorize**: a parameter that must be single (client_id,
      code_challenge, redirect_uri, response_type, ...) appearing twice → 400,
      so our parse and FastMCP's never disagree. Without a valid dashboard
      session: 302 to ``/dashboard?next=<this URL>`` (the sign-in page comes
      back here). With a session: the client must be registered, the
      redirect_uri registered for it and on the exact allowlist
      (``config.MCP_OAUTH_REDIRECT_URIS``); then a server-rendered consent
      page shows the client name, the redirect URI and the account. GET
      never binds and never reaches FastMCP.
    - **POST /authorize/consent**: the page's form. Session again, the same
      checks, and a single-use CSRF nonce (HMAC with the #502 purpose key
      ``mcp_consent_key``, bound to the session tid, the account and the
      exact (client_id, code_challenge, redirect_uri), 5-minute TTL). Only
      then is the request forwarded internally as GET /authorize inside
      ``persistent_oauth.consented_binding``; the provider's ``authorize``
      records the account against the code from FastMCP's own parsed params.
    - **POST /authorize** (FastMCP's form variant): 405, it would skip consent.

    ``MCP_OAUTH_ALLOW_UNBOUND=true`` (single-user deployments without Google
    sign-in) lets a session-less GET and POST /authorize through, unbound.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path")
        method = scope.get("method")
        if path == "/authorize" and method == "GET":
            await self._authorize_get(scope, receive, send)
        elif path in ("/authorize", CONSENT_PATH) and method not in ("GET", "POST"):
            # HEAD (and anything else) never reaches FastMCP's handler.
            logger.info("MCP /authorize refused: code=method_not_allowed")
            await _send_authorize_refusal(send, "method_not_allowed", status=405)
        elif path == "/authorize" and method == "POST":
            from oncofiles.persistent_oauth import unbound_tokens_allowed

            if unbound_tokens_allowed():
                await self.app(scope, receive, send)
            else:
                logger.info("MCP /authorize refused: code=post_not_allowed")
                await _send_authorize_refusal(send, "post_not_allowed", status=405)
        elif path == CONSENT_PATH and method == "POST":
            await self._consent_post(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    async def _authorize_get(self, scope, receive, send) -> None:
        from oncofiles.persistent_oauth import unbound_tokens_allowed

        qs = scope.get("query_string", b"").decode("latin-1", errors="replace")
        params, err = _authorize_params(qs)
        if err:
            logger.info("MCP /authorize refused: code=%s", err)
            await _send_authorize_refusal(send, err)
            return
        session = _session_from_scope(scope)
        if session is None:
            if unbound_tokens_allowed():
                await self.app(scope, receive, send)
                return
            await _send_authorize_to_sign_in(qs, send)
            return
        account, tid = session
        client, redirect_uri, err = await _consent_client(params)
        if err:
            logger.info("MCP /authorize refused: code=%s", err)
            await _send_authorize_refusal(send, err)
            return
        nonce = _make_consent_nonce(
            account, tid, client.client_id, params["code_challenge"], redirect_uri, qs
        )
        body = _consent_page(client, redirect_uri, account, qs, nonce)
        await _send_simple(send, 200, body, b"text/html; charset=utf-8")

    async def _consent_post(self, scope, receive, send) -> None:
        from urllib.parse import parse_qsl

        from oncofiles.persistent_oauth import consented_binding, redirect_host_for_log

        body = await _read_small_body(receive, CONSENT_BODY_MAX)
        if body is None:
            await _send_authorize_refusal(send, "body_too_large", status=413)
            return
        try:
            form = parse_qsl(body.decode("ascii"), keep_blank_values=True)
        except UnicodeDecodeError:
            form = []
        keys = [k for k, _ in form]
        if keys.count("q") != 1 or keys.count("nonce") != 1:
            logger.info("MCP consent refused: code=form_invalid")
            await _send_authorize_refusal(send, "form_invalid")
            return
        values = dict(form)
        qs, nonce = values["q"], values["nonce"]
        params, err = _authorize_params(qs)
        if err:
            logger.info("MCP consent refused: code=%s", err)
            await _send_authorize_refusal(send, err)
            return
        session = _session_from_scope(scope)
        if session is None:
            logger.info("MCP consent refused: code=no_session")
            await _send_authorize_refusal(send, "no_session", status=403)
            return
        account, tid = session
        client, redirect_uri, err = await _consent_client(params)
        if err:
            logger.info("MCP consent refused: code=%s", err)
            await _send_authorize_refusal(send, err)
            return
        err = _check_consent_nonce(
            nonce, account, tid, client.client_id, params["code_challenge"], redirect_uri, qs
        )
        if err:
            logger.info("MCP consent refused: code=%s", err)
            await _send_authorize_refusal(send, err, status=403)
            return
        logger.info(
            "MCP consent granted caller=%s host=%s",
            _acl_caller_log_id(account),
            redirect_host_for_log(redirect_uri),
        )
        headers = [
            (k, v)
            for k, v in scope.get("headers", [])
            if k.decode("latin-1").lower() not in ("content-type", "content-length")
        ]
        forward = dict(
            scope,
            method="GET",
            path="/authorize",
            raw_path=b"/authorize",
            query_string=qs.encode("ascii"),
            headers=headers,
        )

        async def empty_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        with consented_binding(account, client.client_id, params["code_challenge"], redirect_uri):
            await self.app(forward, empty_receive, send)


class SecurityHeadersMiddleware:
    """Add baseline security headers to every HTTP response.

    Does not override headers already set by route handlers (e.g.
    X-Robots-Tag set by a route).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                existing = {k.decode("latin-1").lower(): v for k, v in headers}
                defaults = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"SAMEORIGIN"),
                    (b"referrer-policy", b"strict-origin-when-cross-origin"),
                    (b"permissions-policy", b"geolocation=(), microphone=(), camera=()"),
                    (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
                ]
                for name, value in defaults:
                    if name.decode("latin-1").lower() not in existing:
                        headers.append((name, value))
                # CSP only for HTML responses — avoid interfering with MCP/JSON clients
                ct = existing.get("content-type", b"").decode("latin-1", errors="replace").lower()
                if ct.startswith("text/html") and "content-security-policy" not in existing:
                    csp = _csp_for_path(path)
                    headers.append((b"content-security-policy", csp.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# Per-route CSP (#534). The #501 CSP3 fine-grained directives (`script-src-elem`,
# `script-src-attr 'none'`, `style-src-elem`) were applied **globally** by the
# middleware in `ea2496d` even though only `dashboard.html` had its inline
# blocks extracted to /dashboard.css + /dashboard.js. Result: every other public
# HTML route (landing, /onkologia, /oncology, /eu, /sk, /privacy, /terms,
# /oauth/callback) silently rendered with UA-default styles + broken inline
# scripts (gtag, lang switch, structured data) + dead `onclick=` CTAs for ~7 days
# in production. Modern browsers (Chrome 76+, Firefox 92+, Safari 16+) honor
# `*-elem` and `*-attr` over the legacy `script-src` / `style-src`.
#
# Fix: keep the strict #501/#525 policy for `/dashboard*` (where PHI is
# rendered, XSS attack value is highest, and inline blocks WERE extracted),
# fall back to the pre-#501 permissive policy for everything else. Public-page
# extraction can come later — locking the regression on the public site is
# more urgent than tightening CSP on routes that have no PHI.

# #700: no analytics on the logged-in dashboard (it renders medical records),
# so neither Google tag host is allowed here. Locked by
# tests/test_landing_content.py::test_dashboard_has_no_google_analytics.
_STRICT_DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' "
    "https://accounts.google.com https://apis.google.com; "
    "script-src-elem 'self' "
    "https://accounts.google.com https://apis.google.com; "
    "script-src-attr 'none'; "
    "style-src 'self' 'unsafe-inline' https://accounts.google.com; "
    "style-src-elem 'self' https://accounts.google.com; "
    "style-src-attr 'unsafe-inline'; "
    "font-src 'self' data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self' "
    "https://accounts.google.com https://oauth2.googleapis.com "
    "https://www.googleapis.com; "
    "frame-src https://accounts.google.com https://content.googleapis.com "
    "https://drive.google.com https://docs.google.com; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self' https://accounts.google.com; "
    "upgrade-insecure-requests"
)

# Pre-#501 CSP for public HTML routes — still hardens against framing,
# arbitrary connect-src exfil, mixed content, etc. Restores `'unsafe-inline'`
# in script-src + style-src (the directive form CSP3 still falls back to when
# no `*-elem` directive is present), so inline `<script>`, inline `<style>`,
# and inline event handlers continue to execute as designed on the marketing
# site, legal, topic, and OAuth-callback pages.
_PUBLIC_HTML_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' "
    "https://www.googletagmanager.com https://www.google-analytics.com "
    "https://accounts.google.com https://apis.google.com; "
    "style-src 'self' 'unsafe-inline' https://accounts.google.com; "
    "font-src 'self' data:; "
    "img-src 'self' data: https:; "
    "connect-src 'self' https://*.google-analytics.com "
    "https://*.analytics.google.com https://www.googletagmanager.com "
    "https://accounts.google.com https://oauth2.googleapis.com "
    "https://www.googleapis.com; "
    "frame-src https://accounts.google.com https://content.googleapis.com "
    "https://drive.google.com https://docs.google.com; "
    "frame-ancestors 'self'; "
    "base-uri 'self'; "
    "form-action 'self' https://accounts.google.com; "
    "upgrade-insecure-requests"
)


def _csp_for_path(path: str) -> str:
    """Return the strict #501 CSP for /dashboard* routes, the pre-#501
    permissive CSP for every other HTML route. See #534 for context."""
    if path.startswith("/dashboard"):
        return _STRICT_DASHBOARD_CSP
    if path == "/movement-atlas" or path.startswith("/movement-atlas/"):
        # #830: the hosted three.js atlas loads only same-origin files
        from oncofiles.atlas_showcase import ATLAS_CSP

        return ATLAS_CSP
    return _PUBLIC_HTML_CSP


def main() -> None:
    if MCP_TRANSPORT == "stdio":
        mcp.run()
    else:
        from starlette.middleware import Middleware
        from starlette.middleware.cors import CORSMiddleware

        from oncofiles.ratelimit import RateLimitMiddleware
        from oncofiles.register_guard import RegisterJSONGuardMiddleware

        def _quiet_session_verifier(token: str) -> str | None:
            return _verify_session_token(token, log=False)

        mcp.run(
            transport=MCP_TRANSPORT,
            host=MCP_HOST,
            port=MCP_PORT,
            stateless_http=True,  # no server-side sessions — survives Railway deploys (#229)
            middleware=[
                Middleware(SecurityHeadersMiddleware),
                Middleware(
                    CORSMiddleware,
                    allow_origins=[
                        "https://oncofiles.com",
                        "https://claude.ai",
                        "https://chatgpt.com",
                    ],
                    allow_credentials=True,
                    allow_methods=["GET", "POST", "OPTIONS", "DELETE"],
                    allow_headers=["Authorization", "Content-Type", "mcp-protocol-version"],
                ),
                # #591: innermost, so CORS + security headers wrap its 429/413 and
                # preflights never reach it. /health is exempt. The verifier is
                # HMAC-only (no DB) and silent (log=False), so forged sessions can
                # neither key a victim's bucket nor flood the logs.
                Middleware(RateLimitMiddleware, session_verifier=_quiet_session_verifier),
                # #478/#898: consent + binding for /authorize. INSIDE the rate
                # limiter (auth class, incl. /authorize/consent), so its early
                # returns (consent page, refusals) are throttled too, and still
                # in front of FastMCP's AuthorizationHandler, which only ever
                # sees the internal forward after a valid consent POST.
                Middleware(MCPAuthorizeEmailCaptureMiddleware),
                # #584: non-JSON DCR body on POST /register → 400, not 500.
                # Inside the rate limiter (Wave A integration), so /register
                # probes with junk bodies still count against the auth bucket
                # and its body limit applies before this guard buffers.
                Middleware(RegisterJSONGuardMiddleware),
            ],
            uvicorn_config={
                "timeout_keep_alive": 120,
                "limit_concurrency": 50,
            },
        )


if __name__ == "__main__":
    main()
