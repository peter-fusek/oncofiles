"""Tests for AI enhancement layer (#v0.9)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from oncofiles.database import Database
from oncofiles.enhance import enhance_document_text, infer_institution_from_providers
from oncofiles.sync import enhance_documents
from tests.helpers import make_doc

# ── enhance_document_text ───────────────────────────────────────────────────


def test_enhance_empty_text():
    """Empty text returns empty summary and empty tags."""
    summary, tags = enhance_document_text("")
    assert summary == ""
    assert tags == "[]"


def test_enhance_whitespace_text():
    """Whitespace-only text returns empty results."""
    summary, tags = enhance_document_text("   \n  ")
    assert summary == ""
    assert tags == "[]"


def test_enhance_valid_response():
    """Valid JSON response from Claude is parsed correctly."""
    mock_response = MagicMock()
    mock_response.content = [
        MagicMock(
            text=(
                '{"summary": "Lab results showing elevated CEA.",'
                ' "tags": ["labs", "CEA", "oncology"]}'
            )
        )
    ]

    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        summary, tags = enhance_document_text("CEA: 15.2 ng/mL (ref <5.0)")

    assert summary == "Lab results showing elevated CEA."
    assert tags == '["labs", "CEA", "oncology"]'


def test_enhance_invalid_json():
    """Non-JSON response falls back gracefully."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="This is not JSON")]

    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        summary, tags = enhance_document_text("some text")

    assert summary == "This is not JSON"
    assert tags == "[]"


def test_enhance_missing_keys():
    """JSON response missing expected keys returns defaults."""
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text='{"other": "data"}')]

    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value.messages.create.return_value = mock_response
        summary, tags = enhance_document_text("some text")

    assert summary == ""
    assert tags == "[]"


# ── enhance_documents (integration) ────────────────────────────────────────


async def test_enhance_documents_all_unprocessed(db: Database):
    """Enhances all documents without AI metadata."""
    doc = make_doc()
    doc = await db.insert_document(doc, patient_id="erika")

    # Seed OCR text so enhancement has something to work with
    await db.save_ocr_page(doc.id, 1, "CEA: 15.2 ng/mL", "pymupdf-native")

    files = MagicMock()
    with patch(
        "oncofiles.sync.enhance_document_text",
        return_value=("AI summary", '["labs"]'),
    ):
        stats = await enhance_documents(db, files, None, patient_id="erika")

    assert stats["processed"] == 1
    assert stats["errors"] == 0

    # Verify metadata was saved
    updated = await db.get_document(doc.id)
    assert updated.ai_summary == "AI summary"
    assert updated.ai_tags == '["labs"]'
    assert updated.ai_processed_at is not None


async def test_enhance_documents_specific_ids(db: Database):
    """Enhances only specified document IDs."""
    doc1 = await db.insert_document(make_doc(file_id="f1"), patient_id="erika")
    doc2 = await db.insert_document(
        make_doc(file_id="f2", filename="other.pdf", original_filename="other.pdf"),
        patient_id="erika",
    )

    # Seed OCR text for both
    await db.save_ocr_page(doc1.id, 1, "Text 1", "pymupdf-native")
    await db.save_ocr_page(doc2.id, 1, "Text 2", "pymupdf-native")

    files = MagicMock()
    with patch(
        "oncofiles.sync.enhance_document_text",
        return_value=("summary", '["tag"]'),
    ):
        stats = await enhance_documents(db, files, None, document_ids=[doc1.id], patient_id="erika")

    assert stats["processed"] == 1

    # Only doc1 should be enhanced
    d1 = await db.get_document(doc1.id)
    d2 = await db.get_document(doc2.id)
    assert d1.ai_summary == "summary"
    assert d2.ai_summary is None


async def test_enhance_skips_no_text(db: Database):
    """Documents with no OCR text are skipped (not errors)."""
    doc = make_doc()
    doc = await db.insert_document(doc, patient_id="erika")

    files = MagicMock()
    files.download.side_effect = Exception("not downloadable")

    stats = await enhance_documents(db, files, None, document_ids=[doc.id], patient_id="erika")

    # Should skip gracefully (no text available)
    assert stats["processed"] == 0
    # The enhance function logs a warning but doesn't count it as error
    # since it's expected for some docs


