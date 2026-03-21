"""Database mixin for usage analytics aggregations."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PromptStats:
    """Aggregate prompt log statistics."""

    total_calls: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_errors: int = 0
    error_rate: float = 0.0
    estimated_cost_usd: float = 0.0
    by_call_type: dict = field(default_factory=dict)
    calls_per_day: list = field(default_factory=list)


@dataclass
class ToolUsageStats:
    """Aggregate tool usage from activity log."""

    total_calls: int = 0
    unique_tools: int = 0
    top_tools: list = field(default_factory=list)
    calls_per_day: list = field(default_factory=list)


@dataclass
class PipelineStats:
    """Sync and enhancement pipeline statistics."""

    total_syncs: int = 0
    successful_syncs: int = 0
    failed_syncs: int = 0
    total_docs_imported: int = 0
    total_docs_exported: int = 0
    avg_sync_duration_s: float = 0.0
    docs_enhanced: int = 0
    docs_pending: int = 0


# Haiku 4.5 pricing (per 1M tokens)
_HAIKU_INPUT_PER_M = 0.80
_HAIKU_OUTPUT_PER_M = 4.00


def _estimate_cost(input_tokens: int, output_tokens: int) -> float:
    """Estimate USD cost based on Haiku 4.5 pricing."""
    return (input_tokens * _HAIKU_INPUT_PER_M + output_tokens * _HAIKU_OUTPUT_PER_M) / 1_000_000


def _percentile(values: list[float], p: float) -> float:
    """Calculate percentile from a sorted list of values."""
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * (p / 100.0)
    f = int(k)
    c = f + 1
    if c >= len(values):
        return values[f]
    return values[f] + (k - f) * (values[c] - values[f])


class AnalyticsMixin:
    """Usage analytics aggregation methods."""

    async def get_prompt_stats(self, days: int = 30) -> PromptStats:
        """Aggregate prompt log stats for the last N days."""
        stats = PromptStats()

        # By call type
        async with self.db.execute(
            """
            SELECT call_type,
                   COUNT(*) as cnt,
                   COALESCE(SUM(input_tokens), 0) as in_tok,
                   COALESCE(SUM(output_tokens), 0) as out_tok,
                   ROUND(AVG(duration_ms)) as avg_ms,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errs
            FROM prompt_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            GROUP BY call_type
            ORDER BY cnt DESC
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()

        for r in rows:
            ct = r["call_type"]
            cnt = r["cnt"]
            in_tok = r["in_tok"]
            out_tok = r["out_tok"]
            stats.total_calls += cnt
            stats.total_input_tokens += in_tok
            stats.total_output_tokens += out_tok
            stats.total_errors += r["errs"]
            stats.by_call_type[ct] = {
                "count": cnt,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "avg_duration_ms": r["avg_ms"],
                "errors": r["errs"],
                "cost_usd": round(_estimate_cost(in_tok, out_tok), 4),
            }

        if stats.total_calls > 0:
            stats.error_rate = round(stats.total_errors / stats.total_calls, 4)
        stats.estimated_cost_usd = round(
            _estimate_cost(stats.total_input_tokens, stats.total_output_tokens), 4
        )

        # Calls per day (last N days)
        async with self.db.execute(
            """
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM prompt_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            GROUP BY day
            ORDER BY day
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()
        stats.calls_per_day = [{"date": r["day"], "count": r["cnt"]} for r in rows]

        return stats

    async def get_tool_usage_stats(self, days: int = 30) -> ToolUsageStats:
        """Aggregate MCP tool usage from activity log for the last N days."""
        stats = ToolUsageStats()

        # Top tools
        async with self.db.execute(
            """
            SELECT tool_name, COUNT(*) as cnt
            FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            GROUP BY tool_name
            ORDER BY cnt DESC
            LIMIT 20
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()

        stats.top_tools = [{"tool": r["tool_name"], "count": r["cnt"]} for r in rows]
        stats.total_calls = sum(t["count"] for t in stats.top_tools)

        # Unique tools count
        async with self.db.execute(
            """
            SELECT COUNT(DISTINCT tool_name) as cnt
            FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            """,
            (f"-{days} days",),
        ) as cursor:
            row = await cursor.fetchone()
            stats.unique_tools = row["cnt"] if row else 0

        # Calls per day
        async with self.db.execute(
            """
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            GROUP BY day
            ORDER BY day
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()
        stats.calls_per_day = [{"date": r["day"], "count": r["cnt"]} for r in rows]

        return stats

    async def get_pipeline_stats(self) -> PipelineStats:
        """Aggregate sync and enhancement pipeline statistics."""
        stats = PipelineStats()

        # Sync history
        async with self.db.execute(
            """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errs,
                   COALESCE(SUM(from_gdrive_new), 0) as imported,
                   COALESCE(SUM(to_gdrive_exported), 0) as exported,
                   ROUND(AVG(duration_s), 1) as avg_dur
            FROM sync_history
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stats.total_syncs = row["total"]
                stats.successful_syncs = row["ok"]
                stats.failed_syncs = row["errs"]
                stats.total_docs_imported = row["imported"]
                stats.total_docs_exported = row["exported"]
                stats.avg_sync_duration_s = row["avg_dur"] or 0.0

        # Enhancement status
        async with self.db.execute(
            """
            SELECT
                SUM(CASE WHEN ai_summary IS NOT NULL AND ai_summary != '' THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN ai_summary IS NULL OR ai_summary = '' THEN 1 ELSE 0 END) as pending
            FROM documents
            WHERE deleted_at IS NULL
            """
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stats.docs_enhanced = row["done"] or 0
                stats.docs_pending = row["pending"] or 0

        return stats

    async def get_prompt_latency_percentiles(self, days: int = 30) -> dict:
        """Calculate latency percentiles (p50, p95, p99) from prompt log."""
        async with self.db.execute(
            """
            SELECT call_type, duration_ms
            FROM prompt_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
              AND duration_ms IS NOT NULL
            ORDER BY call_type, duration_ms
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()

        # Group by call_type
        by_type: dict[str, list[float]] = {}
        all_values: list[float] = []
        for r in rows:
            ct = r["call_type"]
            ms = r["duration_ms"]
            by_type.setdefault(ct, []).append(ms)
            all_values.append(ms)

        result: dict = {}
        for ct, values in by_type.items():
            result[ct] = {
                "p50_ms": round(_percentile(values, 50)),
                "p95_ms": round(_percentile(values, 95)),
                "p99_ms": round(_percentile(values, 99)),
                "count": len(values),
            }

        if all_values:
            result["_overall"] = {
                "p50_ms": round(_percentile(all_values, 50)),
                "p95_ms": round(_percentile(all_values, 95)),
                "p99_ms": round(_percentile(all_values, 99)),
                "count": len(all_values),
            }

        return result
