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


async def qa_analysis(
    ctx: Context,
    days: int = 7,
) -> str:
    """Analyze activity logs for errors, slow tools, and improvement opportunities.

    Scans the audit trail for patterns: recurring errors, slow operations,
    failed storage, and usage trends. Returns actionable findings that can
    be used to create GitHub improvement issues.

    Args:
        days: Number of days to analyze (default 7).
    """
    from datetime import date, timedelta

    db = _get_db(ctx)

    date_from = date.today() - timedelta(days=days)
    date_to = date.today()

    # Get stats
    stats = await db.get_activity_stats(date_from=date_from, date_to=date_to)

    # Get errors
    from oncofiles.models import ActivityLogQuery

    error_query = ActivityLogQuery(
        status="error",
        date_from=date_from,
        date_to=date_to,
        limit=50,
    )
    errors = await db.search_activity_log(error_query)

    # Get slow operations (>10s)
    timeout_query = ActivityLogQuery(
        status="timeout",
        date_from=date_from,
        date_to=date_to,
        limit=50,
    )
    timeouts = await db.search_activity_log(timeout_query)

    # Analyze patterns
    findings: list[dict] = []

    # 1. Error patterns
    error_tools: dict[str, int] = {}
    error_messages: dict[str, int] = {}
    for e in errors:
        error_tools[e.tool_name] = error_tools.get(e.tool_name, 0) + 1
        if e.error_message:
            msg = e.error_message[:100]
            error_messages[msg] = error_messages.get(msg, 0) + 1

    for tool, count in sorted(error_tools.items(), key=lambda x: -x[1]):
        if count >= 2:
            findings.append(
                {
                    "type": "recurring_error",
                    "severity": "high" if count >= 5 else "medium",
                    "tool": tool,
                    "count": count,
                    "suggestion": f"Investigate {tool} — {count} errors in {days} days",
                }
            )

    # 2. Timeout patterns
    if timeouts:
        findings.append(
            {
                "type": "timeouts",
                "severity": "medium",
                "count": len(timeouts),
                "tools": list({t.tool_name for t in timeouts}),
                "suggestion": "Operations timing out — check server resources or optimize",
            }
        )

    # 3. Slow tools (avg >5s)
    for s in stats:
        avg_ms = s.get("avg_duration_ms", 0)
        if avg_ms and avg_ms > 5000 and s.get("count", 0) >= 3:
            findings.append(
                {
                    "type": "slow_tool",
                    "severity": "low",
                    "tool": s["tool_name"],
                    "avg_ms": round(avg_ms),
                    "count": s["count"],
                    "suggestion": (
                        f"{s['tool_name']} averages {avg_ms / 1000:.1f}s — consider optimization"
                    ),
                }
            )

    # 4. Usage summary
    total_calls = sum(s.get("count", 0) for s in stats)
    total_errors = sum(s.get("count", 0) for s in stats if s.get("status") == "error")
    error_rate = (total_errors / total_calls * 100) if total_calls > 0 else 0

    result = {
        "period": f"{date_from.isoformat()} to {date_to.isoformat()}",
        "summary": {
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate_pct": round(error_rate, 1),
            "total_timeouts": len(timeouts),
            "unique_tools": len(stats),
        },
        "findings": findings,
        "top_tools": sorted(stats, key=lambda s: -s.get("count", 0))[:10],
    }

    return json.dumps(result)


async def system_health(ctx: Context) -> str:
    """Get system health overview: sync history, document counts, resource usage, and errors.

    Returns a comprehensive status report useful for monitoring and debugging.
    Includes 7-day sync statistics, recent sync runs, memory usage, and document counts.
    """
    import resource
    import sys

    from oncofiles.config import VERSION
    from oncofiles.sync import get_sync_status

    db = _get_db(ctx)

    # Document stats
    doc_count = await db.count_documents()
    unprocessed = await db.get_documents_without_ai()

    # Sync stats
    sync_stats = await db.get_sync_stats_summary()
    recent_syncs = await db.get_sync_history(limit=10)
    current_sync = get_sync_status()

    # Memory
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        rss_mb = rusage.ru_maxrss / (1024 * 1024)
    else:
        rss_mb = rusage.ru_maxrss / 1024

    # Uptime
    started_at = ctx.request_context.lifespan_context.get("started_at")
    uptime_s = None
    if started_at:
        from datetime import UTC, datetime

        uptime_s = int((datetime.now(UTC) - started_at).total_seconds())

    result = {
        "version": VERSION,
        "uptime_s": uptime_s,
        "memory_rss_mb": round(rss_mb, 1),
        "documents": {
            "total": doc_count,
            "unenhanced": len(unprocessed),
        },
        "sync_current": current_sync,
        "sync_7d_summary": {
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
                "error_message": s.get("error_message"),
            }
            for s in recent_syncs
        ],
    }

    return json.dumps(result)


