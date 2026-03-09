"""Import Apple Notes export (patient advocate notes) into Oncofiles.

Reads 15 MD files from ~/Downloads/Archive/ and 1 PDF, uploads to
Anthropic Files API, inserts DB records, caches text as OCR pages,
and runs AI enhancement + structured metadata extraction.

Usage:
    uv run python scripts/import_advocate_notes.py [--dry-run] [--skip-ai]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oncofiles.config import DATABASE_PATH, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL  # noqa: E402
from oncofiles.database import Database  # noqa: E402
from oncofiles.enhance import enhance_document_text, extract_structured_metadata  # noqa: E402
from oncofiles.filename_parser import parse_filename  # noqa: E402
from oncofiles.files_api import FilesClient  # noqa: E402
from oncofiles.models import Document, DocumentCategory  # noqa: E402

logger = logging.getLogger(__name__)

ARCHIVE_DIR = Path.home() / "Downloads" / "Archive"
PDF_PATH = Path.home() / "Downloads" / "2026-02-12 sumar ochorenia.pdf"

# Mapping: source filename → target oncofiles filename
FILE_MAP: list[tuple[str, str]] = [
    (
        "2025-01-22 Bory Nemocnica Prepustacia sprava nemocnica.md",
        "20250122 ErikaFusekova-PacientAdvokat-Other-BoryNemocnicaPrepustaciaSprava.md",
    ),
    (
        "2025-01-27 Sumar prepustacej spravy po operacii v Bory.md",
        "20250127 ErikaFusekova-PacientAdvokat-Other-SumarPrepustacejSpravyPoOperacii.md",
    ),
    (
        "2025-01-28 NOU Mudr. Porsok.md",
        "20250128 ErikaFusekova-PacientAdvokat-Other-NOU[PHYSICIAN_REDACTED]Otazky.md",
    ),
    (
        "2025-01-28, 2025-02-08 Bory otazky.md",
        "20250128 ErikaFusekova-PacientAdvokat-Other-BoryOtazky.md",
    ),
    (
        "2025-02-06 Mudr. Porsok NOU.md",
        "20250206 ErikaFusekova-PacientAdvokat-Other-[PHYSICIAN_REDACTED]NOUAnalyza.md",
    ),
    (
        "2026-02-03 Mudr porsok.md",
        "20260203 ErikaFusekova-PacientAdvokat-Other-[PHYSICIAN_REDACTED]LiecebnyPlan.md",
    ),
    (
        "2026-02-06 príjem 3. Chemo a možno zaciatok bio.md",
        "20260206 ErikaFusekova-PacientAdvokat-Other-PrijemChemoABioliecba.md",
    ),
    (
        "2026-02-12 sumar ochorenia.md",
        "20260212 ErikaFusekova-PacientAdvokat-Other-SumarOchorenia.md",
    ),
    (
        "2026-02-13 Mudr porsok klinické štúdie.md",
        "20260213 ErikaFusekova-PacientAdvokat-Other-KlinickeStudie.md",
    ),
    (
        "2026-02-20 Ľubomír Maslík.md",
        "20260220 ErikaFusekova-PacientAdvokat-Other-PsychoterapeutMaslik.md",
    ),
    (
        "2026-02-20 Mudr Porsok.md",
        "20260220 ErikaFusekova-PacientAdvokat-Other-[PHYSICIAN_REDACTED]HER2.md",
    ),
    (
        "2026-02-22 bioliecba pre MSSpMMR.md",
        "20260222 ErikaFusekova-PacientAdvokat-Other-BioliecbaPreMSSpMMR.md",
    ),
    (
        "2026-02-27 príjem chemo.md",
        "20260227 ErikaFusekova-PacientAdvokat-Other-PrijemChemo.md",
    ),
    (
        "2026-03-02 po 2. Chemo prepustenie\u2028\u2028Nižšie je jedna komplexná tabuľka\u2026.md",
        "20260302 ErikaFusekova-PacientAdvokat-Other-Po2ChemoPrepustenie.md",
    ),
    (
        "2026-03-13 pred 3.chemo.md",
        "20260313 ErikaFusekova-PacientAdvokat-Other-Pred3Chemo.md",
    ),
]

# Minimum text length to run AI enhancement (skip near-empty notes)
MIN_TEXT_FOR_AI = 100


async def import_notes(
    dry_run: bool = False, skip_ai: bool = False, prod: bool = False
) -> None:
    """Import advocate notes into oncofiles."""
    if prod:
        if not TURSO_DATABASE_URL:
            logger.error("TURSO_DATABASE_URL not set — cannot connect to prod")
            return
        logger.info("Connecting to PROD Turso: %s", TURSO_DATABASE_URL[:40])
        db = Database(":memory:", turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        logger.info("Using local DB: %s", DATABASE_PATH)
        db = Database(DATABASE_PATH)
    await db.connect()
    client = FilesClient()

    stats = {"imported": 0, "skipped": 0, "errors": 0, "ai_processed": 0}

    # ── Import MD files ──────────────────────────────────────────────────
    for i, (src_name, target_name) in enumerate(FILE_MAP, 1):
        src_path = ARCHIVE_DIR / src_name
        if not src_path.exists():
            logger.error("[%d/%d] NOT FOUND: %s", i, len(FILE_MAP), src_name)
            stats["errors"] += 1
            continue

        # Idempotency: check by original_filename
        existing = await db.get_document_by_original_filename(target_name)
        if existing:
            logger.info(
                "[%d/%d] SKIP (exists id=%d): %s", i, len(FILE_MAP), existing.id, target_name
            )
            stats["skipped"] += 1
            continue

        text_content = src_path.read_text(encoding="utf-8")
        file_bytes = src_path.read_bytes()

        if dry_run:
            parsed = parse_filename(target_name)
            parsed.category = DocumentCategory.ADVOCATE  # advocate notes are always "other"
            logger.info(
                "[%d/%d] DRY RUN: %s → %s (date=%s, inst=%s, cat=%s, %d bytes text)",
                i, len(FILE_MAP), src_name, target_name,
                parsed.document_date, parsed.institution, parsed.category.value,
                len(text_content),
            )
            continue

        try:
            # Upload to Anthropic Files API
            import io
            metadata = client.upload(io.BytesIO(file_bytes), target_name, "text/markdown")
            logger.info("[%d/%d] Uploaded %s → %s", i, len(FILE_MAP), target_name, metadata.id)

            # Parse filename for metadata — force "other" for advocate notes
            parsed = parse_filename(target_name)
            parsed.category = DocumentCategory.ADVOCATE

            doc = Document(
                file_id=metadata.id,
                filename=target_name,
                original_filename=target_name,
                document_date=parsed.document_date,
                institution=parsed.institution,
                category=parsed.category,
                description=parsed.description,
                mime_type="text/markdown",
                size_bytes=len(file_bytes),
            )
            doc = await db.insert_document(doc)
            logger.info(
                "  → DB id=%d, date=%s, cat=%s", doc.id, doc.document_date, doc.category.value
            )

            # Cache the markdown text as OCR page 1 (so enhance tools can find it)
            await db.save_ocr_page(doc.id, 1, text_content, "apple-notes-export")
            logger.info("  → OCR text cached (%d chars)", len(text_content))

            # AI enhancement (summary + tags + structured metadata)
            if not skip_ai and len(text_content.strip()) >= MIN_TEXT_FOR_AI:
                try:
                    summary, tags_json = enhance_document_text(text_content)
                    await db.update_document_ai_metadata(doc.id, summary, tags_json)
                    logger.info("  → AI summary: %s", summary[:80])

                    struct_meta = extract_structured_metadata(text_content)
                    await db.update_structured_metadata(doc.id, json.dumps(struct_meta))
                    logger.info("  → Structured metadata: %d findings, %d diagnoses",
                                len(struct_meta.get("findings", [])),
                                len(struct_meta.get("diagnoses", [])))
                    stats["ai_processed"] += 1
                except Exception as e:
                    logger.warning("  → AI enhancement failed: %s", e)
            elif not skip_ai:
                logger.info("  → Skipping AI (text too short: %d chars)", len(text_content.strip()))

            stats["imported"] += 1

        except Exception as e:
            logger.error("[%d/%d] ERROR importing %s: %s", i, len(FILE_MAP), src_name, e)
            stats["errors"] += 1

    # ── Import PDF ───────────────────────────────────────────────────────
    pdf_target = "20260212 ErikaFusekova-PacientAdvokat-Other-SumarOchorenia.pdf"
    if PDF_PATH.exists():
        existing = await db.get_document_by_original_filename(pdf_target)
        if existing:
            logger.info("[PDF] SKIP (exists id=%d): %s", existing.id, pdf_target)
            stats["skipped"] += 1
        elif dry_run:
            logger.info("[PDF] DRY RUN: %s → %s (%.1f MB)", PDF_PATH.name, pdf_target,
                        PDF_PATH.stat().st_size / 1024 / 1024)
        else:
            try:
                import io
                pdf_bytes = PDF_PATH.read_bytes()
                metadata = client.upload(io.BytesIO(pdf_bytes), pdf_target, "application/pdf")
                logger.info("[PDF] Uploaded %s → %s", pdf_target, metadata.id)

                parsed = parse_filename(pdf_target)
                doc = Document(
                    file_id=metadata.id,
                    filename=pdf_target,
                    original_filename=pdf_target,
                    document_date=parsed.document_date,
                    institution=parsed.institution,
                    category=parsed.category,
                    description=parsed.description,
                    mime_type="application/pdf",
                    size_bytes=len(pdf_bytes),
                )
                doc = await db.insert_document(doc)
                logger.info("  → DB id=%d", doc.id)
                # PDF shares content with the MD version — no separate AI processing needed
                stats["imported"] += 1
            except Exception as e:
                logger.error("[PDF] ERROR: %s", e)
                stats["errors"] += 1
    else:
        logger.warning("[PDF] Not found: %s", PDF_PATH)

    await db.close()

    logger.info(
        "Import complete: imported=%d, skipped=%d, errors=%d, ai_processed=%d",
        stats["imported"], stats["skipped"], stats["errors"], stats["ai_processed"],
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser(description="Import Apple Notes advocate notes")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--skip-ai", action="store_true", help="Skip AI enhancement")
    parser.add_argument("--prod", action="store_true", help="Import to prod Turso DB")
    args = parser.parse_args()
    asyncio.run(import_notes(dry_run=args.dry_run, skip_ai=args.skip_ai, prod=args.prod))


if __name__ == "__main__":
    main()
