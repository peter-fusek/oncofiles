"""Tests for research entries (#33)."""

from oncofiles.database import Database
from oncofiles.models import ResearchQuery

from .helpers import make_research_entry


async def test_insert_and_get(db: Database):
    entry = make_research_entry()
    saved = await db.insert_research_entry(entry)
    assert saved.id is not None
    assert saved.source == "pubmed"
    assert saved.external_id == "PMID12345"


async def test_dedup_by_source_external_id(db: Database):
    e1 = make_research_entry(source="pubmed", external_id="PMID111", title="First")
    e2 = make_research_entry(source="pubmed", external_id="PMID111", title="Duplicate")

    saved1 = await db.insert_research_entry(e1)
    saved2 = await db.insert_research_entry(e2)

    # Should return existing entry, not create a new one
    assert saved1.id == saved2.id
    assert saved2.title == "First"  # original title preserved


async def test_different_sources_not_deduped(db: Database):
    e1 = make_research_entry(source="pubmed", external_id="X1")
    e2 = make_research_entry(source="clinicaltrials", external_id="X1")

    saved1 = await db.insert_research_entry(e1)
    saved2 = await db.insert_research_entry(e2)
    assert saved1.id != saved2.id


async def test_search_by_text_in_title(db: Database):
    await db.insert_research_entry(
        make_research_entry(external_id="1", title="FOLFOX efficacy study", summary="", tags="[]")
    )
    await db.insert_research_entry(
        make_research_entry(external_id="2", title="Immunotherapy review", summary="", tags="[]")
    )

    results = await db.search_research_entries(ResearchQuery(text="FOLFOX"))
    assert len(results) == 1
    assert "FOLFOX" in results[0].title


async def test_search_by_text_in_summary(db: Database):
    await db.insert_research_entry(
        make_research_entry(external_id="1", summary="oxaliplatin-based regimen")
    )
    await db.insert_research_entry(
        make_research_entry(external_id="2", summary="pembrolizumab data")
    )

    results = await db.search_research_entries(ResearchQuery(text="oxaliplatin"))
    assert len(results) == 1


async def test_search_by_text_in_tags(db: Database):
    await db.insert_research_entry(
        make_research_entry(external_id="1", title="Study A", summary="", tags='["mCRC","FOLFOX"]')
    )
    await db.insert_research_entry(
        make_research_entry(external_id="2", title="Study B", summary="", tags='["melanoma"]')
    )

    results = await db.search_research_entries(ResearchQuery(text="mCRC"))
    assert len(results) == 1


async def test_search_by_source(db: Database):
    await db.insert_research_entry(make_research_entry(source="pubmed", external_id="1"))
    await db.insert_research_entry(make_research_entry(source="clinicaltrials", external_id="2"))

    results = await db.search_research_entries(ResearchQuery(source="pubmed"))
    assert len(results) == 1
    assert results[0].source == "pubmed"


async def test_search_combined_text_and_source(db: Database):
    await db.insert_research_entry(
        make_research_entry(source="pubmed", external_id="1", title="FOLFOX study")
    )
    await db.insert_research_entry(
        make_research_entry(source="clinicaltrials", external_id="2", title="FOLFOX trial")
    )

    results = await db.search_research_entries(
        ResearchQuery(text="FOLFOX", source="clinicaltrials")
    )
    assert len(results) == 1
    assert results[0].source == "clinicaltrials"


async def test_list_all(db: Database):
    await db.insert_research_entry(make_research_entry(external_id="1"))
    await db.insert_research_entry(make_research_entry(external_id="2"))

    entries = await db.list_research_entries()
    assert len(entries) == 2


async def test_list_by_source(db: Database):
    await db.insert_research_entry(make_research_entry(source="pubmed", external_id="1"))
    await db.insert_research_entry(make_research_entry(source="clinicaltrials", external_id="2"))

    entries = await db.list_research_entries(source="pubmed")
    assert len(entries) == 1


async def test_list_with_limit(db: Database):
    for i in range(5):
        await db.insert_research_entry(make_research_entry(external_id=f"PM{i}"))

    entries = await db.list_research_entries(limit=3)
    assert len(entries) == 3


async def test_timestamps_set(db: Database):
    saved = await db.insert_research_entry(make_research_entry())
    assert saved.created_at is not None or saved.id is not None
    # Re-fetch to get timestamps from DB
    entries = await db.list_research_entries()
    assert entries[0].created_at is not None
