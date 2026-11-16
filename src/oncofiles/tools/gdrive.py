"""Google Drive OAuth and sync tools."""

from __future__ import annotations

import asyncio
import json
import logging

from fastmcp import Context
from mcp.types import ToolAnnotations

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
    from oncofiles.google_link import (
        allowed_accounts,
        mcp_principal_privileged,
        store_google_link,
    )
    from oncofiles.oauth import exchange_code, mcp_principal

    if not GOOGLE_OAUTH_CLIENT_ID:
        return json.dumps({"error": "GOOGLE_OAUTH_CLIENT_ID not configured"})

    pid = _get_patient_id()
    db = _get_db(ctx)
    patient = await db.get_patient(pid)
    if patient is None:
        return json.dumps({"error": "patient not found"})

    try:
        tokens = exchange_code(code)
    except Exception as e:
        return json.dumps({"error": f"Token exchange failed: {e}"})

    # #591: same overwrite guard + audit row as /oauth/callback.
    principal = mcp_principal()
    stored, reason = await store_google_link(
        db,
        patient_id=pid,
        tokens=tokens,
        service="drive",
        principal_kind="m",
        privileged=mcp_principal_privileged(principal, patient.caregiver_email),
        allowed_account_hashes=allowed_accounts(principal, patient.caregiver_email),
        source="gdrive_auth_callback",
    )
    if not stored:
        return json.dumps({"error": f"Google account not linked: {reason}"})
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


async def _gdrive_for_patient(ctx: Context, pid: str):
    """The Drive client of ``pid`` — never the ContextVar patient's (#839).

    Falls back to the lifespan client only when ``pid`` is the request's own
    patient, as ``_get_gdrive`` does.
    """
    from oncofiles.server import _create_patient_clients

    clients = await _create_patient_clients(_get_db(ctx), pid)
    if clients:
        return clients[0]
    if pid == _get_patient_id(required=False):
        return ctx.request_context.lifespan_context.get("gdrive")
    return None


_ADMIN_NEEDS_SLUG = (
    "Admin callers must pass patient_slug=<slug>; there is no implicit admin patient scope."
)


async def _folder_tool_patient(ctx: Context, patient_slug: str | None) -> tuple[str, str | None]:
    """Resolve the patient of a folder tool: ``(patient_id, error_json)`` (#839).

    An admin must name the patient (a folder set on "whichever patient is
    current" is the bug this closes); everyone else goes through the ACL gate.
    """
    from oncofiles.tools._helpers import _is_admin_caller, _resolve_patient_id

    if _is_admin_caller() and not patient_slug:
        return "", json.dumps({"error": _ADMIN_NEEDS_SLUG})
    try:
        return await _resolve_patient_id(patient_slug, ctx), None
    except ValueError as exc:
        return "", json.dumps({"error": str(exc)})


