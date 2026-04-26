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

    async def get_prompt_stats(
        self, days: int = 30, *, patient_id: str | None = None
    ) -> PromptStats:
        """Aggregate prompt log stats for the last N days, optionally per patient."""
        stats = PromptStats()

        pid_filter = "AND patient_id = ?" if patient_id else ""
        params: tuple = (f"-{days} days", patient_id) if patient_id else (f"-{days} days",)

        # By call type
        async with self.db.execute(
            f"""
            SELECT call_type,
                   COUNT(*) as cnt,
                   COALESCE(SUM(input_tokens), 0) as in_tok,
                   COALESCE(SUM(output_tokens), 0) as out_tok,
                   ROUND(AVG(duration_ms)) as avg_ms,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errs
            FROM prompt_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            {pid_filter}
            GROUP BY call_type
            ORDER BY cnt DESC
            """,
            params,
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
            f"""
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM prompt_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            {pid_filter}
            GROUP BY day
            ORDER BY day
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        stats.calls_per_day = [{"date": r["day"], "count": r["cnt"]} for r in rows]

        return stats

    async def get_per_patient_cost_leaderboard(
        self, days: int = 30, *, limit: int = 50
    ) -> list[dict]:
        """Aggregate AI spend per patient for the last N days (#411 Part A).

        Admin-only by contract — caller MUST check `_is_admin_caller()` before
        invoking this method. Returns one row per patient with non-zero
        activity, sorted by total cost descending so the dashboard can flag
        the top spenders for anomaly review.

        Joins ``prompt_log`` with ``patients`` so the response includes slug
        and display_name (saved from re-querying per row on the dashboard).
        Patients with zero AI activity in the window are NOT returned —
        the table represents only spenders. The leaderboard sums
        ``estimated_cost_usd`` directly from ``prompt_log``; rows where
        that column is NULL (pre-#442 history) are skipped via ``COALESCE``
        so they don't fall under "free" by mistake — they're recomputed
        from token counts via the same Haiku formula in
        ``_estimate_cost`` for parity with ``get_prompt_stats``.

        Args:
            days: rolling window. Capped at 365 by callers (no DB enforcement).
            limit: max patients to return. Default 50 — covers all multi-tenant
                deployments we expect; the unbounded form would expose every
                patient slug to admin in one call which is fine but not free.

        Returns:
            List of dicts: ``{patient_id, slug, display_name, total_calls,
            total_input_tokens, total_output_tokens, total_cost_usd,
            error_count, last_call_at, top_call_type}``.
        """
        # Build the per-patient aggregate. The COALESCE on estimated_cost_usd
        # falls back to recomputing from tokens when the column is NULL — this
        # keeps historical pre-migration-058 rows from silently disappearing.
        async with self.db.execute(
            """
            SELECT
                p.patient_id            AS patient_id,
                COUNT(*)                AS total_calls,
                COALESCE(SUM(input_tokens), 0)  AS in_tok,
                COALESCE(SUM(output_tokens), 0) AS out_tok,
                COALESCE(
                    SUM(estimated_cost_usd),
                    0
                )                       AS billed_cost_usd,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errs,
                MAX(created_at)         AS last_call_at
            FROM prompt_log p
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            GROUP BY p.patient_id
            ORDER BY billed_cost_usd DESC, total_calls DESC
            LIMIT ?
            """,
            (f"-{days} days", limit),
        ) as cursor:
            rows = await cursor.fetchall()

        # Build a slug/display_name map for the patients we hit, plus an
        # extra top-call-type subquery per row.
        leaderboard: list[dict] = []
        for r in rows:
            pid = r["patient_id"]
            in_tok = r["in_tok"] or 0
            out_tok = r["out_tok"] or 0
            billed = r["billed_cost_usd"] or 0.0

            # If `estimated_cost_usd` was NULL on every row this aggregates,
            # billed will be 0 even though there was activity — recompute
            # from tokens for parity with `get_prompt_stats`.
            cost = billed if billed > 0 else _estimate_cost(in_tok, out_tok)

            # Resolve slug + display_name for non-system patients only — the
            # `__system_no_patient__` sentinel won't have a row in `patients`.
            slug = ""
            display_name = ""
            if pid and not pid.startswith("__"):
                async with self.db.execute(
                    "SELECT slug, display_name FROM patients WHERE patient_id = ?",
                    (pid,),
                ) as p_cur:
                    p_row = await p_cur.fetchone()
                if p_row:
                    p_dict = dict(p_row)
                    slug = p_dict.get("slug") or ""
                    display_name = p_dict.get("display_name") or ""

            # Top call_type for this patient over the window — single extra
            # query per leaderboard row but bounded by `limit` (default 50).
            async with self.db.execute(
                """
                SELECT call_type, COUNT(*) AS cnt
                FROM prompt_log
                WHERE patient_id = ?
                  AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
                GROUP BY call_type
                ORDER BY cnt DESC
                LIMIT 1
                """,
                (pid, f"-{days} days"),
            ) as t_cur:
                top_row = await t_cur.fetchone()
                top_call_type = dict(top_row)["call_type"] if top_row else None

            leaderboard.append(
                {
                    "patient_id": pid,
                    "slug": slug,
                    "display_name": display_name,
                    "total_calls": r["total_calls"],
                    "total_input_tokens": in_tok,
                    "total_output_tokens": out_tok,
                    "total_cost_usd": round(cost, 6),
                    "error_count": r["errs"] or 0,
                    "last_call_at": r["last_call_at"],
                    "top_call_type": top_call_type,
                }
            )

        return leaderboard

    async def get_tool_usage_stats(
        self, days: int = 30, *, patient_id: str | None = None
    ) -> ToolUsageStats:
        """Aggregate MCP tool usage from activity log for the last N days.

        patient_id semantics (#476 hardened): None = unscoped (admin/audit);
        any string (incl. "") = scoped — empty string matches 0 rows
        rather than silently bleeding cross-patient.
        """
        stats = ToolUsageStats()
        pid_clause = "AND patient_id = ?" if patient_id is not None else ""
        params: list = [f"-{days} days"]
        if patient_id is not None:
            params.append(patient_id)

        async with self.db.execute(
            f"""
            SELECT tool_name, COUNT(*) as cnt,
                   AVG(duration_ms) as avg_dur,
                   MAX(created_at) as last_called,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as err_cnt
            FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            {pid_clause}
            GROUP BY tool_name
            ORDER BY cnt DESC
            LIMIT 20
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()

        stats.top_tools = [
            {
                "tool": r["tool_name"],
                "count": r["cnt"],
                "avg_duration_ms": round(r["avg_dur"]) if r["avg_dur"] else None,
                "last_called": r["last_called"],
                "error_count": r["err_cnt"] or 0,
            }
            for r in rows
        ]
        stats.total_calls = sum(t["count"] for t in stats.top_tools)

        async with self.db.execute(
            f"""
            SELECT COUNT(DISTINCT tool_name) as cnt
            FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            {pid_clause}
            """,
            params,
        ) as cursor:
            row = await cursor.fetchone()
            stats.unique_tools = row["cnt"] if row else 0

        async with self.db.execute(
            f"""
            SELECT DATE(created_at) as day, COUNT(*) as cnt
            FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            {pid_clause}
            GROUP BY day
            ORDER BY day
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
        stats.calls_per_day = [{"date": r["day"], "count": r["cnt"]} for r in rows]

        return stats

    async def get_pipeline_stats(self, *, patient_id: str | None = None) -> PipelineStats:
        """Aggregate sync and enhancement pipeline statistics.

        patient_id semantics (#476 hardened): None = unscoped (admin/audit);
        any string (incl. "") = scoped — empty string matches 0 rows
        rather than silently bleeding cross-patient.
        """
        stats = PipelineStats()

        # Sync history
        async with self.db.execute(
            """
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as ok,
                   SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as errs,
                   COALESCE(SUM(from_gdrive_new), 0) as imported,
                   COALESCE(SUM(to_gdrive_exported), 0) as exported,
                   ROUND(AVG(duration_s), 1) as avg_dur
            FROM sync_history
            WHERE started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')
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
        pid_clause2 = "AND patient_id = ?" if patient_id is not None else ""
        params2 = [patient_id] if patient_id is not None else []
        async with self.db.execute(
            f"""
            SELECT
                SUM(CASE WHEN ai_summary IS NOT NULL
                    AND ai_summary != '' THEN 1 ELSE 0 END) as done,
                SUM(CASE WHEN ai_summary IS NULL
                    OR ai_summary = '' THEN 1 ELSE 0 END) as pending
            FROM documents
            WHERE deleted_at IS NULL {pid_clause2}
            """,
            params2,
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                stats.docs_enhanced = row["done"] or 0
                stats.docs_pending = row["pending"] or 0

        return stats

    async def get_prompt_latency_percentiles(
        self, days: int = 30, *, patient_id: str | None = None
    ) -> dict:
        """Calculate latency percentiles (p50, p95, p99) from prompt log."""
        pid_filter = "AND patient_id = ?" if patient_id else ""
        params: tuple = (f"-{days} days", patient_id) if patient_id else (f"-{days} days",)

        async with self.db.execute(
            f"""
            SELECT call_type, duration_ms
            FROM prompt_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
              AND duration_ms IS NOT NULL
            {pid_filter}
            ORDER BY call_type, duration_ms
            """,
            params,
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
