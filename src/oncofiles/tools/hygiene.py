"""GDrive folder hygiene: reconciliation, category validation, and cleanup tools."""

from __future__ import annotations

import asyncio
import json
import logging

from fastmcp import Context

from oncofiles.gdrive_folders import bilingual_name, en_key_from_folder_name
from oncofiles.models import DocumentCategory
from oncofiles.tools._helpers import _gdrive_url, _get_db, _get_gdrive

logger = logging.getLogger(__name__)

# Known unmanaged folders and their target categories
_UNMANAGED_FOLDER_MAP: dict[str, str] = {
    "Karta poistenca VSZP": "other",
    "Guidelines": "reference",
    "Analyzy": "reference",
}

# Document types from AI metadata that map directly to categories
_DOCTYPE_TO_CATEGORY: dict[str, str] = {
    "lab_report": "labs",
    "discharge_summary": "discharge_summary",
    "pathology": "pathology",
    "imaging": "imaging",
    "surgical_report": "surgical_report",
    "genetics": "genetics",
    "consultation": "report",
    "referral": "referral",
    "chemo_sheet": "chemo_sheet",
    "prescription": "prescription",
}


def _get_sync_folder_id(ctx: Context) -> str:
    """Get the GDrive folder ID from config or OAuth tokens."""
    from oncofiles.config import GOOGLE_DRIVE_FOLDER_ID

    if GOOGLE_DRIVE_FOLDER_ID:
        return GOOGLE_DRIVE_FOLDER_ID
    return ctx.request_context.lifespan_context.get("oauth_folder_id", "")


async def reconcile_gdrive(
    ctx: Context,
    dry_run: bool = True,
) -> str:
    """Detect and fix GDrive folder structure issues.

    Scans for: unknown folders, root-level files not in any category folder,
    empty managed folders, and stale backup folders.

    Args:
        dry_run: If True (default), report issues without making changes.
                 If False, move files, rename folders, and clean up backups.
    """
    gdrive = _get_gdrive(ctx)
    if not gdrive:
        return json.dumps({"error": "GDrive client not configured"})

    folder_id = _get_sync_folder_id(ctx)
    if not folder_id:
        return json.dumps({"error": "No sync folder set"})

    report: dict = {
        "unknown_folders": [],
        "root_files": [],
        "empty_folders": [],
        "backup_folders": [],
        "actions_taken": [],
    }

    # List all items directly under root
    items = await asyncio.to_thread(_list_root_items, gdrive, folder_id)

    managed_folder_ids: dict[str, str] = {}  # folder_name -> folder_id

    for item in items:
        name = item["name"]
        item_id = item["id"]
        is_folder = item["mimeType"] == "application/vnd.google-apps.folder"

        if is_folder:
            en_key = en_key_from_folder_name(name)
            if en_key:
                # Known managed folder
                managed_folder_ids[en_key] = item_id
            elif name.startswith(".zaloha_"):
                # Backup folder
                report["backup_folders"].append({"name": name, "id": item_id})
            else:
                # Unknown folder
                suggested = _UNMANAGED_FOLDER_MAP.get(name)
                report["unknown_folders"].append(
                    {
                        "name": name,
                        "id": item_id,
                        "suggested_category": suggested,
                        "action": (
                            f"Move contents to '{suggested}'"
                            if suggested
                            else "Manual review needed"
                        ),
                    }
                )
        else:
            # Root-level file
            if name.startswith("_"):
                # Metadata file (manifest, etc.) — skip
                continue
            report["root_files"].append(
                {
                    "name": name,
                    "id": item_id,
                    "mime_type": item.get("mimeType", ""),
                    "size": item.get("size", ""),
                    "action": "Import to oncofiles and move to correct category folder",
                }
            )

    # Check which managed folders are empty
    for en_key, fid in managed_folder_ids.items():
        if en_key in ("conversations", "treatment", "research"):
            continue  # Metadata folders can be empty
        count = await asyncio.to_thread(_count_files_in_folder, gdrive, fid)
        if count == 0:
            report["empty_folders"].append({"name": bilingual_name(en_key), "category": en_key})

    # Execute mode
    if not dry_run:
        from oncofiles.gdrive_folders import ensure_folder_structure

        folder_map = await asyncio.to_thread(ensure_folder_structure, gdrive, folder_id)

        # Move contents of unknown folders to their target categories
        for uf in report["unknown_folders"]:
            target_cat = _UNMANAGED_FOLDER_MAP.get(uf["name"])
            if target_cat and target_cat in folder_map:
                target_folder = folder_map[target_cat]
                moved = await asyncio.to_thread(
                    _move_folder_contents, gdrive, uf["id"], target_folder
                )
                report["actions_taken"].append(
                    f"Moved {moved} files from '{uf['name']}' to '{bilingual_name(target_cat)}'"
                )

        # Soft-delete backup folders
        for bf in report["backup_folders"]:
            await asyncio.to_thread(gdrive.trash_file, bf["id"])
            report["actions_taken"].append(f"Trashed backup folder '{bf['name']}'")

    report["summary"] = {
        "unknown_folders": len(report["unknown_folders"]),
        "root_files": len(report["root_files"]),
        "empty_folders": len(report["empty_folders"]),
        "backup_folders": len(report["backup_folders"]),
        "dry_run": dry_run,
    }

    return json.dumps(report)


