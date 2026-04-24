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
    resolve_category_folder,
)
from oncofiles.manifest import (
    export_manifest,
    group_conversations_by_month,
    render_conversation_month,
    render_manifest_json,
    render_research_library,
    render_treatment_timeline,
)
from oncofiles.memory import MEMORY_THRESHOLD_MB, get_rss_mb
from oncofiles.models import Document, DocumentCategory

logger = logging.getLogger(__name__)

# Module-level lock to prevent concurrent sync operations.
# Uses a timestamp-based approach to auto-expire stale locks after 10 minutes.
_sync_lock = asyncio.Lock()
_sync_lock_acquired_at: float = 0.0
_SYNC_LOCK_TIMEOUT = 600  # 10 minutes

# Per-patient sync state — keyed by patient_id.
_last_sync_result: dict[str, dict] = {}
_last_sync_error: dict[str, str | None] = {}
_last_sync_time: dict[str, float] = {}
_sync_cycle_count: dict[str, int] = {}  # Per-patient cycle counter for full sync
_FULL_SYNC_EVERY_N = 6  # Run full sync every 6th cycle (every 30 min at 5-min interval)

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".xlsx", ".xls"}
SKIP_EXTENSIONS = {".gdoc", ".ds_store"}
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


async def _get_doc_patient_id(db: Database, doc_id: int) -> str | None:
    """Get the patient_id for a document by its local ID."""
    async with db.db.execute("SELECT patient_id FROM documents WHERE id = ?", (doc_id,)) as cursor:
        row = await cursor.fetchone()
        return row["patient_id"] if row else None


# ── GDrive → Oncofiles import ──────────────────────────────────────────────


