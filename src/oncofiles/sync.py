"""Bidirectional Google Drive sync logic (#v1.0).

Supports folder-aware sync with category/year-month structure,
manifest export, and metadata rendering.
"""

from __future__ import annotations

import asyncio
import gc
import io
import json
import logging
import mimetypes
import time
from datetime import UTC, datetime

from oncofiles.database import Database
from oncofiles.enhance import (
    enhance_document_text,
    extract_structured_metadata,
    generate_filename_description,
    infer_institution_from_providers,
)
from oncofiles.filename_parser import (
    is_corrupted_filename,
    is_standard_format,
    parse_filename,
    rename_to_standard,
)
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import GDriveClient
from oncofiles.gdrive_folders import (
    ensure_folder_structure,
    ensure_year_month_folder,
    get_category_folder_path,
)
from oncofiles.manifest import (
    export_manifest,
    group_conversations_by_month,
    render_conversation_month,
    render_manifest_json,
    render_research_library,
    render_treatment_timeline,
)
from oncofiles.models import Document, DocumentCategory, SearchQuery

logger = logging.getLogger(__name__)

# Module-level lock to prevent concurrent sync operations.
# Uses a timestamp-based approach to auto-expire stale locks after 10 minutes.
_sync_lock = asyncio.Lock()
_sync_lock_acquired_at: float = 0.0
_SYNC_LOCK_TIMEOUT = 600  # 10 minutes

# Last sync result — stored so callers can check status after background sync.
_last_sync_result: dict | None = None
_last_sync_error: str | None = None
_last_sync_time: float = 0.0

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SKIP_EXTENSIONS = {".gdoc", ".xlsx", ".xls", ".ds_store"}
GOOGLE_DOCS_MIMETYPES = {
    "application/vnd.google-apps.document",
    "application/vnd.google-apps.spreadsheet",
    "application/vnd.google-apps.presentation",
}


