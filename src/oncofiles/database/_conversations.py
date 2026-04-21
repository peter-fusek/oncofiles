"""Conversation archive database operations."""

from __future__ import annotations

import json
from datetime import date

from oncofiles.models import ConversationEntry, ConversationQuery

from ._converters import _row_to_conversation_entry


class ConversationMixin:
    """Conversation entry database operations."""

    async def insert_conversation_entry(
        self, entry: ConversationEntry, *, patient_id: str
    ) -> ConversationEntry:
        """Insert a conversation entry and return it with the generated ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO conversation_entries
                (entry_date, entry_type, title, content, participant,
                 session_type, session_id, tags, document_ids,
                 source, source_ref, patient_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_date.isoformat(),
                entry.entry_type,
                entry.title,
                entry.content,
                entry.participant,
                entry.session_type,
                entry.session_id,
                json.dumps(entry.tags) if entry.tags else None,
                json.dumps(entry.document_ids) if entry.document_ids else None,
                entry.source,
                entry.source_ref,
                patient_id,
            ),
        )
        await self.db.commit()
        entry.id = cursor.lastrowid
        return entry

    async def get_conversation_entry(self, entry_id: int) -> ConversationEntry | None:
        """Get a conversation entry by ID.

        Callers that need patient-scoped access should pair this with
        ``check_conversation_entry_ownership`` (Option A pattern #429).
        """
        async with self.db.execute(
            "SELECT * FROM conversation_entries WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_conversation_entry(row) if row else None

    async def check_conversation_entry_ownership(self, entry_id: int, patient_id: str) -> bool:
        """Check if a conversation entry belongs to the given patient.

        Returns False if the entry doesn't exist. Mirrors the pattern used by
        ``check_document_ownership`` / ``check_treatment_event_ownership`` to
        prevent cross-patient access via enumerable integer IDs (#429).
        """
        async with self.db.execute(
            "SELECT patient_id FROM conversation_entries WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["patient_id"] == patient_id)

    async def search_conversation_entries(
        self, query: ConversationQuery, *, max_content_length: int = 510, patient_id: str
    ) -> list[ConversationEntry]:
        """Search conversation entries using FTS5 and/or filters.

        Args:
            query: Search parameters.
            max_content_length: Truncate content to this length in the query
                to reduce memory usage. Use 0 for full content.
        """
        conditions: list[str] = ["patient_id = ?"]
        params: list[str | int] = [patient_id]

        if query.text:
            # Sanitize FTS5 input: quote as a phrase to prevent FTS5 syntax injection
            import re

            safe_text = re.sub(r"[^\w\s-]", "", query.text)
            if safe_text.strip():
                conditions.append(
                    "id IN (SELECT rowid FROM conversation_entries_fts "
                    "WHERE conversation_entries_fts MATCH ?)"
                )
                params.append(f'"{safe_text}"')

        if query.entry_type:
            conditions.append("entry_type = ?")
            params.append(query.entry_type)

        if query.participant:
            conditions.append("participant = ?")
            params.append(query.participant)

        if query.date_from:
            conditions.append("entry_date >= ?")
            params.append(query.date_from.isoformat())

        if query.date_to:
            conditions.append("entry_date <= ?")
            params.append(query.date_to.isoformat())

        if query.tags:
            for tag in query.tags:
                conditions.append("tags LIKE ?")
                params.append(f'%"{tag}"%')

        where = " AND ".join(conditions) if conditions else "1=1"
        # Truncate content at SQL level to avoid loading full text into memory
        content_col = (
            f"SUBSTR(content, 1, {max_content_length}) AS content"
            if max_content_length > 0
            else "content"
        )
        sql = (
            f"SELECT id, entry_date, entry_type, title, {content_col}, "
            f"participant, session_id, tags, document_ids, source, source_ref, "
            f"created_at, updated_at "
            f"FROM conversation_entries WHERE {where} "
            f"ORDER BY entry_date DESC, created_at DESC LIMIT ? OFFSET ?"
        )
        params.extend([query.limit, query.offset])

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_conversation_entry(r) for r in rows]

    async def get_conversation_timeline(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 100,
        *,
        patient_id: str,
    ) -> list[ConversationEntry]:
        """Get conversation entries in chronological (ASC) order."""
        conditions: list[str] = ["patient_id = ?"]
        params: list[str | int] = [patient_id]

        if date_from:
            conditions.append("entry_date >= ?")
            params.append(date_from.isoformat())
        if date_to:
            conditions.append("entry_date <= ?")
            params.append(date_to.isoformat())

        where = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM conversation_entries WHERE {where} "
            f"ORDER BY entry_date ASC, created_at ASC LIMIT ?"
        )
        params.append(limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_conversation_entry(r) for r in rows]

    async def delete_conversation_entry(self, entry_id: int) -> bool:
        """Delete a conversation entry by ID. Returns True if deleted."""
        cursor = await self.db.execute("DELETE FROM conversation_entries WHERE id = ?", (entry_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_entry_by_source_ref(
        self, source_ref: str, *, patient_id: str
    ) -> ConversationEntry | None:
        """Get a conversation entry by source_ref (for idempotent imports)."""
        async with self.db.execute(
            "SELECT * FROM conversation_entries WHERE source_ref = ? AND patient_id = ?",
            (source_ref, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_conversation_entry(row) if row else None