async def gdrive_set_folder(
    ctx: Context,
    folder_id: str,
    patient_slug: str | None = None,
    dry_run: bool = True,
) -> str:
    """Connect a Google Drive folder to ONE patient (operator fallback).

    The normal path is the dashboard wizard (Drive step): it lets the user
    browse to the patient's own folder. Use this tool only when the wizard
    cannot be used, and always run it with dry_run=true first.

    Runs the same checks as the wizard, in the same order: the id must be an
    existing folder; a PARENT of patient folders is refused (error with
    ``code: "container_folder"`` and ``reason``: ``curated_children`` — a
    subfolder is another patient's curated folder; ``too_many_to_check`` —
    more than 200 subfolders; ``check_failed`` — the check could not run, try
    again). No argument lifts a refusal: pass the patient's own folder. A
    curated folder (README.md + _index.json) is connected with
    ``curated: true`` and nothing is created in it. No category folders are
    created by this tool (setup_gdrive does that for a legacy folder).

    Args:
        folder_id: Google Drive folder id or a pasted folder link.
        patient_slug: The patient whose folder is set (#429). Required for
            admin callers (refused without it). Non-admin callers default to
            their own patient and may only name a patient they have access to.
        dry_run: True (default) validates and returns the folder overview and
            what would be set, and writes nothing. Pass false to set it.
    """
    from oncofiles.curated_folder import parse_drive_folder_ref
    from oncofiles.drive_folder_link import connect_folder

    pid, err = await _folder_tool_patient(ctx, patient_slug)
    if err:
        return err

    parsed = parse_drive_folder_ref((folder_id or "").strip())
    if not parsed:
        return json.dumps({"error": "folder_id must be a Google Drive folder link or id"})

    db = _get_db(ctx)
    try:
        outcome = await connect_folder(
            db, pid, folder_id=parsed, create_subfolders=False, dry_run=dry_run
        )
    except Exception:
        logger.exception("gdrive_set_folder failed")
        return json.dumps({"error": "internal error; check gdrive_auth_status before trying again"})

    result = dict(outcome.payload)
    if outcome.kind == "no_token":
        result["error"] = "No OAuth tokens found. Connect Google Drive first."
    if not outcome.ok:
        return json.dumps(result)

    result["patient_id"] = pid
    if outcome.kind == "dry_run":
        result["message"] = (
            "Dry run: nothing was written. Call again with dry_run=false to set this folder."
        )
    elif not result.get("curated"):
        if result.get("owner_email"):
            result["message"] = (
                f"Folder set. Owner '{result['owner_email']}' will get writer access "
                "on all new files."
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

    Legacy (non-curated) Drive folders only. A curated patient folder
    (README.md + _index.json) is never synced: the result is
    ``{"skipped": true, "curated": true}``. Do NOT suggest this tool to
    import a curated folder — it is processed by the curated pipeline
    (continuous mode, or curated_folder_run) and mirrored into search by
    curated_index; check it with curated_folder_status.

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
    completed sync (if any). Legacy Drive sync only — for curated patient
    folders use curated_folder_status.
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

    Legacy (non-curated) Drive folders only. For a curated patient folder
    (README.md + _index.json) it returns ``{"skipped": true, "curated": true}``
    and imports nothing. To make curated documents searchable use
    curated_index; new inbox files are filed by the curated pipeline
    (continuous mode, or curated_folder_run). Check with curated_folder_status.

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

    Legacy (non-curated) Drive folders only. A curated patient folder
    (README.md + _index.json) is write-protected: the result is
    ``{"skipped": true, "curated": true}`` and nothing is exported. Curated
    folders are maintained by the curated pipeline; see curated_folder_status.

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


async def setup_gdrive(ctx: Context, root_folder_id: str, patient_slug: str | None = None) -> str:
    """Create the folder structure in a GDrive root folder (patient-type-aware).

    Idempotent: checks for existing folders by name before creating.
    Handles both bilingual and legacy EN-only folder names (renames old to bilingual).
    Oncology patients skip general-only folders; general patients skip oncology-only.

    Legacy (non-curated) folders only: a curated patient folder (README.md +
    _index.json) keeps its own fixed folder set and is never restructured
    (``{"skipped": true, "curated": true}``). A parent of patient folders is
    refused like in gdrive_set_folder (``code: "container_folder"``). This
    tool never changes which folder a patient is connected to.

    Args:
        root_folder_id: Google Drive folder id (or link) to create subfolders in.
        patient_slug: The patient whose Drive account and patient type are
            used (#429). Required for admin callers; non-admin callers default
            to their own patient.
    """
    from oncofiles.curated_folder import (
        folder_overview,
        parse_drive_folder_ref,
        write_block_reason,
    )
    from oncofiles.drive_folder_link import container_refusal
    from oncofiles.gdrive_folders import _folders_for_patient_type, bilingual_name
    from oncofiles.patient_context import get_context

    pid, err = await _folder_tool_patient(ctx, patient_slug)
    if err:
        return err

    gdrive = await _gdrive_for_patient(ctx, pid)
    if not gdrive:
        msg = "GDrive client not configured. Use gdrive_auth_url to connect."
        return json.dumps({"error": msg})

    parsed = parse_drive_folder_ref((root_folder_id or "").strip())
    if not parsed:
        return json.dumps({"error": "root_folder_id must be a Google Drive folder link or id"})
    root_folder_id = parsed

    # #839: same isolation guard as connecting a folder, before any write.
    overview = await asyncio.to_thread(folder_overview, gdrive, root_folder_id)
    refusal = container_refusal(overview)
    if refusal:
        return json.dumps(refusal)
    if overview["curated"] or write_block_reason(gdrive):
        return json.dumps(
            {
                "skipped": True,
                "curated": True,
                "root_folder_id": root_folder_id,
                "message": "Curated folder (README.md + _index.json): its folder set is "
                "fixed by the README contract; nothing was created.",
            }
        )

    patient_type = get_context(pid).get("patient_type", "oncology")
    folders = _folders_for_patient_type(patient_type)

    created: list[str] = []
    skipped: list[str] = []
    renamed: list[str] = []

    for en_key in folders:
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
            "total_folders": len(folders),
            "created": created,
            "skipped": skipped,
            "renamed": renamed,
            "summary": (
                f"Created {len(created)}, skipped {len(skipped)}, "
                f"renamed {len(renamed)} of {len(folders)} folders."
            ),
        }
    )


async def export_manifest(ctx: Context, patient_slug: str | None = None) -> str:
    """Export one patient's records as a JSON manifest (on-demand).

    Returns the manifest JSON with the patient's documents, conversations,
    treatment events, research entries, and agent state. Always scoped to a
    single patient — never the whole database.

    Args:
        patient_slug: Explicit patient slug (#429). Required for admin
            callers (refused without it). Non-admin callers default to their
            bound patient and may only name a patient they have access to.
    """
    from oncofiles.manifest import export_manifest as _export_manifest
    from oncofiles.manifest import render_manifest_json
    from oncofiles.tools._helpers import _is_admin_caller, _resolve_patient_id

    db = _get_db(ctx)
    # #691: resolve through the ACL gate (#497) and pass the pid through.
    # The tool used to call _export_manifest(db) without patient_id, which
    # raised TypeError on every call; dropping the kwarg must never be
    # "fixed" by exporting the whole DB.
    #
    # Same rule as query_db (#607/#686): an admin must name the patient.
    # Falling back to the middleware-bound/default patient would export
    # "whichever patient happens to be current".
    if _is_admin_caller() and not patient_slug:
        return json.dumps(
            {
                "error": "Admin callers must pass patient_slug=<slug> to export a "
                "manifest; there is no implicit admin patient scope."
            }
        )
    try:
        pid = await _resolve_patient_id(patient_slug, ctx)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    manifest = await _export_manifest(db, patient_id=pid)
    return render_manifest_json(manifest)


def register(mcp):
    mcp.tool()(gdrive_auth_url)
    mcp.tool()(gdrive_auth_callback)
    mcp.tool()(gdrive_auth_status)
    # #839: writes a setting (no delete); the same call twice leaves the same state.
    mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
        )
    )(gdrive_set_folder)
    mcp.tool()(gdrive_sync)
    mcp.tool()(gdrive_sync_status)
    mcp.tool()(sync_from_gdrive)
    mcp.tool()(sync_to_gdrive)
    mcp.tool()(gdrive_fix_permissions)
    mcp.tool()(setup_gdrive)
    mcp.tool()(export_manifest)