async def _build_document_matrix(db, filter_param: str = "all", limit: int = 200) -> dict:
    """Build document status matrix — shared by MCP tool and HTTP API.

    Returns: {"filter", "matched", "summary", "documents": [...]}
    """
    from oncofiles.filename_parser import is_standard_format

    limit = min(limit, 200)

    docs = await db.list_documents(limit=500)
    ocr_ids = await db.get_ocr_document_ids()
    rows = []

    # Pre-compute per-doc status flags for both rows and summary
    doc_statuses = []
    for doc in docs:
        status = {
            "doc": doc,
            "has_ocr": doc.id in ocr_ids,
            "has_ai": doc.ai_summary is not None,
            "has_metadata": bool(doc.structured_metadata),
            "is_synced": doc.gdrive_id is not None,
            "is_standard": is_standard_format(doc.filename),
            "has_date": doc.document_date is not None,
            "has_institution": doc.institution is not None,
        }
        status["fully_complete"] = all(
            [
                status["has_ocr"],
                status["has_ai"],
                status["has_metadata"],
                status["is_synced"],
                status["is_standard"],
                status["has_date"],
                status["has_institution"],
            ]
        )
        doc_statuses.append(status)

    for s in doc_statuses:
        doc = s["doc"]

        # Apply filter — skip documents that don't match
        skip = (
            (filter_param == "missing_ocr" and s["has_ocr"])
            or (filter_param == "missing_ai" and s["has_ai"])
            or (filter_param == "missing_metadata" and s["has_metadata"])
            or (filter_param == "not_synced" and s["is_synced"])
            or (filter_param == "not_renamed" and s["is_standard"])
            or (filter_param == "incomplete" and s["fully_complete"])
        )
        if skip:
            continue

        rows.append(
            {
                "id": doc.id,
                "filename": doc.filename[:60],
                "category": doc.category.value,
                "date": str(doc.document_date) if doc.document_date else None,
                "institution": doc.institution,
                "gdrive_id": doc.gdrive_id,
                "has_ocr": s["has_ocr"],
                "has_ai": s["has_ai"],
                "has_metadata": s["has_metadata"],
                "has_date": s["has_date"],
                "has_institution": s["has_institution"],
                "is_synced": s["is_synced"],
                "is_standard_name": s["is_standard"],
                "fully_complete": s["fully_complete"],
            }
        )
        if len(rows) >= limit:
            break

    # Summary counts (reuse pre-computed flags)
    total = len(doc_statuses)
    summary = {
        "total": total,
        "with_ocr": sum(1 for s in doc_statuses if s["has_ocr"]),
        "with_ai": sum(1 for s in doc_statuses if s["has_ai"]),
        "with_metadata": sum(1 for s in doc_statuses if s["has_metadata"]),
        "synced": sum(1 for s in doc_statuses if s["is_synced"]),
        "standard_named": sum(1 for s in doc_statuses if s["is_standard"]),
        "with_date": sum(1 for s in doc_statuses if s["has_date"]),
        "with_institution": sum(1 for s in doc_statuses if s["has_institution"]),
        "fully_complete": sum(1 for s in doc_statuses if s["fully_complete"]),
    }

    return {
        "filter": filter_param,
        "matched": len(rows),
        "summary": summary,
        "documents": rows,
    }


async def get_document_status_matrix(
    ctx: Context,
    filter: str = "all",
    limit: int = 100,
) -> str:
    """Get per-document status matrix showing OCR, AI, metadata, sync, and rename state.

    Returns a table of documents with their processing status at each pipeline stage.
    Use filters to find documents that need attention.

    Args:
        filter: Filter documents — 'all', 'missing_ocr', 'missing_ai', 'missing_metadata',
                'not_synced', 'not_renamed', 'incomplete' (any gap).
        limit: Maximum documents to return (max 200).
    """
    db = _get_db(ctx)
    result = await _build_document_matrix(db, filter_param=filter, limit=limit)
    return json.dumps(result)


