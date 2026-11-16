"""Connect a Google Drive folder to a patient: ONE decision path (#839).

``/api/gdrive-set-folder`` (dashboard wizard) and the MCP tool
``gdrive_set_folder`` both call :func:`connect_folder`, so the order of the
checks cannot drift between them again:

1. the patient's OAuth token (refreshed when expired);
2. optionally a new folder created by this request (REST only);
3. the id must exist and be a folder;
4. the container guard from #835/#838 (``curated_folder.folder_overview``):
   a parent of other patients' curated folders, a folder with too many
   subfolders to check, or a failed check is refused. Nothing lifts it. A
   folder this request has just created is empty and passes;
5. persist the folder id (+ display name), clear the 404 / client caches;
6. folder owner;
7. curated detection (``mark_if_curated``): a curated folder gets no folders;
8. optionally the legacy category subfolders.

The caller resolves the patient and checks access BEFORE calling (dashboard
session ACL / ``_resolve_patient_id``); nothing here decides who may act.

Drive calls run through ``asyncio.to_thread`` on a client built for this call
and read ``gdrive._service`` in the worker thread itself (#579/#845: a service
never crosses threads).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from oncofiles.gdrive_client import FOLDER_MIME, GDriveClient

logger = logging.getLogger(__name__)

CONTAINER_MESSAGES = {
    "curated_children": "This folder contains other patients' folders and "
    "cannot be connected. Open the patient's own folder.",
    "too_many_to_check": "This folder has too many subfolders to check "
    "safely. Open the folder of the patient you mean, or paste its link.",
    "check_failed": "Could not check this folder right now. Try again in a moment.",
}


@dataclass(frozen=True)
class FolderLinkOutcome:
    """What happened. ``payload`` + ``status`` are the REST response as is.

    ``kind``: ``ok`` | ``dry_run`` | ``no_token`` | ``invalid_parent`` |
    ``curated_parent`` | ``not_a_folder`` | ``not_found`` | ``no_access`` |
    ``container_folder``.
    """

    kind: str
    status: int
    payload: dict

    @property
    def ok(self) -> bool:
        return self.status == 200


def container_refusal(overview: dict) -> dict | None:
    """The refusal body for a ``folder_overview`` verdict, or None when it passes."""
    reason = overview["container_reason"]
    if not reason:
        return None
    return {
        "error": CONTAINER_MESSAGES[reason],
        "code": "container_folder",
        "reason": reason,
        "confirmable": False,
        "subfolder_count": overview["subfolder_count"],
        "curated_children": overview["curated_children"],
    }


async def connect_folder(
    db,
    patient_id: str,
    *,
    folder_id: str = "",
    folder_name: str = "",
    create_parent_raw: str = "",
    create_subfolders: bool = True,
    dry_run: bool = False,
) -> FolderLinkOutcome:
    """Validate ``folder_id`` (or create ``folder_name``) and connect it to ``patient_id``.

    ``folder_id`` is an already parsed Drive id. ``dry_run`` runs steps 1, 3
    and 4 plus a read-only curated check and writes nothing (no DB row, no
    Drive write, no cache change); it needs ``folder_id``. Raises on an
    unexpected error (token refresh, Drive outage after the checks) — the
    caller turns that into its own "internal error".
    """
    from oncofiles import config
    from oncofiles import server as _server
    from oncofiles.oauth import is_token_expired, refresh_access_token

    if dry_run and not folder_id:
        raise ValueError("a dry run needs an existing folder_id")

    token = await db.get_oauth_token(patient_id=patient_id)
    if not token:
        return FolderLinkOutcome(
            "no_token", 400, {"error": "No OAuth token. Connect Google Drive first."}
        )

    access_token = token.access_token
    if is_token_expired(token.token_expiry.isoformat() if token.token_expiry else None):
        refreshed = refresh_access_token(token.refresh_token)
        access_token = refreshed["access_token"]
        if not dry_run:
            new_expiry = datetime.now(UTC) + timedelta(seconds=refreshed.get("expires_in", 3600))
            token.access_token = access_token
            token.token_expiry = new_expiry
            await db.update_oauth_access_token(
                token.patient_id, token.provider, access_token, new_expiry
            )

    gdrive = GDriveClient.from_oauth(
        access_token=access_token,
        refresh_token=token.refresh_token,
        client_id=config.GOOGLE_OAUTH_CLIENT_ID,
        client_secret=config.GOOGLE_OAUTH_CLIENT_SECRET,
    )

    # Create new folder if needed — inside the browsed parent (#563), never
    # inside a curated folder (#562: its README fixes the folder set).
    created_new = False
    if not folder_id and folder_name:
        from oncofiles.curated_folder import is_curated_folder, parse_drive_folder_ref

        raw_parent = str(create_parent_raw or "").strip()
        create_parent = "root"
        if raw_parent and raw_parent != "root":
            create_parent = parse_drive_folder_ref(raw_parent)
            if not create_parent:
                return FolderLinkOutcome("invalid_parent", 400, {"error": "invalid parent_id"})
            if await asyncio.to_thread(is_curated_folder, gdrive, create_parent):
                return FolderLinkOutcome(
                    "curated_parent",
                    400,
                    {"error": "Cannot create a folder inside a curated folder (README.md)."},
                )
        folder_id = await asyncio.to_thread(gdrive.create_folder, folder_name, create_parent)
        created_new = True

    # Validate folder exists before persisting
    try:
        meta = await asyncio.to_thread(
            lambda: (
                gdrive._service.files().get(fileId=folder_id, fields="id,name,mimeType").execute()
            )
        )
        if meta.get("mimeType") != FOLDER_MIME:
            return FolderLinkOutcome(
                "not_a_folder",
                400,
                {"error": f"ID is not a folder (type: {meta.get('mimeType')})"},
            )
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status == 404:
            return FolderLinkOutcome(
                "not_found", 404, {"error": f"Folder '{folder_id}' not found. Check the ID."}
            )
        return FolderLinkOutcome("no_access", 400, {"error": f"Cannot access folder: {exc}"})

    # #835: isolation guard, fails closed. A folder is not connected when a
    # subfolder is another patient's curated folder, when it has too many
    # subfolders to check them all, or when the check could not run (a
    # Drive hiccup means "try again", not a pass). No argument lifts
    # this. Nothing is persisted or created before it passes.
    overview = None
    if not created_new:
        from oncofiles.curated_folder import folder_overview

        overview = await asyncio.to_thread(folder_overview, gdrive, folder_id)
        refusal = container_refusal(overview)
        if refusal:
            return FolderLinkOutcome("container_folder", 409, refusal)

    cached_folder_name = meta.get("name") if meta else None

    from oncofiles.curated_folder import clear_cache, curated_reason, mark_if_curated

    if dry_run:
        # Read-only and uncached: a dry run must not leave a verdict behind.
        reason = await asyncio.to_thread(curated_reason, gdrive, folder_id, use_cache=False)
        clear_cache(folder_id)
        return FolderLinkOutcome(
            "dry_run",
            200,
            {
                "status": "dry_run",
                "dry_run": True,
                "folder_id": folder_id,
                "folder_name": cached_folder_name,
                "current_folder_id": token.gdrive_folder_id or None,
                "would_change": (token.gdrive_folder_id or None) != folder_id,
                "curated": reason is not None,
                "curated_root": reason == "is a curated folder",
                "overview": overview,
            },
        )

    # Persist folder choice (including display name from metadata)
    await db.update_oauth_folder(patient_id, "google", folder_id, folder_name=cached_folder_name)

    # Clear folder-invalid flag so sync resumes immediately
    _server._folder_404_counts.pop(patient_id, None)
    _server._patient_clients_cache.pop(patient_id, None)

    # Detect owner email
    owner_email = await asyncio.to_thread(gdrive.get_folder_owner, folder_id)
    if owner_email:
        await db.update_oauth_owner_email(patient_id, "google", owner_email)
        gdrive.owner_email = owner_email

    # Curated folder (#562): its README fixes the folder set — create nothing.
    clear_cache(folder_id)
    if await asyncio.to_thread(mark_if_curated, gdrive, folder_id):
        # curated_root: the folder itself has the contract (the pipeline
        # processes it), as opposed to lying inside / above a curated one.
        reason = await asyncio.to_thread(curated_reason, gdrive, folder_id)
        return FolderLinkOutcome(
            "ok",
            200,
            {
                "status": "ok",
                "folder_id": folder_id,
                "owner_email": owner_email,
                "curated": True,
                "curated_root": reason == "is a curated folder",
                "subfolders_created": 0,
                "subfolders_skipped": 0,
                "message": "Curated folder detected (README.md + _index.json): "
                "no folders created; legacy sync is disabled for it.",
            },
        )

    payload = {"status": "ok", "folder_id": folder_id, "owner_email": owner_email}
    if create_subfolders:
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
        payload["subfolders_created"] = len(created)
        payload["subfolders_skipped"] = len(skipped)
    return FolderLinkOutcome("ok", 200, payload)
