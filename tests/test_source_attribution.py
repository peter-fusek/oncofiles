"""Tests for source attribution helpers and cross-referencing."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from oncofiles.models import Document, DocumentCategory
from oncofiles.tools._helpers import (
    _doc_to_dict,
    _gdrive_url,
    _research_source_url,
)

# ── URL helpers ──────────────────────────────────────────────────────────────


class TestGdriveUrl:
    def test_with_id(self):
        assert _gdrive_url("abc123") == "https://drive.google.com/file/d/abc123/view"

    def test_none(self):
        assert _gdrive_url(None) is None

    def test_empty_string(self):
        assert _gdrive_url("") is None


class TestResearchSourceUrl:
    def test_pubmed_numeric(self):
        assert _research_source_url("pubmed", "12345") == "https://pubmed.ncbi.nlm.nih.gov/12345/"

    def test_pubmed_prefixed(self):
        assert (
            _research_source_url("pubmed", "PMID:67890") == "https://pubmed.ncbi.nlm.nih.gov/67890/"
        )

    def test_pubmed_pmid_no_colon(self):
        assert (
            _research_source_url("pubmed", "PMID12345") == "https://pubmed.ncbi.nlm.nih.gov/12345/"
        )

    def test_clinicaltrials(self):
        assert (
            _research_source_url("clinicaltrials", "NCT04123456")
            == "https://clinicaltrials.gov/study/NCT04123456"
        )

    def test_clinicaltrials_gov(self):
        assert (
            _research_source_url("clinicaltrials.gov", "NCT04123456")
            == "https://clinicaltrials.gov/study/NCT04123456"
        )

    def test_unknown_source(self):
        assert _research_source_url("other", "XYZ123") is None

    def test_empty_external_id(self):
        assert _research_source_url("pubmed", "") is None

    def test_non_numeric_pubmed(self):
        assert _research_source_url("pubmed", "not-a-number") is None

    def test_non_nct_clinicaltrials(self):
        assert _research_source_url("clinicaltrials", "ISRCTN12345") is None


# ── _doc_to_dict enrichment ──────────────────────────────────────────────────


class TestDocToDict:
    def _make_doc(self, gdrive_id: str | None = None) -> Document:
        return Document(
            id=1,
            file_id="file_abc",
            filename="20240101_test_labs.pdf",
            original_filename="20240101_test_labs.pdf",
            document_date=date(2024, 1, 1),
            institution="TEST",
            category=DocumentCategory.LABS,
            description="Test labs",
            gdrive_id=gdrive_id,
        )

    def test_includes_gdrive_url(self):
        doc = self._make_doc(gdrive_id="gd_123")
        result = _doc_to_dict(doc)
        assert result["gdrive_url"] == "https://drive.google.com/file/d/gd_123/view"

    def test_gdrive_url_none_when_no_gdrive_id(self):
        doc = self._make_doc(gdrive_id=None)
        result = _doc_to_dict(doc)
        assert result["gdrive_url"] is None

    def test_gdrive_url_always_present_in_keys(self):
        doc = self._make_doc()
        result = _doc_to_dict(doc)
        assert "gdrive_url" in result


# ── Cross-references ─────────────────────────────────────────────────────────


@pytest.fixture
async def db():
    from oncofiles.database import Database

    db = Database(":memory:")
    await db.connect()
    await db.migrate()
    yield db
    await db.close()


async def test_cross_reference_insert_and_query(db):
    """insert_cross_reference stores a reference; get_cross_references retrieves it."""
    # Create two documents
    doc1 = Document(
        file_id="f1",
        filename="20240101_test_labs.pdf",
        original_filename="20240101_test_labs.pdf",
        category=DocumentCategory.LABS,
    )
    doc2 = Document(
        file_id="f2",
        filename="20240101_test_imaging.pdf",
        original_filename="20240101_test_imaging.pdf",
        category=DocumentCategory.IMAGING,
    )
    doc1 = await db.insert_document(doc1, patient_id="erika")
    doc2 = await db.insert_document(doc2, patient_id="erika")

    await db.insert_cross_reference(doc1.id, doc2.id, "same_visit", 1.0)
    refs = await db.get_cross_references(doc1.id)
    assert len(refs) == 1
    assert refs[0]["source_document_id"] == doc1.id
    assert refs[0]["target_document_id"] == doc2.id
    assert refs[0]["relationship"] == "same_visit"
    assert refs[0]["confidence"] == 1.0

    # Query from the other direction
    refs2 = await db.get_cross_references(doc2.id)
    assert len(refs2) == 1


async def test_cross_reference_idempotent(db):
    """Duplicate cross-references are silently ignored."""
    doc1 = await db.insert_document(
        Document(
            file_id="f1",
            filename="a.pdf",
            original_filename="a.pdf",
            category=DocumentCategory.LABS,
        ),
        patient_id="erika",
    )
    doc2 = await db.insert_document(
        Document(
            file_id="f2",
            filename="b.pdf",
            original_filename="b.pdf",
            category=DocumentCategory.LABS,
        ),
        patient_id="erika",
    )

    await db.insert_cross_reference(doc1.id, doc2.id, "related", 0.8)
    await db.insert_cross_reference(doc1.id, doc2.id, "related", 0.8)
    refs = await db.get_cross_references(doc1.id)
    assert len(refs) == 1


async def test_bulk_insert_cross_references(db):
    """bulk_insert_cross_references inserts multiple refs at once."""
    docs = []
    for i in range(3):
        d = await db.insert_document(
            Document(
                file_id=f"f{i}",
                filename=f"doc{i}.pdf",
                original_filename=f"doc{i}.pdf",
                category=DocumentCategory.LABS,
            ),
            patient_id="erika",
        )
        docs.append(d)

    count = await db.bulk_insert_cross_references(
        [
            (docs[0].id, docs[1].id, "same_visit", 1.0),
            (docs[0].id, docs[2].id, "related", 0.7),
            (docs[1].id, docs[2].id, "related", 0.8),
        ]
    )
    assert count == 3

    refs = await db.get_cross_references(docs[0].id)
    assert len(refs) == 2


async def test_cross_reference_different_relationships(db):
    """Two docs can have multiple relationships (e.g., same_visit AND related)."""
    doc1 = await db.insert_document(
        Document(
            file_id="f1",
            filename="a.pdf",
            original_filename="a.pdf",
            category=DocumentCategory.LABS,
        ),
        patient_id="erika",
    )
    doc2 = await db.insert_document(
        Document(
            file_id="f2",
            filename="b.pdf",
            original_filename="b.pdf",
            category=DocumentCategory.IMAGING,
        ),
        patient_id="erika",
    )

    await db.insert_cross_reference(doc1.id, doc2.id, "same_visit", 1.0)
    await db.insert_cross_reference(doc1.id, doc2.id, "related", 0.7)
    refs = await db.get_cross_references(doc1.id)
    assert len(refs) == 2


# ── Manifest enrichment ─────────────────────────────────────────────────────


def test_manifest_doc_includes_gdrive_url():
    from oncofiles.manifest import _doc_to_manifest

    doc = MagicMock()
    doc.id = 1
    doc.file_id = "f1"
    doc.filename = "test.pdf"
    doc.original_filename = "test.pdf"
    doc.document_date = date(2024, 1, 1)
    doc.institution = "TEST"
    doc.category = DocumentCategory.LABS
    doc.description = "Test"
    doc.mime_type = "application/pdf"
    doc.size_bytes = 1000
    doc.gdrive_id = "gd_abc"
    doc.ai_summary = None
    doc.ai_tags = None
    doc.structured_metadata = None
    doc.created_at = None

    result = _doc_to_manifest(doc)
    assert result["gdrive_url"] == "https://drive.google.com/file/d/gd_abc/view"


def test_manifest_research_includes_url():
    from oncofiles.manifest import _research_to_manifest

    entry = MagicMock()
    entry.id = 1
    entry.source = "pubmed"
    entry.external_id = "12345"
    entry.title = "Test Article"
    entry.summary = "Summary"
    entry.tags = "[]"

    result = _research_to_manifest(entry)
    assert result["url"] == "https://pubmed.ncbi.nlm.nih.gov/12345/"
