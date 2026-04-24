"""Tests for ``_build_reconciliation_report`` classification (#477 Issue 1).

Before #477, any doc whose gdrive_id wasn't in the sync-root listing landed in
``in_db_not_gdrive`` — scary "NÁZOV red dot" noise for files that were fine
(external_location) or known-gone (deleted_remote). The new report splits the
bucket so the dashboard reconciler can show the right tone per class.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

from oncofiles.database import Database
from oncofiles.tools.hygiene import _build_reconciliation_report
from tests.conftest import ERIKA_UUID
from tests.helpers import make_doc


def _mock_gdrive_listing(files: list[dict]) -> MagicMock:
    gdrive = MagicMock()
    gdrive.list_folder.return_value = files
    return gdrive


async def _insert(db: Database, **fields):
    doc = make_doc(**fields)
    return await db.insert_document(doc, patient_id=ERIKA_UUID)


async def test_report_splits_external_location_from_missing(db: Database):
    present = await _insert(
        db,
        file_id="f_present",
        filename="20260101_Erika_NOU_Labs.pdf",
        gdrive_id="gd_present",
        document_date=date(2026, 1, 1),
    )
    external = await _insert(
        db,
        file_id="f_ext",
        filename="20260313_Erika_PA_Advocate.md",
        gdrive_id="gd_external",
        mime_type="text/markdown",
        document_date=date(2026, 3, 13),
    )
    await db.set_gdrive_parent_outside_root(external.id, True)
    missing = await _insert(
        db,
        file_id="f_miss",
        filename="20260301_Erika_X_Other.pdf",
        gdrive_id="gd_unknown",
        document_date=date(2026, 3, 1),
    )

    # sync-root listing contains ONLY the "present" file
    gdrive = _mock_gdrive_listing([{"id": "gd_present", "name": present.filename}])

    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    assert [e["id"] for e in report["in_db_external_location"]] == [external.id]
    assert [e["id"] for e in report["in_db_deleted_remote"]] == []
    assert [e["id"] for e in report["in_db_not_gdrive"]] == [missing.id]


async def test_report_splits_deleted_remote_from_missing(db: Database):
    gone = await _insert(
        db,
        file_id="f_gone",
        filename="20260201_Erika_NOU_Labs.pdf",
        gdrive_id="gd_gone",
        document_date=date(2026, 2, 1),
    )
    await db.update_sync_state(gone.id, "deleted_remote")

    gdrive = _mock_gdrive_listing([])  # empty root — gone isn't there
    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    assert [e["id"] for e in report["in_db_deleted_remote"]] == [gone.id]
    assert [e["id"] for e in report["in_db_not_gdrive"]] == []


async def test_report_healthy_excludes_classified_classes(db: Database):
    """Healthy contract: external_location and deleted_remote do NOT make a
    patient unhealthy — they're known, classified states. Only the unclassified
    in_db_not_gdrive bucket does."""
    external = await _insert(
        db,
        file_id="f_ext",
        filename="20260313_Erika_PA_Advocate.md",
        gdrive_id="gd_ext",
        mime_type="text/markdown",
        document_date=date(2026, 3, 13),
    )
    await db.set_gdrive_parent_outside_root(external.id, True)

    deleted = await _insert(
        db,
        file_id="f_del",
        filename="20260201_Erika_X_Other.pdf",
        gdrive_id="gd_del",
        document_date=date(2026, 2, 1),
    )
    await db.update_sync_state(deleted.id, "deleted_remote")

    gdrive = _mock_gdrive_listing([])
    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    # Both docs are accounted for in their classified buckets
    assert len(report["in_db_external_location"]) == 1
    assert len(report["in_db_deleted_remote"]) == 1
    assert report["in_db_not_gdrive"] == []
    # and therefore the patient is reported healthy
    assert report["healthy"] is True


# ── #481: unsupported file extensions → expected_orphans (not scary red) ──


async def test_zip_in_gdrive_is_expected_orphan_not_unexpected(db: Database):
    """The exact bug Peter observed on Michal's dashboard: 2 SK-insurance-portal
    ZIPs (8053156100-PDG...zip) sit in the sync folder forever (sync correctly
    skips them as unsupported) but the reconciler classified them as orphans,
    displaying as scary red "2 orphans" count. Post-fix: they land in
    expected_orphans with reason="unsupported_extension" so the dashboard
    can render info-gray."""
    gdrive = _mock_gdrive_listing(
        [
            {"id": "gd_zip1", "name": "8053156100-PDG2200001063145.zip"},
            {"id": "gd_zip2", "name": "8053156100-PDG2400001331201.zip"},
        ]
    )

    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    # No red-orphan noise
    assert report["in_gdrive_not_db"] == []
    # Both in expected_orphans with the right classification
    assert len(report["expected_orphans"]) == 2
    reasons = {e["reason"] for e in report["expected_orphans"]}
    assert reasons == {"unsupported_extension"}
    extensions = {e["extension"] for e in report["expected_orphans"]}
    assert extensions == {".zip"}
    # Patient stays healthy — unsupported-extension files are known skips
    assert report["healthy"] is True


async def test_system_file_reason_distinct_from_unsupported(db: Database):
    """Manifest/OCR system files keep their distinct reason — dashboard
    renders them differently ("skipped: system file") vs unsupported extensions
    ("skipped: unsupported format, upload as PDF")."""
    gdrive = _mock_gdrive_listing(
        [
            {"id": "gd_manifest", "name": "_manifest.md"},
            {"id": "gd_ocr", "name": "some_report_OCR.txt"},
            {"id": "gd_zip", "name": "insurance_2024.zip"},
            {"id": "gd_metadata", "name": "doc.metadata.json"},
        ]
    )

    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    # .metadata.json is filtered out upstream of the orphan loop
    assert len(report["expected_orphans"]) == 3
    by_name = {e["name"]: e for e in report["expected_orphans"]}
    assert by_name["_manifest.md"]["reason"] == "system_file"
    assert by_name["some_report_OCR.txt"]["reason"] == "system_file"
    assert by_name["insurance_2024.zip"]["reason"] == "unsupported_extension"
    assert by_name["insurance_2024.zip"]["extension"] == ".zip"
    assert report["in_gdrive_not_db"] == []


async def test_supported_extension_missing_from_db_is_real_orphan(db: Database):
    """Genuine orphan: a PDF (supported type) sits in the sync folder but was
    never ingested into DB. That IS error-red — sync failed to pick it up
    and the user should be alerted. Fix must NOT silence these."""
    gdrive = _mock_gdrive_listing(
        [
            {"id": "gd_real", "name": "20260101_ErikaFusekova_NOU_Labs_Bloods.pdf"},
        ]
    )

    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    assert len(report["in_gdrive_not_db"]) == 1
    assert report["in_gdrive_not_db"][0]["name"] == "20260101_ErikaFusekova_NOU_Labs_Bloods.pdf"
    assert report["expected_orphans"] == []
    assert report["healthy"] is False  # real orphan fails health


async def test_extensionless_file_is_real_orphan(db: Database):
    """A file with NO extension is treated as a real orphan (conservative —
    could be anything). Prevents accidentally silencing truly unknown files."""
    gdrive = _mock_gdrive_listing(
        [
            {"id": "gd_weird", "name": "NOTES_NO_EXTENSION"},
        ]
    )

    report = await _build_reconciliation_report(db, gdrive, folder_id="root", patient_id=ERIKA_UUID)

    assert len(report["in_gdrive_not_db"]) == 1
    assert report["expected_orphans"] == []