async def get_pipeline_status(ctx: Context) -> str:
    """Get pipeline operations status: scheduled jobs, stage counts, and sync history.

    Shows which automated processes run, their schedule, last results,
    and how many documents are at each pipeline stage (OCR → AI → metadata → sync → rename).
    """
    import resource
    import sys

    from oncofiles.config import SYNC_INTERVAL_MINUTES, VERSION
    from oncofiles.filename_parser import is_standard_format
    from oncofiles.sync import get_sync_status

    db = _get_db(ctx)

    # Pipeline stage counts
    docs = await db.list_documents(limit=500)
    total = len(docs)
    with_ai = sum(1 for d in docs if d.ai_summary)
    with_metadata = sum(1 for d in docs if d.structured_metadata and d.structured_metadata != "")
    synced = sum(1 for d in docs if d.gdrive_id)
    standard = sum(1 for d in docs if is_standard_format(d.filename))
    with_date = sum(1 for d in docs if d.document_date)
    with_institution = sum(1 for d in docs if d.institution)

    # Sync state
    sync_stats = await db.get_sync_stats_summary()
    recent_syncs = await db.get_sync_history(limit=5)
    current_sync = get_sync_status()

    # Memory
    rusage = resource.getrusage(resource.RUSAGE_SELF)
    if sys.platform == "darwin":
        rss_mb = rusage.ru_maxrss / (1024 * 1024)
    else:
        rss_mb = rusage.ru_maxrss / 1024

    result = {
        "version": VERSION,
        "memory_rss_mb": round(rss_mb, 1),
        "pipeline_stages": {
            "total_documents": total,
            "with_ai_summary": with_ai,
            "with_structured_metadata": with_metadata,
            "with_date": with_date,
            "with_institution": with_institution,
            "synced_to_gdrive": synced,
            "standard_filename": standard,
            "fully_complete": sum(
                1
                for d in docs
                if d.ai_summary
                and d.structured_metadata
                and d.gdrive_id
                and d.document_date
                and d.institution
                and is_standard_format(d.filename)
            ),
        },
        "scheduled_jobs": [
            {
                "name": "startup_catchup",
                "trigger": "60s after boot",
                "description": "Import-only sync + category validation",
            },
            {
                "name": "gdrive_sync",
                "trigger": f"every {SYNC_INTERVAL_MINUTES} min",
                "description": "Full bidirectional sync (incremental export if no changes)",
            },
            {
                "name": "trash_cleanup",
                "trigger": "daily 3:00 AM",
                "description": "Purge soft-deleted docs older than 30 days",
            },
            {
                "name": "metadata_extraction",
                "trigger": "daily 3:30 AM",
                "description": "Backfill structured metadata (max 5 docs)",
            },
            {
                "name": "category_validation",
                "trigger": "daily 3:45 AM",
                "description": "Auto-correct categories from AI document_type",
            },
            {
                "name": "oauth_cleanup",
                "trigger": "daily 4:00 AM",
                "description": "Remove expired MCP OAuth tokens",
            },
            {
                "name": "rss_monitor",
                "trigger": "every 6h",
                "description": "Log RSS memory usage",
            },
        ],
        "sync_current": current_sync,
        "sync_7d": {
            "total": sync_stats.get("total_syncs", 0),
            "successful": sync_stats.get("successful", 0),
            "failed": sync_stats.get("failed", 0),
            "avg_duration_s": sync_stats.get("avg_duration_s"),
        },
        "recent_syncs": [
            {
                "started_at": s.get("started_at"),
                "trigger": s.get("sync_trigger"),
                "status": s.get("status"),
                "duration_s": s.get("duration_s"),
            }
            for s in recent_syncs
        ],
    }

    return json.dumps(result)


async def list_tool_definitions(ctx: Context) -> str:
    """List all registered MCP tools with their descriptions and parameter schemas.

    Returns the complete tool inventory for discovery and documentation.
    Useful for agents to understand available capabilities.
    """
    mcp_server = ctx.fastmcp
    tools = await mcp_server.list_tools()

    tool_list = []
    for t in sorted(tools, key=lambda x: x.name):
        entry = {
            "name": t.name,
            "description": (t.description or "")[:200],
        }
        if hasattr(t, "parameters") and t.parameters:
            params = t.parameters
            if isinstance(params, dict):
                props = params.get("properties", {})
                entry["parameters"] = list(props.keys()) if props else []
            else:
                entry["parameters"] = []
        tool_list.append(entry)

    return json.dumps(
        {
            "total_tools": len(tool_list),
            "tools": tool_list,
        }
    )


def register(mcp):
    mcp.tool()(reconcile_gdrive)
    mcp.tool()(validate_categories)
    mcp.tool()(qa_analysis)
    mcp.tool()(system_health)
    mcp.tool()(get_document_status_matrix)
    mcp.tool()(get_pipeline_status)
    mcp.tool()(list_tool_definitions)