async def sync_from_gdrive(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient,
    folder_id: str,
    *,
    dry_run: bool = False,
    enhance: bool = True,
    patient_id: str,
) -> dict:
    """Import new/changed files from GDrive into oncofiles.

    Walks category/year-month subfolders. Uses appProperties.oncofiles_id
    for reliable matching, falls back to gdrive_id.

    Returns summary dict: {new, updated, unchanged, skipped, missing, errors}.
    """
    # Proactive reconnect before batch to avoid stale replica (#378)
    await db.reconnect_if_stale(timeout=10.0)

    logger.info("sync_from_gdrive: listing folder %s (dry_run=%s)", folder_id, dry_run)
    gdrive_files, folder_map = await asyncio.to_thread(gdrive.list_folder_with_structure, folder_id)
    logger.info(
        "sync_from_gdrive: found %d files, %d folders",
        len(gdrive_files),
        len(folder_map),
    )

    stats = {
        "new": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        # #477: "missing" is legacy — kept for backward-compat with callers that
        # summed it. It now only counts docs whose gdrive_id is neither in the
        # sync-root listing NOR classifiable (metadata fetch failed with non-404).
        # External-location and remote-deletion are reported as their own keys.
        "missing": 0,
        "external_location": 0,
        "deleted_remote": 0,
        "errors": 0,
    }
    batch_size = 10  # Process files in batches, gc.collect between batches

    async def _sync_category_from_folder(gf: dict, existing: Document, filename: str) -> None:
        """Update document category if GDrive folder structure implies a different one.

        Never downgrades from a specific AI-validated category to "other" —
        folder detection returns "other" for root-level files, which would
        overwrite validated categories on every sync cycle (#256).
        """
        detected = _detect_category_from_parents(gf, folder_map)
        if detected and detected != existing.category.value:
            # Don't overwrite a specific category with "other" — AI validation wins
            if detected == "other" and existing.category.value != "other":
                return
            logger.info(
                "sync_from_gdrive: category changed %s → %s for %s",
                existing.category.value,
                detected,
                filename,
            )
            await db.update_document_category(existing.id, detected)

    # FUP: check document limit before syncing new files
    from oncofiles.config import MAX_DOCUMENTS_PER_PATIENT

    current_doc_count = await db.count_documents(patient_id=patient_id)
    fup_reached = current_doc_count >= MAX_DOCUMENTS_PER_PATIENT

    # Track which gdrive IDs we've seen (for detecting deletions)
    seen_gdrive_ids: set[str] = set()
    files_processed = 0

    for gf in gdrive_files:
        # Batch memory management: gc.collect + RSS log every batch_size files
        files_processed += 1
        if files_processed % batch_size == 0:
            gc.collect()
            rss = get_rss_mb()
            logger.info(
                "sync_from_gdrive: batch %d/%d — RSS: %.1f MB",
                files_processed,
                len(gdrive_files),
                rss,
            )
            # Abort early if memory pressure — return partial results
            if rss > MEMORY_THRESHOLD_MB:
                logger.warning(
                    "sync_from_gdrive: aborting at %d/%d — RSS %.1f MB exceeds %d MB",
                    files_processed,
                    len(gdrive_files),
                    rss,
                    MEMORY_THRESHOLD_MB,
                )
                stats["skipped"] += len(gdrive_files) - files_processed
                break
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
                try:
                    candidate = await db.get_document(int(oncofiles_id))
                except (ValueError, TypeError):
                    logger.warning("sync: invalid oncofiles_id '%s' on %s", oncofiles_id, gdrive_id)
                    candidate = None
                # Only trust appProperties match if doc belongs to this patient
                if candidate:
                    doc_pid = await _get_doc_patient_id(db, candidate.id)
                    if doc_pid == patient_id:
                        existing = candidate
            if not existing:
                existing = await db.get_document_by_gdrive_id(gdrive_id, patient_id=patient_id)

            if existing:
                # Check if modified
                gdrive_modified = _parse_gdrive_time(modified_time_str)
                if existing.gdrive_modified_time and gdrive_modified:
                    gd_naive = gdrive_modified.replace(tzinfo=None)
                    ex_naive = existing.gdrive_modified_time.replace(tzinfo=None)
                    if gd_naive <= ex_naive:
                        # GDrive doesn't update modifiedTime on folder moves,
                        # so always check parent folder for category changes
                        await _sync_category_from_folder(gf, existing, filename)
                        stats["unchanged"] += 1
                        continue

                # md5Checksum detects content vs metadata-only changes (e.g. rename).
                # GDrive modifiedTime changes on rename, but md5 stays the same.
                gdrive_md5 = gf.get("md5Checksum")
                content_changed = not (
                    gdrive_md5 and existing.gdrive_md5 and gdrive_md5 == existing.gdrive_md5
                )

                if not content_changed:
                    # Metadata-only change (rename, move) — just update timestamp
                    logger.info(
                        "sync_from_gdrive: metadata-only change for %s (md5 unchanged)",
                        filename,
                    )
                    await db.update_gdrive_id(existing.id, gdrive_id, modified_time_str)
                    now_str = datetime.now(UTC).isoformat()
                    await db.update_sync_state(existing.id, "synced", now_str)

                    await _sync_category_from_folder(gf, existing, filename)

                    stats["unchanged"] += 1
                    continue

                # File content changed on GDrive — GDrive wins (re-import)
                if dry_run:
                    logger.info("sync_from_gdrive: WOULD UPDATE %s", filename)
                    stats["updated"] += 1
                    continue

                logger.info("sync_from_gdrive: updating %s (GDrive wins)", filename)

                # Clear old OCR before re-import (will be re-extracted by enhance)
                await db.delete_ocr_pages(existing.id)

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
                if gdrive_md5:
                    await db.update_gdrive_md5(existing.id, gdrive_md5)
                now_str = datetime.now(UTC).isoformat()
                await db.update_sync_state(existing.id, "synced", now_str)

                await _sync_category_from_folder(gf, existing, filename)

                # Re-run AI enhancement (will re-extract OCR)
                if enhance:
                    from oncofiles.config import ENHANCE_TIMEOUT_S

                    try:
                        await asyncio.wait_for(
                            _enhance_document(db, existing, files, gdrive, patient_id=patient_id),
                            timeout=ENHANCE_TIMEOUT_S,
                        )
                    except TimeoutError:
                        logger.warning(
                            "sync: enhance timed out for doc %d (limit=%.0fs)",
                            existing.id,
                            ENHANCE_TIMEOUT_S,
                        )
                        stats["errors"] = stats.get("errors", 0) + 1

                stats["updated"] += 1
            else:
                # New file — import
                if fup_reached:
                    logger.info(
                        "sync_from_gdrive: FUP limit (%d) — skipping new file %s",
                        MAX_DOCUMENTS_PER_PATIENT,
                        filename,
                    )
                    stats["skipped"] = stats.get("skipped", 0) + 1
                    continue

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
                    gdrive_md5=gf.get("md5Checksum"),
                    sync_state="synced",
                    last_synced_at=datetime.now(UTC),
                )
                doc = await db.insert_document(doc, patient_id=patient_id)

                # Notify oncoteam of new document (fire-and-forget)
                from oncofiles.webhook import notify_oncoteam

                notify_oncoteam(doc.id, doc.filename, doc.category.value)

                # Set appProperties on GDrive for future matching
                try:
                    await asyncio.to_thread(
                        gdrive.set_app_properties, gdrive_id, {"oncofiles_id": str(doc.id)}
                    )
                except Exception:
                    logger.warning("Failed to set appProperties on %s", gdrive_id)

                # AI enhancement
                if enhance:
                    from oncofiles.config import ENHANCE_TIMEOUT_S

                    try:
                        await asyncio.wait_for(
                            _enhance_document(db, doc, files, gdrive, patient_id=patient_id),
                            timeout=ENHANCE_TIMEOUT_S,
                        )
                    except TimeoutError:
                        logger.warning(
                            "sync: enhance timed out for doc %d (limit=%.0fs)",
                            doc.id,
                            ENHANCE_TIMEOUT_S,
                        )
                        stats["errors"] = stats.get("errors", 0) + 1

                stats["new"] += 1
                current_doc_count += 1
                if current_doc_count >= MAX_DOCUMENTS_PER_PATIENT:
                    fup_reached = True

        except Exception as exc:
            # #477 Issue 2: previous log was "error processing <filename>" with only
            # the traceback in exc_info. Grep-friendly format means we can run
            # `rg "sync_from_gdrive: error"` in Railway logs and see the exception
            # class + message in the first line without reading stacktraces.
            logger.exception(
                "sync_from_gdrive: error processing %r (gdrive_id=%s, mime=%s) — %s: %s",
                filename,
                gdrive_id,
                mime_type,
                type(exc).__name__,
                str(exc)[:300],
            )
            # Track error classes in stats so audit_document_pipeline can surface
            # recurring patterns without log-scraping.
            stats["errors"] += 1
            err_cls = type(exc).__name__
            stats.setdefault("errors_by_type", {})
            stats["errors_by_type"][err_cls] = stats["errors_by_type"].get(err_cls, 0) + 1

    # Classify docs whose gdrive_id isn't in the patient's sync-root listing
    # into three buckets (#477):
    #   1. external_location: file exists in Drive, but under a different parent
    #      (e.g., PacientAdvokat uploaded markdown summaries to its own folder
    #      and registered the gdrive_id with us). Flag + keep; gdrive_url still
    #      works for the user.
    #   2. deleted_remote: file is permanently gone (404). Mark sync_state so
    #      reconciliation surfaces it but don't auto-delete the DB row.
    #   3. missing (unclassifiable): metadata fetch errored with a non-404.
    #      Retained as a legacy bucket for alerting — zero expected in steady
    #      state.
    # No matter the class, we NEVER auto-delete.
    all_docs = await db.list_documents(limit=200, patient_id=patient_id)
    for doc in all_docs:
        if not doc.gdrive_id or doc.gdrive_id in seen_gdrive_ids:
            # If a previously-external file is now back in the root, clear the flag.
            if doc.gdrive_id and doc.gdrive_parent_outside_root:
                try:
                    await db.set_gdrive_parent_outside_root(doc.id, False)
                    logger.info(
                        "sync_from_gdrive: cleared external-location flag on %s "
                        "(file is back in sync root)",
                        doc.filename,
                    )
                except Exception:
                    logger.exception(
                        "sync_from_gdrive: failed to clear external flag on doc %d",
                        doc.id,
                    )
            continue

        # Not in root — classify via direct metadata fetch
        try:
            meta = await asyncio.to_thread(gdrive.get_file_metadata, doc.gdrive_id)
        except FileNotFoundError:
            logger.info(
                "sync_from_gdrive: file %s (gdrive_id=%s) is gone from GDrive — "
                "marking deleted_remote",
                doc.filename,
                doc.gdrive_id,
            )
            try:
                await db.update_sync_state(doc.id, "deleted_remote")
            except Exception:
                logger.exception("sync_from_gdrive: failed to set deleted_remote on doc %d", doc.id)
            stats["deleted_remote"] += 1
            continue
        except Exception:
            # Unclassifiable (403 / 5xx / transport). Keep the legacy "missing"
            # warning so ops notices; don't mutate state.
            logger.warning(
                "sync_from_gdrive: file %s (gdrive_id=%s) not in root and metadata "
                "fetch failed — flagging",
                doc.filename,
                doc.gdrive_id,
                exc_info=True,
            )
            stats["missing"] += 1
            continue

        if meta.get("trashed"):
            logger.info(
                "sync_from_gdrive: file %s (gdrive_id=%s) is trashed in GDrive — "
                "marking deleted_remote",
                doc.filename,
                doc.gdrive_id,
            )
            try:
                await db.update_sync_state(doc.id, "deleted_remote")
            except Exception:
                logger.exception("sync_from_gdrive: failed to set deleted_remote on doc %d", doc.id)
            stats["deleted_remote"] += 1
            continue

        # File exists, just outside the root we sync.
        if not doc.gdrive_parent_outside_root:
            try:
                await db.set_gdrive_parent_outside_root(doc.id, True)
                logger.info(
                    "sync_from_gdrive: %s (gdrive_id=%s) lives outside sync root %s — "
                    "marked gdrive_parent_outside_root",
                    doc.filename,
                    doc.gdrive_id,
                    folder_id,
                )
            except Exception:
                logger.exception(
                    "sync_from_gdrive: failed to set external-location flag on doc %d",
                    doc.id,
                )
        stats["external_location"] += 1

    # ── Multi-document splitting & consolidation (AI-powered) ──────────────
    if enhance and all_docs:
        try:
            await _detect_and_split_multi_docs(
                db,
                files,
                gdrive,
                all_docs,
                folder_id=folder_id,
                folder_map=folder_map,
                patient_id=patient_id,
            )
        except Exception:
            logger.warning("sync: multi-document detection failed", exc_info=True)

        try:
            await _detect_and_consolidate(db, gdrive, all_docs, patient_id=patient_id)
        except Exception:
            logger.warning("sync: consolidation detection failed", exc_info=True)

    logger.info("sync_from_gdrive: done — %s", stats)
    return stats


