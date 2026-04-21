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

    async def insert_treatment_event(
        self, event: TreatmentEvent, *, patient_id: str
    ) -> TreatmentEvent:
        """Insert a treatment event and return it with the generated ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO treatment_events
                (event_date, event_type, title, notes, metadata, patient_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_date.isoformat(),
                event.event_type,
                event.title,
                event.notes,
                event.metadata,
                patient_id,
            ),
        )
        await self.db.commit()
        event.id = cursor.lastrowid
        return event

    async def get_treatment_event(self, event_id: int) -> TreatmentEvent | None:
        """Get a treatment event by ID.

        Callers that need patient-scoped access should pair this with
        ``check_treatment_event_ownership`` (see Option A pattern #429).
        """
        async with self.db.execute(
            "SELECT * FROM treatment_events WHERE id = ?", (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_treatment_event(row) if row else None

    async def check_treatment_event_ownership(self, event_id: int, patient_id: str) -> bool:
        """Check if a treatment event belongs to the given patient.

        Returns False if the event doesn't exist. Mirrors the pattern used by
        ``check_document_ownership`` to prevent cross-patient access via
        enumerable integer IDs (#429).
        """
        async with self.db.execute(
            "SELECT patient_id FROM treatment_events WHERE id = ?", (event_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["patient_id"] == patient_id)

    async def list_treatment_events(
        self, query: TreatmentEventQuery, *, patient_id: str
    ) -> list[TreatmentEvent]:
        """List treatment events with optional filters."""
        conditions: list[str] = ["patient_id = ?"]
        params: list[str | int] = [patient_id]

        if query.event_type:
            conditions.append("event_type = ?")
            params.append(query.event_type)
        if query.date_from:
            conditions.append("event_date >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("event_date <= ?")
            params.append(query.date_to.isoformat())

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM treatment_events WHERE {where} ORDER BY event_date DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            events = [_row_to_treatment_event(r) for r in rows]

        # Enrich lab_result events with lab values — BATCHED.
        # Previously this was an N+1 query (one sub-SELECT per event). On
        # Turso's single-connection serialisation that pattern observed up
        # to 49.8s latency in production (#406 Finding 3). Now a single
        # SELECT joins lab_values → documents for patient scoping AND
        # covers every event_date at once.
        dates_to_enrich = [
            event.event_date.isoformat()
            for event in events
            if event.event_type == "lab_result" and (not event.metadata or event.metadata == "{}")
        ]
        if dates_to_enrich:
            try:
                placeholders = ",".join("?" for _ in dates_to_enrich)
                lv_sql = (
                    "SELECT lv.lab_date, lv.parameter, lv.value, lv.unit, lv.flag "
                    "FROM lab_values lv "
                    "JOIN documents d ON d.id = lv.document_id "
                    f"WHERE d.patient_id = ? AND lv.lab_date IN ({placeholders}) "
                    "ORDER BY lv.lab_date, lv.parameter"
                )
                lv_params: list[str] = [patient_id, *dates_to_enrich]
                async with self.db.execute(lv_sql, lv_params) as lv_cursor:
                    lv_rows = await lv_cursor.fetchall()

                import json

                by_date: dict[str, dict[str, dict]] = {}
                for r in lv_rows:
                    d = r["lab_date"]
                    by_date.setdefault(d, {})[r["parameter"]] = {
                        "value": r["value"],
                        "unit": r["unit"] or "",
                        "flag": r["flag"] or "",
                    }
                for event in events:
                    if event.event_type != "lab_result":
                        continue
                    if event.metadata and event.metadata != "{}":
                        continue
                    lab_data = by_date.get(event.event_date.isoformat())
                    if lab_data:
                        event.metadata = json.dumps(lab_data)
            except Exception:
                pass  # Don't break listing if enrichment fails

        return events

    async def delete_treatment_event(self, event_id: int) -> bool:
        """Delete a treatment event by ID. Returns True if deleted."""
        cursor = await self.db.execute("DELETE FROM treatment_events WHERE id = ?", (event_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def update_treatment_event(
        self,
        event_id: int,
        title: str | None = None,
        notes: str | None = None,
        metadata: str | None = None,
    ) -> TreatmentEvent | None:
        """Update a treatment event's title, notes, or metadata."""
        updates: list[str] = []
        params: list = []
        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if notes is not None:
            updates.append("notes = ?")
            params.append(notes)
        if metadata is not None:
            updates.append("metadata = ?")
            params.append(metadata)
        if not updates:
            return await self.get_treatment_event(event_id)
        params.append(event_id)
        await self.db.execute(
            f"UPDATE treatment_events SET {', '.join(updates)} WHERE id = ?", params
        )
        await self.db.commit()
        return await self.get_treatment_event(event_id)

    async def get_treatment_events_timeline(
        self, limit: int = 200, *, patient_id: str
    ) -> list[TreatmentEvent]:
        """Get treatment events in chronological (ASC) order."""
        async with self.db.execute(
            "SELECT * FROM treatment_events WHERE patient_id = ? "
            "ORDER BY event_date ASC, created_at ASC LIMIT ?",
            (patient_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_treatment_event(r) for r in rows]

    # ── Research entries (#33) ───────────────────────────────────────────

    async def insert_research_entry(
        self, entry: ResearchEntry, *, patient_id: str
    ) -> ResearchEntry:
        """Insert a research entry. Ignores duplicates (source+external_id)."""
        cursor = await self.db.execute(
            """
            INSERT OR IGNORE INTO research_entries
                (source, external_id, title, summary, tags, raw_data, patient_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.source,
                entry.external_id,
                entry.title,
                entry.summary,
                entry.tags,
                entry.raw_data,
                patient_id,
            ),
        )
        await self.db.commit()

        if cursor.rowcount > 0:
            entry.id = cursor.lastrowid
            return entry

        # Duplicate (INSERT OR IGNORE skipped) — return existing row
        async with self.db.execute(
            "SELECT * FROM research_entries WHERE source = ? AND external_id = ? "
            "AND patient_id = ?",
            (entry.source, entry.external_id, patient_id),
        ) as cur:
            row = await cur.fetchone()
            return _row_to_research_entry(row)

    async def search_research_entries(
        self, query: ResearchQuery, *, patient_id: str
    ) -> list[ResearchEntry]:
        """Search research entries using LIKE on title/summary/tags."""
        conditions: list[str] = ["patient_id = ?"]
        params: list[str | int] = [patient_id]

        if query.text:
            like_param = f"%{query.text}%"
            conditions.append("(title LIKE ? OR summary LIKE ? OR tags LIKE ?)")
            params.extend([like_param, like_param, like_param])
        if query.source:
            conditions.append("source = ?")
            params.append(query.source)

        where = " AND ".join(conditions)
        sql = f"SELECT * FROM research_entries WHERE {where} ORDER BY created_at DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_research_entry(r) for r in rows]

    async def list_research_entries(
        self, source: str | None = None, limit: int = 50, *, patient_id: str
    ) -> list[ResearchEntry]:
        """List research entries, optionally filtered by source."""
        if source:
            async with self.db.execute(
                "SELECT * FROM research_entries WHERE source = ? AND patient_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (source, patient_id, limit),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM research_entries WHERE patient_id = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (patient_id, limit),
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
        join = ""

        if query.patient_id:
            join = "JOIN documents d ON d.id = lv.document_id"
            conditions.append("d.patient_id = ?")
            params.append(query.patient_id)
        if query.parameter:
            conditions.append("lv.parameter = ?")
            params.append(query.parameter)
        if query.date_from:
            conditions.append("lv.lab_date >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("lv.lab_date <= ?")
            params.append(query.date_to.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT lv.* FROM lab_values lv {join} WHERE {where}"
            " ORDER BY lv.lab_date ASC, lv.parameter ASC LIMIT ?"
        )
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_lab_snapshot(self, document_id: int, *, patient_id: str = "") -> list[LabValue]:
        """Get all lab values from a specific document."""
        if patient_id:
            async with self.db.execute(
                """SELECT lv.* FROM lab_values lv
                   JOIN documents d ON d.id = lv.document_id
                   WHERE lv.document_id = ? AND d.patient_id = ?
                   ORDER BY lv.parameter""",
                (document_id, patient_id),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute(
                "SELECT * FROM lab_values WHERE document_id = ? ORDER BY parameter",
                (document_id,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_lab_value(r) for r in rows]

    async def get_latest_lab_value(
        self, parameter: str, *, patient_id: str = ""
    ) -> LabValue | None:
        """Get the most recent value for a given parameter."""
        if patient_id:
            sql = """SELECT lv.* FROM lab_values lv
                     JOIN documents d ON d.id = lv.document_id
                     WHERE lv.parameter = ? AND d.patient_id = ?
                     ORDER BY lv.lab_date DESC LIMIT 1"""
            params: tuple = (parameter, patient_id)
        else:
            sql = "SELECT * FROM lab_values WHERE parameter = ? ORDER BY lab_date DESC LIMIT 1"
            params = (parameter,)
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return _row_to_lab_value(row) if row else None

    async def get_all_latest_lab_values(self, *, patient_id: str = "") -> list[LabValue]:
        """Get the most recent value for every tracked parameter."""
        if patient_id:
            sql = """
                SELECT lv.* FROM lab_values lv
                JOIN documents d ON d.id = lv.document_id
                INNER JOIN (
                    SELECT lv2.parameter, MAX(lv2.lab_date) AS max_date
                    FROM lab_values lv2
                    JOIN documents d2 ON d2.id = lv2.document_id
                    WHERE d2.patient_id = ?
                    GROUP BY lv2.parameter
                ) latest ON lv.parameter = latest.parameter
                    AND lv.lab_date = latest.max_date
                WHERE d.patient_id = ?
                ORDER BY lv.parameter
            """
            params_t: tuple = (patient_id, patient_id)
        else:
            sql = """
                SELECT lv.* FROM lab_values lv
                INNER JOIN (
                    SELECT parameter, MAX(lab_date) AS max_date
                    FROM lab_values GROUP BY parameter
                ) latest ON lv.parameter = latest.parameter
                    AND lv.lab_date = latest.max_date
                ORDER BY lv.parameter
            """
            params_t = ()
        async with self.db.execute(sql, params_t) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_previous_lab_values(self, *, patient_id: str = "") -> dict[str, LabValue]:
        """Get the second-most-recent value for every parameter (for trend calculation)."""
        if patient_id:
            sql = """
                SELECT lv.* FROM lab_values lv
                JOIN documents d ON d.id = lv.document_id
                INNER JOIN (
                    SELECT lv2.parameter, MAX(lv2.lab_date) AS max_date
                    FROM lab_values lv2
                    JOIN documents d2 ON d2.id = lv2.document_id
                    WHERE d2.patient_id = ?
                    GROUP BY lv2.parameter
                ) latest ON lv.parameter = latest.parameter
                WHERE d.patient_id = ?
                AND lv.lab_date < latest.max_date
                AND lv.lab_date = (
                    SELECT MAX(lv3.lab_date) FROM lab_values lv3
                    JOIN documents d3 ON d3.id = lv3.document_id
                    WHERE lv3.parameter = lv.parameter AND lv3.lab_date < latest.max_date
                    AND d3.patient_id = ?
                )
                ORDER BY lv.parameter
            """
            params_t2: tuple = (patient_id, patient_id, patient_id)
        else:
            sql = """
                SELECT lv.* FROM lab_values lv
                INNER JOIN (
                    SELECT parameter, MAX(lab_date) AS max_date
                    FROM lab_values GROUP BY parameter
                ) latest ON lv.parameter = latest.parameter
                WHERE lv.lab_date < latest.max_date
                AND lv.lab_date = (
                    SELECT MAX(lv2.lab_date) FROM lab_values lv2
                    WHERE lv2.parameter = lv.parameter AND lv2.lab_date < latest.max_date
                )
                ORDER BY lv.parameter
            """
            params_t2 = ()
        async with self.db.execute(sql, params_t2) as cursor:
            rows = await cursor.fetchall()
            return {v.parameter: v for r in rows if (v := _row_to_lab_value(r))}

    async def get_lab_values_by_date(
        self, lab_date: str, *, patient_id: str = ""
    ) -> list[LabValue]:
        """Get all lab values for a specific date."""
        if patient_id:
            sql = """SELECT lv.* FROM lab_values lv
                     JOIN documents d ON d.id = lv.document_id
                     WHERE lv.lab_date = ? AND d.patient_id = ?
                     ORDER BY lv.parameter"""
            params_d: tuple = (lab_date, patient_id)
        else:
            sql = "SELECT * FROM lab_values WHERE lab_date = ? ORDER BY parameter"
            params_d = (lab_date,)
        async with self.db.execute(sql, params_d) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_lab_value(r) for r in rows]

    async def get_distinct_lab_dates(self, *, patient_id: str = "") -> list[str]:
        """Get all distinct lab dates, most recent first."""
        if patient_id:
            sql = """SELECT DISTINCT lv.lab_date FROM lab_values lv
                     JOIN documents d ON d.id = lv.document_id
                     WHERE d.patient_id = ?
                     ORDER BY lv.lab_date DESC"""
            params_dd: tuple = (patient_id,)
        else:
            sql = "SELECT DISTINCT lab_date FROM lab_values ORDER BY lab_date DESC"
            params_dd = ()
        async with self.db.execute(sql, params_dd) as cursor:
            rows = await cursor.fetchall()
            return [row["lab_date"] if isinstance(row, dict) else row[0] for row in rows]