# ── Database AI metadata methods ────────────────────────────────────────────


async def test_update_document_ai_metadata(db: Database):
    """AI metadata is stored and retrievable."""
    doc = make_doc()
    doc = await db.insert_document(doc, patient_id="erika")

    await db.update_document_ai_metadata(doc.id, "Test summary", '["test"]')

    updated = await db.get_document(doc.id)
    assert updated.ai_summary == "Test summary"
    assert updated.ai_tags == '["test"]'
    assert updated.ai_processed_at is not None


async def test_get_documents_without_ai(db: Database):
    """Only returns documents without AI processing."""
    doc1 = await db.insert_document(make_doc(file_id="f1"), patient_id="erika")
    doc2 = await db.insert_document(
        make_doc(file_id="f2", filename="other.pdf", original_filename="other.pdf"),
        patient_id="erika",
    )

    # Process doc1 only
    await db.update_document_ai_metadata(doc1.id, "summary", "[]")

    unprocessed = await db.get_documents_without_ai(patient_id="erika")
    assert len(unprocessed) == 1
    assert unprocessed[0].id == doc2.id


async def test_search_documents_includes_ai_fields(db: Database):
    """Search finds documents by AI summary and tags content."""
    from oncofiles.models import SearchQuery

    doc = await db.insert_document(make_doc(), patient_id="erika")
    await db.update_document_ai_metadata(doc.id, "Elevated CEA tumor marker", '["tumor-marker"]')

    # Search by AI summary content
    results = await db.search_documents(SearchQuery(text="tumor marker"), patient_id="erika")
    assert len(results) == 1
    assert results[0].id == doc.id

    # Search by AI tag content
    results = await db.search_documents(SearchQuery(text="tumor-marker"), patient_id="erika")
    assert len(results) == 1


# ── text/* enhancement path ───────────────────────────────────────────────


async def test_enhance_text_document(db: Database):
    """Text documents (text/markdown, text/plain) get enhanced via UTF-8 decode."""
    doc = make_doc(
        file_id="f_md",
        filename="20260311_DeVita_reference_Ch40.md",
        original_filename="chapter_40_full.md",
        mime_type="text/markdown",
    )
    doc = await db.insert_document(doc, patient_id="erika")

    files = MagicMock()
    files.download.return_value = b"# Chapter 40\n\nColon cancer treatment overview."

    with patch(
        "oncofiles.sync.enhance_document_text",
        return_value=("DeVita Ch 40 summary", '["mCRC", "DeVita"]'),
    ):
        stats = await enhance_documents(db, files, None, document_ids=[doc.id], patient_id="erika")

    assert stats["processed"] == 1

    updated = await db.get_document(doc.id)
    assert updated.ai_summary == "DeVita Ch 40 summary"

    # Verify OCR cache was populated
    assert await db.has_ocr_text(doc.id)


# ── infer_institution_from_providers ──────────────────────────────────────


def test_infer_institution_nou():
    """NOU matched from provider names."""
    assert infer_institution_from_providers(["MUDr. Stefan Porsok, PhD."]) == "NOU"
    assert infer_institution_from_providers(["NOU Klenova"]) == "NOU"
    assert infer_institution_from_providers(["MUDr. Natalia Pazderova"]) == "NOU"


def test_infer_institution_bory():
    """Bory Nemocnica matched from provider names."""
    assert infer_institution_from_providers(["MUDr. Rychlý Boris, PhD."]) == "BoryNemocnica"
    assert infer_institution_from_providers(["Nemocnica Bory"]) == "BoryNemocnica"
    assert infer_institution_from_providers(["MUDr. Peter Štefánik"]) == "BoryNemocnica"


def test_infer_institution_medirex():
    """Medirex matched."""
    assert infer_institution_from_providers(["Medirex a.s."]) == "Medirex"


def test_infer_institution_none():
    """No match returns None."""
    assert infer_institution_from_providers([]) is None
    assert infer_institution_from_providers(["Unknown Doctor"]) is None


