"""Bidirectional Google Drive sync logic (#v1.0).

Supports folder-aware sync with category/year-month structure,
manifest export, and metadata rendering.
"""

from __future__ import annotations

import io
import json
import logging
import mimetypes
from datetime import UTC, datetime

from oncofiles.database import Database
from oncofiles.enhance import enhance_document_text, extract_structured_metadata
from oncofiles.filename_parser import parse_filename
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
from oncofiles.models import Document, DocumentCategory

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SKIP_EXTENSIONS = {".gdoc", ".xlsx", ".xls", ".ds_store"}


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
    gdrive_files, folder_map = gdrive.list_folder_with_structure(folder_id)
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

        # Skip non-document files (manifest, markdown metadata files)
        if filename.endswith((".json", ".md")):
            stats["skipped"] += 1
            continue

        if not _should_sync(filename):
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
                content_bytes = gdrive.download(gdrive_id)
                metadata = files.upload(io.BytesIO(content_bytes), filename, mime_type)
                await db.update_document_file_id(existing.id, metadata.id, len(content_bytes))
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
                    await _enhance_document(db, existing, files, gdrive)

                await db.delete_ocr_pages(existing.id)
                stats["updated"] += 1
            else:
                # New file — import
                if dry_run:
                    logger.info("sync_from_gdrive: WOULD IMPORT %s", filename)
                    stats["new"] += 1
                    continue

                logger.info("sync_from_gdrive: importing %s", filename)
                content_bytes = gdrive.download(gdrive_id)
                metadata = files.upload(io.BytesIO(content_bytes), filename, mime_type)

                parsed = parse_filename(filename)
                guessed_mime = mimetypes.guess_type(filename)[0] or mime_type
                gdrive_modified = _parse_gdrive_time(modified_time_str)

                # Try to detect category from folder structure
                detected_category = _detect_category_from_parents(gf, folder_map)
                category = (
                    DocumentCategory(detected_category) if detected_category else parsed.category
                )

                now_str = datetime.now(UTC).isoformat()
                doc = Document(
                    file_id=metadata.id,
                    filename=filename,
                    original_filename=filename,
                    document_date=parsed.document_date,
                    institution=parsed.institution,
                    category=category,
                    description=parsed.description,
                    mime_type=guessed_mime,
                    size_bytes=len(content_bytes),
                    gdrive_id=gdrive_id,
                    gdrive_modified_time=gdrive_modified,
                    sync_state="synced",
                    last_synced_at=datetime.now(UTC),
                )
                doc = await db.insert_document(doc)

                # Set appProperties on GDrive for future matching
                try:
                    gdrive.set_app_properties(gdrive_id, {"oncofiles_id": str(doc.id)})
                except Exception:
                    logger.warning("Failed to set appProperties on %s", gdrive_id)

                # AI enhancement
                if enhance:
                    await _enhance_document(db, doc, files, gdrive)

                stats["new"] += 1

        except Exception:
            logger.exception("sync_from_gdrive: error processing %s", filename)
            stats["errors"] += 1

    # Detect deleted files (in DB but not on GDrive) — flag only, never auto-delete
    all_docs = await db.list_documents(limit=1000)
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
) -> dict:
    """Export documents from oncofiles to GDrive with folder structure.

    Uploads documents to correct category/YYYY-MM/ folders, sets appProperties,
    and exports manifest + metadata markdown files.

    Returns summary dict: {exported, skipped, metadata_exported, errors}.
    """
    logger.info("sync_to_gdrive: starting (dry_run=%s)", dry_run)

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
    folder_map = ensure_folder_structure(gdrive, folder_id)

    # Collect all organized folder IDs (category folders + their year-month subfolders)
    organized_folder_ids = set(folder_map.values())

    # Export documents
    docs = await db.list_documents(limit=500)
    for doc in docs:
        if doc.gdrive_id:
            # File already on GDrive — check if it needs to be moved to organized folder
            try:
                _move_to_organized_folder(
                    gdrive, doc, folder_id, folder_map, organized_folder_ids, stats
                )
            except Exception:
                logger.exception("sync_to_gdrive: error organizing %s", doc.filename)
                stats["errors"] += 1
            continue

        try:
            logger.info("sync_to_gdrive: exporting %s", doc.filename)
            content_bytes = files.download(doc.file_id)

            # Determine target folder
            cat_name, year_month = get_category_folder_path(
                doc.category.value,
                doc.document_date.isoformat() if doc.document_date else None,
            )
            target_folder = folder_map.get(cat_name, folder_id)
            if year_month:
                target_folder = ensure_year_month_folder(gdrive, target_folder, year_month + "-01")

            # Upload with appProperties
            uploaded = gdrive.upload(
                filename=doc.filename,
                content_bytes=content_bytes,
                mime_type=doc.mime_type,
                folder_id=target_folder,
                app_properties={"oncofiles_id": str(doc.id)},
            )

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

    # Export metadata files (may fail with service account — no storage quota)
    try:
        await _export_metadata(db, gdrive, folder_id, folder_map)
        stats["metadata_exported"] += 1
    except Exception as e:
        err_str = str(e)
        logger.warning("sync_to_gdrive: metadata export failed — %s", err_str)
        if "storage" not in err_str.lower() and "403" not in err_str:
            stats["errors"] += 1

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

    # Check if already in an organized folder (category folder or year-month subfolder)
    if any(p in organized_folder_ids for p in parents):
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

    logger.info(
        "sync_to_gdrive: moving %s to %s/%s",
        doc.filename,
        cat_name,
        year_month or "",
    )
    gdrive.move_file(doc.gdrive_id, target_folder)
    stats["organized"] += 1


