"""Backfill OCR text cache for existing documents (#36).

Downloads each document, converts to images, runs Claude Vision OCR,
and stores extracted text in the document_pages table.

Legacy one-off. It works on ONE patient, named explicitly with ``--patient``
(slug or UUID); there is no default patient (#551). It uses the CLOUD vision
model, so it refuses to start while cloud AI is off, and it never touches a
curated or Drive-only document (``file_id`` starting ``gdrive:``): those are
read by the local model only (#603, #777).

Usage:
    uv run python -m oncofiles.backfill_ocr --patient <slug> --dry-run
    uv run python -m oncofiles.backfill_ocr --patient <slug>
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from oncofiles.config import (
    DATABASE_PATH,
    DOC_QUERY_LIMIT,
    GOOGLE_DRIVE_FOLDER_ID,
)
from oncofiles.curated.indexer import is_curated_file_id
from oncofiles.database import Database
from oncofiles.files_api import FilesClient, files_api_refusal_reason
from oncofiles.ocr import OCR_MODEL, extract_text_from_image
from oncofiles.server import _extract_pdf_text, _inline_content, _resize_image_if_needed

logger = logging.getLogger(__name__)


async def backfill(
    *, patient: str, dry_run: bool = False, db: Database | None = None
) -> dict[str, int]:
    """OCR ONE patient's documents that don't have cached text yet.

    ``patient`` (slug or UUID) is required; an unknown or archived patient
    stops the script before anything is read. ``db`` is for tests (an already
    connected database, left open).

    Returns summary dict with counts: total, skipped, local_only, processed,
    errors.
    """
    if not (patient or "").strip():
        logger.error("No patient given. Pass --patient <slug or UUID>.")
        sys.exit(1)
    if (reason := files_api_refusal_reason()) is not None:
        logger.error("Cloud OCR is not available (%s). Nothing was read or changed.", reason)
        sys.exit(1)

    own_db = db is None
    if db is None:
        db = Database(DATABASE_PATH)
        await db.connect()
        await db.migrate()
    try:
        return await _backfill(db, patient.strip(), dry_run)
    finally:
        if own_db:
            await db.close()


async def _backfill(db: Database, patient: str, dry_run: bool) -> dict[str, int]:
    patient_id = await db.resolve_patient_id(patient)
    if not patient_id:
        logger.error("Unknown or archived patient. Nothing was read or changed.")
        sys.exit(1)

    files = FilesClient()

    # Optional GDrive fallback
    gdrive = None
    if GOOGLE_DRIVE_FOLDER_ID:
        try:
            from oncofiles.gdrive_client import create_gdrive_client

            gdrive = create_gdrive_client()
        except Exception as e:
            logger.warning("GDrive client init failed: %s", e)

    docs = await db.list_documents(limit=DOC_QUERY_LIMIT, patient_id=patient_id)
    logger.info("Found %d documents in database.", len(docs))

    stats = {"total": len(docs), "skipped": 0, "local_only": 0, "processed": 0, "errors": 0}

    for doc in docs:
        # Curated / Drive-only rows are never sent to a cloud model (#603, #777).
        # Checked here by the row itself: this process has not loaded the
        # curated-id registry that ``refuse_cloud`` relies on in the server.
        if is_curated_file_id(doc.file_id):
            logger.info("  LOCAL doc %s (curated or Drive-only, local model only)", doc.id)
            stats["local_only"] += 1
            continue

        # Check if already cached
        if await db.has_ocr_text(doc.id, patient_id=patient_id):
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
                text = extract_text_from_image(resized, document_id=doc.id)
                await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
            logger.info("  OCR   %s (%d pages)", doc.original_filename, len(images))
            stats["processed"] += 1
        except Exception as e:
            logger.error("  ERROR %s — OCR failed: %s", doc.original_filename, e)
            stats["errors"] += 1

    logger.info(
        "%s — total=%d processed=%d skipped=%d local_only=%d errors=%d",
        "Dry run" if dry_run else "Backfill complete",
        stats["total"],
        stats["processed"],
        stats["skipped"],
        stats["local_only"],
        stats["errors"],
    )

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill OCR text for existing documents")
    parser.add_argument("--patient", required=True, help="Patient slug or UUID (required)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without running OCR")
    args = parser.parse_args()
    asyncio.run(backfill(patient=args.patient, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