async def _detect_and_split_multi_docs(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None,
    docs: list[Document],
    *,
    folder_id: str,
    folder_map: dict[str, str],
    patient_id: str,
) -> None:
    """Scan documents for multi-document PDFs and split them."""
    from oncofiles.doc_analysis import analyze_document_composition
    from oncofiles.split import split_document

    for doc in docs:
        # Only process ungrouped PDFs with cached OCR text (2+ pages)
        if doc.group_id or doc.deleted_at:
            continue
        if doc.mime_type != "application/pdf":
            continue
        if not await db.has_ocr_text(doc.id):
            continue

        pages = await db.get_ocr_pages(doc.id)
        if len(pages) < 2:
            continue

        full_text = "\n\n".join(p["extracted_text"] for p in pages)
        sub_docs = analyze_document_composition(full_text, db=db, document_id=doc.id)

        if len(sub_docs) > 1:
            logger.info(
                "sync: detected %d sub-documents in doc %d (%s)",
                len(sub_docs),
                doc.id,
                doc.filename,
            )
            await split_document(
                db,
                files,
                gdrive,
                doc,
                sub_docs,
                patient_id=patient_id,
                folder_id=folder_id,
                folder_map=folder_map,
            )


async def _detect_and_consolidate(
    db: Database,
    gdrive: GDriveClient | None,
    docs: list[Document],
    *,
    patient_id: str,
) -> None:
    """Scan documents for multi-file logical documents and consolidate them."""
    from oncofiles.consolidate import consolidate_documents
    from oncofiles.doc_analysis import analyze_consolidation

    # Only consider ungrouped, active documents
    ungrouped = [d for d in docs if d.group_id is None and d.deleted_at is None]
    if len(ungrouped) < 2:
        return

    # Gather OCR text for each document
    doc_texts = []
    for doc in ungrouped:
        text = ""
        if await db.has_ocr_text(doc.id):
            pages = await db.get_ocr_pages(doc.id)
            text = "\n\n".join(p["extracted_text"] for p in pages)
        doc_texts.append((doc, text))

    groups = analyze_consolidation(doc_texts, db=db)
    for group in groups:
        if len(group.get("document_ids", [])) >= 2:
            logger.info(
                "sync: consolidating %d documents: %s",
                len(group["document_ids"]),
                group.get("reasoning", ""),
            )
            await consolidate_documents(db, gdrive, group, patient_id=patient_id)