def _should_sync(filename: str) -> bool:
    """Check if a file should be synced based on extension."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in SKIP_EXTENSIONS:
        return False
    return ext in SUPPORTED_EXTENSIONS


# ── GDrive → Oncofiles import ──────────────────────────────────────────────


async def sync_from_gdrive(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient,
    folder_id: str,
    *,
    dry_run: bool = False,
    enhance: bool = True,
) -> dict:
    """Import new/changed files from GDrive into oncofiles.

    Walks category/year-month subfolders. Uses appProperties.oncofiles_id
    for reliable matching, falls back to gdrive_id.

    Returns summary dict: {new, updated, unchanged, skipped, missing, errors}.
    """
    logger.info("sync_from_gdrive: listing folder %s (dry_run=%s)", folder_id, dry_run)
    gdrive_files, folder_map = await asyncio.to_thread(gdrive.list_folder_with_structure, folder_id)
    logger.info(
        "sync_from_gdrive: found %d files, %d folders",
        len(gdrive_files),
        len(folder_map),
    )

    stats = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "missing": 0, "errors": 0}

    # Track which gdrive IDs we've seen (for detecting deletions)
    seen_gdrive_ids: set[str] = set()

    for gf in gdrive_files:
        filename = gf["name"]
        gdrive_id = gf["id"]
        modified_time_str = gf.get("modifiedTime", "")
        mime_type = gf.get("mimeType", "application/octet-stream")
        app_props = gf.get("appProperties", {})

        # Skip non-document files (manifest, markdown metadata, OCR companions)
        if filename.endswith((".json", ".md", "_OCR.txt")):
            stats["skipped"] += 1
            continue

        # Google Docs: export as PDF instead of download
        is_google_doc = mime_type in GOOGLE_DOCS_MIMETYPES

        if not is_google_doc and not _should_sync(filename):
            logger.debug("sync_from_gdrive: skipping %s (unsupported type)", filename)
            stats["skipped"] += 1
            continue

        seen_gdrive_ids.add(gdrive_id)

        try:
            # Try to find existing doc by appProperties or gdrive_id
            existing = None
            oncofiles_id = app_props.get("oncofiles_id")
            if oncofiles_id:
                existing = await db.get_document(int(oncofiles_id))
            if not existing:
                existing = await db.get_document_by_gdrive_id(gdrive_id)

            if existing:
                # Check if modified
                gdrive_modified = _parse_gdrive_time(modified_time_str)
                if existing.gdrive_modified_time and gdrive_modified:
                    gd_naive = gdrive_modified.replace(tzinfo=None)
                    ex_naive = existing.gdrive_modified_time.replace(tzinfo=None)
                    if gd_naive <= ex_naive:
                        stats["unchanged"] += 1
                        continue

                # File changed on GDrive — GDrive wins (re-import)
                if dry_run:
                    logger.info("sync_from_gdrive: WOULD UPDATE %s", filename)
                    stats["updated"] += 1
                    continue

                logger.info("sync_from_gdrive: updating %s (GDrive wins)", filename)
                if is_google_doc:
                    content_bytes = await asyncio.to_thread(
                        gdrive.export_google_doc, gdrive_id, "application/pdf"
                    )
                else:
                    content_bytes = await asyncio.to_thread(gdrive.download, gdrive_id)
                upload_mime = "application/pdf" if is_google_doc else mime_type
                upload_name = filename
                if is_google_doc and "." not in filename:
                    upload_name = f"{filename}.pdf"
                metadata = files.upload(io.BytesIO(content_bytes), upload_name, upload_mime)
                await db.update_document_file_id(existing.id, metadata.id, len(content_bytes))
                del content_bytes  # Free large buffer immediately
                await db.update_gdrive_id(existing.id, gdrive_id, modified_time_str)
                now_str = datetime.now(UTC).isoformat()
                await db.update_sync_state(existing.id, "synced", now_str)

                # Detect category change from folder structure
                detected_category = _detect_category_from_parents(gf, folder_map)
                if detected_category and detected_category != existing.category.value:
                    logger.info(
                        "sync_from_gdrive: category changed %s → %s for %s",
                        existing.category.value,
                        detected_category,
                        filename,
                    )
                    await db.update_document_category(existing.id, detected_category)

                # Re-run AI enhancement
                if enhance:
                    try:
                        await asyncio.wait_for(
                            _enhance_document(db, existing, files, gdrive),
                            timeout=60.0,
                        )
                    except TimeoutError:
                        logger.warning("sync: enhance timed out for doc %d", existing.id)
                        stats["errors"] = stats.get("errors", 0) + 1

                await db.delete_ocr_pages(existing.id)
                stats["updated"] += 1
            else:
                # New file — import
                if dry_run:
                    logger.info("sync_from_gdrive: WOULD IMPORT %s", filename)
                    stats["new"] += 1
                    continue

                logger.info("sync_from_gdrive: importing %s", filename)
                if is_google_doc:
                    content_bytes = await asyncio.to_thread(
                        gdrive.export_google_doc, gdrive_id, "application/pdf"
                    )
                    # Google Docs: export as PDF, fix filename and mime
                    import_filename = f"{filename}.pdf" if "." not in filename else filename
                    import_mime = "application/pdf"
                else:
                    content_bytes = await asyncio.to_thread(gdrive.download, gdrive_id)
                    import_filename = filename
                    import_mime = mime_type
                metadata = files.upload(io.BytesIO(content_bytes), import_filename, import_mime)

                parsed = parse_filename(import_filename)
                guessed_mime = mimetypes.guess_type(import_filename)[0] or import_mime
                gdrive_modified = _parse_gdrive_time(modified_time_str)

                # Try to detect category from folder structure
                detected_category = _detect_category_from_parents(gf, folder_map)
                category = (
                    DocumentCategory(detected_category) if detected_category else parsed.category
                )

                size = len(content_bytes)
                del content_bytes  # Free large buffer immediately

                now_str = datetime.now(UTC).isoformat()
                doc = Document(
                    file_id=metadata.id,
                    filename=import_filename,
                    original_filename=filename,
                    document_date=parsed.document_date,
                    institution=parsed.institution,
                    category=category,
                    description=parsed.description,
                    mime_type=guessed_mime,
                    size_bytes=size,
                    gdrive_id=gdrive_id,
                    gdrive_modified_time=gdrive_modified,
                    sync_state="synced",
                    last_synced_at=datetime.now(UTC),
                )
                doc = await db.insert_document(doc)

                # Set appProperties on GDrive for future matching
                try:
                    await asyncio.to_thread(
                        gdrive.set_app_properties, gdrive_id, {"oncofiles_id": str(doc.id)}
                    )
                except Exception:
                    logger.warning("Failed to set appProperties on %s", gdrive_id)

                # AI enhancement
                if enhance:
                    try:
                        await asyncio.wait_for(
                            _enhance_document(db, doc, files, gdrive),
                            timeout=60.0,
                        )
                    except TimeoutError:
                        logger.warning("sync: enhance timed out for doc %d", doc.id)
                        stats["errors"] = stats.get("errors", 0) + 1

                stats["new"] += 1

        except Exception:
            logger.exception("sync_from_gdrive: error processing %s", filename)
            stats["errors"] += 1

        # Periodic GC to keep memory in check (every 10 documents)
        processed = stats["new"] + stats["updated"] + stats["unchanged"] + stats["errors"]
        if processed > 0 and processed % 10 == 0:
            gc.collect()

    # Detect deleted files (in DB but not on GDrive) — flag only, never auto-delete
    all_docs = await db.list_documents(limit=200)
    for doc in all_docs:
        if doc.gdrive_id and doc.gdrive_id not in seen_gdrive_ids:
            logger.warning(
                "sync_from_gdrive: file %s (gdrive_id=%s) missing from GDrive — flagging",
                doc.filename,
                doc.gdrive_id,
            )
            stats["missing"] += 1

    logger.info("sync_from_gdrive: done — %s", stats)
    return stats


# ── Oncofiles → GDrive export ──────────────────────────────────────────────


async def sync_to_gdrive(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient,
    folder_id: str,
    *,
    dry_run: bool = False,
    full: bool = True,
) -> dict:
    """Export documents from oncofiles to GDrive with folder structure.

    Uploads documents to correct category/YYYY-MM/ folders, sets appProperties,
    and exports manifest + metadata markdown files.

    When full=False (no import changes), skips expensive batch operations
    (organize, rename, OCR cleanup) that scan all docs. Only exports new
    docs without gdrive_id and updates metadata.

    Returns summary dict: {exported, skipped, metadata_exported, errors}.
    """
    logger.info("sync_to_gdrive: starting (dry_run=%s, full=%s)", dry_run, full)

    stats = {"exported": 0, "organized": 0, "skipped": 0, "metadata_exported": 0, "errors": 0}

    if dry_run:
        # Count what would be exported/organized
        docs = await db.list_documents(limit=500)
        for doc in docs:
            if doc.gdrive_id:
                stats["skipped"] += 1
            else:
                stats["exported"] += 1
        logger.info("sync_to_gdrive: dry run — %s", stats)
        return stats

    # Ensure folder structure
    folder_map = await asyncio.to_thread(ensure_folder_structure, gdrive, folder_id)

    # Collect all organized folder IDs (category folders + their year-month subfolders)
    organized_folder_ids = set(folder_map.values())

    # Export documents
    docs = await db.list_documents(limit=500)

    # Phase 1: Batch-organize existing GDrive files (skip if no changes)
    docs_to_organize = [d for d in docs if d.gdrive_id] if full else []
    docs_to_export = [d for d in docs if not d.gdrive_id]

    if docs_to_organize:
        try:
            await asyncio.to_thread(
                _batch_organize_files,
                gdrive,
                docs_to_organize,
                folder_id,
                folder_map,
                organized_folder_ids,
                stats,
            )
        except Exception:
            logger.exception("sync_to_gdrive: batch organize failed, falling back")
            # Fallback to sequential
            for doc in docs_to_organize:
                try:
                    await asyncio.to_thread(
                        _move_to_organized_folder,
                        gdrive,
                        doc,
                        folder_id,
                        folder_map,
                        organized_folder_ids,
                        stats,
                    )
                except Exception:
                    logger.exception("sync_to_gdrive: error organizing %s", doc.filename)
                    stats["errors"] += 1

    # Phase 2: Export new documents
    for doc in docs_to_export:
        try:
            logger.info("sync_to_gdrive: exporting %s", doc.filename)

            # Text files can't be downloaded from Anthropic Files API — use OCR cache
            if doc.mime_type and doc.mime_type.startswith("text/"):
                if await db.has_ocr_text(doc.id):
                    pages = await db.get_ocr_pages(doc.id)
                    text = "\n\n".join(p["extracted_text"] for p in pages)
                    content_bytes = text.encode("utf-8")
                else:
                    logger.warning(
                        "sync_to_gdrive: skipped %s — text file with no OCR cache",
                        doc.filename,
                    )
                    stats["skipped"] += 1
                    continue
            else:
                content_bytes = files.download(doc.file_id)

            # Determine target folder
            cat_name, year_month = get_category_folder_path(
                doc.category.value,
                doc.document_date.isoformat() if doc.document_date else None,
            )
            target_folder = folder_map.get(cat_name, folder_id)
            if year_month:
                target_folder = await asyncio.to_thread(
                    ensure_year_month_folder, gdrive, target_folder, year_month + "-01"
                )

            # Upload with appProperties
            uploaded = await asyncio.to_thread(
                gdrive.upload,
                filename=doc.filename,
                content_bytes=content_bytes,
                mime_type=doc.mime_type,
                folder_id=target_folder,
                app_properties={"oncofiles_id": str(doc.id)},
            )

            del content_bytes  # Free large buffer immediately

            modified_time = uploaded.get("modifiedTime", "")
            await db.update_gdrive_id(doc.id, uploaded["id"], modified_time)
            now_str = datetime.now(UTC).isoformat()
            await db.update_sync_state(doc.id, "synced", now_str)

            stats["exported"] += 1

        except Exception as e:
            err_str = str(e)
            if "not downloadable" in err_str.lower():
                logger.warning(
                    "sync_to_gdrive: skipped %s — not downloadable from Files API",
                    doc.filename,
                )
                stats["skipped"] += 1
            else:
                logger.exception("sync_to_gdrive: error exporting %s", doc.filename)
                stats["errors"] += 1

    # Heavy phases: only run when imports changed (full=True)
    if full:
        # Rename files to standard format (underscore-separated, EN description)
        try:
            rename_stats = await _rename_to_standard(db, gdrive)
            stats["renamed"] = rename_stats["renamed"]
        except Exception as e:
            logger.warning("sync_to_gdrive: standard rename failed — %s", str(e)[:200])
            rename_stats = {"renamed": 0, "renamed_ids": []}

        # Post-rename: immediately organize renamed docs into correct folders
        if rename_stats.get("renamed_ids"):
            renamed_set = set(rename_stats["renamed_ids"])
            renamed_docs = [d for d in docs if d.id in renamed_set]
            # Refresh docs from DB to get updated filenames/dates
            refreshed = []
            for d in renamed_docs:
                try:
                    refreshed.append(await db.get_document(d.id))
                except Exception:
                    refreshed.append(d)
            if refreshed:
                try:
                    await asyncio.to_thread(
                        _batch_organize_files,
                        gdrive,
                        refreshed,
                        folder_id,
                        folder_map,
                        organized_folder_ids,
                        stats,
                    )
                    logger.info(
                        "sync_to_gdrive: post-rename organized %d docs",
                        len(refreshed),
                    )
                except Exception:
                    logger.exception("sync_to_gdrive: post-rename organize failed")

            # Post-rename: re-enhance renamed docs (metadata/FTS reflect new name)
            for doc_id in rename_stats["renamed_ids"]:
                try:
                    doc = await db.get_document(doc_id)
                    await asyncio.wait_for(
                        _enhance_document(db, doc, files, gdrive),
                        timeout=60.0,
                    )
                    logger.info("sync_to_gdrive: post-rename enhanced doc %d", doc_id)
                except TimeoutError:
                    logger.warning(
                        "sync_to_gdrive: post-rename enhance timed out for doc %d",
                        doc_id,
                    )
                except Exception:
                    logger.exception(
                        "sync_to_gdrive: post-rename enhance failed for doc %d",
                        doc_id,
                    )

            # Post-rename: verify full pipeline for each renamed doc
            all_gaps = []
            for doc_id in rename_stats["renamed_ids"]:
                gaps = await _assert_doc_pipeline_complete(
                    db, doc_id, gdrive=gdrive, organized_folder_ids=organized_folder_ids
                )
                all_gaps.extend(gaps)
            if all_gaps:
                stats["pipeline_gaps"] = all_gaps
                logger.warning("sync_to_gdrive: %d pipeline gaps after rename", len(all_gaps))

        # Clean up orphaned OCR files (old names from before bilingual rename)
        try:
            cleanup_stats = await _cleanup_orphan_ocr(db, gdrive)
            stats["ocr_cleaned"] = cleanup_stats["deleted"]
        except Exception as e:
            logger.warning("sync_to_gdrive: OCR cleanup failed — %s", str(e)[:200])

        # Export OCR companion text files alongside originals
        try:
            ocr_stats = await _export_ocr_texts(db, gdrive, files)
            stats["ocr_exported"] = ocr_stats["exported"]
            stats["ocr_extracted"] = ocr_stats.get("extracted", 0)
            stats["ocr_skipped"] = ocr_stats["skipped"]
        except Exception as e:
            logger.warning("sync_to_gdrive: OCR text export failed — %s", str(e)[:200])
    else:
        logger.info("sync_to_gdrive: skipping heavy phases (no import changes)")

    # Export metadata files (may fail with service account — no storage quota)
    try:
        await _export_metadata(db, gdrive, folder_id, folder_map)
        stats["metadata_exported"] += 1
    except Exception as e:
        err_str = str(e)
        logger.warning("sync_to_gdrive: metadata export failed — %s", err_str)
        stats["metadata_error"] = err_str[:200]

    logger.info("sync_to_gdrive: done — %s", stats)
    return stats


def _move_to_organized_folder(
    gdrive: GDriveClient,
    doc: Document,
    root_folder_id: str,
    folder_map: dict[str, str],
    organized_folder_ids: set[str],
    stats: dict,
) -> None:
    """Move a GDrive file into the correct category/year-month folder if needed.

    Checks if the file is already under an organized folder. If not, moves it
    to the correct category/year-month subfolder.
    """
    # Get current parents
    parents = gdrive.get_file_parents(doc.gdrive_id)
    if not parents:
        logger.warning("sync_to_gdrive: cannot get parents for %s — skipping", doc.filename)
        stats["skipped"] += 1
        return

    # Determine target folder
    cat_name, year_month = get_category_folder_path(
        doc.category.value,
        doc.document_date.isoformat() if doc.document_date else None,
    )
    target_folder = folder_map.get(cat_name, root_folder_id)
    if year_month:
        target_folder = ensure_year_month_folder(gdrive, target_folder, year_month + "-01")
        # Track new year-month folder as organized
        organized_folder_ids.add(target_folder)

    # Check if already in the correct target folder
    if any(p == target_folder for p in parents):
        stats["skipped"] += 1
        return

    # Skip if no date and already in any organized folder
    if year_month is None and any(p in organized_folder_ids for p in parents):
        # No date → category folder is fine
        stats["skipped"] += 1
        return

    logger.info(
        "sync_to_gdrive: moving %s to %s/%s",
        doc.filename,
        cat_name,
        year_month or "",
    )
    gdrive.move_file(doc.gdrive_id, target_folder)
    # Verify move succeeded
    new_parents = gdrive.get_file_parents(doc.gdrive_id)
    if new_parents and any(p == target_folder for p in new_parents):
        stats["organized"] += 1
    else:
        logger.error(
            "sync_to_gdrive: move verification FAILED for %s (expected parent %s, got %s)",
            doc.filename,
            target_folder,
            new_parents,
        )
        stats["errors"] = stats.get("errors", 0) + 1


def _batch_organize_files(
    gdrive: GDriveClient,
    docs: list,
    root_folder_id: str,
    folder_map: dict[str, str],
    organized_folder_ids: set[str],
    stats: dict,
) -> None:
    """Batch-organize GDrive files into correct category/year-month folders.

    Uses batch API to fetch all parents at once, then batch-moves files
    that are not yet in organized folders. Falls back to sequential on error.
    """
    # Step 1: Batch-fetch parents for all docs
    file_ids = [d.gdrive_id for d in docs if d.gdrive_id]
    if not file_ids:
        return

    parents_map = gdrive.batch_get_parents(file_ids)

    # Step 2: Determine which docs need moving
    moves: dict[str, tuple[str, str]] = {}  # file_id -> (new_parent, old_parents_csv)
    for doc in docs:
        if not doc.gdrive_id:
            continue
        parents = parents_map.get(doc.gdrive_id, [])
        if not parents:
            stats["skipped"] += 1
            continue
        # Determine target folder
        cat_name, year_month = get_category_folder_path(
            doc.category.value,
            doc.document_date.isoformat() if doc.document_date else None,
        )
        target_folder = folder_map.get(cat_name, root_folder_id)
        if year_month:
            target_folder = ensure_year_month_folder(gdrive, target_folder, year_month + "-01")
            organized_folder_ids.add(target_folder)

        # Skip if already in the correct target folder
        if any(p == target_folder for p in parents):
            stats["skipped"] += 1
            continue
        # Skip if no date and already in any organized folder
        if year_month is None and any(p in organized_folder_ids for p in parents):
            stats["skipped"] += 1
            continue

        old_parents_csv = ",".join(parents)
        moves[doc.gdrive_id] = (target_folder, old_parents_csv)

    if not moves:
        return

    # Step 3: Batch-move all files
    logger.info("sync_to_gdrive: batch-moving %d files to organized folders", len(moves))
    results = gdrive.batch_move(moves)
    for _fid, success in results.items():
        if success:
            stats["organized"] += 1
        else:
            stats["errors"] += 1


async def _assert_doc_pipeline_complete(
    db: Database,
    doc_id: int,
    gdrive: GDriveClient | None = None,
    organized_folder_ids: set[str] | None = None,
) -> list[str]:
    """Check that a document has completed the full pipeline.

    Returns a list of gap descriptions (empty = fully complete).
    Gaps are logged as warnings for monitoring via /status.
    """
    gaps: list[str] = []
    try:
        doc = await db.get_document(doc_id)
    except Exception:
        return [f"doc {doc_id}: not found in DB"]

    if not is_standard_format(doc.filename):
        gaps.append(f"doc {doc_id}: filename not in standard format ({doc.filename})")
    if not doc.gdrive_id:
        gaps.append(f"doc {doc_id}: no gdrive_id (not exported)")
    if not doc.ai_summary:
        gaps.append(f"doc {doc_id}: no AI summary (enhance incomplete)")
    if not await db.has_ocr_text(doc.id):
        gaps.append(f"doc {doc_id}: no OCR text (extraction incomplete)")

    if gdrive and organized_folder_ids and doc.gdrive_id:
        try:
            parents = await asyncio.to_thread(gdrive.get_file_parents, doc.gdrive_id)
            if not any(p in organized_folder_ids for p in parents):
                gaps.append(
                    f"doc {doc_id}: not in organized folder "
                    f"(parents={parents}, filename={doc.filename})"
                )
        except Exception:
            logger.warning("pipeline assert: could not fetch parents for doc %d", doc_id)

    for gap in gaps:
        logger.warning("pipeline gap: %s", gap)
    return gaps


async def _rename_to_standard(db: Database, gdrive: GDriveClient) -> dict:
    """Rename GDrive files to standard format (underscore-separated, EN description).

    For each document: checks if filename is already in standard format.
    If not, renames on GDrive and updates DB filename.
    Stores original_filename before rename for reversibility.

    Returns: {renamed, skipped, errors, renamed_ids}.
    """
    stats: dict = {"renamed": 0, "skipped": 0, "errors": 0, "renamed_ids": []}
    docs = await db.list_documents(limit=500)
    pending_renames: list[tuple] = []

    for doc in docs:
        if not doc.gdrive_id:
            stats["skipped"] += 1
            continue

        # Skip if already in standard format AND filename matches DB metadata
        if is_standard_format(doc.filename):
            # Check if filename institution/category matches DB values
            parts = doc.filename.split("_", 4)  # YYYYMMDD_Patient_Inst_Cat_Desc
            if len(parts) >= 4:
                fn_inst = parts[2]
                fn_cat = parts[3]
                db_inst = doc.institution or "Unknown"
                from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS

                db_cat = CATEGORY_FILENAME_TOKENS.get(doc.category, "Other")
                if fn_inst != db_inst or fn_cat != db_cat:
                    logger.info(
                        "sync_to_gdrive: re-rename %s (fn=%s/%s db=%s/%s)",
                        doc.filename[:40],
                        fn_inst,
                        fn_cat,
                        db_inst,
                        db_cat,
                    )
                else:
                    stats["skipped"] += 1
                    continue
            else:
                stats["skipped"] += 1
                continue

        try:
            # Handle corrupted filenames: use DB metadata instead of parsing
            if is_corrupted_filename(doc.filename):
                from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS
                from oncofiles.patient_context import get_patient_name

                patient = get_patient_name().replace(" ", "") or "ErikaFusekova"
                cat_token = CATEGORY_FILENAME_TOKENS.get(doc.category, "Other")
                # Use document_date or created_at or fallback
                if doc.document_date:
                    date_str = doc.document_date.strftime("%Y%m%d")
                elif doc.created_at:
                    date_str = doc.created_at.strftime("%Y%m%d")
                else:
                    date_str = "20260201"
                inst = doc.institution or "Unknown"
                desc = doc.description or "Document"
                # Clean description for filename
                import re

                desc = re.sub(r"[^a-zA-Z0-9]", "", desc)[:60]
                ext = "." + doc.filename.rsplit(".", 1)[-1] if "." in doc.filename else ".pdf"
                new_name = f"{date_str}_{patient}_{inst}_{cat_token}_{desc}{ext}"
                logger.info(
                    "Fixing corrupted filename doc %d: %d chars → '%s'",
                    doc.id,
                    len(doc.filename),
                    new_name,
                )
            else:
                new_name = rename_to_standard(doc.filename, category=doc.category.value)

            # If rename_to_standard couldn't parse (returned unchanged),
            # build from DB metadata like we do for corrupted filenames
            if new_name == doc.filename:
                from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS
                from oncofiles.patient_context import get_patient_name

                patient = get_patient_name().replace(" ", "") or "ErikaFusekova"
                cat_token = CATEGORY_FILENAME_TOKENS.get(doc.category, "Other")
                if doc.document_date:
                    date_str = doc.document_date.strftime("%Y%m%d")
                elif doc.gdrive_modified_time:
                    date_str = doc.gdrive_modified_time.strftime("%Y%m%d")
                elif doc.created_at:
                    date_str = doc.created_at.strftime("%Y%m%d")
                else:
                    date_str = "20260101"
                inst = doc.institution or "Unknown"
                desc = doc.description or "Document"
                import re

                desc = re.sub(r"[^a-zA-Z0-9]", "", desc)[:60]
                ext = "." + doc.filename.rsplit(".", 1)[-1] if "." in doc.filename else ".pdf"
                new_name = f"{date_str}_{patient}_{inst}_{cat_token}_{desc}{ext}"
                logger.info(
                    "Renaming unparseable doc %d: '%s' → '%s'",
                    doc.id,
                    doc.filename[:40],
                    new_name,
                )

            if new_name == doc.filename:
                stats["skipped"] += 1
                continue

            # Collect rename for batch execution below
            pending_renames.append((doc, new_name))

        except Exception:
            logger.exception("_rename_to_standard: error for doc %d (%s)", doc.id, doc.filename)
            stats["errors"] += 1

    # Batch-rename all collected files on GDrive
    if pending_renames:
        gdrive_renames = {doc.gdrive_id: new_name for doc, new_name in pending_renames}
        rename_results = await asyncio.to_thread(gdrive.batch_rename, gdrive_renames)

        # Also handle OCR companion renames (sequential — rare, small count)
        for doc, new_name in pending_renames:
            if not rename_results.get(doc.gdrive_id, False):
                stats["errors"] += 1
                continue

            old_stem = doc.filename.rsplit(".", 1)[0] if "." in doc.filename else doc.filename
            new_stem = new_name.rsplit(".", 1)[0] if "." in new_name else new_name
            old_ocr_name = f"{old_stem}_OCR.txt"
            new_ocr_name = f"{new_stem}_OCR.txt"
            try:
                parents = await asyncio.to_thread(gdrive.get_file_parents, doc.gdrive_id)
                if parents:
                    siblings = await asyncio.to_thread(
                        gdrive.list_folder, parents[0], recursive=False
                    )
                    for sib in siblings:
                        if sib["name"] == old_ocr_name:
                            await asyncio.to_thread(gdrive.rename_file, sib["id"], new_ocr_name)
                            logger.info("Renamed OCR '%s' → '%s'", old_ocr_name, new_ocr_name)
                            break
            except Exception:
                logger.warning("_rename_to_standard: OCR rename failed for %s", old_ocr_name)

            # Update DB filename (keep original_filename for reversibility)
            await db.update_document_filename(doc.id, new_name)
            logger.info("Renamed '%s' → '%s' (doc %d)", doc.filename, new_name, doc.id)
            stats["renamed"] += 1
            stats["renamed_ids"].append(doc.id)

    logger.info("_rename_to_standard: done — %s", stats)
    return stats


async def _cleanup_orphan_ocr(db: Database, gdrive: GDriveClient) -> dict:
    """Delete orphaned OCR files whose names don't match any current document.

    After bilingual rename, old OCR files (pre-rename names) remain as duplicates.
    This finds _OCR.txt files in document folders and deletes those that don't
    correspond to any current document filename.

    Returns: {deleted, skipped, errors}.
    """
    stats = {"deleted": 0, "skipped": 0, "errors": 0}

    # Build set of expected OCR filenames from current documents
    docs = await db.list_documents(limit=500)
    expected_ocr_names: set[str] = set()
    doc_gdrive_ids: set[str] = set()

    for doc in docs:
        if not doc.gdrive_id:
            continue
        doc_gdrive_ids.add(doc.gdrive_id)
        stem = doc.filename.rsplit(".", 1)[0] if "." in doc.filename else doc.filename
        expected_ocr_names.add(f"{stem}_OCR.txt")

    # For each doc, check siblings for orphaned OCR files
    checked_folders: set[str] = set()
    for doc in docs:
        if not doc.gdrive_id:
            continue

        try:
            parents = await asyncio.to_thread(gdrive.get_file_parents, doc.gdrive_id)
            if not parents:
                continue

            parent_folder = parents[0]
            if parent_folder in checked_folders:
                continue
            checked_folders.add(parent_folder)

            siblings = await asyncio.to_thread(gdrive.list_folder, parent_folder, recursive=False)
            for sib in siblings:
                name = sib["name"]
                if not name.endswith("_OCR.txt"):
                    continue
                if name in expected_ocr_names:
                    stats["skipped"] += 1
                    continue
                # Orphan — trash it (soft delete)
                try:
                    await asyncio.to_thread(gdrive.trash_file, sib["id"])
                    logger.info("_cleanup_orphan_ocr: trashed '%s'", name)
                    stats["deleted"] += 1
                except Exception:
                    logger.warning("_cleanup_orphan_ocr: failed to trash '%s'", name)
                    stats["errors"] += 1

        except Exception:
            logger.exception("_cleanup_orphan_ocr: error checking folder for doc %d", doc.id)
            stats["errors"] += 1

    logger.info("_cleanup_orphan_ocr: done — %s", stats)
    return stats


async def _export_ocr_texts(
    db: Database,
    gdrive: GDriveClient,
    files: FilesClient,
) -> dict:
    """Export OCR text as companion _OCR.txt files alongside originals in GDrive.

    For each document with a gdrive_id:
    1. If OCR text is missing, extract it (PDF native text or Vision OCR)
    2. Create/update a companion {stem}_OCR.txt in the same GDrive folder

    Returns: {exported, extracted, skipped, errors}.
    """
    from oncofiles.ocr import OCR_MODEL, extract_text_from_image
    from oncofiles.tools._helpers import _extract_pdf_text, _resize_image_if_needed

    stats = {"exported": 0, "extracted": 0, "skipped": 0, "errors": 0}

    docs = await db.list_documents(limit=500)
    for doc in docs:
        if not doc.gdrive_id:
            continue

        try:
            # Step 1: Ensure OCR text exists
            if not await db.has_ocr_text(doc.id):
                # Download content and extract text
                content_bytes = None
                try:
                    content_bytes = files.download(doc.file_id)
                except Exception:
                    try:
                        content_bytes = await asyncio.to_thread(gdrive.download, doc.gdrive_id)
                    except Exception:
                        logger.warning(
                            "_export_ocr_texts: cannot download %s — skipping",
                            doc.filename,
                        )
                        stats["skipped"] += 1
                        continue

                # PDF: try native text extraction first
                if doc.mime_type == "application/pdf":
                    pdf_texts = _extract_pdf_text(content_bytes)
                    if pdf_texts:
                        for page_num, text in enumerate(pdf_texts, start=1):
                            await db.save_ocr_page(doc.id, page_num, text, "pymupdf-native")
                        stats["extracted"] += 1
                    else:
                        # Scanned PDF — convert to images and OCR
                        import pymupdf
                        from fastmcp.utilities.types import Image as MImage

                        pdf_doc = pymupdf.open(stream=content_bytes, filetype="pdf")
                        try:
                            for page_num, page in enumerate(pdf_doc, start=1):
                                pix = page.get_pixmap(dpi=200)
                                try:
                                    img = MImage(data=pix.tobytes("jpeg"), format="jpeg")
                                    img = _resize_image_if_needed(img)
                                    text = extract_text_from_image(img, db=db, document_id=doc.id)
                                    await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
                                finally:
                                    del pix  # Free large pixmap buffer
                        finally:
                            pdf_doc.close()
                        stats["extracted"] += 1

                # Image: Vision OCR
                elif doc.mime_type and doc.mime_type.startswith("image/"):
                    from fastmcp.utilities.types import Image as MImage

                    fmt = doc.mime_type.split("/")[1]
                    img = MImage(data=content_bytes, format=fmt)
                    img = _resize_image_if_needed(img)
                    text = extract_text_from_image(img, db=db, document_id=doc.id)
                    await db.save_ocr_page(doc.id, 1, text, OCR_MODEL)
                    stats["extracted"] += 1
                else:
                    stats["skipped"] += 1
                    continue

                # Free downloaded content after extraction
                del content_bytes
                gc.collect()

            # Step 2: Export OCR text to GDrive — single file, faithful word-by-word
            # OCR is the source of truth in the document's original language.
            # No translation — just the raw extraction.
            pages = await db.get_ocr_pages(doc.id)
            text_parts = [p["extracted_text"] for p in pages if p["extracted_text"]]
            if not text_parts:
                stats["skipped"] += 1
                continue

            stem = doc.filename.rsplit(".", 1)[0] if "." in doc.filename else doc.filename

            # Get parent folder of original file
            parents = await asyncio.to_thread(gdrive.get_file_parents, doc.gdrive_id)
            if not parents:
                logger.warning("_export_ocr_texts: no parent folder for %s", doc.filename)
                stats["errors"] += 1
                continue
            parent_folder = parents[0]

            full_text = "\n\n---\n\n".join(text_parts)
            await asyncio.to_thread(
                _upload_or_update_text,
                gdrive,
                f"{stem}_OCR.txt",
                full_text,
                parent_folder,
                "text/plain",
            )
            stats["exported"] += 1

        except Exception:
            logger.exception("_export_ocr_texts: error for doc %d (%s)", doc.id, doc.filename)
            stats["errors"] += 1

    logger.info("_export_ocr_texts: done — %s", stats)
    return stats


async def _export_metadata(
    db: Database,
    gdrive: GDriveClient,
    root_folder_id: str,
    folder_map: dict[str, str],
) -> None:
    """Export manifest.json and metadata markdown files to GDrive.

    Exports EN (primary) and preferred language (secondary) versions of all
    markdown files. If preferred lang is EN, only one file is created.
    """
    from oncofiles.i18n import needs_secondary, preferred_lang

    langs = ["en"]
    if needs_secondary():
        langs.append(preferred_lang())

    # 1. Export _manifest.json to root
    manifest = await export_manifest(db)
    manifest_json = render_manifest_json(manifest)
    await asyncio.to_thread(
        _upload_or_update_text,
        gdrive,
        "_manifest.json",
        manifest_json,
        root_folder_id,
        "application/json",
    )

    # 2. Export conversation monthly logs
    conversations_folder = folder_map.get("conversations")
    if conversations_folder:
        entries = await db.get_conversation_timeline(limit=200)
        by_month = group_conversations_by_month(entries)
        for month_key, month_entries in by_month.items():
            md_content = render_conversation_month(month_entries)
            for lang in langs:
                suffix = f"_{lang.upper()}" if lang != "en" else ""
                filename = f"{month_key}-conversation-log{suffix}.md"
                await asyncio.to_thread(
                    _upload_or_update_text,
                    gdrive,
                    filename,
                    md_content,
                    conversations_folder,
                    "text/markdown",
                )

    # 3. Export treatment timeline
    treatment_folder = folder_map.get("treatment")
    if treatment_folder:
        events = await db.get_treatment_events_timeline(limit=200)
        for lang in langs:
            md_content = render_treatment_timeline(events, lang=lang)
            suffix = f"_{lang.upper()}" if lang != "en" else ""
            filename = f"treatment-timeline{suffix}.md"
            await asyncio.to_thread(
                _upload_or_update_text,
                gdrive,
                filename,
                md_content,
                treatment_folder,
                "text/markdown",
            )

    # 4. Export research library
    research_folder = folder_map.get("research")
    if research_folder:
        entries = await db.list_research_entries(limit=200)
        for lang in langs:
            md_content = render_research_library(entries, lang=lang)
            suffix = f"_{lang.upper()}" if lang != "en" else ""
            filename = f"research-library{suffix}.md"
            await asyncio.to_thread(
                _upload_or_update_text,
                gdrive,
                filename,
                md_content,
                research_folder,
                "text/markdown",
            )


def _upload_or_update_text(
    gdrive: GDriveClient,
    filename: str,
    content: str,
    folder_id: str,
    mime_type: str,
) -> None:
    """Upload a text file, or update it if it already exists in the folder."""
    content_bytes = content.encode("utf-8")
    # Search for existing file
    existing_files = gdrive.list_folder(folder_id, recursive=False)
    for f in existing_files:
        if f["name"] == filename:
            gdrive.update(f["id"], content_bytes, mime_type)
            return
    gdrive.upload(
        filename=filename,
        content_bytes=content_bytes,
        mime_type=mime_type,
        folder_id=folder_id,
    )


# ── Unified bidirectional sync ────────────────────────────────────────────


async def sync(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient,
    folder_id: str,
    *,
    dry_run: bool = False,
    enhance: bool = True,
    trigger: str = "manual",
) -> dict:
    """Run full bidirectional sync.

    1. sync_from_gdrive first (import human changes — GDrive wins)
    2. sync_to_gdrive second (export system changes)

    Returns combined stats. Uses a module-level lock to prevent concurrent execution.
    """
    global _sync_lock_acquired_at  # noqa: PLW0603

    if _sync_lock.locked():
        elapsed = time.monotonic() - _sync_lock_acquired_at if _sync_lock_acquired_at > 0 else 0.0
        if elapsed < _SYNC_LOCK_TIMEOUT:
            logger.info("sync: already in progress (%.0fs) — skipping", elapsed)
            return {"skipped": True, "message": "Sync already in progress"}
        # Stale lock — force release so we can re-acquire
        logger.warning(
            "sync: stale lock detected (%.0fs > %ds) — force releasing",
            elapsed,
            _SYNC_LOCK_TIMEOUT,
        )
        _sync_lock.release()

    async with _sync_lock:
        _sync_lock_acquired_at = time.monotonic()
        try:
            return await _sync_inner(
                db,
                files,
                gdrive,
                folder_id,
                dry_run=dry_run,
                enhance=enhance,
                trigger=trigger,
            )
        except Exception:
            global _last_sync_error  # noqa: PLW0603
            _last_sync_error = "Sync failed — check server logs"
            raise
        finally:
            _sync_lock_acquired_at = 0.0


async def _sync_inner(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient,
    folder_id: str,
    *,
    dry_run: bool = False,
    enhance: bool = True,
    trigger: str = "manual",
) -> dict:
    """Inner sync logic (called under lock)."""
    global _last_sync_result, _last_sync_error, _last_sync_time  # noqa: PLW0603

    logger.info("sync: starting bidirectional sync (dry_run=%s)", dry_run)
    _last_sync_error = None

    # Record sync start in history (skip for dry runs)
    sync_id = None
    start_mono = time.monotonic()
    if not dry_run:
        try:
            sync_id = await db.insert_sync_history(trigger=trigger)
        except Exception:
            logger.warning("sync: failed to record sync start", exc_info=True)

    try:
        from_stats = await sync_from_gdrive(
            db, files, gdrive, folder_id, dry_run=dry_run, enhance=enhance
        )
        # Skip heavy export phases if nothing changed during import
        # AND all docs are already standard-named (no pending renames)
        has_changes = from_stats.get("new", 0) > 0 or from_stats.get("updated", 0) > 0
        if not has_changes and not dry_run:
            # Check if any docs still need renaming (e.g., after backfill added dates)
            all_docs = await db.list_documents(limit=500)
            needs_rename = any(not is_standard_format(d.filename) for d in all_docs if d.gdrive_id)
            if needs_rename:
                has_changes = True
                logger.info(
                    "sync: forcing full export — %d docs need rename",
                    sum(1 for d in all_docs if d.gdrive_id and not is_standard_format(d.filename)),
                )
        to_stats = await sync_to_gdrive(
            db, files, gdrive, folder_id, dry_run=dry_run, full=has_changes
        )

        combined = {
            "from_gdrive": from_stats,
            "to_gdrive": to_stats,
        }
        _last_sync_result = combined
        _last_sync_time = time.monotonic()

        # Record sync completion
        if sync_id is not None:
            duration = time.monotonic() - start_mono
            try:
                await db.complete_sync_history(
                    sync_id,
                    status="completed",
                    duration_s=round(duration, 1),
                    from_new=from_stats.get("new", 0),
                    from_updated=from_stats.get("updated", 0),
                    from_errors=from_stats.get("errors", 0),
                    to_exported=to_stats.get("exported", 0),
                    to_organized=to_stats.get("organized", 0),
                    to_renamed=to_stats.get("renamed", 0),
                    to_errors=to_stats.get("errors", 0),
                    stats_json=json.dumps(combined, ensure_ascii=False),
                )
            except Exception:
                logger.warning("sync: failed to record sync completion", exc_info=True)

        logger.info("sync: done — %s", combined)
        return combined

    except Exception as exc:
        # Record sync failure
        if sync_id is not None:
            duration = time.monotonic() - start_mono
            try:
                await db.complete_sync_history(
                    sync_id,
                    status="failed",
                    duration_s=round(duration, 1),
                    error_message=str(exc)[:500],
                )
            except Exception:
                logger.warning("sync: failed to record sync failure", exc_info=True)
        raise


def get_sync_status() -> dict:
    """Return current sync status (running/idle) and last result."""
    running = _sync_lock.locked()
    elapsed = time.monotonic() - _sync_lock_acquired_at if _sync_lock_acquired_at > 0 else 0.0

    status: dict = {"running": running}
    if running:
        status["elapsed_s"] = round(elapsed, 1)

    if _last_sync_result is not None:
        status["last_result"] = _last_sync_result
        age = time.monotonic() - _last_sync_time if _last_sync_time > 0 else 0.0
        status["last_sync_age_s"] = round(age, 1)

    if _last_sync_error is not None:
        status["last_error"] = _last_sync_error

    return status


# ── AI enhancement helper ──────────────────────────────────────────────────


async def enhance_documents(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None = None,
    document_ids: list[int] | None = None,
) -> dict:
    """Run AI enhancement on documents.

    If document_ids is None, processes all documents without AI metadata.
    Returns summary dict: {processed, skipped, errors}.
    """
    if document_ids:
        docs = []
        for doc_id in document_ids:
            doc = await db.get_document(doc_id)
            if doc:
                docs.append(doc)
    else:
        docs = await db.get_documents_without_ai()

    logger.info("enhance_documents: %d documents to process", len(docs))
    stats = {"processed": 0, "skipped": 0, "errors": 0}

    for doc in docs:
        try:
            enhanced = await asyncio.wait_for(
                _enhance_document(db, doc, files, gdrive),
                timeout=60.0,
            )
            if enhanced:
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
        except TimeoutError:
            logger.warning(
                "enhance_documents: doc %d (%s) timed out after 60s — skipping",
                doc.id,
                doc.filename,
            )
            stats["errors"] += 1
        except Exception:
            logger.exception("enhance_documents: error on doc %d (%s)", doc.id, doc.filename)
            stats["errors"] += 1

    logger.info("enhance_documents: done — %s", stats)
    return stats


async def _generate_cross_references(db: Database, doc: Document, metadata: dict) -> int:
    """Generate cross-references between a document and related documents.

    Uses heuristic matching:
    - same_visit: same date + same institution (confidence 1.0)
    - related: same date within 3 days (confidence 0.7)
    - related: shared diagnoses (confidence 0.8)

    Returns count of new cross-references inserted.
    """
    refs: list[tuple[int, int, str, float]] = []

    # Match by same date + institution (same visit)
    if doc.document_date and doc.institution:
        candidates = await db.search_documents(
            SearchQuery(
                institution=doc.institution,
                date_from=doc.document_date,
                date_to=doc.document_date,
                limit=20,
            )
        )
        for c in candidates:
            if c.id != doc.id and c.deleted_at is None:
                refs.append((doc.id, c.id, "same_visit", 1.0))

    # Match by date proximity (within 3 days)
    if doc.document_date:
        from datetime import timedelta

        date_from = doc.document_date - timedelta(days=3)
        date_to = doc.document_date + timedelta(days=3)
        nearby = await db.search_documents(
            SearchQuery(date_from=date_from, date_to=date_to, limit=20)
        )
        for c in nearby:
            if c.id != doc.id and c.deleted_at is None and c.document_date != doc.document_date:
                refs.append((doc.id, c.id, "related", 0.7))

    if refs:
        return await db.bulk_insert_cross_references(refs)
    return 0


async def extract_all_metadata(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None = None,
) -> dict:
    """Backfill structured_metadata for documents that have AI summaries but no metadata.

    Returns summary dict: {processed, skipped, errors}.
    """
    docs = await db.get_documents_without_metadata()
    docs = docs[:5]  # Process max 5 per run to limit memory
    logger.info("extract_all_metadata: %d documents to process", len(docs))
    stats = {"processed": 0, "skipped": 0, "errors": 0}

    for doc in docs:
        try:
            # Skip documents larger than 10MB to avoid OOM
            if doc.size_bytes and doc.size_bytes > 10_000_000:
                logger.warning(
                    "extract_all_metadata: doc %d too large (%d bytes) — skipping",
                    doc.id,
                    doc.size_bytes,
                )
                stats["skipped"] += 1
                continue

            # Get text from OCR cache first
            text_parts = []
            if await db.has_ocr_text(doc.id):
                pages = await db.get_ocr_pages(doc.id)
                text_parts = [p["extracted_text"] for p in pages]

            # Fall back to downloading and extracting text
            content_bytes = None
            if not text_parts:
                import contextlib

                from oncofiles.tools._helpers import _extract_pdf_text

                try:
                    content_bytes = files.download(doc.file_id)
                except Exception:
                    if gdrive and doc.gdrive_id:
                        with contextlib.suppress(Exception):
                            content_bytes = await asyncio.to_thread(gdrive.download, doc.gdrive_id)

                if content_bytes and doc.mime_type == "application/pdf":
                    try:
                        pdf_texts = _extract_pdf_text(content_bytes)
                    except Exception:
                        pdf_texts = None
                    if pdf_texts:
                        for page_num, text in enumerate(pdf_texts, start=1):
                            await db.save_ocr_page(doc.id, page_num, text, "pymupdf-native")
                        text_parts = pdf_texts

            if not text_parts:
                logger.warning(
                    "extract_all_metadata: no text for doc %d (%s) — skipping",
                    doc.id,
                    doc.filename,
                )
                stats["skipped"] += 1
                continue

            full_text = "\n\n".join(text_parts)
            metadata = extract_structured_metadata(full_text, db=db, document_id=doc.id)
            await db.update_structured_metadata(doc.id, json.dumps(metadata, ensure_ascii=False))
            logger.info(
                "extract_all_metadata: doc %d (%s) — metadata extracted",
                doc.id,
                doc.filename,
            )
            stats["processed"] += 1

            # Generate cross-references based on heuristic matching
            await _generate_cross_references(db, doc, metadata)

            # Free memory between documents
            del full_text, text_parts
            if content_bytes is not None:
                del content_bytes
        except Exception:
            logger.exception("extract_all_metadata: error on doc %d (%s)", doc.id, doc.filename)
            stats["errors"] += 1

    logger.info("extract_all_metadata: done — %s", stats)
    return stats


async def _enhance_document(
    db: Database,
    doc: Document,
    files: FilesClient,
    gdrive: GDriveClient | None = None,
) -> bool:
    """Run AI enhancement on a single document. Returns True if enhanced."""
    # Get text from OCR cache
    text_parts = []
    if await db.has_ocr_text(doc.id):
        pages = await db.get_ocr_pages(doc.id)
        text_parts = [p["extracted_text"] for p in pages]
    else:
        # Try to get text by downloading the document
        import contextlib

        from oncofiles.server import _extract_pdf_text

        content_bytes = None
        try:
            content_bytes = files.download(doc.file_id)
        except Exception:
            if gdrive and doc.gdrive_id:
                with contextlib.suppress(Exception):
                    content_bytes = await asyncio.to_thread(gdrive.download, doc.gdrive_id)
                # Google Docs fallback: export as PDF
                if not content_bytes:
                    with contextlib.suppress(Exception):
                        content_bytes = await asyncio.to_thread(
                            gdrive.export_google_doc, doc.gdrive_id, "application/pdf"
                        )

        if content_bytes and doc.mime_type == "application/pdf":
            try:
                pdf_texts = _extract_pdf_text(content_bytes)
            except Exception:
                pdf_texts = None
            if pdf_texts:
                for page_num, text in enumerate(pdf_texts, start=1):
                    await db.save_ocr_page(doc.id, page_num, text, "pymupdf-native")
                text_parts = pdf_texts
            else:
                # Scanned PDF: fall back to Vision OCR
                import fitz
                from fastmcp.utilities.types import Image as MImage

                from oncofiles.ocr import OCR_MODEL, extract_text_from_image
                from oncofiles.tools._helpers import _resize_image_if_needed

                try:
                    pdf_doc = fitz.open(stream=content_bytes, filetype="pdf")
                    for page_num, page in enumerate(pdf_doc, start=1):
                        pix = page.get_pixmap(dpi=200)
                        try:
                            img = MImage(data=pix.tobytes("jpeg"), format="jpeg")
                            img = _resize_image_if_needed(img)
                            text = extract_text_from_image(img, db=db, document_id=doc.id)
                            await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
                            text_parts.append(text)
                        finally:
                            del pix
                    pdf_doc.close()
                    if text_parts:
                        logger.info(
                            "enhance: Vision OCR for doc %d (%d pages)",
                            doc.id,
                            len(text_parts),
                        )
                except Exception:
                    logger.warning("enhance: Vision OCR failed for doc %d", doc.id, exc_info=True)
        elif content_bytes and doc.mime_type and doc.mime_type.startswith("text/"):
            try:
                text_content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text_content = content_bytes.decode("latin-1")
            if text_content.strip():
                await db.save_ocr_page(doc.id, 1, text_content, "text-decode")
                text_parts = [text_content]

    if not text_parts:
        logger.warning("enhance: no text available for doc %d (%s)", doc.id, doc.filename)
        return False

    full_text = "\n\n".join(text_parts)
    summary, tags_json = enhance_document_text(full_text, db=db, document_id=doc.id)
    await db.update_document_ai_metadata(doc.id, summary, tags_json)

    # Extract structured metadata (diagnoses, medications, findings, etc.)
    metadata = None
    try:
        metadata = extract_structured_metadata(full_text, db=db, document_id=doc.id)
        await db.update_structured_metadata(doc.id, json.dumps(metadata, ensure_ascii=False))
        logger.info("enhance: doc %d (%s) — structured metadata extracted", doc.id, doc.filename)
    except Exception:
        logger.warning(
            "enhance: doc %d (%s) — structured metadata extraction failed",
            doc.id,
            doc.filename,
        )

    # ── Backfill null top-level fields from structured metadata ──────────
    if metadata:
        backfill_date = None
        backfill_institution = None
        backfill_description = None

        # Date: prefer filename YYYYMMDD prefix, then AI dates, then GDrive/created time
        if not doc.document_date:
            # Trust filename date prefix first (avoids AI extracting DOB as document date)
            import re as _re

            fn_match = _re.match(r"^(\d{4})(\d{2})(\d{2})_", doc.filename)
            if fn_match:
                y, m, d = fn_match.groups()
                fn_date = f"{y}-{m}-{d}"
                # Sanity check: year should be 2020-2030 range
                if 2020 <= int(y) <= 2030:
                    backfill_date = fn_date
                    logger.info(
                        "enhance: doc %d — backfill date=%s (from filename)", doc.id, backfill_date
                    )
            if not backfill_date:
                dates = metadata.get("dates_mentioned", [])
                if dates:
                    # Filter out obvious DOB dates (before 2020)
                    valid_dates = [d for d in dates if d >= "2020-"]
                    if valid_dates:
                        backfill_date = valid_dates[0]
                        logger.info(
                            "enhance: doc %d — backfill date=%s (from AI)", doc.id, backfill_date
                        )
            elif doc.gdrive_modified_time:
                backfill_date = doc.gdrive_modified_time.strftime("%Y-%m-%d")
                logger.info("enhance: doc %d — backfill date=%s (GDrive)", doc.id, backfill_date)
            elif doc.created_at:
                backfill_date = doc.created_at.strftime("%Y-%m-%d")
                logger.info(
                    "enhance: doc %d — backfill date=%s (created_at)", doc.id, backfill_date
                )

        # Institution: map providers to known institution codes
        if not doc.institution:
            providers = metadata.get("providers", [])
            inst = infer_institution_from_providers(providers)
            if inst:
                backfill_institution = inst
                logger.info("enhance: doc %d — backfill institution=%s", doc.id, inst)

        # Description: generate English CamelCase description if filename is non-standard
        if not is_standard_format(doc.filename):
            try:
                desc = generate_filename_description(full_text, db=db, document_id=doc.id)
                if desc:
                    backfill_description = desc
                    logger.info("enhance: doc %d — backfill description=%s", doc.id, desc)
            except Exception:
                logger.warning("enhance: doc %d — description generation failed", doc.id)

        if backfill_date or backfill_institution or backfill_description:
            await db.backfill_document_fields(
                doc.id,
                document_date=backfill_date,
                institution=backfill_institution,
                description=backfill_description,
                force_description=not is_standard_format(doc.filename),
            )
            logger.info(
                "enhance: doc %d — backfilled fields (date=%s, inst=%s, desc=%s)",
                doc.id,
                backfill_date,
                backfill_institution,
                backfill_description is not None,
            )

    logger.info(
        "enhance: doc %d (%s) — summary=%d chars, tags=%s",
        doc.id,
        doc.filename,
        len(summary),
        tags_json,
    )
    return True


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_gdrive_time(time_str: str) -> datetime | None:
    """Parse GDrive modifiedTime (ISO 8601 with Z suffix)."""
    if not time_str:
        return None
    try:
        return datetime.fromisoformat(time_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def _detect_category_from_parents(file_info: dict, folder_map: dict[str, str]) -> str | None:
    """Detect document category from its parent folder name in GDrive.

    Handles both legacy EN-only names ('labs') and bilingual names
    ('labs — laboratórne výsledky').

    Returns category string if parent folder matches a known category, else None.
    """
    from oncofiles.gdrive_folders import en_key_from_folder_name

    parents = file_info.get("parents", [])
    valid_categories = {cat.value for cat in DocumentCategory}

    for parent_id in parents:
        folder_name = folder_map.get(parent_id, "")
        # Direct match (legacy EN-only)
        if folder_name in valid_categories:
            return folder_name
        # Bilingual name: extract EN key
        en_key = en_key_from_folder_name(folder_name)
        if en_key and en_key in valid_categories:
            return en_key
    return None
