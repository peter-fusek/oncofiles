"""Tests for migration 045: fix e5g document dates incorrectly set to 2026-04-05 (#262)."""

import json

from oncofiles.database import Database
from tests.helpers import ERIKA_UUID

_MIGRATION_SQL = """
UPDATE documents
SET
    document_date = (
        SELECT MIN(je.value)
        FROM json_each(structured_metadata, '$.dates_mentioned') AS je
        WHERE date(je.value) IS NOT NULL
          AND je.value != '2026-04-05'
          AND je.value >= '1970-01-01'
          AND je.value <= '2025-12-31'
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE document_date = '2026-04-05'
  AND structured_metadata IS NOT NULL
  AND json_valid(structured_metadata)
  AND (
        SELECT MIN(je.value)
        FROM json_each(structured_metadata, '$.dates_mentioned') AS je
        WHERE date(je.value) IS NOT NULL
          AND je.value != '2026-04-05'
          AND je.value >= '1970-01-01'
          AND je.value <= '2025-12-31'
  ) IS NOT NULL
"""


async def _insert_raw(db: Database, file_id: str, document_date: str, metadata: dict) -> None:
    await db.db.execute(
        "INSERT INTO documents "
        "(file_id, filename, original_filename, document_date, category, patient_id, structured_metadata) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            file_id,
            f"{file_id}.pdf",
            f"{file_id}.pdf",
            document_date,
            "other",
            ERIKA_UUID,
            json.dumps(metadata),
        ),
    )
    await db.db.commit()


async def _get_date(db: Database, file_id: str) -> str | None:
    async with db.db.execute(
        "SELECT document_date FROM documents WHERE file_id = ?", (file_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return row["document_date"] if isinstance(row, dict) else row[0]


async def test_migration_updates_date_from_dates_mentioned(db: Database):
    """Doc with wrong date gets earliest valid date from dates_mentioned."""
    await _insert_raw(
        db,
        "doc_happy",
        "2026-04-05",
        {"dates_mentioned": ["2023-11-14", "2023-11-15", "2024-01-10"]},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_happy") == "2023-11-14"


async def test_migration_picks_earliest_date(db: Database):
    """Migration picks MIN date from dates_mentioned."""
    await _insert_raw(
        db,
        "doc_min",
        "2026-04-05",
        {"dates_mentioned": ["2022-06-01", "1998-03-15", "2020-09-22"]},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_min") == "1998-03-15"


async def test_migration_skips_doc_with_no_valid_dates(db: Database):
    """Doc with only invalid/future dates is not touched."""
    await _insert_raw(
        db,
        "doc_no_valid",
        "2026-04-05",
        {"dates_mentioned": ["2026-04-05", "not-a-date", "2027-01-01"]},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_no_valid") == "2026-04-05"


async def test_migration_skips_doc_with_empty_dates(db: Database):
    """Doc with empty dates_mentioned array is not touched."""
    await _insert_raw(
        db,
        "doc_empty",
        "2026-04-05",
        {"dates_mentioned": []},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_empty") == "2026-04-05"


async def test_migration_does_not_touch_correct_dates(db: Database):
    """Docs with document_date != '2026-04-05' are never modified."""
    await _insert_raw(
        db,
        "doc_correct",
        "2024-05-10",
        {"dates_mentioned": ["2023-01-01", "2022-06-15"]},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_correct") == "2024-05-10"


async def test_migration_idempotent(db: Database):
    """Running the migration twice produces the same result."""
    await _insert_raw(
        db,
        "doc_idem",
        "2026-04-05",
        {"dates_mentioned": ["2019-08-22", "2020-03-11"]},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_idem") == "2019-08-22"

    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_idem") == "2019-08-22"


async def test_migration_excludes_pre_1970_dates(db: Database):
    """Dates before 1970 are excluded (likely parsing artifacts)."""
    await _insert_raw(
        db,
        "doc_ancient",
        "2026-04-05",
        {"dates_mentioned": ["1969-12-31", "2001-07-04"]},
    )
    await db.db.execute(_MIGRATION_SQL)
    await db.db.commit()
    assert await _get_date(db, "doc_ancient") == "2001-07-04"
