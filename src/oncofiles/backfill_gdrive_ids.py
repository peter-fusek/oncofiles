"""Backfill gdrive_id for existing documents by matching filenames.

Lists all files in the Google Drive medical folder, matches them to DB documents
by original_filename, and updates the gdrive_id + gdrive_modified_time columns.

Legacy one-off from before per-patient Drive folders. It works on ONE patient,
named explicitly with ``--patient`` (slug or UUID); there is no default patient
(#551). The folder (``--folder-id`` or ``GOOGLE_DRIVE_FOLDER_ID``) must be that
patient's folder: matching is by filename only.

Usage:
    uv run python -m oncofiles.backfill_gdrive_ids --patient <slug> --dry-run
    uv run python -m oncofiles.backfill_gdrive_ids --patient <slug>
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
from oncofiles.database import Database
from oncofiles.gdrive_client import create_gdrive_client

logger = logging.getLogger(__name__)


async def backfill(
    *, patient: str, dry_run: bool = False, folder_id: str = "", db: Database | None = None
) -> dict[str, int]:
    """Match GDrive files to ONE patient's DB documents and update gdrive_id.

    ``patient`` (slug or UUID) is required; an unknown or archived patient
    stops the script before any Drive call. ``db`` is for tests (an already
    connected database, left open).

    Returns summary dict with counts: total_gdrive, matched, already_set, not_found.
    """
    if not (patient or "").strip():
        logger.error("No patient given. Pass --patient <slug or UUID>.")
        sys.exit(1)
    folder_id = folder_id or GOOGLE_DRIVE_FOLDER_ID
    if not folder_id:
        logger.error("GOOGLE_DRIVE_FOLDER_ID not set. Pass --folder-id or set env var.")
        sys.exit(1)

    own_db = db is None
    if db is None:
        db = Database(DATABASE_PATH)
        await db.connect()
        await db.migrate()
    try:
        return await _backfill(db, patient.strip(), dry_run, folder_id)
    finally:
        if own_db:
            await db.close()


async def _backfill(db: Database, patient: str, dry_run: bool, folder_id: str) -> dict[str, int]:
    patient_id = await db.resolve_patient_id(patient)
    if not patient_id:
        logger.error("Unknown or archived patient. Nothing was read or changed.")
        sys.exit(1)

    gdrive = create_gdrive_client()
    if not gdrive:
        logger.error("No GDrive credentials configured.")
        sys.exit(1)

    logger.info("Listing files in GDrive folder %s...", folder_id)
    gdrive_files = gdrive.list_folder(folder_id)
    logger.info("Found %d files in Google Drive.", len(gdrive_files))

    # Build lookup: filename → gdrive file info
    gdrive_by_name: dict[str, dict] = {}
    for gf in gdrive_files:
        gdrive_by_name[gf["name"]] = gf

    docs = await db.list_documents(limit=DOC_QUERY_LIMIT, patient_id=patient_id)
    logger.info("Found %d documents in database.", len(docs))

    stats = {"total_gdrive": len(gdrive_files), "matched": 0, "already_set": 0, "not_found": 0}

    for doc in docs:
        gf = gdrive_by_name.get(doc.original_filename)
        if not gf:
            logger.info("  MISS  %s", doc.original_filename)
            stats["not_found"] += 1
            continue

        if doc.gdrive_id:
            logger.info("  SKIP  %s (already has gdrive_id)", doc.original_filename)
            stats["already_set"] += 1
            continue

        modified_time = gf.get("modifiedTime", "")
        if dry_run:
            logger.info("  MATCH %s -> %s", doc.original_filename, gf["id"])
        else:
            await db.update_gdrive_id(doc.id, gf["id"], modified_time)
            logger.info("  SET   %s -> %s", doc.original_filename, gf["id"])
        stats["matched"] += 1

    logger.info(
        "%s — gdrive=%d matched=%d already=%d not_found=%d",
        "Dry run" if dry_run else "Backfill complete",
        stats["total_gdrive"],
        stats["matched"],
        stats["already_set"],
        stats["not_found"],
    )

    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser(description="Backfill gdrive_id for existing documents")
    parser.add_argument("--patient", required=True, help="Patient slug or UUID (required)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--folder-id", default="", help="GDrive folder ID (overrides env)")
    args = parser.parse_args()
    asyncio.run(backfill(patient=args.patient, dry_run=args.dry_run, folder_id=args.folder_id))


if __name__ == "__main__":
    main()
