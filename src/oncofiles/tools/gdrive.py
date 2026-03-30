"""Google Drive OAuth and sync tools."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC

from fastmcp import Context

from oncofiles.config import GOOGLE_DRIVE_FOLDER_ID, GOOGLE_OAUTH_CLIENT_ID
from oncofiles.tools._helpers import _get_db, _get_files, _get_gdrive, _get_patient_id

logger = logging.getLogger(__name__)


def _get_sync_folder_id(ctx: Context) -> str:
    """Get the GDrive folder ID from config or OAuth tokens."""
    if GOOGLE_DRIVE_FOLDER_ID:
        return GOOGLE_DRIVE_FOLDER_ID
    return ctx.request_context.lifespan_context.get("oauth_folder_id", "")


async def gdrive_auth_url(ctx: Context) -> str:
    """Get the Google OAuth authorization URL for the user to visit.

    Returns a URL that the user should open in their browser to authorize
    Google Drive access. After authorization, Google redirects to the callback
    URL which stores the tokens automatically.
    """
    from oncofiles.oauth import get_auth_url

    if not GOOGLE_OAUTH_CLIENT_ID:
        return json.dumps({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"})

    url = get_auth_url(patient_id=_get_patient_id())
    return json.dumps(
        {
            "auth_url": url,
            "instructions": "Open this URL in your browser to connect Google Drive.",
        }
    )


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
        patient_id=_get_patient_id(),
        access_token=tokens["access_token"],
        refresh_token=tokens.get("refresh_token", ""),
        token_expiry=expiry,
    )

    db = _get_db(ctx)
    await db.upsert_oauth_token(oauth_token)
    msg = "Google Drive connected. Use gdrive_set_folder to pick a sync folder."
    return json.dumps({"status": "ok", "message": msg})


async def gdrive_auth_status(ctx: Context) -> str:
    """Check if the user has valid Google Drive OAuth tokens."""
    from oncofiles.oauth import is_token_expired

    db = _get_db(ctx)
    token = await db.get_oauth_token(patient_id=_get_patient_id())

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


async def gdrive_set_folder(ctx: Context, folder_id: str) -> str:
    """Set the Google Drive folder to sync with.

    Detects the folder owner's email and stores it for automatic permission
    sharing. When the service account creates files/folders, it grants writer
    access to the original folder owner so they can see the files.

    Args:
        folder_id: The Google Drive folder ID to use as the sync root.
    """
    db = _get_db(ctx)
    pid = _get_patient_id()
    token = await db.get_oauth_token(patient_id=pid)
    if not token:
        return json.dumps({"error": "No OAuth tokens found. Connect Google Drive first."})

    await db.update_oauth_folder(pid, token.provider, folder_id)

    # Detect folder owner and store for permission sharing
    gdrive = await _get_gdrive(ctx)
    owner_email = None
    if gdrive:
        owner_email = await asyncio.to_thread(gdrive.get_folder_owner, folder_id)
        if owner_email:
            await db.update_oauth_owner_email(pid, token.provider, owner_email)
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


async def gdrive_sync(
    ctx: Context,
    dry_run: bool = False,
    enhance: bool = True,
) -> str:
    """Run full bidirectional Google Drive sync (runs in background).

    Returns immediately with status. Use gdrive_sync_status to check progress
    and get the result when done.

    1. Imports new/changed files from GDrive (GDrive wins on conflicts)
    2. Exports documents to organized category/year-month folders
    3. Exports manifest + metadata markdown files

    Args:
        dry_run: Preview changes without syncing.
        enhance: Run AI summary/tag generation on new files (default True).
    """
    from oncofiles.sync import get_sync_status
    from oncofiles.sync import sync as _sync

    db = _get_db(ctx)
    files = _get_files(ctx)
    gdrive = await _get_gdrive(ctx)
    patient_id = _get_patient_id()
    if not gdrive:
        msg = "GDrive client not configured. Use gdrive_auth_url to connect."
        return json.dumps({"error": msg})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set. Use gdrive_set_folder to pick one."})

    # Dry run executes inline (fast)
    if dry_run:
        try:
            stats = await _sync(
                db,
                files,
                gdrive,
                folder_id,
                dry_run=True,
                enhance=enhance,
                trigger="manual",
                patient_id=patient_id,
            )
        except Exception:
            logger.exception("gdrive_sync dry_run failed")
            return json.dumps({"error": "Sync dry run failed — check server logs"})
        return json.dumps(stats)

    # Check if already running
    status = get_sync_status()
    if status["running"]:
        return json.dumps(
            {
                "status": "already_running",
                "elapsed_s": status.get("elapsed_s", 0),
                "message": "Sync already in progress. Use gdrive_sync_status to check.",
            }
        )

    # Fire-and-forget: launch sync as background task
    async def _background_sync() -> None:
        try:
            await _sync(
                db,
                files,
                gdrive,
                folder_id,
                dry_run=False,
                enhance=enhance,
                trigger="manual",
                patient_id=patient_id,
            )
        except Exception:
            logger.exception("Background sync failed")

    asyncio.create_task(_background_sync())

    return json.dumps(
        {
            "status": "started",
            "message": "Sync started in background. Use gdrive_sync_status to check progress.",
        }
    )


async def gdrive_sync_status(ctx: Context) -> str:
    """Check the status of the last or current GDrive sync.

    Returns whether a sync is currently running, and the result of the last
    completed sync (if any).
    """
    from oncofiles.sync import get_sync_status

    return json.dumps(get_sync_status())


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
    gdrive = await _get_gdrive(ctx)
    patient_id = _get_patient_id()
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
        patient_id=patient_id,
    )
    return json.dumps(stats)


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
    gdrive = await _get_gdrive(ctx)
    patient_id = _get_patient_id()
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
        patient_id=patient_id,
    )
    return json.dumps(stats)


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
    gdrive = await _get_gdrive(ctx)
    if not gdrive:
        return json.dumps({"error": "GDrive client not configured."})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set."})

    # Resolve email
    target_email = email
    if not target_email:
        target_email = await asyncio.to_thread(gdrive.get_folder_owner, folder_id)
    if not target_email:
        return json.dumps({"error": "Could not detect folder owner. Pass email explicitly."})

    # Store owner_email for future auto-sharing
    pid = _get_patient_id()
    token = await db.get_oauth_token(patient_id=pid)
    if token:
        await db.update_oauth_owner_email(pid, token.provider, target_email)
    gdrive.owner_email = target_email

    # Grant access recursively
    count = await asyncio.to_thread(gdrive.grant_access_recursive, folder_id, target_email)

    return json.dumps(
        {
            "status": "ok",
            "email": target_email,
            "files_shared": count,
            "message": f"Granted writer access to {target_email} on {count} files/folders.",
        }
    )


async def setup_gdrive(ctx: Context, root_folder_id: str) -> str:
    """Create the full folder structure (17 categories + 3 metadata) in a GDrive root folder.

    Idempotent: checks for existing folders by name before creating.
    Handles both bilingual and legacy EN-only folder names (renames old to bilingual).

    Args:
        root_folder_id: The Google Drive folder ID to create subfolders in.
    """
    from oncofiles.gdrive_folders import ALL_FOLDERS, bilingual_name

    gdrive = await _get_gdrive(ctx)
    if not gdrive:
        msg = "GDrive client not configured. Use gdrive_auth_url to connect."
        return json.dumps({"error": msg})

    created: list[str] = []
    skipped: list[str] = []
    renamed: list[str] = []

    for en_key in ALL_FOLDERS:
        display = bilingual_name(en_key)

        # Check bilingual name first
        existing = await asyncio.to_thread(gdrive.find_folder, display, root_folder_id)
        if existing:
            skipped.append(display)
            continue

        # Check old EN-only name and rename
        old = await asyncio.to_thread(gdrive.find_folder, en_key, root_folder_id)
        if old:
            await asyncio.to_thread(gdrive.rename_file, old, display)
            renamed.append(f"{en_key} → {display}")
            continue

        # Create new folder
        await asyncio.to_thread(gdrive.create_folder, display, root_folder_id)
        created.append(display)

    return json.dumps(
        {
            "status": "ok",
            "root_folder_id": root_folder_id,
            "total_folders": len(ALL_FOLDERS),
            "created": created,
            "skipped": skipped,
            "renamed": renamed,
            "summary": (
                f"Created {len(created)}, skipped {len(skipped)}, "
                f"renamed {len(renamed)} of {len(ALL_FOLDERS)} folders."
            ),
        }
    )


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


def register(mcp):
    mcp.tool()(gdrive_auth_url)
    mcp.tool()(gdrive_auth_callback)
    mcp.tool()(gdrive_auth_status)
    mcp.tool()(gdrive_set_folder)
    mcp.tool()(gdrive_sync)
    mcp.tool()(gdrive_sync_status)
    mcp.tool()(sync_from_gdrive)
    mcp.tool()(sync_to_gdrive)
    mcp.tool()(gdrive_fix_permissions)
    mcp.tool()(setup_gdrive)
    mcp.tool()(export_manifest)