# ── Oncofiles → GDrive export ──────────────────────────────────────────────


async def sync_to_gdrive(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient,
    folder_id: str,
    *,
    dry_run: bool = False,
    full: bool = True,
    patient_id: str,
) -> dict:
    """Export documents from oncofiles to GDrive with folder structure.

    Uploads documents to correct category/YYYY-MM/ folders, sets appProperties,
    and exports manifest + metadata markdown files.

    When full=False (no import changes), skips expensive batch operations
    (organize, rename, OCR cleanup) that scan all docs. Only exports new
    docs without gdrive_id and updates metadata.

    Returns summary dict: {exported, skipped, metadata_exported, errors}.
    """
    # Proactive reconnect before batch to avoid stale replica (#378)
    await db.reconnect_if_stale(timeout=10.0)

    logger.info("sync_to_gdrive: starting (dry_run=%s, full=%s)", dry_run, full)

    stats = {"exported": 0, "organized": 0, "skipped": 0, "metadata_exported": 0, "errors": 0}

    if dry_run:
        # Count what would be exported/organized
        docs = await db.list_documents(limit=500, patient_id=patient_id)
        for doc in docs:
            if doc.gdrive_id:
                stats["skipped"] += 1
            else:
                stats["exported"] += 1
        logger.info("sync_to_gdrive: dry run — %s", stats)
        return stats

    # Ensure folder structure (patient-type-aware)
    from oncofiles.patient_context import get_context

    _pt = get_context(patient_id).get("patient_type", "oncology")
    folder_map = await asyncio.to_thread(
        ensure_folder_structure, gdrive, folder_id, patient_type=_pt
    )

    # Collect all organized folder IDs (category folders + their year-month subfolders)
    organized_folder_ids = set(folder_map.values())

    # Export documents
    docs = await db.list_documents(limit=500, patient_id=patient_id)

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
            target_folder = resolve_category_folder(folder_map, cat_name, folder_id)
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
            rename_stats = await _rename_to_standard(db, gdrive, patient_id=patient_id)
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
            from oncofiles.config import ENHANCE_TIMEOUT_S

            for doc_id in rename_stats["renamed_ids"]:
                try:
                    doc = await db.get_document(doc_id)
                    await asyncio.wait_for(
                        _enhance_document(db, doc, files, gdrive, patient_id=patient_id),
                        timeout=ENHANCE_TIMEOUT_S,
                    )
                    logger.info("sync_to_gdrive: post-rename enhanced doc %d", doc_id)
                except TimeoutError:
                    logger.warning(
                        "sync_to_gdrive: post-rename enhance timed out for doc %d (limit=%.0fs)",
                        doc_id,
                        ENHANCE_TIMEOUT_S,
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
                    db,
                    doc_id,
                    gdrive=gdrive,
                    organized_folder_ids=organized_folder_ids,
                    patient_id=patient_id,
                )
                all_gaps.extend(gaps)
            if all_gaps:
                stats["pipeline_gaps"] = all_gaps
                logger.warning("sync_to_gdrive: %d pipeline gaps after rename", len(all_gaps))

        # Clean up orphaned OCR files (old names from before bilingual rename)
        try:
            cleanup_stats = await _cleanup_orphan_ocr(db, gdrive, patient_id=patient_id)
            stats["ocr_cleaned"] = cleanup_stats["deleted"]
        except Exception as e:
            logger.warning("sync_to_gdrive: OCR cleanup failed — %s", str(e)[:200])

        # OCR companion files (_OCR.txt) disabled — text is cached in DB document_pages
        # and accessible via MCP tools. Companion files caused orphan accumulation (#114).
    else:
        logger.info("sync_to_gdrive: skipping heavy phases (no import changes)")

    # Export metadata files (may fail with service account — no storage quota)
    try:
        await _export_metadata(db, gdrive, folder_id, folder_map, patient_id=patient_id)
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
    target_folder = resolve_category_folder(folder_map, cat_name, root_folder_id)
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
        target_folder = resolve_category_folder(folder_map, cat_name, root_folder_id)
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
    patient_id: str = "",
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

    if not is_standard_format(doc.filename, patient_id=patient_id):
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


async def _rename_to_standard(db: Database, gdrive: GDriveClient, *, patient_id: str) -> dict:
    """Rename GDrive files to standard format (underscore-separated, EN description).

    For each document: checks if filename is already in standard format.
    If not, renames on GDrive and updates DB filename.
    Stores original_filename before rename for reversibility.

    Returns: {renamed, skipped, errors, renamed_ids}.
    """
    stats: dict = {"renamed": 0, "skipped": 0, "errors": 0, "renamed_ids": []}
    docs = await db.list_documents(limit=500, patient_id=patient_id)
    pending_renames: list[tuple] = []

    for doc in docs:
        if not doc.gdrive_id:
            stats["skipped"] += 1
            continue

        # Skip if already in standard format
        if is_standard_format(doc.filename, patient_id=patient_id):
            stats["skipped"] += 1
            continue

        try:
            # Handle corrupted filenames: use DB metadata instead of parsing
            if is_corrupted_filename(doc.filename, patient_id=patient_id):
                from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS
                from oncofiles.patient_context import get_patient_name

                patient = get_patient_name(patient_id).replace(" ", "") or "Patient"
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
                new_name = rename_to_standard(
                    doc.filename,
                    category=doc.category.value,
                    patient_id=patient_id,
                    institution_override=doc.institution,
                )

            # If rename_to_standard couldn't parse (returned unchanged),
            # build from DB metadata like we do for corrupted filenames
            if new_name == doc.filename:
                from oncofiles.filename_parser import CATEGORY_FILENAME_TOKENS
                from oncofiles.patient_context import get_patient_name

                patient = get_patient_name(patient_id).replace(" ", "") or "Patient"
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


