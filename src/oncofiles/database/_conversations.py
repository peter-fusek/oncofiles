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

    async def get_conversation_entry(
        self, entry_id: int, *, patient_id: str, include_deleted: bool = False
    ) -> ConversationEntry | None:
        """Get a conversation entry by ID, scoped to ``patient_id`` (#499).

        ``patient_id`` is required: the SQL filter blocks cross-patient
        disclosure if a caller forgets ``check_conversation_entry_ownership``.
        Soft-deleted entries (#723) are hidden unless ``include_deleted``.
        """
        sql = "SELECT * FROM conversation_entries WHERE id = ? AND patient_id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        async with self.db.execute(sql, (entry_id, patient_id)) as cursor:
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
        conditions: list[str] = ["patient_id = ?", "deleted_at IS NULL"]
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
        """Get live conversation entries in chronological (ASC) order."""
        conditions: list[str] = ["patient_id = ?", "deleted_at IS NULL"]
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

    async def delete_conversation_entry(
        self, entry_id: int, *, patient_id: str, deleted_by: str | None = None
    ) -> bool:
        """Soft-delete a conversation entry, scoped to ``patient_id`` (#515, #723).

        Sets ``deleted_at`` / ``deleted_by``; the row stays restorable via
        ``restore_conversation_entry`` (deletion policy: trash only). Returns
        False when the entry does not exist for this patient or is already
        deleted. ``patient_id`` is required: the SQL ``AND patient_id = ?``
        clause is the data-layer guarantee that a caller without an ownership
        check cannot delete another patient's row by enumerating IDs.
        """
        cursor = await self.db.execute(
            "UPDATE conversation_entries "
            "   SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), deleted_by = ? "
            " WHERE id = ? AND patient_id = ? AND deleted_at IS NULL",
            (deleted_by, entry_id, patient_id),
        )
        await self.db.commit()
        if cursor.rowcount <= 0:
            return False
        await self.log_soft_delete_activity(
            action="soft_delete",
            table="conversation_entries",
            row_id=entry_id,
            patient_id=patient_id,
            actor=deleted_by,
        )
        return True

    async def restore_conversation_entry(
        self, entry_id: int, *, patient_id: str, restored_by: str | None = None
    ) -> ConversationEntry | None:
        """Undo a soft delete, scoped to ``patient_id`` (#723).

        Returns the restored entry, or None when it does not exist for this
        patient or is not deleted.
        """
        cursor = await self.db.execute(
            "UPDATE conversation_entries SET deleted_at = NULL, deleted_by = NULL "
            " WHERE id = ? AND patient_id = ? AND deleted_at IS NOT NULL",
            (entry_id, patient_id),
        )
        await self.db.commit()
        if cursor.rowcount <= 0:
            return None
        await self.log_soft_delete_activity(
            action="restore",
            table="conversation_entries",
            row_id=entry_id,
            patient_id=patient_id,
            actor=restored_by,
        )
        return await self.get_conversation_entry(entry_id, patient_id=patient_id)

    async def get_entry_by_source_ref(
        self, source_ref: str, *, patient_id: str
    ) -> ConversationEntry | None:
        """Get a conversation entry by source_ref (for idempotent imports).

        Deliberately includes soft-deleted entries (#723): a re-import must not
        bring back an entry the user deleted.
        """
        async with self.db.execute(
            "SELECT * FROM conversation_entries WHERE source_ref = ? AND patient_id = ?",
            (source_ref, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_conversation_entry(row) if row else None