# ── backfill fields from structured metadata ──────────────────────────────


async def test_enhance_backfills_null_date_and_institution(db: Database):
    """Enhancement backfills document_date and institution from structured metadata."""
    doc = make_doc(
        file_id="f_backfill",
        filename="Dodatok histológia p. Fuseková.pdf",
        original_filename="Dodatok histológia p. Fuseková.pdf",
        document_date=None,
        institution=None,
        description="Dodatok histológia p. Fuseková",
    )
    doc = await db.insert_document(doc, patient_id="erika")

    # Seed OCR text
    await db.save_ocr_page(doc.id, 1, "Pathology report from Nemocnica Bory", "pymupdf-native")

    files = MagicMock()
    metadata = {
        "document_type": "pathology",
        "findings": ["[BIOMARKER_REDACTED]"],
        "diagnoses": [],
        "medications": [],
        "dates_mentioned": ["2026-02-23"],
        "providers": ["MUDr. Rychlý Boris, PhD.", "Nemocnica Bory"],
        "plain_summary": "Pathology report.",
        "plain_summary_sk": "Patologická správa.",
    }

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("AI summary", '["pathology"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value=metadata),
        patch("oncofiles.sync.generate_filename_description", return_value="GeneticsKrasResults"),
    ):
        stats = await enhance_documents(db, files, None, document_ids=[doc.id], patient_id="erika")

    assert stats["processed"] == 1

    updated = await db.get_document(doc.id)
    assert updated.document_date is not None
    assert str(updated.document_date) == "2026-02-23"
    assert updated.institution == "BoryNemocnica"
    assert updated.description == "GeneticsKrasResults"


async def test_enhance_does_not_overwrite_existing_fields(db: Database):
    """Enhancement does not overwrite already-set date/institution/description."""
    from datetime import date

    doc = make_doc(
        file_id="f_no_overwrite",
        filename="20260227_ErikaFusekova_NOU_Labs_BloodResults.pdf",
        original_filename="20260227_ErikaFusekova_NOU_Labs_BloodResults.pdf",
        document_date=date(2026, 2, 27),
        institution="NOU",
        description="BloodResults",
    )
    doc = await db.insert_document(doc, patient_id="erika")

    await db.save_ocr_page(doc.id, 1, "Lab results from NOU", "pymupdf-native")

    files = MagicMock()
    metadata = {
        "document_type": "lab_report",
        "findings": [],
        "diagnoses": [],
        "medications": [],
        "dates_mentioned": ["2026-03-01"],
        "providers": ["Nemocnica Bory"],
        "plain_summary": "",
        "plain_summary_sk": "",
    }

    with (
        patch("oncofiles.sync.enhance_document_text", return_value=("summary", '["labs"]')),
        patch("oncofiles.sync.extract_structured_metadata", return_value=metadata),
    ):
        await enhance_documents(db, files, None, document_ids=[doc.id], patient_id="erika")

    updated = await db.get_document(doc.id)
    # Original values preserved — NOT overwritten by metadata
    assert str(updated.document_date) == "2026-02-27"
    assert updated.institution == "NOU"
    assert updated.description == "BloodResults"


# ── backfill_document_fields DB method ────────────────────────────────────


async def test_backfill_document_fields_coalesce(db: Database):
    """backfill_document_fields only fills NULL fields, doesn't overwrite."""
    doc = make_doc(
        file_id="f_coalesce",
        filename="test.pdf",
        original_filename="test.pdf",
        document_date=None,
        institution="NOU",  # Already set
        description=None,
    )
    doc = await db.insert_document(doc, patient_id="erika")

    await db.backfill_document_fields(
        doc.id,
        document_date="2026-03-01",
        institution="BoryNemocnica",  # Should NOT overwrite existing NOU
        description="TestDescription",
    )

    updated = await db.get_document(doc.id)
    assert str(updated.document_date) == "2026-03-01"
    assert updated.institution == "NOU"  # Preserved, not overwritten
    assert updated.description == "TestDescription"
