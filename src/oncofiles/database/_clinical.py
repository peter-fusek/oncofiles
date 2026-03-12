"""Clinical data: treatment events, research entries, lab values."""

from __future__ import annotations

from oncofiles.models import (
    LabTrendQuery,
    LabValue,
    ResearchEntry,
    ResearchQuery,
    TreatmentEvent,
    TreatmentEventQuery,
)

from ._converters import _row_to_lab_value, _row_to_research_entry, _row_to_treatment_event


class ClinicalMixin:
    """Treatment events, research entries, and lab value operations."""

    # ── Treatment events (#34) ───────────────────────────────────────────

    async def insert_treatment_event(self, event: TreatmentEvent) -> TreatmentEvent:
        """Insert a treatment event and return it with the generated ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO treatment_events (event_date, event_type, title, notes, metadata)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event.event_date.isoformat(),
                event.event_type,
                event.title,
                event.notes,
                event.metadata,
            ),
        )
        await self.db.commit()
        event.id = cursor.lastrowid
        return event

    async def get_treatment_event(self, event_id: int) -> TreatmentEvent | None:
        """Get a treatment event by ID."""
        async with self.db.execute(
            "SELECT * FROM treatment_events WHERE id = ?", (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_treatment_event(row) if row else None

    async def list_treatment_events(self, query: TreatmentEventQuery) -> list[TreatmentEvent]:
        """List treatment events with optional filters."""
        conditions: list[str] = []
        params: list[str | int] = []

        if query.event_type:
            conditions.append("event_type = ?")
            params.append(query.event_type)
        if query.date_from:
            conditions.append("event_date >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("event_date <= ?")
            params.append(query.date_to.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM treatment_events WHERE {where} ORDER BY event_date DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_treatment_event(r) for r in rows]

    async def get_treatment_events_timeline(self, limit: int = 200) -> list[TreatmentEvent]:
        """Get treatment events in chronological (ASC) order."""
        async with self.db.execute(
            "SELECT * FROM treatment_events ORDER BY event_date ASC, created_at ASC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_treatment_event(r) for r in rows]

    # ── Research entries (#33) ───────────────────────────────────────────

    async def insert_research_entry(self, entry: ResearchEntry) -> ResearchEntry:
        """Insert a research entry. Ignores duplicates (source+external_id)."""
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO research_entries
                (source, external_id, title, summary, tags, raw_data)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                entry.source,
                entry.external_id,
                entry.title,
                entry.summary,
                entry.tags,
                entry.raw_data,
            ),
        )
        await self.db.commit()

        if cursor.rowcount > 0:
            entry.id = cursor.lastrowid
            return entry

        # Duplicate (INSERT OR IGNORE skipped) — return existing row
        async with self.db.execute(
            "SELECT * FROM research_entries WHERE source = ? AND external_id = ?",
            (entry.source, entry.external_id),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_research_entry(row)

    async def search_research_entries(self, query: ResearchQuery) -> list[ResearchEntry]:
        """Search research entries using LIKE on title/summary/tags."""
        conditions: list[str] = []
        params: list[str | int] = []

        if query.text:
            like_param = f"%{query.text}%"
            conditions.append("(title LIKE ? OR summary LIKE ? OR tags LIKE ?)")
            params.extend([like_param, like_param, like_param])
        if query.source:
            conditions.append("source = ?")
            params.append(query.source)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM research_entries WHERE {where} ORDER BY created_at DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_research_entry(r) for r in rows]

    async def list_research_entries(
        self, source: str | None = None, limit: int = 50
    ) -> list[ResearchEntry]:
        """List research entries, optionally filtered by source."""
        if source:
            async with self.db.execute(
                "SELECT * FROM research_entries WHERE source = ? ORDER BY created_at DESC LIMIT ?",
                (source, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM research_entries ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_research_entry(r) for r in rows]

    # ── Lab values (#59) ──────────────────────────────────────────────

    async def insert_lab_values(self, values: list[LabValue]) -> int:
        """Bulk insert lab values. Uses INSERT OR REPLACE for idempotency."""
        count = 0
        for v in values:
            await self.db.execute(
                """
                INSERT OR REPLACE INTO lab_values
                    (document_id, lab_date, parameter, value, unit,
                     reference_low, reference_high, flag)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    v.document_id,
                    v.lab_date.isoformat(),
                    v.parameter,
                    v.value,
                    v.unit,
                    v.reference_low,
                    v.reference_high,
                    v.flag,
                ),
            )
            count += 1
        await self.db.commit()
        return count

    async def get_lab_trends(self, query: LabTrendQuery) -> list[LabValue]:
        """Get lab values filtered by parameter and date range, chronological order."""
        conditions: list[str] = []
        params: list[str | int | float] = []

        if query.parameter:
            conditions.append("parameter = ?")
            params.append(query.parameter)
        if query.date_from:
            conditions.append("lab_date >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("lab_date <= ?")
            params.append(query.date_to.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM lab_values WHERE {where} ORDER BY lab_date ASC, parameter ASC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_lab_snapshot(self, document_id: int) -> list[LabValue]:
        """Get all lab values from a specific document."""
        async with self.db.execute(
            "SELECT * FROM lab_values WHERE document_id = ? ORDER BY parameter",
            (document_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_latest_lab_value(self, parameter: str) -> LabValue | None:
        """Get the most recent value for a given parameter."""
        async with self.db.execute(
            "SELECT * FROM lab_values WHERE parameter = ? ORDER BY lab_date DESC LIMIT 1",
            (parameter,),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_lab_value(row) if row else None

    async def get_all_latest_lab_values(self) -> list[LabValue]:
        """Get the most recent value for every tracked parameter."""
        async with self.db.execute(
            """
            SELECT lv.* FROM lab_values lv
            INNER JOIN (
                SELECT parameter, MAX(lab_date) AS max_date
                FROM lab_values GROUP BY parameter
            ) latest ON lv.parameter = latest.parameter
                AND lv.lab_date = latest.max_date
            ORDER BY lv.parameter
            """,
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_lab_values_by_date(self, lab_date: str) -> list[LabValue]:
        """Get all lab values for a specific date."""
        async with self.db.execute(
            "SELECT * FROM lab_values WHERE lab_date = ? ORDER BY parameter",
            (lab_date,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_distinct_lab_dates(self) -> list[str]:
        """Get all distinct lab dates, most recent first."""
        async with self.db.execute(
            "SELECT DISTINCT lab_date FROM lab_values ORDER BY lab_date DESC",
        ) as cursor:
            rows = await cursor.fetchall()
            return [row["lab_date"] if isinstance(row, dict) else row[0] for row in rows]
