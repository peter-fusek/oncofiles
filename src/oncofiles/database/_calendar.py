"""Calendar entry database mixin."""

from __future__ import annotations

from oncofiles.database._converters import _row_to_calendar_entry
from oncofiles.models import CalendarEntry, CalendarQuery


class CalendarMixin:
    """CRUD operations for calendar_entries table."""

    async def upsert_calendar_entry(self, entry: CalendarEntry) -> CalendarEntry:
        """Insert or update a calendar entry (idempotent by google_event_id)."""
        async with self.db.execute(
            """
            INSERT INTO calendar_entries
                (patient_id, google_event_id, summary, description, start_time,
                 end_time, location, attendees, recurrence, status,
                 ai_summary, treatment_event_id, is_medical, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(patient_id, google_event_id) DO UPDATE SET
                summary = excluded.summary,
                description = excluded.description,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                location = excluded.location,
                attendees = excluded.attendees,
                recurrence = excluded.recurrence,
                status = excluded.status,
                ai_summary = COALESCE(excluded.ai_summary, calendar_entries.ai_summary),
                treatment_event_id = COALESCE(
                    excluded.treatment_event_id, calendar_entries.treatment_event_id),
                is_medical = excluded.is_medical,
                updated_at = excluded.updated_at
            """,
            (
                entry.patient_id,
                entry.google_event_id,
                entry.summary,
                entry.description,
                entry.start_time.isoformat(),
                entry.end_time.isoformat() if entry.end_time else None,
                entry.location,
                entry.attendees,
                entry.recurrence,
                entry.status,
                entry.ai_summary,
                entry.treatment_event_id,
                1 if entry.is_medical else 0,
            ),
        ) as cursor:
            last_id = cursor.lastrowid
        await self.db.commit()
        if last_id:
            result = await self.get_calendar_entry(last_id)
            if result:
                return result
        return await self.get_calendar_entry_by_google_id(entry.google_event_id, entry.patient_id)

    async def get_calendar_entry(self, entry_id: int) -> CalendarEntry | None:
        """Get a calendar entry by internal ID."""
        async with self.db.execute(
            "SELECT * FROM calendar_entries WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_calendar_entry(row) if row else None

    async def get_calendar_entry_by_google_id(
        self, google_event_id: str, patient_id: str = "erika"
    ) -> CalendarEntry | None:
        """Get a calendar entry by Google event ID."""
        async with self.db.execute(
            "SELECT * FROM calendar_entries WHERE patient_id = ? AND google_event_id = ?",
            (patient_id, google_event_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_calendar_entry(row) if row else None

    async def search_calendar_entries(
        self, query: CalendarQuery, patient_id: str = "erika"
    ) -> list[CalendarEntry]:
        """Search calendar entries with text, date, and medical filters."""
        conditions = ["patient_id = ?"]
        params: list = [patient_id]
        if query.text:
            like = f"%{query.text}%"
            conditions.append("(summary LIKE ? OR description LIKE ?)")
            params.extend([like, like])
        if query.date_from:
            conditions.append("start_time >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("start_time <= ?")
            params.append(query.date_to.isoformat() + "T23:59:59Z")
        if query.is_medical is not None:
            conditions.append("is_medical = ?")
            params.append(1 if query.is_medical else 0)
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM calendar_entries WHERE {where} ORDER BY start_time DESC LIMIT ?"
        params.append(min(query.limit, 200))
        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_calendar_entry(r) for r in rows]

    async def list_calendar_entries(
        self, patient_id: str = "erika", limit: int = 50
    ) -> list[CalendarEntry]:
        """List recent calendar entries."""
        async with self.db.execute(
            "SELECT * FROM calendar_entries WHERE patient_id = ? ORDER BY start_time DESC LIMIT ?",
            (patient_id, min(limit, 200)),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_calendar_entry(r) for r in rows]

    async def count_calendar_entries(self, patient_id: str = "erika") -> int:
        """Count calendar entries for a user."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM calendar_entries WHERE patient_id = ?", (patient_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["COUNT(*)"] if isinstance(row, dict) else row[0]