async def _export_metadata(
    db: Database,
    gdrive: GDriveClient,
    root_folder_id: str,
    folder_map: dict[str, str],
) -> None:
    """Export manifest.json and metadata markdown files to GDrive."""
    # 1. Export _manifest.json to root
    manifest = await export_manifest(db)
    manifest_json = render_manifest_json(manifest)
    _upload_or_update_text(
        gdrive,
        "_manifest.json",
        manifest_json,
        root_folder_id,
        "application/json",
    )

    # 2. Export conversation monthly logs
    conversations_folder = folder_map.get("conversations")
    if conversations_folder:
        entries = await db.get_conversation_timeline(limit=1000)
        by_month = group_conversations_by_month(entries)
        for month_key, month_entries in by_month.items():
            md_content = render_conversation_month(month_entries)
            filename = f"{month_key}-conversation-log.md"
            _upload_or_update_text(
                gdrive,
                filename,
                md_content,
                conversations_folder,
                "text/markdown",
            )

    # 3. Export treatment timeline
    treatment_folder = folder_map.get("treatment")
    if treatment_folder:
        events = await db.get_treatment_events_timeline(limit=1000)
        md_content = render_treatment_timeline(events)
        _upload_or_update_text(
            gdrive,
            "treatment-timeline.md",
            md_content,
            treatment_folder,
            "text/markdown",
        )

    # 4. Export research library
    research_folder = folder_map.get("research")
    if research_folder:
        entries = await db.list_research_entries(limit=1000)
        md_content = render_research_library(entries)
        _upload_or_update_text(
            gdrive,
            "research-library.md",
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
) -> dict:
    """Run full bidirectional sync.

    1. sync_from_gdrive first (import human changes — GDrive wins)
    2. sync_to_gdrive second (export system changes)

    Returns combined stats.
    """
    logger.info("sync: starting bidirectional sync (dry_run=%s)", dry_run)

    from_stats = await sync_from_gdrive(
        db, files, gdrive, folder_id, dry_run=dry_run, enhance=enhance
    )
    to_stats = await sync_to_gdrive(db, files, gdrive, folder_id, dry_run=dry_run)

    combined = {
        "from_gdrive": from_stats,
        "to_gdrive": to_stats,
    }
    logger.info("sync: done — %s", combined)
    return combined


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
            enhanced = await _enhance_document(db, doc, files, gdrive)
            if enhanced:
                stats["processed"] += 1
            else:
                stats["skipped"] += 1
        except Exception:
            logger.exception("enhance_documents: error on doc %d (%s)", doc.id, doc.filename)
            stats["errors"] += 1

    logger.info("enhance_documents: done — %s", stats)
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
                    content_bytes = gdrive.download(doc.gdrive_id)

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
        logger.warning("enhance: no text available for doc %d (%s)", doc.id, doc.filename)
        return False

    full_text = "\n\n".join(text_parts)
    summary, tags_json = enhance_document_text(full_text)
    await db.update_document_ai_metadata(doc.id, summary, tags_json)

    # Extract structured metadata (diagnoses, medications, findings, etc.)
    try:
        metadata = extract_structured_metadata(full_text)
        await db.update_structured_metadata(doc.id, json.dumps(metadata, ensure_ascii=False))
        logger.info("enhance: doc %d (%s) — structured metadata extracted", doc.id, doc.filename)
    except Exception:
        logger.warning(
            "enhance: doc %d (%s) — structured metadata extraction failed",
            doc.id,
            doc.filename,
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

    Returns category string if parent folder matches a known category, else None.
    """
    parents = file_info.get("parents", [])
    valid_categories = {cat.value for cat in DocumentCategory}

    for parent_id in parents:
        folder_name = folder_map.get(parent_id, "")
        if folder_name in valid_categories:
            return folder_name
        # Check if parent of parent is a category (year-month subfolder case)
        # The folder_map tracks all folders, so we check if any ancestor is a category
    return None
