"""Tests for document search improvements: relevance, multi-term, pagination."""

from __future__ import annotations

from oncofiles.database import Database
from oncofiles.models import SearchQuery
from tests.helpers import make_doc


async def test_multi_term_and_semantics(db: Database):
    """Multiple search terms use AND — all must match somewhere."""
    doc1 = make_doc(file_id="f1", filename="20260213_NOUonko_labs_CBC.pdf")
    doc2 = make_doc(
        file_id="f2",
        filename="20260213_OUSA_labs_CBC.pdf",
        institution="OUSA",
    )
    await db.insert_document(doc1)
    await db.insert_document(doc2)

    # "NOUonko CBC" → only doc1 matches both terms
    results = await db.search_documents(SearchQuery(text="NOUonko CBC"))
    assert len(results) == 1
    assert results[0].file_id == "f1"

    # "labs CBC" → both match
    results = await db.search_documents(SearchQuery(text="labs CBC"))
    assert len(results) == 2


async def test_relevance_filename_over_summary(db: Database):
    """Documents matching in filename rank higher than AI summary matches."""
    # doc1: "CEA" in filename (weight 3)
    doc1 = make_doc(file_id="f1", filename="20260213_NOUonko_labs_CEA.pdf", description="CEA")
    doc1 = await db.insert_document(doc1)

    # doc2: "CEA" only in ai_summary (weight 2)
    doc2 = make_doc(file_id="f2", filename="20260213_NOUonko_labs_markers.pdf")
    doc2 = await db.insert_document(doc2)
    await db.update_document_ai_metadata(doc2.id, "CEA elevated to 1559", "[]")

    results = await db.search_documents(SearchQuery(text="CEA"))
    assert len(results) == 2
    # doc1 should rank first (filename match = higher weight)
    assert results[0].file_id == "f1"
    assert results[1].file_id == "f2"


async def test_relevance_summary_over_tags(db: Database):
    """AI summary matches (weight 2) rank above tag matches (weight 1)."""
    doc1 = make_doc(file_id="f1", filename="20260213_NOUonko_labs_a.pdf")
    doc1 = await db.insert_document(doc1)
    await db.update_document_ai_metadata(doc1.id, "FOLFOX regimen started", "[]")

    doc2 = make_doc(file_id="f2", filename="20260213_NOUonko_labs_b.pdf")
    doc2 = await db.insert_document(doc2)
    await db.update_document_ai_metadata(doc2.id, "Blood count results", '["FOLFOX"]')

    results = await db.search_documents(SearchQuery(text="FOLFOX"))
    assert len(results) == 2
    assert results[0].file_id == "f1"  # summary match (weight 2)
    assert results[1].file_id == "f2"  # tags match (weight 1)


async def test_search_structured_metadata(db: Database):
    """Search finds matches in structured_metadata JSON."""
    doc = make_doc(file_id="f1", filename="20260213_NOUonko_genetics_panel.pdf")
    doc = await db.insert_document(doc)
    await db.db.execute(
        "UPDATE documents SET structured_metadata = ? WHERE id = ?",
        ('{"biomarkers": ["KRAS G12S", "pMMR"]}', doc.id),
    )
    await db.db.commit()

    results = await db.search_documents(SearchQuery(text="KRAS"))
    assert len(results) == 1
    assert results[0].file_id == "f1"


async def test_search_offset_pagination(db: Database):
    """Offset parameter enables pagination through results."""
    for i in range(5):
        await db.insert_document(
            make_doc(file_id=f"f{i}", filename=f"2026021{i}_NOUonko_labs_CBC.pdf")
        )

    # Page 1: first 2
    page1 = await db.search_documents(SearchQuery(text="CBC", limit=2, offset=0))
    assert len(page1) == 2

    # Page 2: next 2
    page2 = await db.search_documents(SearchQuery(text="CBC", limit=2, offset=2))
    assert len(page2) == 2

    # No overlap
    ids1 = {d.file_id for d in page1}
    ids2 = {d.file_id for d in page2}
    assert ids1.isdisjoint(ids2)

    # Page 3: last 1
    page3 = await db.search_documents(SearchQuery(text="CBC", limit=2, offset=4))
    assert len(page3) == 1


async def test_search_no_text_date_order(db: Database):
    """Without text, results sort by document_date DESC."""
    from datetime import date

    doc1 = make_doc(
        file_id="f1", filename="20260101_NOUonko_labs_a.pdf", document_date=date(2026, 1, 1)
    )
    doc2 = make_doc(
        file_id="f2", filename="20260215_NOUonko_labs_b.pdf", document_date=date(2026, 2, 15)
    )
    await db.insert_document(doc1)
    await db.insert_document(doc2)

    results = await db.search_documents(SearchQuery(category="labs"))
    assert len(results) == 2
    # Newer first when no text query
    assert results[0].file_id == "f2"


async def test_search_empty_query_returns_all(db: Database):
    """Empty text with no filters returns all non-deleted documents."""
    await db.insert_document(make_doc(file_id="f1"))
    await db.insert_document(make_doc(file_id="f2"))

    results = await db.search_documents(SearchQuery())
    assert len(results) == 2
