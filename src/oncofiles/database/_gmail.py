"""Gmail email entry database mixin."""

from __future__ import annotations

from oncofiles.database._converters import _row_to_email_entry
from oncofiles.models import EmailEntry, EmailQuery


class GmailMixin:
    """CRUD operations for email_entries table."""

    async def upsert_email_entry(self, entry: EmailEntry) -> EmailEntry:
        """Insert or update an email entry (idempotent by gmail_message_id)."""
        async with self.db.execute(
            """
            INSERT INTO email_entries
                (user_id, gmail_message_id, thread_id, subject, sender, recipients,
                 date, body_snippet, body_text, labels, has_attachments,
                 ai_summary, ai_relevance_score, structured_metadata,
                 linked_document_ids, is_medical, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(user_id, gmail_message_id) DO UPDATE SET
                thread_id = excluded.thread_id,
                subject = excluded.subject,
                sender = excluded.sender,
                recipients = excluded.recipients,
                date = excluded.date,
                body_snippet = excluded.body_snippet,
                body_text = excluded.body_text,
                labels = excluded.labels,
                has_attachments = excluded.has_attachments,
                ai_summary = COALESCE(excluded.ai_summary, email_entries.ai_summary),
                ai_relevance_score = COALESCE(
                    excluded.ai_relevance_score, email_entries.ai_relevance_score),
                structured_metadata = COALESCE(
                    excluded.structured_metadata, email_entries.structured_metadata),
                linked_document_ids = excluded.linked_document_ids,
                is_medical = excluded.is_medical,
                updated_at = excluded.updated_at
            """,
            (
                entry.user_id,
                entry.gmail_message_id,
                entry.thread_id,
                entry.subject,
                entry.sender,
                entry.recipients,
                entry.date.isoformat(),
                entry.body_snippet,
                entry.body_text,
                entry.labels,
                1 if entry.has_attachments else 0,
                entry.ai_summary,
                entry.ai_relevance_score,
                entry.structured_metadata,
                entry.linked_document_ids,
                1 if entry.is_medical else 0,
            ),
        ) as cursor:
            last_id = cursor.lastrowid
        await self.db.commit()
        # Re-fetch to get defaults
        if last_id:
            result = await self.get_email_entry(last_id)
            if result:
                return result
        # Fallback: fetch by gmail_message_id
        return await self.get_email_entry_by_gmail_id(entry.gmail_message_id, entry.user_id)

    async def get_email_entry(self, entry_id: int) -> EmailEntry | None:
        """Get an email entry by internal ID."""
        async with self.db.execute(
            "SELECT * FROM email_entries WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_email_entry(row) if row else None

    async def get_email_entry_by_gmail_id(
        self, gmail_message_id: str, user_id: str = "default"
    ) -> EmailEntry | None:
        """Get an email entry by Gmail message ID."""
        async with self.db.execute(
            "SELECT * FROM email_entries WHERE user_id = ? AND gmail_message_id = ?",
            (user_id, gmail_message_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_email_entry(row) if row else None

    async def search_email_entries(
        self, query: EmailQuery, user_id: str = "default"
    ) -> list[EmailEntry]:
        """Search email entries with text, date, and medical filters."""
        conditions = ["user_id = ?"]
        params: list = [user_id]
        if query.text:
            like = f"%{query.text}%"
            conditions.append("(subject LIKE ? OR body_snippet LIKE ? OR sender LIKE ?)")
            params.extend([like, like, like])
        if query.date_from:
            conditions.append("date >= ?")
            params.append(query.date_from.isoformat())
        if query.date_to:
            conditions.append("date <= ?")
            params.append(query.date_to.isoformat() + "T23:59:59Z")
        if query.is_medical is not None:
            conditions.append("is_medical = ?")
            params.append(1 if query.is_medical else 0)
        if query.sender:
            conditions.append("sender LIKE ?")
            params.append(f"%{query.sender}%")
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM email_entries WHERE {where} ORDER BY date DESC LIMIT ?"
        params.append(min(query.limit, 200))
        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_email_entry(r) for r in rows]

    async def list_email_entries(
        self, user_id: str = "default", limit: int = 50
    ) -> list[EmailEntry]:
        """List recent email entries."""
        async with self.db.execute(
            "SELECT * FROM email_entries WHERE user_id = ? ORDER BY date DESC LIMIT ?",
            (user_id, min(limit, 200)),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_email_entry(r) for r in rows]

    async def count_email_entries(self, user_id: str = "default") -> int:
        """Count email entries for a user."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM email_entries WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["COUNT(*)"] if isinstance(row, dict) else row[0]
