"""Database mixin for prompt log (AI call observability)."""

from __future__ import annotations

from oncofiles.database._converters import _row_to_prompt_log
from oncofiles.models import PromptLogEntry, PromptLogQuery


class PromptLogMixin:
    """CRUD methods for the prompt_log table."""

    async def insert_prompt_log(self, entry: PromptLogEntry) -> PromptLogEntry:
        """Insert a prompt log entry and return it with the generated ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO prompt_log
                (call_type, document_id, patient_id, model, system_prompt, user_prompt,
                 raw_response, input_tokens, output_tokens, duration_ms,
                 result_summary, status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.call_type.value if hasattr(entry.call_type, "value") else entry.call_type,
                entry.document_id,
                entry.patient_id,
                entry.model,
                entry.system_prompt,
                entry.user_prompt,
                entry.raw_response,
                entry.input_tokens,
                entry.output_tokens,
                entry.duration_ms,
                entry.result_summary,
                entry.status,
                entry.error_message,
            ),
        )
        await self.db.commit()
        entry.id = cursor.lastrowid
        return entry

    async def get_prompt_log(self, entry_id: int) -> PromptLogEntry | None:
        """Get a single prompt log entry by ID."""
        async with self.db.execute("SELECT * FROM prompt_log WHERE id = ?", (entry_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_prompt_log(row) if row else None

    async def search_prompt_log(
        self, query: PromptLogQuery, *, patient_id: str | None = None
    ) -> list[PromptLogEntry]:
        """Search prompt logs with filters, optionally scoped to a patient."""
        conditions: list[str] = []
        params: list = []

        if patient_id:
            conditions.append("patient_id = ?")
            params.append(patient_id)

        if query.call_type:
            conditions.append("call_type = ?")
            params.append(query.call_type)
        if query.document_id is not None:
            conditions.append("document_id = ?")
            params.append(query.document_id)
        if query.status:
            conditions.append("status = ?")
            params.append(query.status)
        if query.date_from:
            conditions.append("created_at >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("created_at <= ?")
            params.append(query.date_to.isoformat() + "T23:59:59Z")
        if query.text:
            conditions.append(
                "(user_prompt LIKE ? OR raw_response LIKE ? OR result_summary LIKE ?)"
            )
            like = f"%{query.text}%"
            params.extend([like, like, like])

        where = " AND ".join(conditions) if conditions else "1=1"
        limit = min(max(1, query.limit), 200)
        params.append(limit)

        async with self.db.execute(
            f"SELECT * FROM prompt_log WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_prompt_log(r) for r in rows]

    async def get_prompt_log_stats(self, *, patient_id: str | None = None) -> dict:
        """Get aggregate stats by call_type, optionally scoped to a patient."""
        where = "WHERE patient_id = ?" if patient_id else ""
        params = [patient_id] if patient_id else []

        async with self.db.execute(
            f"""
            SELECT call_type,
                   COUNT(*) as count,
                   SUM(input_tokens) as total_input_tokens,
                   SUM(output_tokens) as total_output_tokens,
                   ROUND(AVG(duration_ms)) as avg_duration_ms,
                   SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as errors
            FROM prompt_log
            {where}
            GROUP BY call_type
            ORDER BY call_type
            """,
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            stats = {}
            for r in rows:
                stats[r["call_type"]] = {
                    "count": r["count"],
                    "total_input_tokens": r["total_input_tokens"] or 0,
                    "total_output_tokens": r["total_output_tokens"] or 0,
                    "avg_duration_ms": r["avg_duration_ms"],
                    "errors": r["errors"],
                }
            return stats
