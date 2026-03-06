"""One-time migration: export metadata from local SQLite → Turso cloud DB."""

from __future__ import annotations

import asyncio
import os
import sys

from oncofiles.config import DATABASE_PATH, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL
from oncofiles.database import Database


async def migrate() -> None:
    turso_url = TURSO_DATABASE_URL
    turso_token = TURSO_AUTH_TOKEN

    if not turso_url:
        print("ERROR: TURSO_DATABASE_URL not set")
        sys.exit(1)

    local_path = os.environ.get("LOCAL_DB_PATH", str(DATABASE_PATH))
    print(f"Source:  {local_path}")
    print(f"Target:  {turso_url}")

    # Connect to local SQLite
    local_db = Database(local_path)
    await local_db.connect()

    # Connect to Turso and run migrations (creates tables)
    turso_db = Database(turso_url=turso_url, turso_token=turso_token)
    await turso_db.connect()
    await turso_db.migrate()
    print("Turso schema migrated.")

    # Read all local documents
    docs = await local_db.list_documents(limit=500)
    print(f"Found {len(docs)} documents in local DB.")

    if not docs:
        print("Nothing to migrate.")
        return

    inserted = 0
    skipped = 0
    for doc in docs:
        # Check if already exists in Turso (idempotent)
        existing = await turso_db.get_document_by_file_id(doc.file_id)
        if existing:
            skipped += 1
            continue

        doc.id = None  # Let Turso auto-assign ID
        await turso_db.insert_document(doc)
        inserted += 1
        print(f"  + {doc.filename}")

    await local_db.close()
    await turso_db.close()

    print(f"\nDone: {inserted} inserted, {skipped} skipped (already existed).")


def main() -> None:
    asyncio.run(migrate())


if __name__ == "__main__":
    main()
