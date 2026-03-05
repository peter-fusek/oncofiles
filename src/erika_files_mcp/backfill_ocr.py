"""Backfill OCR text cache for existing documents (#36).

Downloads each document, converts to images, runs Claude Vision OCR,
and stores extracted text in the document_pages table.

Usage:
    uv run python -m erika_files_mcp.backfill_ocr --dry-run
    uv run python -m erika_files_mcp.backfill_ocr
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from erika_files_mcp.config import (
    DATABASE_PATH,
    GOOGLE_DRIVE_FOLDER_ID,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from erika_files_mcp.database import Database
from erika_files_mcp.files_api import FilesClient
from erika_files_mcp.ocr import OCR_MODEL, extract_text_from_image
from erika_files_mcp.server import _extract_pdf_text, _inline_content, _resize_image_if_needed

logger = logging.getLogger(__name__)


async def backfill(dry_run: bool = False) -> dict[str, int]:
    """OCR all documents that don't have cached text yet.

    Returns summary dict with counts: total, skipped, processed, errors.
    """
    if TURSO_DATABASE_URL:
        db = Database(turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()

    files = FilesClient()

    # Optional GDrive fallback
    gdrive = None
    if GOOGLE_DRIVE_FOLDER_ID:
        try:
            from erika_files_mcp.gdrive_client import create_gdrive_client

            gdrive = create_gdrive_client()
        except Exception as e:
            logger.warning("GDrive client init failed: %s", e)

    docs = await db.list_documents(limit=500)
    logger.info("Found %d documents in database.", len(docs))

    stats = {"total": len(docs), "skipped": 0, "processed": 0, "errors": 0}

    for doc in docs:
        # Check if already cached
        if await db.has_ocr_text(doc.id):
            logger.debug("  SKIP  %s (already has OCR text)", doc.original_filename)
            stats["skipped"] += 1
            continue

        if dry_run:
            logger.info("  WOULD %s", doc.original_filename)
            stats["processed"] += 1
            continue

        # Download content
        content_bytes = None
        try:
            content_bytes = files.download(doc.file_id)
        except Exception:
            if gdrive and doc.gdrive_id:
                try:
                    content_bytes = gdrive.download(doc.gdrive_id)
                except Exception as e:
                    logger.error("  ERROR %s — download failed: %s", doc.original_filename, e)
                    stats["errors"] += 1
                    continue
            else:
                logger.error("  ERROR %s — not downloadable", doc.original_filename)
                stats["errors"] += 1
                continue

        # 1. For PDFs, try native text extraction first (free, fast)
        if doc.mime_type == "application/pdf":
            pdf_texts = _extract_pdf_text(content_bytes)
            if pdf_texts:
                for page_num, text in enumerate(pdf_texts, start=1):
                    await db.save_ocr_page(doc.id, page_num, text, "pymupdf-native")
                logger.info("  TEXT  %s (%d pages, native)", doc.original_filename, len(pdf_texts))
                stats["processed"] += 1
                continue

        # 2. Fall back to Vision OCR for scanned docs / images
        content_items = _inline_content(doc, content_bytes)
        from fastmcp.utilities.types import Image

        images = [item for item in content_items if isinstance(item, Image)]
        if not images:
            logger.info("  SKIP  %s (no images to OCR)", doc.original_filename)
            stats["skipped"] += 1
            continue

        try:
            for page_num, image in enumerate(images, start=1):
                resized = _resize_image_if_needed(image)
                text = extract_text_from_image(resized)
                await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
            logger.info("  OCR   %s (%d pages)", doc.original_filename, len(images))
            stats["processed"] += 1
        except Exception as e:
            logger.error("  ERROR %s — OCR failed: %s", doc.original_filename, e)
            stats["errors"] += 1

    await db.close()

    logger.info(
        "%s — total=%d processed=%d skipped=%d errors=%d",
        "Dry run" if dry_run else "Backfill complete",
        stats["total"],
        stats["processed"],
        stats["skipped"],
        stats["errors"],
    )

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill OCR text for existing documents")
    parser.add_argument("--dry-run", action="store_true", help="Preview without running OCR")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
