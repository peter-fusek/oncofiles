"""Bidirectional Google Drive sync logic (#v0.9)."""

from __future__ import annotations

import io
import logging
import mimetypes
from datetime import datetime

from erika_files_mcp.database import Database
from erika_files_mcp.enhance import enhance_document_text
from erika_files_mcp.filename_parser import parse_filename
from erika_files_mcp.files_api import FilesClient
from erika_files_mcp.gdrive_client import GDriveClient
from erika_files_mcp.models import Document

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

    Returns summary dict: {new, updated, unchanged, skipped, errors}.
    """
    logger.info("sync_from_gdrive: listing folder %s (dry_run=%s)", folder_id, dry_run)
    gdrive_files = gdrive.list_folder(folder_id)
    logger.info("sync_from_gdrive: found %d files in GDrive", len(gdrive_files))

    stats = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "errors": 0}

    for gf in gdrive_files:
        filename = gf["name"]
        gdrive_id = gf["id"]
        modified_time_str = gf.get("modifiedTime", "")
        mime_type = gf.get("mimeType", "application/octet-stream")

        if not _should_sync(filename):
            logger.debug("sync_from_gdrive: skipping %s (unsupported type)", filename)
            stats["skipped"] += 1
            continue

        try:
            existing = await db.get_document_by_gdrive_id(gdrive_id)

            if existing:
                # Check if modified
                gdrive_modified = _parse_gdrive_time(modified_time_str)
                if existing.gdrive_modified_time and gdrive_modified:
                    # Normalize both to naive UTC for comparison
                    gd_naive = gdrive_modified.replace(tzinfo=None)
                    ex_naive = existing.gdrive_modified_time.replace(tzinfo=None)
                    if gd_naive <= ex_naive:
                        stats["unchanged"] += 1
                        continue

                # File changed on GDrive — re-import
                if dry_run:
                    logger.info("sync_from_gdrive: WOULD UPDATE %s", filename)
                    stats["updated"] += 1
                    continue

                logger.info("sync_from_gdrive: updating %s", filename)
                content_bytes = gdrive.download(gdrive_id)
                metadata = files.upload(io.BytesIO(content_bytes), filename, mime_type)
                await db.update_document_file_id(existing.id, metadata.id, len(content_bytes))
                await db.update_gdrive_id(existing.id, gdrive_id, modified_time_str)

                # Re-run AI enhancement
                if enhance:
                    await _enhance_document(db, existing, files, gdrive)

                # Clear OCR cache so it's regenerated
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

                doc = Document(
                    file_id=metadata.id,
                    filename=filename,
                    original_filename=filename,
                    document_date=parsed.document_date,
                    institution=parsed.institution,
                    category=parsed.category,
                    description=parsed.description,
                    mime_type=guessed_mime,
                    size_bytes=len(content_bytes),
                    gdrive_id=gdrive_id,
                    gdrive_modified_time=gdrive_modified,
                )
                doc = await db.insert_document(doc)

                # AI enhancement
                if enhance:
                    await _enhance_document(db, doc, files, gdrive)

                stats["new"] += 1

        except Exception:
            logger.exception("sync_from_gdrive: error processing %s", filename)
            stats["errors"] += 1

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
    """Export documents from oncofiles to GDrive that don't have a gdrive_id.

    Returns summary dict: {exported, skipped, errors}.
    """
    logger.info("sync_to_gdrive: starting (dry_run=%s)", dry_run)
    docs = await db.list_documents(limit=500)

    stats = {"exported": 0, "skipped": 0, "errors": 0}

    for doc in docs:
        if doc.gdrive_id:
            stats["skipped"] += 1
            continue

        try:
            if dry_run:
                logger.info("sync_to_gdrive: WOULD EXPORT %s", doc.filename)
                stats["exported"] += 1
                continue

            logger.info("sync_to_gdrive: exporting %s", doc.filename)

            # Download from Files API
            content_bytes = files.download(doc.file_id)

            # Upload to GDrive
            uploaded = gdrive.upload(
                filename=doc.filename,
                content_bytes=content_bytes,
                mime_type=doc.mime_type,
                folder_id=folder_id,
            )

            modified_time = uploaded.get("modifiedTime", "")
            await db.update_gdrive_id(doc.id, uploaded["id"], modified_time)

            stats["exported"] += 1

        except Exception:
            logger.exception("sync_to_gdrive: error exporting %s", doc.filename)
            stats["errors"] += 1

    logger.info("sync_to_gdrive: done — %s", stats)
    return stats


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

        from erika_files_mcp.server import _extract_pdf_text

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
