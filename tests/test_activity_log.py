"""Tests for activity log (#38)."""

from oncofiles.database import Database
from oncofiles.models import ActivityLogQuery

from .helpers import make_activity_log


async def test_insert(db: Database):
    entry = make_activity_log()
    saved = await db.insert_activity_log(entry)
    assert saved.id is not None
    assert saved.tool_name == "search_pubmed"


async def test_search_by_session(db: Database):
    await db.insert_activity_log(make_activity_log(session_id="s1"))
    await db.insert_activity_log(make_activity_log(session_id="s2"))

    results = await db.search_activity_log(ActivityLogQuery(session_id="s1"))
    assert len(results) == 1
    assert results[0].session_id == "s1"


async def test_search_by_agent(db: Database):
    await db.insert_activity_log(make_activity_log(agent_id="oncoteam"))
    await db.insert_activity_log(make_activity_log(agent_id="other"))

    results = await db.search_activity_log(ActivityLogQuery(agent_id="oncoteam"))
    assert len(results) == 1


async def test_search_by_tool_name(db: Database):
    await db.insert_activity_log(make_activity_log(tool_name="search_pubmed"))
    await db.insert_activity_log(make_activity_log(tool_name="add_research_entry"))

    results = await db.search_activity_log(ActivityLogQuery(tool_name="search_pubmed"))
    assert len(results) == 1


async def test_search_by_status(db: Database):
    await db.insert_activity_log(make_activity_log(status="ok"))
    await db.insert_activity_log(make_activity_log(status="error", error_message="timeout"))

    results = await db.search_activity_log(ActivityLogQuery(status="error"))
    assert len(results) == 1
    assert results[0].error_message == "timeout"


async def test_search_by_text(db: Database):
    await db.insert_activity_log(make_activity_log(input_summary="FOLFOX search"))
    await db.insert_activity_log(make_activity_log(input_summary="blood count"))

    results = await db.search_activity_log(ActivityLogQuery(text="FOLFOX"))
    assert len(results) == 1


async def test_search_with_limit(db: Database):
    for i in range(5):
        await db.insert_activity_log(make_activity_log(session_id=f"s{i}"))

    results = await db.search_activity_log(ActivityLogQuery(limit=3))
    assert len(results) == 3


async def test_search_empty(db: Database):
    results = await db.search_activity_log(ActivityLogQuery())
    assert results == []


async def test_stats_basic(db: Database):
    await db.insert_activity_log(make_activity_log(tool_name="search_pubmed", status="ok"))
    await db.insert_activity_log(make_activity_log(tool_name="search_pubmed", status="ok"))
    await db.insert_activity_log(make_activity_log(tool_name="search_pubmed", status="error"))
    await db.insert_activity_log(make_activity_log(tool_name="add_research_entry", status="ok"))

    stats = await db.get_activity_stats()
    assert len(stats) == 3  # search_pubmed/ok, search_pubmed/error, add_research_entry/ok
    # Most frequent first
    assert stats[0]["tool_name"] == "search_pubmed"
    assert stats[0]["count"] == 2


async def test_stats_filter_by_agent(db: Database):
    await db.insert_activity_log(make_activity_log(agent_id="oncoteam"))
    await db.insert_activity_log(make_activity_log(agent_id="other"))

    stats = await db.get_activity_stats(agent_id="oncoteam")
    assert len(stats) == 1
    assert stats[0]["count"] == 1


async def test_timeline_recent(db: Database):
    await db.insert_activity_log(make_activity_log())

    timeline = await db.get_activity_timeline(hours=24)
    assert len(timeline) == 1


async def test_timestamps_set(db: Database):
    await db.insert_activity_log(make_activity_log())
    # Re-fetch via search to get DB timestamps
    results = await db.search_activity_log(ActivityLogQuery())
    assert len(results) == 1
    assert results[0].created_at is not None
