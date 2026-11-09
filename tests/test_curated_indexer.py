"""Curated folder → documents read model (#603). Synthetic data only."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.curated import indexer
from oncofiles.models import DocumentCategory, SearchQuery
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_curated_runner import DUPS, INBOX, LABS, REPORT, FakeDrive

SIDECAR = """---
source_file: "20260805-JanaNovakova-NOU-Labs-Novak-KlinikaOnkohematologie.pdf"
patient: "Jana Novakova"
doc_date: "2026-08-05"
institution: "NOU"
doc_type: "Labs"
doctor: "MUDr. Novak"
diagnoses: [C18.7]
handwritten: false
review_needed: false
summary: "Krvny obraz pred chemoterapiou, KRAS p.(Gly12Ser) v anamneze."
ocr_engine: "qwen-local"   # inline comment
---

<!-- page 1 -->
Hemoglobin 98 g/l
<!-- page 2 -->
Leukocyty 9.37
"""

FILED = "20260805-JanaNovakova-NOU-Labs-Novak-KlinikaOnkohematologie.pdf"


def _drive():
    d = FakeDrive()
    src = d.add(FILED, d.folder[LABS], b"%PDF fake", modified="2026-11-02T00:00:00Z")
    d.add(FILED + ".md", d.folder[LABS], SIDECAR.encode(), modified="2026-11-02T00:00:01Z")
    d.add("note.md", d.folder[REPORT], b"# Poznamka\nOtazky pred chemo", modified="2026-11-03T0")
    d.add("scan_new.pdf", d.folder[INBOX], b"%PDF inbox")  # not filed yet
    d.add("old.pdf", d.folder[DUPS], b"%PDF dup")  # quarantine
    d.add(".DS_Store", d.folder[LABS], b"x")
    return d, src


# ── parsing ──


def test_parse_sidecar_front_matter_and_pages():
    front, pages = indexer.parse_sidecar(SIDECAR)
    assert front["doc_date"] == "2026-08-05"
    assert front["diagnoses"] == ["C18.7"]
    assert front["handwritten"] is False
    assert front["ocr_engine"] == "qwen-local"  # comment stripped
    assert pages == ["Hemoglobin 98 g/l", "Leukocyty 9.37"]


def test_parse_sidecar_without_front_matter_is_one_page():
    assert indexer.parse_sidecar("just text\n") == ({}, ["just text"])


def test_category_mapping():
    assert indexer.category_for("Labs") is DocumentCategory.LABS
    assert indexer.category_for("Treatment") is DocumentCategory.CHEMO_SHEET
    assert indexer.category_for("Admin") is DocumentCategory.OTHER
    assert indexer.category_for("Nonsense") is DocumentCategory.OTHER


# ── scan (Drive reads only) ──


def test_scan_indexes_filed_documents_only():
    d, src = _drive()
    s = indexer.scan(d, d.root)
    by_name = {r.filename: r for r in s.records}
    assert set(by_name) == {FILED, "note.md"}  # no inbox, duplicates, sidecars, dotfiles
    rec = by_name[FILED]
    assert rec.gdrive_id == src and rec.file_id == f"gdrive:{src}"
    assert rec.category is DocumentCategory.LABS
    assert rec.document_date.isoformat() == "2026-08-05"
    assert rec.institution == "NOU"
    assert rec.pages == ["Hemoglobin 98 g/l", "Leukocyty 9.37"]
    assert rec.metadata["doctor"] == "MUDr. Novak"
    assert by_name["note.md"].pages == ["# Poznamka\nOtazky pred chemo"]
    assert d.writes == []  # never a write


def test_scan_skips_unchanged_sidecars():
    d, src = _drive()
    side_modified = next(r for r in indexer.scan(d, d.root).records if r.gdrive_id == src)
    downloads = []
    orig = d.download
    d.download = lambda fid: (downloads.append(fid), orig(fid))[1]
    s = indexer.scan(d, d.root, known={src: side_modified.sidecar_modified})
    rec = next(r for r in s.records if r.gdrive_id == src)
    assert rec.unchanged and rec.pages == []
    assert d.find(d.folder[LABS], FILED + ".md") not in downloads


# ── index_patient (DB) ──


@pytest.fixture
def env():
    d, src = _drive()
    gdrive = MagicMock()
    gdrive.dedicated_copy.return_value = MagicMock()
    with (
        patch(
            "oncofiles.server._create_patient_clients",
            AsyncMock(return_value=(gdrive, None, None, d.root)),
        ),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: d),
    ):
        yield d, src


async def test_index_creates_searchable_rows_never_sent_to_cloud(db, env):
    d, src = env
    stats = await tool.index_patient(db, ERIKA_UUID)
    assert stats["created"] == 2 and stats["errors"] == 0
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    assert doc.file_id == f"gdrive:{src}" and doc.category == DocumentCategory.LABS
    assert doc.ai_summary.startswith("Krvny obraz")
    # found by page text, not only by name/summary
    hits = await db.search_documents(SearchQuery(text="Leukocyty"), patient_id=ERIKA_UUID)
    assert [h.id for h in hits] == [doc.id]
    pages = await db.get_ocr_pages(doc.id, patient_id=ERIKA_UUID)
    assert [p["model"] for p in pages] == [indexer.INDEX_ENGINE] * 2
    # cloud enrichment never selects it, and refuses it if asked directly
    assert await db.get_documents_without_ai(patient_id=ERIKA_UUID) == []
    assert await db.get_documents_without_metadata(patient_id=ERIKA_UUID) == []
    assert indexer.is_curated_id(doc.id)
    with pytest.raises(indexer.CuratedCloudError):
        indexer.refuse_cloud(doc.id, "test")


async def test_index_is_incremental_and_follows_renames_and_removals(db, env):
    d, src = env
    await tool.index_patient(db, ERIKA_UUID)
    again = await tool.index_patient(db, ERIKA_UUID)
    assert again["unchanged"] == 2 and again["created"] == 0
    # a rename/move inside the filed folders updates the same row
    d.items[src]["name"] = "20260805-JanaNovakova-NOU-Labs-Novak-Renamed.pdf"
    d.items[d.find(d.folder[LABS], FILED + ".md")]["name"] = (
        "20260805-JanaNovakova-NOU-Labs-Novak-Renamed.pdf.md"
    )
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    assert doc.filename.endswith("Renamed.pdf") and doc.deleted_at is None
    # moved to the duplicates folder → soft-deleted, never hard-deleted
    d.items[src]["parent"] = d.folder[DUPS]
    removed = await tool.index_patient(db, ERIKA_UUID)
    assert removed["removed"] == 1
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    assert doc is not None and doc.deleted_at is not None


async def test_enhance_refuses_a_curated_document_before_any_client(db, env):
    from oncofiles import enhance

    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(env[1], patient_id=ERIKA_UUID)
    with (
        patch.object(enhance, "_get_client", side_effect=AssertionError("cloud called")),
        pytest.raises(indexer.CuratedCloudError),
    ):
        enhance.enhance_document_text("text", document_id=doc.id)


async def test_download_never_tries_the_files_api_for_curated_rows(db, env):
    from oncofiles.tools._helpers import _try_download

    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(env[1], patient_id=ERIKA_UUID)
    files = MagicMock()
    gdrive = MagicMock()
    import pymupdf

    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "Hemoglobin 105")
        real_pdf = pdf.tobytes()
    gdrive.download.return_value = real_pdf
    ok, _, raw = await _try_download(files, doc, gdrive)
    files.download.assert_not_called()
    gdrive.download.assert_called_once_with(doc.gdrive_id)
    assert ok and raw == real_pdf


def test_guard_fails_closed_when_the_registry_did_not_load():
    try:
        indexer.mark_guard_failed()
        with pytest.raises(indexer.CuratedCloudError, match="not loaded"):
            indexer.refuse_cloud(123456, "test")
        indexer.refuse_cloud(None, "test")  # calls without a document are unaffected
    finally:
        indexer.mark_guard_ready()


def test_vision_ocr_refuses_a_curated_document():
    from oncofiles import ocr

    indexer.register_curated_ids([987654])
    try:
        with (
            patch.object(ocr, "_get_client", side_effect=AssertionError("cloud called")),
            pytest.raises(indexer.CuratedCloudError),
        ):
            ocr.extract_text_from_image(MagicMock(), document_id=987654)
    finally:
        indexer.forget_curated_ids([987654])


async def test_curated_row_without_pages_never_falls_back_to_cloud_ocr(db, env):
    from oncofiles.tools._helpers import _ensure_ocr_text

    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(env[1], patient_id=ERIKA_UUID)
    await db.delete_ocr_pages(doc.id)
    with patch("oncofiles.ocr.extract_text_from_image", side_effect=AssertionError("cloud")):
        assert await _ensure_ocr_text(db, doc, [MagicMock()], None) == []


async def test_an_interrupted_document_is_redone_whole_on_the_next_index(db, env):
    d, src = env
    real_save = db.save_ocr_page
    calls = {"n": 0}

    async def dies_on_second_page(doc_id, n, text, model):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("deploy killed the container")
        await real_save(doc_id, n, text, model)

    with patch.object(db, "save_ocr_page", dies_on_second_page):
        first = await tool.index_patient(db, ERIKA_UUID)
    assert first["errors"] == 1
    again = await tool.index_patient(db, ERIKA_UUID)  # no marker → redone, not "unchanged"
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    pages = await db.get_ocr_pages(doc.id, patient_id=ERIKA_UUID)
    assert len(pages) == 2 and again["errors"] == 0
    rows = await db.list_documents(patient_id=ERIKA_UUID)
    assert sum(1 for r in rows if r.gdrive_id == src) == 1  # never duplicated
