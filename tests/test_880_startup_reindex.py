"""Startup re-index covers every curated patient with a stale index (#880).

After an ``INDEX_VERSION`` bump the startup index used to reach only patients
enrolled in the loop (``nightly_enabled = 1``); a curated patient filed by
manual runs but not enrolled kept the old parser generation. Synthetic
patients and ids only (public repo).
"""

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from oncofiles.curated.indexer import INDEX_VERSION
from oncofiles.tools import curated as tool

ENROLLED_STALE = "00000000-0000-4000-8000-00000000c801"  # invented patients
LOOSE_STALE = "00000000-0000-4000-8000-00000000c802"
LOOSE_CURRENT = "00000000-0000-4000-8000-00000000c803"
NO_CURATED = "00000000-0000-4000-8000-00000000c804"
LEGACY_ONLY = "00000000-0000-4000-8000-00000000c805"
DELETED_ONLY = "00000000-0000-4000-8000-00000000c806"
ENROLLED_CURRENT = "00000000-0000-4000-8000-00000000c807"

ALL = (
    ENROLLED_STALE,
    LOOSE_STALE,
    LOOSE_CURRENT,
    NO_CURATED,
    LEGACY_ONLY,
    DELETED_ONLY,
    ENROLLED_CURRENT,
)

NO_WAIT = {  # loop timing, not data
    "poll_s": 1,
    "startup_grace_s": 0,
    "probe_delay_s": 0,
}
_next_id = iter(range(88001, 89000))


def _meta(version: int | None) -> str:
    curated = {"sidecar_id": "side-x", "sidecar_modified": "2031-07-19T13:37:42Z"}
    if version is not None:
        curated["index_version"] = version
    return json.dumps({"source": "curated", "curated": curated})


async def _doc(db, pid, *, file_id: str, meta: str | None, deleted: bool = False) -> None:
    doc_id = next(_next_id)
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, "
        "mime_type, size_bytes, patient_id, gdrive_id, structured_metadata, deleted_at) "
        "VALUES (?, ?, ?, ?, 'labs', 'application/pdf', 1, ?, ?, ?, ?)",
        (
            doc_id,
            file_id,
            f"d{doc_id}.pdf",
            f"d{doc_id}.pdf",
            pid,
            file_id.removeprefix("gdrive:"),
            meta,
            "2027-03-01T00:00:00" if deleted else None,
        ),
    )


@pytest.fixture
async def world(db):
    for i, pid in enumerate(ALL):
        await db.db.execute(
            "INSERT INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
            (pid, f"synth-880-{i}", f"Synthetic {i}"),
        )
    await db.db.commit()
    old = INDEX_VERSION - 1
    await _doc(db, ENROLLED_STALE, file_id="gdrive:es1", meta=_meta(old))
    # non-enrolled, documents filed by manual runs: one stale row is enough
    await _doc(db, LOOSE_STALE, file_id="gdrive:ls1", meta=_meta(INDEX_VERSION))
    await _doc(db, LOOSE_STALE, file_id="gdrive:ls2", meta=_meta(old))
    await _doc(db, LOOSE_CURRENT, file_id="gdrive:lc1", meta=_meta(INDEX_VERSION))
    await _doc(db, NO_CURATED, file_id="upload-1", meta=_meta(None))  # not a mirror row
    # #777: a legacy Drive-only row shares the prefix but is not the indexer's
    await _doc(db, LEGACY_ONLY, file_id="gdrive:lg1", meta=json.dumps({"ocr_engine": "x"}))
    await _doc(db, DELETED_ONLY, file_id="gdrive:dl1", meta=_meta(old), deleted=True)
    await _doc(db, ENROLLED_CURRENT, file_id="gdrive:ec1", meta=_meta(INDEX_VERSION))
    await db.db.commit()
    for pid, on in ((ENROLLED_STALE, True), (ENROLLED_CURRENT, True), (LOOSE_STALE, False),
                    (LOOSE_CURRENT, False)):  # fmt: skip
        await db.set_curated_nightly(patient_id=pid, enabled=on, updated_by="test")
    tool._startup_index_stats.clear()
    yield db
    tool._startup_index_stats.clear()