async def _cleanup_orphan_ocr(db: Database, gdrive: GDriveClient, *, patient_id: str) -> dict:
    """Delete orphaned OCR files whose names don't match any current document.

    After bilingual rename, old OCR files (pre-rename names) remain as duplicates.
    This finds _OCR.txt files in document folders and deletes those that don't
    correspond to any current document filename.

    Returns: {deleted, skipped, errors}.
    """
    stats = {"deleted": 0, "skipped": 0, "errors": 0}

    # Build set of expected OCR filenames from current documents
    docs = await db.list_documents(limit=500, patient_id=patient_id)
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


async def _export_metadata(
    db: Database,
    gdrive: GDriveClient,
    root_folder_id: str,
    folder_map: dict[str, str],
    *,
    patient_id: str,
) -> None:
    """Export manifest.json and metadata markdown files to GDrive.

    Exports EN (primary) and preferred language (secondary) versions of all
    markdown files. If preferred lang is EN, only one file is created.
    """
    from oncofiles.i18n import needs_secondary, preferred_lang

    langs = ["en"]
    if needs_secondary():
        langs.append(preferred_lang())

    # Pre-fetch folder listings once per target folder to avoid repeated
    # list_folder API calls in _upload_or_update_text (#309).
    folder_listings: dict[str, list[dict]] = {}
    for key in ("conversations", "treatment", "research"):
        fid = folder_map.get(key)
        if fid:
            folder_listings[fid] = await asyncio.to_thread(gdrive.list_folder, fid, recursive=False)
    root_listing = await asyncio.to_thread(gdrive.list_folder, root_folder_id, recursive=False)
    folder_listings[root_folder_id] = root_listing

    # 1. Export _manifest.json to root (it's a catalogue/index — root is correct)
    manifest = await export_manifest(db, patient_id=patient_id)
    manifest_json = render_manifest_json(manifest)
    await asyncio.to_thread(
        _upload_or_update_text,
        gdrive,
        "_manifest.json",
        manifest_json,
        root_folder_id,
        "application/json",
        folder_listings.get(root_folder_id),
    )

    # 2. Export conversation monthly logs
    conversations_folder = folder_map.get("conversations")
    if conversations_folder:
        entries = await db.get_conversation_timeline(limit=200, patient_id=patient_id)
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
                    folder_listings.get(conversations_folder),
                )

    # 3. Export treatment timeline
    treatment_folder = folder_map.get("treatment")
    if treatment_folder:
        events = await db.get_treatment_events_timeline(limit=200, patient_id=patient_id)
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
                folder_listings.get(treatment_folder),
            )

    # 4. Export research library
    research_folder = folder_map.get("research")
    if research_folder:
        entries = await db.list_research_entries(limit=200, patient_id=patient_id)
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
                folder_listings.get(research_folder),
            )


def _upload_or_update_text(
    gdrive: GDriveClient,
    filename: str,
    content: str,
    folder_id: str,
    mime_type: str,
    folder_listing: list[dict] | None = None,
) -> None:
    """Upload a text file, or update it if it already exists in the folder.

    If *folder_listing* is provided, uses it instead of calling list_folder
    (saves one API call per file — significant when exporting multiple files
    to the same folder, see #309).
    """
    content_bytes = content.encode("utf-8")
    existing_files = (
        folder_listing
        if folder_listing is not None
        else gdrive.list_folder(folder_id, recursive=False)
    )
    for f in existing_files:
        if f["name"] == filename:
            gdrive.update(f["id"], content_bytes, mime_type)
            return
    result = gdrive.upload(
        filename=filename,
        content_bytes=content_bytes,
        mime_type=mime_type,
        folder_id=folder_id,
    )
    # Keep shared listing fresh so subsequent calls in the same batch
    # see the new file and update instead of creating duplicates (#309).
    if folder_listing is not None and result:
        folder_listing.append({"id": result["id"], "name": filename})


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
    patient_id: str,
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
                patient_id=patient_id,
            )
        except Exception:
            _last_sync_error[patient_id] = "Sync failed — check server logs"
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
    patient_id: str,
) -> dict:
    """Inner sync logic (called under lock)."""
    logger.info("sync: starting bidirectional sync (dry_run=%s)", dry_run)
    _last_sync_error[patient_id] = None

    from oncofiles.memory import db_slot

    # Record sync start in history (skip for dry runs)
    sync_id = None
    start_mono = time.monotonic()
    if not dry_run:
        try:
            async with db_slot("insert_sync_history", priority=False):
                sync_id = await db.insert_sync_history(trigger=trigger, patient_id=patient_id)
        except Exception:
            logger.warning("sync: failed to record sync start", exc_info=True)

    try:
        from_stats = await sync_from_gdrive(
            db, files, gdrive, folder_id, dry_run=dry_run, enhance=enhance, patient_id=patient_id
        )
        # Determine whether heavy phases are needed (#309: full=True every cycle
        # caused 357 MB/hour RSS growth).  Run full sync when:
        # (a) new or updated imports detected, OR
        # (b) every Nth cycle as safety net for date backfills / category changes (#278).
        has_changes = from_stats.get("new", 0) > 0 or from_stats.get("updated", 0) > 0
        cycle = _sync_cycle_count.get(patient_id, 0) + 1
        _sync_cycle_count[patient_id] = cycle
        run_full = has_changes or (cycle % _FULL_SYNC_EVERY_N == 0)
        to_stats = await sync_to_gdrive(
            db, files, gdrive, folder_id, dry_run=dry_run, full=run_full, patient_id=patient_id
        )

        combined = {
            "from_gdrive": from_stats,
            "to_gdrive": to_stats,
        }
        _last_sync_result[patient_id] = combined
        _last_sync_time[patient_id] = time.monotonic()

        # Record sync completion
        if sync_id is not None:
            duration = time.monotonic() - start_mono
            try:
                async with db_slot("complete_sync_history", priority=False):
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
                async with db_slot("complete_sync_history_fail", priority=False):
                    await db.complete_sync_history(
                        sync_id,
                        status="failed",
                        duration_s=round(duration, 1),
                        error_message=str(exc)[:500],
                    )
            except Exception:
                logger.warning("sync: failed to record sync failure", exc_info=True)
        raise


