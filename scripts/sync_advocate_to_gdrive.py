"""Upload advocate notes to GDrive and update prod DB with gdrive_ids.

Uses user OAuth tokens from prod DB (not service account, which lacks upload quota).

Usage:
    uv run python scripts/sync_advocate_to_gdrive.py [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from oncofiles.config import (  # noqa: E402
    GOOGLE_DRIVE_FOLDER_ID,
    TURSO_AUTH_TOKEN,
    TURSO_DATABASE_URL,
)
from oncofiles.database import Database  # noqa: E402
from oncofiles.gdrive_client import GDriveClient  # noqa: E402
from oncofiles.gdrive_folders import ensure_folder_structure, ensure_year_month_folder  # noqa: E402

logger = logging.getLogger(__name__)

ADVOCATE_DOC_IDS = list(range(46, 62))  # IDs 46-61

ARCHIVE_DIR = Path.home() / "Downloads" / "Archive"
PDF_PATH = Path.home() / "Downloads" / "2026-02-12 sumar ochorenia.pdf"

# Import the FILE_MAP from the import script to resolve local paths
sys.path.insert(0, str(Path(__file__).resolve().parent))
from import_advocate_notes import FILE_MAP  # noqa: E402

# Build reverse lookup: target filename → local path
_LOCAL_FILES: dict[str, Path] = {}
for src_name, target_name in FILE_MAP:
    _LOCAL_FILES[target_name] = ARCHIVE_DIR / src_name
_LOCAL_FILES[
    "20260212 ErikaFusekova-PacientAdvokat-Other-SumarOchorenia.pdf"
] = PDF_PATH


async def _get_oauth_gdrive(db: Database) -> GDriveClient | None:
    """Create GDrive client from user OAuth tokens stored in prod DB."""
    async with db.db.execute(
        "SELECT access_token, refresh_token, owner_email "
        "FROM oauth_tokens ORDER BY id DESC LIMIT 1"
    ) as c:
        row = await c.fetchone()
        if not row:
            return None

    # Load client credentials from local GCP client secrets file
    import json

    secrets_path = Path.home() / (
        "Downloads/client_secret_2_1046242484537-"
        "l8fo2fupar88tvboiggo25tkf7c2u5js.apps.googleusercontent.com.json"
    )
    with open(secrets_path) as f:
        secrets = json.load(f)
    web = secrets.get("web", secrets.get("installed", {}))

    # Refresh the token
    import httpx

    access_token = row["access_token"]
    try:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "refresh_token": row["refresh_token"],
                "client_id": web["client_id"],
                "client_secret": web["client_secret"],
                "grant_type": "refresh_token",
            },
        )
        resp.raise_for_status()
        access_token = resp.json()["access_token"]
        logger.info("Refreshed OAuth access token")
    except Exception as e:
        logger.warning("Token refresh failed, using stored token: %s", e)

    return GDriveClient.from_oauth(
        access_token=access_token,
        refresh_token=row["refresh_token"],
        client_id=web["client_id"],
        client_secret=web["client_secret"],
        owner_email=row["owner_email"],
    )


async def sync_to_gdrive(dry_run: bool = False) -> None:
    db = Database(":memory:", turso_url=TURSO_DATABASE_URL, turso_token=TURSO_AUTH_TOKEN)
    await db.connect()

    gdrive = await _get_oauth_gdrive(db)
    if not gdrive:
        logger.error("No OAuth tokens found in prod DB")
        return

    folder_id = GOOGLE_DRIVE_FOLDER_ID
    if not folder_id:
        logger.error("GOOGLE_DRIVE_FOLDER_ID not set")
        return

    folder_map = ensure_folder_structure(gdrive, folder_id)
    logger.info("Folder structure ready (%d folders)", len(folder_map))

    synced = 0
    for doc_id in ADVOCATE_DOC_IDS:
        doc = await db.get_document(doc_id)
        if not doc:
            logger.warning("Doc %d not found", doc_id)
            continue
        if doc.gdrive_id:
            logger.info("Doc %d already synced: %s", doc_id, doc.gdrive_id)
            continue

        cat_folder = folder_map.get(doc.category.value, folder_id)
        target_folder = cat_folder
        if doc.document_date:
            target_folder = ensure_year_month_folder(
                gdrive, cat_folder, doc.document_date.isoformat()
            )

        if dry_run:
            logger.info(
                "DRY RUN: would upload doc %d (%s) to folder %s",
                doc.id, doc.filename, target_folder,
            )
            continue

        local_path = _LOCAL_FILES.get(doc.filename)
        if not local_path or not local_path.exists():
            logger.error("Doc %d: local file not found for %s", doc.id, doc.filename)
            continue
        file_bytes = local_path.read_bytes()

        try:
            uploaded = gdrive.upload(
                filename=doc.filename,
                content_bytes=file_bytes,
                mime_type=doc.mime_type,
                folder_id=target_folder,
                app_properties={"oncofiles_id": str(doc.id)},
            )
            gdrive_id = uploaded["id"]
            modified_time = uploaded.get("modifiedTime", "")
            await db.update_gdrive_id(doc.id, gdrive_id, modified_time)
            logger.info("Doc %d → GDrive %s (%s)", doc.id, gdrive_id, doc.filename)
            synced += 1
        except Exception as e:
            logger.error("Doc %d: GDrive upload failed: %s", doc.id, e)

    await db.close()
    logger.info("Done: %d docs synced to GDrive", synced)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-8s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(sync_to_gdrive(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
