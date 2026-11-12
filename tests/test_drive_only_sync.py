"""#777: with cloud AI off, Drive sync and uploads store documents Drive-only.

The Files API is never called (no upload, no download), the row carries
``file_id = "gdrive:<drive id>"`` like a curated document, every read path
fetches the bytes from Drive, and a Drive-only import is a sync success, not a
failure (#552 accounting). All names, ids, dates and bytes are synthetic.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.curated import indexer
from oncofiles.database import Database
from oncofiles.files_api import (
    DRIVE_ONLY_PREFIX,
    DRIVE_SYNC_SOURCE,
    FilesClient,
    files_api_refusal_reason,
)
from oncofiles.sync import sync, sync_from_gdrive
from tests.helpers import ERIKA_UUID, make_doc

SYNTH_NAME = "20270301_Testpatient_NOU_Labs_SyntheticPanel.pdf"


def _synthetic_pdf() -> bytes:
    import pymupdf

    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "Synthetic page 2027")
    try:
        return pdf.tobytes()
    finally:
        pdf.close()


SYNTH_PDF = _synthetic_pdf()


@pytest.fixture
def cloud_off(monkeypatch):
    """Production default: CLOUD_AI_ENABLED off (a key alone changes nothing)."""
    monkeypatch.setattr("oncofiles.config.CLOUD_AI_ENABLED", False)


@pytest.fixture(autouse=True)
def _forget_registered_ids():
    before = set(indexer._curated_ids)
    yield
    indexer._curated_ids.intersection_update(before)


def _gfile(gid: str = "gd_synth_1", name: str = SYNTH_NAME, **extra) -> dict:
    return {
        "id": gid,
        "name": name,
        "mimeType": "application/pdf",
        "modifiedTime": "2027-03-01T10:00:00Z",
        "md5Checksum": "md5-invented-alpha",
        "size": "2048",
        "appProperties": {},
        "parents": [],
        **extra,
    }


def _gdrive(gdrive_files: list[dict]) -> MagicMock:
    gdrive = MagicMock()
    gdrive.list_folder_with_structure.return_value = (gdrive_files, {})
    gdrive.list_folder.return_value = gdrive_files
    gdrive.download.return_value = SYNTH_PDF
    gdrive.upload.return_value = {"id": "gd_uploaded", "modifiedTime": "2031-11-23T19:47:51Z"}
    gdrive.find_folder.return_value = None
    gdrive.create_folder.side_effect = lambda name, parent: f"folder_{name}"
    gdrive.get_file_parents.return_value = ["unorganized"]
    gdrive.batch_get_parents.side_effect = lambda fids: {fid: ["unorganized"] for fid in fids}
    gdrive.batch_move.side_effect = lambda moves: {fid: True for fid in moves}
    gdrive.batch_rename.side_effect = lambda renames: {fid: True for fid in renames}
    gdrive.get_file_metadata.side_effect = FileNotFoundError("synthetic: gone")
    return gdrive


def _real_files_client() -> FilesClient:
    """A real FilesClient whose SDK client is a recorder: any API use shows up."""
    fc = FilesClient(api_key="sk-ant-synthetic-not-real")
    fc._client = MagicMock()
    # every Files API method goes through _guarded(); count attempts, even refused ones
    fc._guarded = MagicMock(wraps=fc._guarded)
    return fc


def _assert_no_files_api(fc: FilesClient) -> None:
    fc._guarded.assert_not_called()
    assert fc._client.mock_calls == []


# ── the predicate ────────────────────────────────────────────────────────────


def test_prefix_is_the_curated_prefix():
    assert DRIVE_ONLY_PREFIX == indexer.CURATED_FILE_PREFIX


def test_refusal_reason_follows_cloud_guard(cloud_off):
    from oncofiles.cloud_guard import CLOUD_AI_FLAG_OFF_MESSAGE

    assert files_api_refusal_reason() == CLOUD_AI_FLAG_OFF_MESSAGE


def test_refusal_reason_none_when_cloud_on():
    assert files_api_refusal_reason() is None


def test_refusal_reason_inside_local_only_job():
    from oncofiles.cloud_guard import local_only

    with local_only("synthetic-job"):
        assert "synthetic-job" in files_api_refusal_reason()


# ── import with cloud AI off ─────────────────────────────────────────────────


async def test_import_cloud_off_is_drive_only_and_ok(db: Database, cloud_off):
    files = _real_files_client()
    gdrive = _gdrive([_gfile()])

    stats = await sync_from_gdrive(db, files, gdrive, "folder_synth", patient_id=ERIKA_UUID)

    assert stats["new"] == 1
    assert stats["errors"] == 0
    assert stats["drive_only"] == 1
    _assert_no_files_api(files)  # no Files API call of any kind, not even a refused one
    gdrive.download.assert_not_called()  # the bytes are not even read at import
    gdrive.export_google_doc.assert_not_called()

    [doc] = await db.list_documents(patient_id=ERIKA_UUID)
    assert doc.file_id == "gdrive:gd_synth_1"
    assert doc.gdrive_id == "gd_synth_1"
    assert doc.size_bytes == 2048
    assert doc.sync_state == "synced"
    assert json.loads(doc.structured_metadata) == {"source": DRIVE_SYNC_SOURCE}
    assert indexer.is_curated_id(doc.id)  # under the same cloud refusal as curated rows


async def test_content_change_cloud_off_converts_to_drive_only(db: Database, cloud_off):
    doc = make_doc(
        file_id="file_synth_old",
        filename=SYNTH_NAME,
        gdrive_id="gd_synth_1",
        gdrive_modified_time=datetime(2027, 2, 1, tzinfo=UTC),
        gdrive_md5="md5-synthetic-old",
    )
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)
    files = _real_files_client()
    gdrive = _gdrive([_gfile(md5Checksum="md5-synthetic-new")])

    stats = await sync_from_gdrive(db, files, gdrive, "folder_synth", patient_id=ERIKA_UUID)

    assert stats["updated"] == 1
    assert stats["errors"] == 0
    _assert_no_files_api(files)
    updated = await db.get_document(doc.id, patient_id=ERIKA_UUID)
    assert updated.file_id == "gdrive:gd_synth_1"
    assert updated.gdrive_md5 == "md5-synthetic-new"


async def test_content_change_cloud_off_keeps_page_text_and_marks_stale(db: Database, cloud_off):
    """No new text is written on a Drive-only update, so the old text is kept."""
    doc = await db.insert_document(
        make_doc(
            file_id="file_synth_old",
            filename=SYNTH_NAME,
            gdrive_id="gd_synth_1",
            gdrive_modified_time=datetime(2027, 2, 1, tzinfo=UTC),
            gdrive_md5="md5-synthetic-old",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.save_ocr_page(doc.id, 1, "Invented page text 2027", "pymupdf-native")
    await db.update_structured_metadata(doc.id, json.dumps({"findings": ["invented"]}))
    gdrive = _gdrive([_gfile(md5Checksum="md5-synthetic-new")])

    await sync_from_gdrive(db, _real_files_client(), gdrive, "f", patient_id=ERIKA_UUID)

    pages = await db.get_ocr_pages(doc.id)
    assert [p["extracted_text"] for p in pages] == ["Invented page text 2027"]
    updated = await db.get_document(doc.id, patient_id=ERIKA_UUID)
    assert json.loads(updated.structured_metadata) == {
        "findings": ["invented"],
        "text_stale": True,
    }


async def test_content_change_cloud_on_still_replaces_text(db: Database):
    """Unchanged cloud-on path: the old text is cleared because enhance re-extracts it."""
    doc = await db.insert_document(
        make_doc(
            filename=SYNTH_NAME,
            gdrive_id="gd_synth_1",
            gdrive_modified_time=datetime(2027, 2, 1, tzinfo=UTC),
            gdrive_md5="md5-synthetic-old",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.save_ocr_page(doc.id, 1, "Invented page text 2027", "pymupdf-native")
    files = MagicMock()
    files.upload.return_value = MagicMock(id="file_synth_new")
    gdrive = _gdrive([_gfile(md5Checksum="md5-synthetic-new")])

    await sync_from_gdrive(db, files, gdrive, "f", enhance=False, patient_id=ERIKA_UUID)

    assert not await db.has_ocr_text(doc.id)


def test_mark_text_stale_never_overwrites_unparseable_metadata():
    from oncofiles.sync import _mark_text_stale

    assert _mark_text_stale("not json") == "not json"
    assert json.loads(_mark_text_stale(None)) == {
        "source": DRIVE_SYNC_SOURCE,
        "text_stale": True,
    }


async def test_logs_never_contain_the_filename(db: Database, cloud_off, caplog):
    """Medical filenames carry names/dates/diagnoses: logs show ids + length only."""
    import logging

    from oncofiles.gmail_sync import _process_attachment
    from oncofiles.tools.documents import upload_document

    caplog.set_level(logging.DEBUG)
    # sync: import, content change, skipped type, export of a Files API row
    await db.insert_document(
        make_doc(
            file_id="file_synth_old",
            filename=SYNTH_NAME,
            gdrive_id="gd_invented_kappa",
            gdrive_modified_time=datetime(2027, 2, 1, tzinfo=UTC),
            gdrive_md5="md5-synthetic-old",
        ),
        patient_id=ERIKA_UUID,
    )
    await db.insert_document(
        make_doc(file_id="file_synth_legacy", filename=SYNTH_NAME, gdrive_id=None),
        patient_id=ERIKA_UUID,
    )
    gdrive = _gdrive(
        [
            _gfile(),
            _gfile("gd_invented_kappa", md5Checksum="md5-invented-omega"),
            _gfile("gd_synth_3", name=SYNTH_NAME.replace(".pdf", ".exe")),
        ]
    )
    await sync(db, _real_files_client(), gdrive, "f", trigger="test", patient_id=ERIKA_UUID)
    # gmail attachment refused
    await _process_attachment(
        db,
        _real_files_client(),
        MagicMock(),
        "msg_synth",
        {"filename": SYNTH_NAME, "mime_type": "application/pdf", "attachment_id": "att_1"},
        MagicMock(),
        patient_id=ERIKA_UUID,
    )
    # Drive-only upload failure
    failing = _gdrive([])
    failing.upload.side_effect = RuntimeError("synthetic: upload failed")
    with patch(
        "oncofiles.server._create_patient_clients",
        AsyncMock(return_value=(failing, None, None, "folder_synth")),
    ):
        await upload_document(
            _ctx(db, _real_files_client()),
            content=base64.b64encode(SYNTH_PDF).decode(),
            filename=SYNTH_NAME,
        )

    ours = [r.getMessage() for r in caplog.records if r.name.startswith("oncofiles")]
    text = "\n".join(ours)
    assert "Gmail attachment <name len=" in text
    assert "Drive-only upload failed for <name len=" in text
    assert "gdrive_id=gd_synth_1 <name len=" in text
    stem = SYNTH_NAME.rsplit(".", 1)[0]
    leaking = [m for m in ours if stem in m]
    assert leaking == []


async def test_full_sync_cloud_off_makes_no_cloud_call(db: Database, cloud_off):
    """A whole sync (import + export, enhance requested) reads nothing in the cloud."""
    # A legacy Files API row without a Drive copy: export would need the Files API.
    await db.insert_document(
        make_doc(file_id="file_synth_legacy", filename="20270105_Legacy.pdf", gdrive_id=None),
        patient_id=ERIKA_UUID,
    )
    files = _real_files_client()
    gdrive = _gdrive([_gfile()])
    anthropic_client = MagicMock()

    with (
        patch("oncofiles.enhance._get_client", anthropic_client),
        patch("oncofiles.sync.enhance_document_text") as enhance_text,
        patch("oncofiles.sync.extract_structured_metadata") as extract_meta,
        patch("oncofiles.doc_analysis.analyze_document_composition") as split_ai,
        patch("oncofiles.doc_analysis.analyze_consolidation") as consolidate_ai,
    ):
        stats = await sync(
            db, files, gdrive, "folder_synth", enhance=True, trigger="test", patient_id=ERIKA_UUID
        )

    _assert_no_files_api(files)
    anthropic_client.assert_not_called()
    enhance_text.assert_not_called()
    extract_meta.assert_not_called()
    split_ai.assert_not_called()
    consolidate_ai.assert_not_called()

    # #552: a Drive-only import and a refused Files API read are not failures
    assert stats["from_gdrive"]["new"] == 1
    assert stats["from_gdrive"]["errors"] == 0
    assert stats["to_gdrive"]["errors"] == 0
    assert stats["to_gdrive"]["skipped"] >= 1  # the legacy row, skipped not failed
    async with db.db.execute(
        "SELECT status, from_gdrive_errors, to_gdrive_errors FROM sync_history "
        "WHERE patient_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (ERIKA_UUID,),
    ) as cur:
        row = dict(await cur.fetchone())
    assert row == {"status": "completed", "from_gdrive_errors": 0, "to_gdrive_errors": 0}


# ── cloud AI on: unchanged ───────────────────────────────────────────────────


async def test_import_cloud_on_uploads_to_files_api_as_before(db: Database):
    files = MagicMock()
    files.upload.return_value = MagicMock(id="file_synth_new", mime_type="application/pdf")
    gdrive = _gdrive([_gfile()])

    stats = await sync_from_gdrive(
        db, files, gdrive, "folder_synth", enhance=False, patient_id=ERIKA_UUID
    )

    assert stats["new"] == 1
    assert "drive_only" not in stats
    files.upload.assert_called_once()
    gdrive.download.assert_called_once_with("gd_synth_1")
    [doc] = await db.list_documents(patient_id=ERIKA_UUID)
    assert doc.file_id == "file_synth_new"
    assert doc.size_bytes == len(SYNTH_PDF)
    assert not indexer.is_curated_id(doc.id)


# ── downstream reads of a Drive-only document ────────────────────────────────


async def test_try_download_reads_drive_only_doc_from_drive(cloud_off):
    from oncofiles.tools._helpers import _try_download

    files = _real_files_client()
    gdrive = _gdrive([])
    doc = make_doc(id=913, file_id="gdrive:gd_synth_1", gdrive_id="gd_synth_1")

    ok, _content, raw = await _try_download(files, doc, gdrive)

    assert ok is True
    assert raw == SYNTH_PDF
    _assert_no_files_api(files)
    gdrive.download.assert_called_once_with("gd_synth_1")


async def test_try_download_exports_a_google_doc_registered_as_pdf(cloud_off):
    from oncofiles.tools._helpers import _try_download

    files = _real_files_client()
    gdrive = _gdrive([])
    gdrive.download.side_effect = RuntimeError("synthetic: fileNotDownloadable")
    gdrive.export_google_doc.return_value = SYNTH_PDF
    doc = make_doc(id=914, file_id="gdrive:gd_synth_doc", gdrive_id="gd_synth_doc")

    ok, _content, raw = await _try_download(files, doc, gdrive)

    assert ok is True
    assert raw == SYNTH_PDF
    gdrive.export_google_doc.assert_called_once_with("gd_synth_doc", "application/pdf")


async def test_enhance_reads_drive_only_doc_without_files_api(db: Database):
    """Even with cloud on, a Drive-only row is never fetched via the Files API."""
    from oncofiles.sync import _enhance_document

    doc = await db.insert_document(
        make_doc(file_id="gdrive:gd_synth_1", gdrive_id="gd_synth_1"), patient_id=ERIKA_UUID
    )
    files = MagicMock()
    gdrive = _gdrive([])
    with (
        patch("oncofiles.server._extract_pdf_text", return_value=None),
        patch("fitz.open", side_effect=RuntimeError("synthetic: no OCR here")),
    ):
        await _enhance_document(db, doc, files, gdrive, patient_id=ERIKA_UUID)

    files.download.assert_not_called()
    gdrive.download.assert_called_once_with("gd_synth_1")


# ── curated indexer never removes a Drive-only sync row ──────────────────────


async def test_curated_index_state_marks_sync_rows_foreign(db: Database, cloud_off):
    gdrive = _gdrive([_gfile()])
    await sync_from_gdrive(db, _real_files_client(), gdrive, "f", patient_id=ERIKA_UUID)
    # an interrupted curated insert: gdrive: prefix, no metadata yet
    await db.insert_document(
        make_doc(file_id="gdrive:gd_curated_1", gdrive_id="gd_curated_1"), patient_id=ERIKA_UUID
    )

    state = await db.curated_index_state(patient_id=ERIKA_UUID)

    assert state["gd_synth_1"]["foreign"] is True
    assert state["gd_curated_1"]["foreign"] is False


# ── upload_document with cloud AI off ────────────────────────────────────────


def _ctx(db: Database, files) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": files, "gdrive": None}
    ctx.info = AsyncMock()
    return ctx


async def test_upload_document_cloud_off_stores_drive_only(db: Database, cloud_off):
    from oncofiles.tools.documents import upload_document

    files = _real_files_client()
    gdrive = _gdrive([])
    content = base64.b64encode(SYNTH_PDF).decode()
    with patch(
        "oncofiles.server._create_patient_clients",
        AsyncMock(return_value=(gdrive, None, None, "folder_synth")),
    ) as clients:
        result = json.loads(
            await upload_document(_ctx(db, files), content=content, filename=SYNTH_NAME)
        )

    clients.assert_awaited_once_with(db, ERIKA_UUID)  # the resolved patient's own Drive
    assert result["storage"] == "drive_only"
    assert result["file_id"] == "gdrive:gd_uploaded"
    _assert_no_files_api(files)
    assert gdrive.upload.call_args.kwargs["content_bytes"] == SYNTH_PDF
    gdrive.set_app_properties.assert_called_once_with(
        "gd_uploaded", {"oncofiles_id": str(result["id"])}
    )
    doc = await db.get_document(result["id"], patient_id=ERIKA_UUID)
    assert doc.file_id == "gdrive:gd_uploaded"
    assert doc.gdrive_id == "gd_uploaded"
    assert doc.size_bytes == len(SYNTH_PDF)
    assert indexer.is_curated_id(doc.id)


async def test_upload_document_cloud_off_drive_failure_stores_nothing(db: Database, cloud_off):
    from oncofiles.curated_folder import DriveWriteBlockedError
    from oncofiles.tools.documents import upload_document

    gdrive = _gdrive([])
    gdrive.upload.side_effect = DriveWriteBlockedError("synthetic: curated folder")
    before = await db.count_documents(patient_id=ERIKA_UUID)
    content = base64.b64encode(SYNTH_PDF).decode()
    with patch(
        "oncofiles.server._create_patient_clients",
        AsyncMock(return_value=(gdrive, None, None, "folder_synth")),
    ):
        result = json.loads(
            await upload_document(
                _ctx(db, _real_files_client()), content=content, filename=SYNTH_NAME
            )
        )

    assert "nothing was stored" in result["error"]
    assert await db.count_documents(patient_id=ERIKA_UUID) == before


# ── Gmail attachments with cloud AI off ──────────────────────────────────────


async def test_gmail_attachment_cloud_off_not_uploaded(db: Database, cloud_off):
    from oncofiles.gmail_sync import _process_attachment

    files = _real_files_client()
    gmail = MagicMock()
    doc = await _process_attachment(
        db,
        files,
        gmail,
        "msg_synth",
        {"filename": SYNTH_NAME, "mime_type": "application/pdf", "attachment_id": "att_1"},
        MagicMock(),
        patient_id=ERIKA_UUID,
    )

    assert doc is None
    _assert_no_files_api(files)
    gmail.get_attachment.assert_not_called()