def get_sync_status(patient_id: str = "") -> dict:
    """Return current sync status (running/idle) and last result for a patient."""
    running = _sync_lock.locked()
    elapsed = time.monotonic() - _sync_lock_acquired_at if _sync_lock_acquired_at > 0 else 0.0

    status: dict = {"running": running}
    if running:
        status["elapsed_s"] = round(elapsed, 1)

    result = _last_sync_result.get(patient_id) if patient_id else None
    if result is not None:
        status["last_result"] = result
        t = _last_sync_time.get(patient_id, 0.0)
        age = time.monotonic() - t if t > 0 else 0.0
        status["last_sync_age_s"] = round(age, 1)

    error = _last_sync_error.get(patient_id) if patient_id else None
    if error is not None:
        status["last_error"] = error

    return status


# ── AI enhancement helper ──────────────────────────────────────────────────


async def enhance_documents(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None = None,
    document_ids: list[int] | None = None,
    *,
    patient_id: str,
    limit: int = 0,
    force: bool = False,
    only_new: bool = False,
    max_age_hours: int | None = None,
) -> dict:
    """Run AI enhancement on documents.

    If document_ids is None, processes all documents without AI metadata.
    Returns summary dict: {processed, skipped, errors}.

    Args:
        limit: Max documents to process (0 = no limit). Use to avoid MCP proxy timeouts.
        force: If True, bypass the ai_processed_at guard in _enhance_document
            (user-initiated MCP tool calls set force=True). See #433.
        only_new: If True, filter to docs created in the last max_age_hours.
        max_age_hours: Sliding window for only_new (default AI_REPROCESS_MAX_AGE_HOURS).
    """
    if document_ids:
        docs = []
        for doc_id in document_ids:
            doc = await db.get_document(doc_id)
            if doc:
                doc_pid = await _get_doc_patient_id(db, doc.id)
                if doc_pid == patient_id:
                    docs.append(doc)
    else:
        docs = await db.get_documents_without_ai(patient_id=patient_id)

    if only_new:
        from datetime import timedelta

        from oncofiles.config import AI_REPROCESS_MAX_AGE_HOURS

        hours = max_age_hours if max_age_hours is not None else AI_REPROCESS_MAX_AGE_HOURS
        cutoff = datetime.now(UTC) - timedelta(hours=hours)

        def _new(created):
            if created is None:
                return False
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return created >= cutoff

        docs = [d for d in docs if _new(d.created_at)]

    if limit > 0:
        docs = docs[:limit]

    logger.info("enhance_documents: %d documents to process", len(docs))
    stats = {"processed": 0, "skipped": 0, "errors": 0}

    # Proactive reconnect before batch to avoid stale replica (#378)
    await db.reconnect_if_stale(timeout=10.0)

    from oncofiles.config import ENHANCE_TIMEOUT_S
    from oncofiles.memory import reclaim_memory

    for doc in docs:
        try:
            enhanced = await asyncio.wait_for(
                _enhance_document(db, doc, files, gdrive, patient_id=patient_id, force=force),
                timeout=ENHANCE_TIMEOUT_S,
            )
            if enhanced:
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
        except TimeoutError:
            logger.warning(
                "enhance_documents: doc %d (%s) timed out after %.0fs — skipping",
                doc.id,
                doc.filename,
                ENHANCE_TIMEOUT_S,
            )
            stats["errors"] += 1
        except Exception:
            logger.exception("enhance_documents: error on doc %d (%s)", doc.id, doc.filename)
            stats["errors"] += 1
        finally:
            # Per-doc reclaim — addresses #426 memory leak under nightly batch.
            # Each scanned-PDF enhance leaks ~40 MB (fitz pixmaps + Anthropic SSL
            # buffers on timeout). Calling gc + malloc_trim between docs keeps
            # RSS bounded instead of growing monotonically through the batch.
            reclaim_memory(f"enhance:{doc.id}")

    logger.info("enhance_documents: done — %s", stats)
    return stats


async def _generate_cross_references(
    db: Database, doc: Document, metadata: dict, *, patient_id: str
) -> int:
    """Generate cross-references between a document and related documents using AI.

    AI analyzes the document content and candidate summaries to determine
    relationships: same_visit, follow_up, supersedes, related, contradicts.

    Returns count of new cross-references inserted.
    """
    from oncofiles.doc_analysis import analyze_document_relationships

    # Get document text
    doc_text = ""
    if await db.has_ocr_text(doc.id):
        pages = await db.get_ocr_pages(doc.id)
        doc_text = "\n\n".join(p["extracted_text"] for p in pages)

    if not doc_text:
        return 0

    # Get candidate documents (all active docs for this patient, excluding self)
    all_docs = await db.list_documents(limit=200, patient_id=patient_id)
    candidates = []
    for c in all_docs:
        if c.id != doc.id and c.deleted_at is None:
            candidates.append(
                {
                    "id": c.id,
                    "filename": c.filename,
                    "document_date": c.document_date.isoformat() if c.document_date else None,
                    "institution": c.institution,
                    "category": c.category.value,
                    "ai_summary": c.ai_summary,
                }
            )

    if not candidates:
        return 0

    try:
        relationships = analyze_document_relationships(doc_text, doc.id, candidates, db=db)
    except Exception:
        logger.warning(
            "AI cross-reference analysis failed for doc %d, skipping", doc.id, exc_info=True
        )
        return 0

    refs: list[tuple[int, int, str, float]] = []
    for rel in relationships:
        target_id = rel.get("target_id")
        rel_type = rel.get("relationship", "related")
        confidence = rel.get("confidence", 0.5)
        if target_id and target_id != doc.id:
            refs.append((doc.id, target_id, rel_type, confidence))

    if refs:
        return await db.bulk_insert_cross_references(refs)
    return 0


