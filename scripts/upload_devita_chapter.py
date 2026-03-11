"""Upload DeVita Ch 40 combined markdown to Oncofiles.

Uploads the combined OCR text (chapter_40_full.md) to Anthropic Files API,
inserts a DB record as 'reference' category, caches text as OCR page,
and runs AI enhancement + structured metadata extraction.

Usage:
    uv run python scripts/upload_devita_chapter.py [--dry-run] [--skip-ai]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oncofiles.config import DATABASE_PATH, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL  # noqa: E402
from oncofiles.database import Database  # noqa: E402
from oncofiles.enhance import enhance_document_text, extract_structured_metadata  # noqa: E402
from oncofiles.filename_parser import parse_filename  # noqa: E402
from oncofiles.files_api import FilesClient  # noqa: E402
from oncofiles.models import Document  # noqa: E402

logger = logging.getLogger(__name__)

SOURCE_PATH = Path(__file__).resolve().parent.parent / "data" / "devita-ch40" / "chapter_40_full.md"
TARGET_FILENAME = "20260311_DeVita_reference_Ch40CancerOfTheColon.md"
MIME_TYPE = "text/markdown"


async def main(dry_run: bool = False, skip_ai: bool = False) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not SOURCE_PATH.exists():
        logger.error("Source file not found: %s", SOURCE_PATH)
        sys.exit(1)

    content = SOURCE_PATH.read_bytes()
    text_content = content.decode("utf-8")
    logger.info("Read %d bytes (%d chars) from %s", len(content), len(text_content), SOURCE_PATH.name)

    if dry_run:
        parsed = parse_filename(TARGET_FILENAME)
        logger.info("[DRY RUN] Would upload as: %s", TARGET_FILENAME)
        logger.info("[DRY RUN] Parsed: date=%s, institution=%s, category=%s", parsed.document_date, parsed.institution, parsed.category)
        return

    # Connect to DB
    db = Database(DATABASE_PATH, turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    await db.connect()

    # Check for existing document with same filename
    existing = await db.get_active_document_by_filename(TARGET_FILENAME)
    if existing:
        logger.warning("Document already exists: id=%d, file_id=%s — skipping upload", existing.id, existing.file_id)
        logger.info("To re-upload, delete the existing document first.")
        await db.close()
        return

    # Upload to Files API
    files = FilesClient()
    logger.info("Uploading to Files API...")
    import io
    metadata = files.upload(io.BytesIO(content), TARGET_FILENAME, MIME_TYPE)
    logger.info("Files API: id=%s, size=%d", metadata.id, metadata.size_bytes)

    # Parse filename for structured metadata
    parsed = parse_filename(TARGET_FILENAME)

    doc = Document(
        file_id=metadata.id,
        filename=TARGET_FILENAME,
        original_filename="chapter_40_full.md",
        document_date=parsed.document_date,
        institution=parsed.institution,
        category=parsed.category,
        description=parsed.description,
        mime_type=metadata.mime_type,
        size_bytes=metadata.size_bytes,
    )

    doc = await db.insert_document(doc)
    logger.info("Inserted doc id=%d, category=%s", doc.id, doc.category.value)

    # Cache text as OCR page (enables search + enhancement)
    await db.save_ocr_page(doc.id, 1, text_content, "text-import")
    logger.info("Cached %d chars as OCR page", len(text_content))

    if not skip_ai:
        # AI enhancement
        logger.info("Running AI enhancement...")
        # Use first 8000 chars for summary (Haiku context limit)
        summary_text = text_content[:8000]
        summary, tags = enhance_document_text(summary_text)
        if summary:
            await db.update_document_ai_metadata(doc.id, summary, tags)
            logger.info("AI summary: %s", summary[:100])
            logger.info("AI tags: %s", tags)

        # Structured metadata extraction
        logger.info("Extracting structured metadata...")
        struct_meta = extract_structured_metadata(summary_text)
        if struct_meta:
            meta_dict = json.loads(struct_meta) if isinstance(struct_meta, str) else struct_meta
            await db.update_structured_metadata(doc.id, meta_dict)
            logger.info("Structured metadata: %s", json.dumps(meta_dict)[:200])

    logger.info("Done! Document id=%d uploaded successfully.", doc.id)
    await db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Upload DeVita Ch 40 to Oncofiles")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't upload")
    parser.add_argument("--skip-ai", action="store_true", help="Skip AI enhancement")
    args = parser.parse_args()
    asyncio.run(main(dry_run=args.dry_run, skip_ai=args.skip_ai))
