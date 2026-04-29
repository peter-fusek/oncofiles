"""Tests for ``consolidate_external_gdrive_files`` (#477 Issue 1).

The tool moves GDrive files flagged ``gdrive_parent_outside_root`` back into
the patient's sync root, chunked per-batch. These tests stub out
``_create_patient_clients`` so we can inject a MagicMock GDrive client and
exercise the tool without touching real OAuth.
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools import hygiene as hygiene_tools
from tests.conftest import ERIKA_UUID
from tests.helpers import make_doc


class _StubCtx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def _seed_external_doc(db: Database, *, gdrive_id: str, doc_id_hint: str) -> int:
    doc = make_doc(
        file_id=f"file_{doc_id_hint}",
        filename=f"20260313_Erika_PA_Advocate_{doc_id_hint}.md",
        mime_type="text/markdown",
        document_date=date(2026, 3, 13),
        gdrive_id=gdrive_id,
    )
    inserted = await db.insert_document(doc, patient_id=ERIKA_UUID)
    await db.set_gdrive_parent_outside_root(inserted.id, True)
    return inserted.id


def _patch_clients(monkeypatch, gdrive_mock, root="root_folder_xyz"):
    """Make ``_create_patient_clients`` return our mock client tuple."""
    from oncofiles import server as server_mod

    async def _fake(db, patient_id):
        return (gdrive_mock, None, None, root)

    monkeypatch.setattr(server_mod, "_create_patient_clients", _fake)


async def test_dry_run_lists_candidates_without_moving(db: Database, monkeypatch):
    doc_id = await _seed_external_doc(db, gdrive_id="gd_ext_1", doc_id_hint="1")
    gdrive = MagicMock()
    _patch_clients(monkeypatch, gdrive)

    ctx = _StubCtx(db)
    result = json.loads(await hygiene_tools.consolidate_external_gdrive_files(ctx, dry_run=True))

    assert result["dry_run"] is True
    assert len(result["moved"]) == 1
    assert result["moved"][0]["id"] == doc_id
    assert result["moved"][0]["planned_target_parent"] == "root_folder_xyz"
    # Dry-run does NOT call move_file
    gdrive.move_file.assert_not_called()
    # Flag still set
    refreshed = await db.get_document(doc_id, patient_id=ERIKA_UUID)
    assert refreshed.gdrive_parent_outside_root is True
    # Remaining reflects the still-flagged doc
    assert result["remaining"] == 1


async def test_apply_moves_file_and_clears_flag(db: Database, monkeypatch):
    doc_id = await _seed_external_doc(db, gdrive_id="gd_ext_1", doc_id_hint="1")
    gdrive = MagicMock()
    _patch_clients(monkeypatch, gdrive)

    ctx = _StubCtx(db)
    result = json.loads(await hygiene_tools.consolidate_external_gdrive_files(ctx, dry_run=False))

    assert len(result["moved"]) == 1
    assert result["moved"][0]["id"] == doc_id
    assert result["moved"][0]["new_parent"] == "root_folder_xyz"
    gdrive.move_file.assert_called_once_with("gd_ext_1", "root_folder_xyz")
    # Flag cleared
    refreshed = await db.get_document(doc_id, patient_id=ERIKA_UUID)
    assert refreshed.gdrive_parent_outside_root is False
    assert result["remaining"] == 0


async def test_permission_denied_reports_without_crashing(db: Database, monkeypatch):
    """3rd-party-owned files return 403 insufficientFilePermissions — tool must
    report them in ``skipped_permissions``, NOT crash, and NOT clear the flag.
    """
    doc_id = await _seed_external_doc(db, gdrive_id="gd_readonly", doc_id_hint="ro")
    gdrive = MagicMock()

    class _FakePermError(Exception):
        pass

    gdrive.move_file.side_effect = _FakePermError(
        "<HttpError 403: insufficientFilePermissions: The user does not have permission>"
    )
    _patch_clients(monkeypatch, gdrive)

    ctx = _StubCtx(db)
    result = json.loads(await hygiene_tools.consolidate_external_gdrive_files(ctx, dry_run=False))

    assert result["moved"] == []
    assert len(result["skipped_permissions"]) == 1
    assert result["skipped_permissions"][0]["id"] == doc_id
    assert "insufficientFilePermissions" in result["skipped_permissions"][0]["error"]
    assert result["note"]  # human-readable resolution hint
    # Flag must remain set — user still sees "external_location" in audit
    refreshed = await db.get_document(doc_id, patient_id=ERIKA_UUID)
    assert refreshed.gdrive_parent_outside_root is True
    assert result["remaining"] == 1


async def test_batch_size_chunks_work(db: Database, monkeypatch):
    """With 5 flagged docs and batch_size=2, one call processes 2 and reports remaining=3."""
    for n in range(5):
        await _seed_external_doc(db, gdrive_id=f"gd_b_{n}", doc_id_hint=f"b{n}")
    gdrive = MagicMock()
    _patch_clients(monkeypatch, gdrive)

    ctx = _StubCtx(db)
    result = json.loads(
        await hygiene_tools.consolidate_external_gdrive_files(ctx, dry_run=False, batch_size=2)
    )

    assert len(result["moved"]) == 2
    # Remaining counts flags still set — 5 total, 2 cleared in this batch
    assert result["remaining"] == 3
    assert result["batch_size"] == 2


async def test_no_gdrive_client_returns_error(db: Database, monkeypatch):
    """If _create_patient_clients returns None, we surface a clear error string."""
    from oncofiles import server as server_mod

    async def _fake(db_, pid):
        return None

    monkeypatch.setattr(server_mod, "_create_patient_clients", _fake)

    ctx = _StubCtx(db)
    result = json.loads(await hygiene_tools.consolidate_external_gdrive_files(ctx))
    assert "error" in result
    assert "GDrive" in result["error"]


async def test_no_flagged_docs_returns_empty(db: Database, monkeypatch):
    """Patient has no external-flagged docs → empty moved/skipped/failed + remaining=0."""
    gdrive = MagicMock()
    _patch_clients(monkeypatch, gdrive)

    ctx = _StubCtx(db)
    result = json.loads(await hygiene_tools.consolidate_external_gdrive_files(ctx, dry_run=False))

    assert result["moved"] == []
    assert result["skipped_permissions"] == []
    assert result["failed"] == []
    assert result["remaining"] == 0
    gdrive.move_file.assert_not_called()