async def validate_categories(
    ctx: Context,
    dry_run: bool = True,
) -> str:
    """Validate and fix document categories by comparing with AI-detected document types.

    Checks each document's category against its structured_metadata.document_type.
    Reports mismatches and optionally corrects them.

    Args:
        dry_run: If True (default), report mismatches without fixing.
                 If False, update categories and move GDrive files.
    """
    db = _get_db(ctx)
    gdrive = _get_gdrive(ctx)

    docs = await db.list_documents(limit=500)
    valid_categories = {c.value for c in DocumentCategory}

    mismatches: list[dict] = []
    corrected: list[dict] = []
    skipped: list[dict] = []

    for doc in docs:
        if not doc.structured_metadata:
            continue

        try:
            meta = json.loads(doc.structured_metadata)
        except (json.JSONDecodeError, TypeError):
            continue

        doc_type = meta.get("document_type")
        if not doc_type:
            continue

        # Map AI document_type to expected category
        expected_category = _DOCTYPE_TO_CATEGORY.get(doc_type)

        # Also check direct match (doc_type might already be a valid category name)
        if not expected_category and doc_type in valid_categories:
            expected_category = doc_type

        if not expected_category:
            continue

        current = doc.category.value

        # Special cases: advocate docs should stay as advocate regardless of content type
        if current == "advocate":
            continue

        # Special case: reference docs should stay as reference
        if current == "reference":
            continue

        if current != expected_category:
            entry = {
                "doc_id": doc.id,
                "filename": doc.filename,
                "current_category": current,
                "ai_document_type": doc_type,
                "suggested_category": expected_category,
                "gdrive_url": _gdrive_url(doc.gdrive_id),
            }

            if not dry_run:
                # Update category in DB
                await db.update_document_category(doc.id, expected_category)

                # Move file in GDrive if available
                if gdrive and doc.gdrive_id:
                    try:
                        folder_id = _get_sync_folder_id(ctx)
                        if folder_id:
                            await _move_doc_to_correct_folder(
                                gdrive, doc, expected_category, folder_id
                            )
                    except Exception:
                        logger.warning(
                            "Failed to move %s in GDrive after category change",
                            doc.filename,
                            exc_info=True,
                        )

                entry["action"] = "corrected"
                corrected.append(entry)
            else:
                entry["action"] = "would_correct"
                mismatches.append(entry)

    # Also detect "other" docs that should be "reference" based on filename patterns
    for doc in docs:
        if doc.category.value != "other":
            continue
        fn = doc.filename.lower()
        if any(
            kw in fn for kw in ("devita", "nccn", "modra_kniha", "modrá_kniha", "esmo", "guideline")
        ):
            entry = {
                "doc_id": doc.id,
                "filename": doc.filename,
                "current_category": "other",
                "ai_document_type": "reference_material",
                "suggested_category": "reference",
                "gdrive_url": _gdrive_url(doc.gdrive_id),
            }

            if not dry_run:
                await db.update_document_category(doc.id, "reference")
                if gdrive and doc.gdrive_id:
                    try:
                        folder_id = _get_sync_folder_id(ctx)
                        if folder_id:
                            await _move_doc_to_correct_folder(gdrive, doc, "reference", folder_id)
                    except Exception:
                        logger.warning("Failed to move %s in GDrive", doc.filename, exc_info=True)
                entry["action"] = "corrected"
                corrected.append(entry)
            else:
                entry["action"] = "would_correct"
                mismatches.append(entry)

    # Detect genetics docs miscategorized as pathology by filename
    for doc in docs:
        if doc.category.value != "pathology":
            continue
        fn = doc.filename.lower()
        if any(kw in fn for kw in ("genetik", "genetic", "kras", "nras", "braf", "msi", "mmr")):
            entry = {
                "doc_id": doc.id,
                "filename": doc.filename,
                "current_category": "pathology",
                "ai_document_type": "genetics",
                "suggested_category": "genetics",
                "gdrive_url": _gdrive_url(doc.gdrive_id),
            }

            if not dry_run:
                await db.update_document_category(doc.id, "genetics")
                if gdrive and doc.gdrive_id:
                    try:
                        folder_id = _get_sync_folder_id(ctx)
                        if folder_id:
                            await _move_doc_to_correct_folder(gdrive, doc, "genetics", folder_id)
                    except Exception:
                        logger.warning("Failed to move %s in GDrive", doc.filename, exc_info=True)
                entry["action"] = "corrected"
                corrected.append(entry)
            else:
                entry["action"] = "would_correct"
                mismatches.append(entry)

    result = {
        "mismatches": mismatches if dry_run else [],
        "corrected": corrected if not dry_run else [],
        "skipped": skipped,
        "summary": {
            "total_docs": len(docs),
            "mismatches_found": len(mismatches) + len(corrected),
            "corrected": len(corrected),
            "dry_run": dry_run,
        },
    }

    return json.dumps(result)


