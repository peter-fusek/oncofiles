"""Backfill gdrive_id for existing documents by matching filenames.

Lists all files in the Google Drive Erika folder, matches them to DB documents
by original_filename, and updates the gdrive_id + gdrive_modified_time columns.

Usage:
    uv run python -m erika_files_mcp.backfill_gdrive_ids --dry-run
    uv run python -m erika_files_mcp.backfill_gdrive_ids
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from erika_files_mcp.config import (
    DATABASE_PATH,
    GOOGLE_DRIVE_FOLDER_ID,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from erika_files_mcp.database import Database
from erika_files_mcp.gdrive_client import create_gdrive_client


async def backfill(dry_run: bool = False, folder_id: str = "") -> dict[str, int]:
    """Match GDrive files to DB documents and update gdrive_id.

    Returns summary dict with counts: total_gdrive, matched, already_set, not_found.
    """
    folder_id = folder_id or GOOGLE_DRIVE_FOLDER_ID
    if not folder_id:
        print("Error: GOOGLE_DRIVE_FOLDER_ID not set. Pass --folder-id or set env var.")
        sys.exit(1)

    gdrive = create_gdrive_client()
    if not gdrive:
        print("Error: No GDrive credentials configured.")
        sys.exit(1)

    print(f"Listing files in GDrive folder {folder_id}...")
    gdrive_files = gdrive.list_folder(folder_id)
    print(f"Found {len(gdrive_files)} files in Google Drive.\n")

    # Build lookup: filename → gdrive file info
    gdrive_by_name: dict[str, dict] = {}
    for gf in gdrive_files:
        gdrive_by_name[gf["name"]] = gf

    if TURSO_DATABASE_URL:
        db = Database(turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    else:
        db = Database(DATABASE_PATH)
    await db.connect()
    await db.migrate()

    docs = await db.list_documents(limit=500)
    print(f"Found {len(docs)} documents in database.\n")

    stats = {"total_gdrive": len(gdrive_files), "matched": 0, "already_set": 0, "not_found": 0}

    for doc in docs:
        gf = gdrive_by_name.get(doc.original_filename)
        if not gf:
            print(f"  MISS  {doc.original_filename}")
            stats["not_found"] += 1
            continue

        if doc.gdrive_id:
            print(f"  SKIP  {doc.original_filename} (already has gdrive_id)")
            stats["already_set"] += 1
            continue

        modified_time = gf.get("modifiedTime", "")
        if dry_run:
            print(f"  MATCH {doc.original_filename} → {gf['id']}")
        else:
            await db.update_gdrive_id(doc.id, gf["id"], modified_time)
            print(f"  SET   {doc.original_filename} → {gf['id']}")
        stats["matched"] += 1

    await db.close()

    print(f"\n{'Dry run — no changes made.' if dry_run else 'Backfill complete.'}")
    print(f"  GDrive files:  {stats['total_gdrive']}")
    print(f"  Matched:       {stats['matched']}")
    print(f"  Already set:   {stats['already_set']}")
    print(f"  Not found:     {stats['not_found']}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill gdrive_id for existing documents")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--folder-id", default="", help="GDrive folder ID (overrides env)")
    args = parser.parse_args()
    asyncio.run(backfill(dry_run=args.dry_run, folder_id=args.folder_id))


if __name__ == "__main__":
    main()
