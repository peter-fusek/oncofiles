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

from erika_files_mcp.config import (
    DATABASE_PATH,
    GOOGLE_DRIVE_FOLDER_ID,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from erika_files_mcp.database import Database
from erika_files_mcp.files_api import FilesClient
from erika_files_mcp.ocr import OCR_MODEL, extract_text_from_image
from erika_files_mcp.server import _inline_content


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
            print(f"Warning: GDrive client init failed: {e}")

    docs = await db.list_documents(limit=500)
    print(f"Found {len(docs)} documents in database.\n")

    stats = {"total": len(docs), "skipped": 0, "processed": 0, "errors": 0}

    for doc in docs:
        # Check if already cached
        if await db.has_ocr_text(doc.id):
            print(f"  SKIP  {doc.original_filename} (already has OCR text)")
            stats["skipped"] += 1
            continue

        if dry_run:
            print(f"  WOULD {doc.original_filename}")
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
                    print(f"  ERROR {doc.original_filename} — download failed: {e}")
                    stats["errors"] += 1
                    continue
            else:
                print(f"  ERROR {doc.original_filename} — not downloadable")
                stats["errors"] += 1
                continue

        # Convert to images
        content_items = _inline_content(doc, content_bytes)
        from fastmcp.utilities.types import Image

        images = [item for item in content_items if isinstance(item, Image)]
        if not images:
            print(f"  SKIP  {doc.original_filename} (no images to OCR)")
            stats["skipped"] += 1
            continue

        # OCR each page
        try:
            for page_num, image in enumerate(images, start=1):
                text = extract_text_from_image(image)
                await db.save_ocr_page(doc.id, page_num, text, OCR_MODEL)
            print(f"  OCR   {doc.original_filename} ({len(images)} pages)")
            stats["processed"] += 1
        except Exception as e:
            print(f"  ERROR {doc.original_filename} — OCR failed: {e}")
            stats["errors"] += 1

    await db.close()

    print(f"\n{'Dry run — no changes made.' if dry_run else 'Backfill complete.'}")
    print(f"  Total:     {stats['total']}")
    print(f"  Processed: {stats['processed']}")
    print(f"  Skipped:   {stats['skipped']}")
    print(f"  Errors:    {stats['errors']}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill OCR text for existing documents")
    parser.add_argument("--dry-run", action="store_true", help="Preview without running OCR")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
