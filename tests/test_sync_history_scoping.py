"""Lock tests: sync history never leaks across patients (#540, #532).

#541 (query_db admin-only tables) is locked in test_query_db_hardened.py / test_db_query.py.
"""

from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools import hygiene as hygiene_tools
from tests.helpers import ERIKA_UUID

OTHER_PID = "00000000-0000-4000-8000-0000000000b0"
LEAK_MARKER = "OTHER-PATIENT-SECRET-folder-xyz"


def _ctx(db: Database) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": MagicMock(), "gdrive": None}
    return ctx


async def _seed(db: Database) -> None:
    mine = await db.insert_sync_history("scheduled", patient_id=ERIKA_UUID)
    await db.complete_sync_history(mine, status="completed", duration_s=1.0, from_new=1)
    for _ in range(3):
        theirs = await db.insert_sync_history("scheduled", patient_id=OTHER_PID)
        await db.complete_sync_history(
            theirs, status="failed", duration_s=9.0, from_new=7, error_message=LEAK_MARKER
        )


async def test_system_health_scopes_sync_history(db: Database):
    await _seed(db)
    out = await hygiene_tools.system_health(_ctx(db))
    assert LEAK_MARKER not in out
    assert OTHER_PID not in out


async def test_get_pipeline_status_scopes_sync_history(db: Database):
    await _seed(db)
    out = await hygiene_tools.get_pipeline_status(_ctx(db))
    assert LEAK_MARKER not in out
    assert OTHER_PID not in out


async def test_pipeline_stats_scopes_sync_history(db: Database):
    await _seed(db)
    mine = await db.get_pipeline_stats(patient_id=ERIKA_UUID)
    assert mine.total_syncs == 1
    assert mine.failed_syncs == 0
    assert mine.total_docs_imported == 1


async def test_pipeline_stats_sentinel_matches_nothing(db: Database):
    await _seed(db)
    stats = await db.get_pipeline_stats(patient_id="")
    assert stats.total_syncs == 0


async def test_pipeline_stats_unscoped_is_global_for_admin_callers(db: Database):
    await _seed(db)
    stats = await db.get_pipeline_stats()
    assert stats.total_syncs == 4
    assert stats.total_docs_imported == 22
