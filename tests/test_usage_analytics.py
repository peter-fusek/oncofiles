"""Tests for usage analytics mixin."""

from __future__ import annotations

import pytest

from oncofiles.database._analytics import _estimate_cost, _percentile

# ── Unit tests for helpers ────────────────────────────────────────────


def test_estimate_cost_zero():
    assert _estimate_cost(0, 0) == 0.0


def test_estimate_cost_haiku():
    # 1M input + 1M output at Haiku 4.5 rates
    cost = _estimate_cost(1_000_000, 1_000_000)
    assert cost == pytest.approx(4.80, abs=0.01)  # $0.80 + $4.00


def test_estimate_cost_real_world():
    # Typical enhance call: ~1000 in, ~400 out
    cost = _estimate_cost(1000, 400)
    assert cost == pytest.approx(0.0024, abs=0.001)


def test_percentile_empty():
    assert _percentile([], 50) == 0.0


def test_percentile_single():
    assert _percentile([100.0], 50) == 100.0


def test_percentile_median():
    assert _percentile([1, 2, 3, 4, 5], 50) == 3.0


def test_percentile_p95():
    values = list(range(1, 101))  # 1..100
    p95 = _percentile(values, 95)
    assert 95 <= p95 <= 96


def test_percentile_p99():
    values = list(range(1, 101))
    p99 = _percentile(values, 99)
    assert 99 <= p99 <= 100


# ── Integration tests against in-memory DB ────────────────────────────


async def _seed_prompt_log(db, count=5, call_type="summary_tags", status="ok"):
    """Insert test prompt log entries."""
    for i in range(count):
        await db.db.execute(
            """
            INSERT INTO prompt_log (call_type, model, input_tokens,
                output_tokens, duration_ms, status, created_at)
            VALUES (?, 'haiku-4-5', ?, ?, ?, ?,
                strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            """,
            (call_type, 1000 + i * 100, 400 + i * 50, 3000 + i * 500, status),
        )
    await db.db.commit()


async def _seed_activity_log(db, count=5, tool_name="search_documents"):
    """Insert test activity log entries."""
    for _ in range(count):
        await db.db.execute(
            """
            INSERT INTO activity_log (session_id, agent_id, tool_name, status, created_at)
            VALUES ('test-session', 'test-agent', ?, 'ok',
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            """,
            (tool_name,),
        )
    await db.db.commit()


async def _seed_sync_history(db, count=3, status="completed"):
    """Insert test sync history entries."""
    for i in range(count):
        await db.db.execute(
            """
            INSERT INTO sync_history (started_at, finished_at, status, duration_s,
                                       from_gdrive_new, to_gdrive_exported)
            VALUES (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                    ?, ?, ?, ?)
            """,
            (status, 10.0 + i, 2 + i, 1),
        )
    await db.db.commit()


@pytest.mark.asyncio
async def test_prompt_stats_empty(db):
    stats = await db.get_prompt_stats()
    assert stats.total_calls == 0
    assert stats.estimated_cost_usd == 0.0
    assert stats.by_call_type == {}


@pytest.mark.asyncio
async def test_prompt_stats_with_data(db):
    await _seed_prompt_log(db, count=5, call_type="ocr")
    await _seed_prompt_log(db, count=3, call_type="summary_tags")
    stats = await db.get_prompt_stats()
    assert stats.total_calls == 8
    assert stats.total_input_tokens > 0
    assert stats.total_output_tokens > 0
    assert stats.estimated_cost_usd > 0
    assert "ocr" in stats.by_call_type
    assert "summary_tags" in stats.by_call_type
    assert stats.by_call_type["ocr"]["count"] == 5
    assert stats.error_rate == 0.0


@pytest.mark.asyncio
async def test_prompt_stats_errors(db):
    await _seed_prompt_log(db, count=4, status="ok")
    await _seed_prompt_log(db, count=1, status="error")
    stats = await db.get_prompt_stats()
    assert stats.total_errors == 1
    assert stats.error_rate == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_prompt_stats_calls_per_day(db):
    await _seed_prompt_log(db, count=3)
    stats = await db.get_prompt_stats()
    assert len(stats.calls_per_day) >= 1
    assert stats.calls_per_day[0]["count"] == 3


@pytest.mark.asyncio
async def test_tool_usage_stats_empty(db):
    stats = await db.get_tool_usage_stats()
    assert stats.total_calls == 0
    assert stats.unique_tools == 0
    assert stats.top_tools == []


@pytest.mark.asyncio
async def test_tool_usage_stats_with_data(db):
    await _seed_activity_log(db, count=10, tool_name="search_documents")
    await _seed_activity_log(db, count=5, tool_name="view_document")
    await _seed_activity_log(db, count=2, tool_name="list_treatment_events")
    stats = await db.get_tool_usage_stats()
    assert stats.total_calls == 17
    assert stats.unique_tools == 3
    assert stats.top_tools[0]["tool"] == "search_documents"
    assert stats.top_tools[0]["count"] == 10


@pytest.mark.asyncio
async def test_tool_usage_calls_per_day(db):
    await _seed_activity_log(db, count=5)
    stats = await db.get_tool_usage_stats()
    assert len(stats.calls_per_day) >= 1


@pytest.mark.asyncio
async def test_pipeline_stats_empty(db):
    stats = await db.get_pipeline_stats()
    assert stats.total_syncs == 0
    assert stats.docs_enhanced == 0


@pytest.mark.asyncio
async def test_pipeline_stats_with_syncs(db):
    await _seed_sync_history(db, count=3, status="completed")
    await _seed_sync_history(db, count=1, status="error")
    stats = await db.get_pipeline_stats()
    assert stats.total_syncs == 4
    assert stats.successful_syncs == 3
    assert stats.failed_syncs == 1
    assert stats.avg_sync_duration_s > 0
    assert stats.total_docs_imported > 0


@pytest.mark.asyncio
async def test_latency_percentiles_empty(db):
    result = await db.get_prompt_latency_percentiles()
    assert result == {}


@pytest.mark.asyncio
async def test_latency_percentiles_with_data(db):
    await _seed_prompt_log(db, count=10, call_type="ocr")
    result = await db.get_prompt_latency_percentiles()
    assert "ocr" in result
    assert "_overall" in result
    assert result["ocr"]["p50_ms"] > 0
    assert result["ocr"]["p95_ms"] >= result["ocr"]["p50_ms"]
    assert result["ocr"]["count"] == 10
