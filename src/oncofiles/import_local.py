"""Bulk import of local medical documents into the Erika Files MCP system.

Walks the Google Drive sync folder recursively, uploads each PDF/JPG/PNG
to the Anthropic Files API, parses the filename, and stores metadata in SQLite.

Usage:
    uv run python -m oncofiles.import_local [--dry-run] [--path PATH]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import mimetypes
import sys
from datetime import datetime
from pathlib import Path

from oncofiles.config import DATABASE_PATH, GOOGLE_DRIVE_FOLDER_ID
from oncofiles.database import Database
from oncofiles.filename_parser import parse_filename
from oncofiles.files_api import FilesClient
from oncofiles.gdrive_client import create_gdrive_client
from oncofiles.models import Document

logger = logging.getLogger(__name__)

DEFAULT_SOURCE = Path.home() / (
    "Library/CloudStorage/GoogleDrive-peterfusek1980@gmail.com/My Drive/Zdravie/Erika"
)

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SKIP_EXTENSIONS = {".gdoc", ".xlsx", ".xls", ".ds_store"}
SKIP_PREFIXES = (".", "~")


def _should_import(path: Path) -> bool:
    """Check if a file should be imported."""
    if path.name.startswith(SKIP_PREFIXES):
        return False
    ext = path.suffix.lower()
    if ext in SKIP_EXTENSIONS:
        return False
    return ext in SUPPORTED_EXTENSIONS


def _collect_files(source_dir: Path) -> list[Path]:
    """Recursively collect importable files, skipping hidden dirs."""
    files = []
    for item in sorted(source_dir.rglob("*")):
        # Skip hidden directories
        if any(part.startswith(".") for part in item.relative_to(source_dir).parts):
            continue
        if item.is_file() and _should_import(item):
            files.append(item)
    return files


async def import_documents(
    source_dir: Path = DEFAULT_SOURCE,
    dry_run: bool = False,
) -> dict[str, int]:
    """Import all documents from source_dir into the system.

    Returns summary dict with counts: total, imported, skipped, errors.
    """
    if not source_dir.exists():
        logger.error("Source directory not found: %s", source_dir)
        sys.exit(1)

    files = _collect_files(source_dir)
    logger.info("Found %d importable files in %s", len(files), source_dir)

    if dry_run:
        for f in files:
            parsed = parse_filename(f.name)
            rel = f.relative_to(source_dir)
            cat = parsed.category.value
            inst = parsed.institution or "?"
            date_s = parsed.document_date.isoformat() if parsed.document_date else "????"
            logger.info("  [%s] %-20s %-12s %s", date_s, inst, cat, rel)
        logger.info("Dry run: %d files would be imported.", len(files))
        return {"total": len(files), "imported": 0, "skipped": 0, "errors": 0}

    db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()
    client = FilesClient()

    # Build GDrive filename → file info lookup for gdrive_id resolution
    gdrive_lookup: dict[str, dict] = {}
    gdrive = create_gdrive_client()
    if gdrive and GOOGLE_DRIVE_FOLDER_ID:
        logger.info("Building GDrive filename lookup...")
        for gf in gdrive.list_folder(GOOGLE_DRIVE_FOLDER_ID):
            gdrive_lookup[gf["name"]] = gf
        logger.info("  %d files indexed from Google Drive.", len(gdrive_lookup))

    stats = {"total": len(files), "imported": 0, "skipped": 0, "errors": 0}

    for i, filepath in enumerate(files, 1):
        name = filepath.name
        rel = filepath.relative_to(source_dir)

        # Idempotency check
        existing = await db.get_document_by_original_filename(name)
        if existing:
            logger.info("  [%d/%d] SKIP (exists) %s", i, len(files), rel)
            stats["skipped"] += 1
            continue

        try:
            # Upload to Files API
            logger.info("  [%d/%d] Uploading %s...", i, len(files), rel)
            metadata = client.upload_path(filepath)
            logger.info("  → %s", metadata.id)

            # Parse filename for metadata
            parsed = parse_filename(name)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"

            # Resolve GDrive file ID if available
            gf = gdrive_lookup.get(name)
            gdrive_id = gf["id"] if gf else None
            gdrive_modified_str = gf.get("modifiedTime") if gf else None
            gdrive_modified = (
                datetime.fromisoformat(gdrive_modified_str.replace("Z", "+00:00"))
                if gdrive_modified_str
                else None
            )

            doc = Document(
                file_id=metadata.id,
                filename=name,
                original_filename=name,
                document_date=parsed.document_date,
                institution=parsed.institution,
                category=parsed.category,
                description=parsed.description,
                mime_type=mime,
                size_bytes=filepath.stat().st_size,
                gdrive_id=gdrive_id,
                gdrive_modified_time=gdrive_modified,
            )

            await db.insert_document(doc)
            stats["imported"] += 1

        except Exception as e:
            logger.error("  ERROR: %s", e)
            stats["errors"] += 1

    await db.close()

    logger.info(
        "Import complete: total=%d imported=%d skipped=%d errors=%d",
        stats["total"],
        stats["imported"],
        stats["skipped"],
        stats["errors"],
    )

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser(description="Import local medical documents")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--path", type=Path, default=DEFAULT_SOURCE, help="Source directory")
    args = parser.parse_args()
    asyncio.run(import_documents(source_dir=args.path, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