EXTRACTABLE_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/heic",
        "image/heif",
        "image/webp",
    }
)
"""Mime types whose byte stream can feed OCR / metadata extraction.

xlsx/docx/csv/markdown are persisted and searchable, but the OCR-based
metadata pipeline has no path for them, so `extract_all_metadata` filters
them out of its candidate set rather than counting them as errors (#466).
"""


async def extract_all_metadata(
    db: Database,
    files: FilesClient,
    gdrive: GDriveClient | None = None,
    *,
    patient_id: str,
    only_new: bool = False,
    max_age_hours: int | None = None,
    cap: int = 5,
) -> dict:
    """Backfill structured_metadata for documents that have AI summaries but no metadata.

    Args:
        only_new: If True, filter to docs created in the last max_age_hours. See #433.
        max_age_hours: Sliding window for only_new (default AI_REPROCESS_MAX_AGE_HOURS).
        cap: Max docs to process per run (default 5, keeps memory bounded).

    Returns summary dict: {processed, skipped, skipped_unsupported_mime, errors}.
    """
    # Proactive reconnect before batch to avoid stale replica (#378)
    await db.reconnect_if_stale(timeout=10.0)

    docs = await db.get_documents_without_metadata(patient_id=patient_id)

    if only_new:
        from datetime import timedelta

        from oncofiles.config import AI_REPROCESS_MAX_AGE_HOURS

        hours = max_age_hours if max_age_hours is not None else AI_REPROCESS_MAX_AGE_HOURS
        cutoff = datetime.now(UTC) - timedelta(hours=hours)

        def _new(created):
            if created is None:
                return False
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            return created >= cutoff

        docs = [d for d in docs if _new(d.created_at)]

    skipped_unsupported_mime = sum(
        1 for d in docs if d.mime_type and d.mime_type not in EXTRACTABLE_MIME_TYPES
    )
    docs = [d for d in docs if not d.mime_type or d.mime_type in EXTRACTABLE_MIME_TYPES]

    docs = docs[:cap]  # bound memory / Anthropic spend per run
    logger.info(
        "extract_all_metadata: %d documents to process (skipped %d unsupported mime)",
        len(docs),
        skipped_unsupported_mime,
    )
    stats = {
        "processed": 0,
        "skipped": 0,
        "skipped_unsupported_mime": skipped_unsupported_mime,
        "errors": 0,
    }

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
            metadata = extract_structured_metadata(
                full_text, db=db, document_id=doc.id, filename=doc.filename
            )
            await db.update_structured_metadata(doc.id, json.dumps(metadata, ensure_ascii=False))
            logger.info(
                "extract_all_metadata: doc %d (%s) — metadata extracted",
                doc.id,
                doc.filename,
            )
            stats["processed"] += 1

            # Generate cross-references based on heuristic matching
            await _generate_cross_references(db, doc, metadata, patient_id=patient_id)

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
    patient_id: str = "",
    *,
    force: bool = False,
) -> bool:
    """Run AI enhancement on a single document. Returns True if enhanced.

    Guard: if doc already has `ai_processed_at` set, skip to avoid re-spending
    Claude tokens on already-enhanced docs. User-initiated MCP tool calls pass
    force=True to bypass. See #433.
    """
    if doc.ai_processed_at is not None and not force:
        return False

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

                pdf_doc = None
                try:
                    pdf_doc = fitz.open(stream=content_bytes, filetype="pdf")
                    # fitz copies the stream internally; drop our reference so
                    # the caller's bytes can be collected mid-OCR (#426).
                    del content_bytes
                    content_bytes = None
                    for page_num, page in enumerate(pdf_doc, start=1):
                        pix = page.get_pixmap(dpi=200)
                        img = None
                        try:
                            img = MImage(data=pix.tobytes("jpeg"), format="jpeg")
                            img = _resize_image_if_needed(img)
                            text = extract_text_from_image(img, db=db, document_id=doc.id)
                            await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
                            text_parts.append(text)
                        finally:
                            del pix
                            if img is not None:
                                del img
                    if text_parts:
                        logger.info(
                            "enhance: Vision OCR for doc %d (%d pages)",
                            doc.id,
                            len(text_parts),
                        )
                except Exception:
                    logger.warning("enhance: Vision OCR failed for doc %d", doc.id, exc_info=True)
                finally:
                    # Always release MuPDF's C-level buffer — on any exception
                    # (Vision timeout, API error), leaving pdf_doc open leaks
                    # ~10 MB/doc of MuPDF memory that GC cannot reclaim. #426.
                    if pdf_doc is not None:
                        import contextlib as _contextlib

                        with _contextlib.suppress(Exception):
                            pdf_doc.close()
        elif content_bytes and doc.mime_type and doc.mime_type.startswith("image/"):
            # Image files: Vision OCR
            from fastmcp.utilities.types import Image as MImage

            from oncofiles.ocr import OCR_MODEL, extract_text_from_image
            from oncofiles.tools._helpers import _resize_image_if_needed

            img = None
            try:
                fmt = doc.mime_type.split("/")[1]
                img = MImage(data=content_bytes, format=fmt)
                # Drop caller's bytes — MImage holds its own reference. #426
                del content_bytes
                content_bytes = None
                img = _resize_image_if_needed(img)
                text = extract_text_from_image(img, db=db, document_id=doc.id)
                await db.save_ocr_page(doc.id, 1, text, OCR_MODEL)
                text_parts = [text]
                logger.info("enhance: Vision OCR for image doc %d (%d chars)", doc.id, len(text))
            except Exception:
                logger.warning("enhance: Vision OCR failed for image doc %d", doc.id, exc_info=True)
            finally:
                if img is not None:
                    del img
                if content_bytes is not None:
                    del content_bytes
        elif content_bytes and doc.mime_type and doc.mime_type.startswith("text/"):
            try:
                text_content = content_bytes.decode("utf-8")
            except UnicodeDecodeError:
                text_content = content_bytes.decode("latin-1")
            if text_content.strip():
                await db.save_ocr_page(doc.id, 1, text_content, "text-decode")
                text_parts = [text_content]
            del content_bytes

    if not text_parts:
        logger.warning("enhance: no text available for doc %d (%s)", doc.id, doc.filename)
        return False

    full_text = "\n\n".join(text_parts)
    summary, tags_json = enhance_document_text(full_text, db=db, document_id=doc.id)
    await db.update_document_ai_metadata(doc.id, summary, tags_json)

    # Extract structured metadata (diagnoses, medications, findings, etc.)
    metadata = None
    try:
        metadata = extract_structured_metadata(
            full_text, db=db, document_id=doc.id, filename=doc.filename
        )
        await db.update_structured_metadata(doc.id, json.dumps(metadata, ensure_ascii=False))
        logger.info("enhance: doc %d (%s) — structured metadata extracted", doc.id, doc.filename)
    except Exception:
        logger.warning(
            "enhance: doc %d (%s) — structured metadata extraction failed",
            doc.id,
            doc.filename,
        )

    # ── Backfill null top-level fields via AI classification ──────────────
    needs_classification = (
        not doc.document_date or not doc.institution or doc.category.value == "other"
    )
    if needs_classification and full_text:
        from oncofiles.enhance import classify_document

        classification = classify_document(full_text, db=db, document_id=doc.id)

        backfill_date = None
        backfill_institution = None
        backfill_description = None

        # Date from AI classification
        if not doc.document_date:
            ai_date = classification.get("document_date")
            if ai_date:
                try:
                    from datetime import date as _date

                    parsed = _date.fromisoformat(ai_date)
                    if 1900 <= parsed.year <= 2030:
                        backfill_date = parsed.isoformat()
                        logger.info(
                            "enhance: doc %d — backfill date=%s (AI)", doc.id, backfill_date
                        )
                except (ValueError, TypeError):
                    pass

            # Fallback: filename YYYYMMDD
            if not backfill_date:
                import re as _re

                fn_match = _re.match(r"^(\d{4})(\d{2})(\d{2})_", doc.filename)
                if fn_match:
                    y, m, d = fn_match.groups()
                    try:
                        from datetime import date as _date

                        parsed = _date(int(y), int(m), int(d))
                        if 1900 <= parsed.year <= 2030:
                            backfill_date = parsed.isoformat()
                    except ValueError:
                        pass

            # Fallback: GDrive time
            if not backfill_date and doc.gdrive_modified_time:
                backfill_date = doc.gdrive_modified_time.strftime("%Y-%m-%d")

        # Institution from AI classification
        if not doc.institution:
            ai_inst = classification.get("institution_code")
            if ai_inst:
                backfill_institution = ai_inst
                logger.info("enhance: doc %d — backfill institution=%s (AI)", doc.id, ai_inst)
            elif metadata:
                # Fallback: keyword matching (deprecated)
                providers = metadata.get("providers", [])
                inst = infer_institution_from_providers(providers)
                if inst:
                    backfill_institution = inst

        # Category from AI classification (only upgrade other → specific)
        ai_category = classification.get("category")
        if ai_category and doc.category.value == "other" and ai_category != "other":
            from oncofiles.models import DocumentCategory as _DocCat

            try:
                valid_cat = _DocCat(ai_category)
                await db.update_document_category(doc.id, valid_cat.value)
                logger.info(
                    "enhance: doc %d — category %s → %s (AI)", doc.id, "other", valid_cat.value
                )
            except ValueError:
                pass

        # Description: generate English CamelCase description if filename is non-standard
        if not is_standard_format(doc.filename, patient_id=patient_id):
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
                force_description=not is_standard_format(doc.filename, patient_id=patient_id),
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
    ('labs — laboratórne výsledky'). Auto-remaps legacy categories
    (surgical_report → surgery, discharge_summary → discharge).

    Returns category string if parent folder matches a known category, else None.
    """
    from oncofiles.gdrive_folders import CATEGORY_MERGES, en_key_from_folder_name

    parents = file_info.get("parents", [])
    valid_categories = {cat.value for cat in DocumentCategory}

    for parent_id in parents:
        folder_name = folder_map.get(parent_id, "")
        detected = None
        # Direct match (legacy EN-only)
        if folder_name in valid_categories:
            detected = folder_name
        else:
            # Bilingual name: extract EN key
            en_key = en_key_from_folder_name(folder_name)
            if en_key and en_key in valid_categories:
                detected = en_key
        if detected:
            # Auto-remap legacy categories
            return CATEGORY_MERGES.get(detected, detected)
    return None