# ── Helper functions ──────────────────────────────────────────────────────────


def _list_root_items(gdrive, folder_id: str) -> list[dict]:
    """List all items (files and folders) directly under a folder."""
    items: list[dict] = []
    page_token = None
    while True:
        response = (
            gdrive._service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        items.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return items


def _count_files_in_folder(gdrive, folder_id: str) -> int:
    """Count non-folder items recursively in a folder."""
    count = 0
    page_token = None
    while True:
        response = (
            gdrive._service.files()
            .list(
                q=f"'{folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, mimeType)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("files", []):
            if item["mimeType"] == "application/vnd.google-apps.folder":
                count += _count_files_in_folder(gdrive, item["id"])
            else:
                count += 1
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return count


def _move_folder_contents(gdrive, source_folder_id: str, target_folder_id: str) -> int:
    """Move all files from one folder to another. Returns count of moved files."""
    moved = 0
    page_token = None
    while True:
        response = (
            gdrive._service.files()
            .list(
                q=f"'{source_folder_id}' in parents and trashed = false",
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=100,
                pageToken=page_token,
            )
            .execute()
        )
        for item in response.get("files", []):
            if item["mimeType"] != "application/vnd.google-apps.folder":
                gdrive.move_file(item["id"], target_folder_id)
                moved += 1
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return moved


async def _move_doc_to_correct_folder(
    gdrive, doc, target_category: str, root_folder_id: str
) -> None:
    """Move a document's GDrive file to the correct category/year-month folder."""
    from oncofiles.gdrive_folders import (
        ensure_folder_structure,
        ensure_year_month_folder,
        get_category_folder_path,
    )

    folder_map = await asyncio.to_thread(ensure_folder_structure, gdrive, root_folder_id)

    cat_name, year_month = get_category_folder_path(
        target_category,
        doc.document_date.isoformat() if doc.document_date else None,
    )
    target_folder = folder_map.get(cat_name, root_folder_id)
    if year_month:
        target_folder = await asyncio.to_thread(
            ensure_year_month_folder, gdrive, target_folder, year_month + "-01"
        )

    await asyncio.to_thread(gdrive.move_file, doc.gdrive_id, target_folder)
    logger.info(
        "Moved %s from %s to %s/%s",
        doc.filename,
        doc.category.value,
        cat_name,
        year_month or "",
    )


def register(mcp):
    mcp.tool()(reconcile_gdrive)
    mcp.tool()(validate_categories)