async def test_helper_lists_only_patients_with_stale_curated_rows(world):
    got = await world.list_stale_curated_index_patients(INDEX_VERSION)
    assert got == [ENROLLED_STALE, LOOSE_STALE]


async def test_helper_counts_a_row_without_a_stored_version_as_stale(world):
    await _doc(world, LOOSE_CURRENT, file_id="gdrive:lc2", meta=_meta(None))
    await world.db.commit()
    assert LOOSE_CURRENT in await world.list_stale_curated_index_patients(INDEX_VERSION)


async def test_startup_index_covers_non_enrolled_stale_patients(world):
    indexed: list[str] = []

    async def fake_index(_db, pid, **_kw):
        indexed.append(pid)
        return {}

    with patch.object(tool, "index_patient", fake_index):
        targets = await tool._startup_index_targets(world)
        await tool._startup_index(world, targets)
    # enrolled (stale or current) + the non-enrolled stale one; the current
    # non-enrolled patient and patients without curated rows are never touched
    assert sorted(indexed) == sorted([ENROLLED_STALE, ENROLLED_CURRENT, LOOSE_STALE])
    for pid in (LOOSE_CURRENT, NO_CURATED, LEGACY_ONLY, DELETED_ONLY):
        assert pid not in indexed
    assert not targets
    stats = tool.startup_index_stats()
    assert stats == {
        "index_version": INDEX_VERSION,
        "enrolled": 2,
        "stale_index": 2,
        "stale_not_enrolled": 1,
        "due": 3,
        "indexed": 3,
        "failed": 0,
        "pending": 0,
    }
    assert all(isinstance(v, int) for v in stats.values())  # counts only, no ids


async def test_locked_patient_is_retried_next_iteration_without_waiting(world):
    indexed: list[str] = []

    async def fake_index(_db, pid, **_kw):
        indexed.append(pid)
        return {}

    with patch.object(tool, "index_patient", fake_index):
        targets = await tool._startup_index_targets(world)
        async with tool.patient_lock(LOOSE_STALE):  # a job holds the lock
            await asyncio.wait_for(tool._startup_index(world, targets), timeout=1)
        assert LOOSE_STALE not in indexed and targets == {LOOSE_STALE}
        assert tool.startup_index_stats()["pending"] == 1
        await tool._startup_index(world, targets)  # next loop iteration
    assert indexed.count(LOOSE_STALE) == 1 and not targets
    assert tool.startup_index_stats()["pending"] == 0


async def test_a_failing_index_is_counted_and_not_retried_forever(world):
    with patch.object(tool, "index_patient", AsyncMock(side_effect=RuntimeError("x"))):
        targets = await tool._startup_index_targets(world)
        await tool._startup_index(world, targets)
    stats = tool.startup_index_stats()
    assert stats["failed"] == 3 and stats["indexed"] == 0 and not targets


async def test_the_loop_indexes_a_non_enrolled_stale_patient_after_the_grace(world):
    indexed: list[str] = []

    async def fake_index(_db, pid, **_kw):
        indexed.append(pid)
        if pid == LOOSE_STALE:
            tool._stopping.set()  # the non-enrolled patient was reached: done
        return {}

    tool._stopping.clear()
    try:
        with (
            patch.object(tool, "index_patient", fake_index),
            patch.object(tool, "nightly_settings", lambda: (False, 1, 1)),
        ):
            await asyncio.wait_for(
                tool.curated_nightly_loop(world, **NO_WAIT),
                timeout=5,
            )
    finally:
        tool._stopping.clear()
    assert indexed == [ENROLLED_STALE, LOOSE_STALE]  # sorted ids; stopped after it
    assert tool.startup_index_stats()["stale_not_enrolled"] == 1
