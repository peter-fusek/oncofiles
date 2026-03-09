"""SQLite database for document metadata with FTS5 search."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from oncofiles.models import (
    ActivityLogEntry,
    ActivityLogQuery,
    AgentState,
    ConversationEntry,
    ConversationQuery,
    Document,
    DocumentCategory,
    LabTrendQuery,
    LabValue,
    OAuthToken,
    ResearchEntry,
    ResearchQuery,
    SearchQuery,
    TreatmentEvent,
    TreatmentEventQuery,
)

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"

# "key" is a reserved word — always quote it in SQL and use aliased SELECT
_AGENT_STATE_SELECT = (
    'SELECT id, agent_id, "key" AS state_key, value, created_at, updated_at FROM agent_state'
)


# ── Turso async wrappers ──────────────────────────────────────────────────────


class _TursoCursor:
    """Async-compatible wrapper around sync libsql cursor.

    Converts tuple rows to dicts using cursor.description so that
    row["column_name"] access works identically to aiosqlite.Row.
    """

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def _to_dict(self, row: tuple | None) -> dict | None:
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row, strict=True))

    async def fetchone(self) -> dict | None:
        return self._to_dict(self._cursor.fetchone())

    async def fetchall(self) -> list[dict]:
        return [self._to_dict(r) for r in self._cursor.fetchall()]

    async def __aenter__(self) -> _TursoCursor:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass


class _TursoExecProxy:
    """Awaitable + async-context-manager proxy matching aiosqlite.execute()."""

    def __init__(self, conn: Any, sql: str, params: tuple) -> None:
        self._conn = conn
        self._sql = sql
        self._params = params

    async def _run(self) -> _TursoCursor:
        cursor = await asyncio.to_thread(self._conn.execute, self._sql, self._params)
        return _TursoCursor(cursor)

    def __await__(self):  # noqa: ANN204
        return self._run().__await__()

    async def __aenter__(self) -> _TursoCursor:
        return await self._run()

    async def __aexit__(self, *args: Any) -> None:
        pass


class _TursoConnection:
    """Async wrapper around sync libsql connection matching aiosqlite interface."""

    def __init__(self, url: str, auth_token: str) -> None:
        self._url = url
        self._auth_token = auth_token
        self._conn: Any = None

    async def connect(self) -> None:
        import libsql_experimental as libsql

        self._conn = libsql.connect(self._url, auth_token=self._auth_token)

    def execute(self, sql: str, params: tuple | list = ()) -> _TursoExecProxy:
        return _TursoExecProxy(self._conn, sql, tuple(params))

    async def executescript(self, sql: str) -> None:
        await asyncio.to_thread(self._conn.executescript, sql)

    async def commit(self) -> None:
        await asyncio.to_thread(self._conn.commit)

    async def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None


# ── Database ──────────────────────────────────────────────────────────────────


class Database:
    """Async database for document metadata. Uses aiosqlite locally, Turso in cloud."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        turso_url: str = "",
        turso_token: str = "",
    ) -> None:
        self.path = str(path)
        self._turso_url = turso_url
        self._turso_token = turso_token
        self._use_turso = bool(turso_url)
        self._db: aiosqlite.Connection | _TursoConnection | None = None

    async def connect(self) -> None:
        if self._use_turso:
            conn = _TursoConnection(self._turso_url, self._turso_token)
            await conn.connect()
            self._db = conn
        else:
            self._db = await aiosqlite.connect(self.path)
            self._db.row_factory = aiosqlite.Row
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection | _TursoConnection:
        if self._db is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._db

    async def migrate(self) -> None:
        """Run numbered SQL migration files from migrations/ directory.

        Pattern: migrations/001_description.sql, 002_description.sql, etc.
        Migrations are idempotent (CREATE TABLE IF NOT EXISTS, etc.) and run
        in sorted filename order on every startup. Add new migrations by
        creating the next numbered .sql file.
        """
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = sql_file.read_text()
            if sql.strip():
                await self.db.executescript(sql)
        # Add columns if missing (ALTER TABLE can't use IF NOT EXISTS)
        for col, typedef in [
            ("sync_state", "TEXT NOT NULL DEFAULT 'synced'"),
            ("last_synced_at", "TEXT"),
            ("deleted_at", "TEXT"),
        ]:
            try:
                await self.db.execute(f"ALTER TABLE documents ADD COLUMN {col} {typedef}")
                await self.db.commit()
            except Exception:
                pass  # Column already exists
        # Create indexes (safe with IF NOT EXISTS)
        for idx_sql in [
            "CREATE INDEX IF NOT EXISTS idx_documents_deleted_at ON documents(deleted_at)",
            "CREATE INDEX IF NOT EXISTS idx_documents_ai_processed_at"
            " ON documents(ai_processed_at)",
        ]:
            await self.db.execute(idx_sql)
            await self.db.commit()

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def insert_document(self, doc: Document) -> Document:
        """Insert a document and return it with the generated ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO documents
                (file_id, filename, original_filename, document_date,
                 institution, category, description, mime_type, size_bytes,
                 gdrive_id, gdrive_modified_time)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc.file_id,
                doc.filename,
                doc.original_filename,
                doc.document_date.isoformat() if doc.document_date else None,
                doc.institution,
                doc.category.value,
                doc.description,
                doc.mime_type,
                doc.size_bytes,
                doc.gdrive_id,
                doc.gdrive_modified_time.isoformat() if doc.gdrive_modified_time else None,
            ),
        )
        await self.db.commit()
        doc.id = cursor.lastrowid
        return doc

    async def get_document(self, doc_id: int) -> Document | None:
        """Get a document by its local ID."""
        async with self.db.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def get_document_by_file_id(self, file_id: str) -> Document | None:
        """Get a document by its Anthropic Files API file_id."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE file_id = ?", (file_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def get_document_by_original_filename(self, original_filename: str) -> Document | None:
        """Get a document by its original filename (for idempotent imports)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE original_filename = ?", (original_filename,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def list_documents(
        self,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        """List documents ordered by date descending."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE deleted_at IS NULL "
            "ORDER BY document_date DESC, created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def search_documents(self, query: SearchQuery) -> list[Document]:
        """Search documents with relevance scoring and multi-term matching.

        When text is provided, terms are split on whitespace and ALL must
        match somewhere across the searchable fields (AND semantics).
        Results are ranked by field weight: filename/description (3),
        ai_summary (2), ai_tags/structured_metadata (1).
        """
        conditions: list[str] = []
        params: list[str | int] = []
        score_parts: list[str] = []

        # Searchable fields with weights (higher = more relevant)
        search_fields = [
            ("filename", 3),
            ("original_filename", 3),
            ("institution", 3),
            ("description", 3),
            ("ai_summary", 2),
            ("ai_tags", 1),
            ("structured_metadata", 1),
        ]

        if query.text:
            # Split into terms — all must match somewhere (AND semantics).
            # Use LIKE for substring matching — works reliably on both SQLite
            # and Turso/libSQL (FTS5 content-sync triggers are unreliable on
            # Turso and FTS5 tokenization misses CamelCase substrings).
            terms = query.text.split()
            for term in terms:
                like_param = f"%{term}%"
                field_checks = " OR ".join(f"{f} LIKE ?" for f, _ in search_fields)
                conditions.append(f"({field_checks})")
                params.extend([like_param] * len(search_fields))

            # Relevance score: sum of weights for each field that matches
            # any term. Higher score = more relevant.
            for field, weight in search_fields:
                term_cases = " OR ".join(
                    f"{field} LIKE ?" for _ in terms
                )
                score_parts.append(f"(CASE WHEN ({term_cases}) THEN {weight} ELSE 0 END)")
                params_for_score = [f"%{t}%" for t in terms]
                params.extend(params_for_score)

        if query.institution:
            conditions.append("institution = ?")
            params.append(query.institution)

        if query.category:
            conditions.append("category = ?")
            params.append(query.category.value)

        if query.date_from:
            conditions.append("document_date >= ?")
            params.append(query.date_from.isoformat())

        if query.date_to:
            conditions.append("document_date <= ?")
            params.append(query.date_to.isoformat())

        conditions.append("deleted_at IS NULL")
        where = " AND ".join(conditions)

        if score_parts:
            score_expr = " + ".join(score_parts)
            sql = (
                f"SELECT *, ({score_expr}) AS _relevance "
                f"FROM documents WHERE {where} "
                f"ORDER BY _relevance DESC, document_date DESC "
                f"LIMIT ? OFFSET ?"
            )
        else:
            sql = (
                f"SELECT * FROM documents WHERE {where} "
                f"ORDER BY document_date DESC LIMIT ? OFFSET ?"
            )

        params.append(query.limit)
        params.append(query.offset)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def delete_document(self, doc_id: int) -> bool:
        """Soft-delete a document by local ID. Returns True if updated."""
        cursor = await self.db.execute(
            "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND deleted_at IS NULL",
            (doc_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_document_by_file_id(self, file_id: str) -> bool:
        """Soft-delete a document by Anthropic file_id. Returns True if updated."""
        cursor = await self.db.execute(
            "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE file_id = ? AND deleted_at IS NULL",
            (file_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def restore_document(self, doc_id: int) -> bool:
        """Restore a soft-deleted document. Returns True if restored."""
        cursor = await self.db.execute(
            "UPDATE documents SET deleted_at = NULL WHERE id = ? AND deleted_at IS NOT NULL",
            (doc_id,),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def list_trash(self, limit: int = 50) -> list[Document]:
        """List soft-deleted documents ordered by deletion time descending."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def count_documents(self) -> int:
        """Count active (non-deleted) documents."""
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM documents WHERE deleted_at IS NULL"
        ) as cursor:
            row = await cursor.fetchone()
            return row["cnt"] if row else 0

    async def get_treatment_timeline(self, limit: int = 200) -> list[Document]:
        """Get treatment documents in chronological (ASC) order."""
        treatment_categories = (
            "surgery",
            "surgical_report",
            "discharge",
            "discharge_summary",
            "report",
            "pathology",
            "genetics",
            "labs",
            "imaging",
            "imaging_ct",
            "imaging_us",
            "chemo_sheet",
            "prescription",
            "referral",
        )
        placeholders = ", ".join("?" for _ in treatment_categories)
        async with self.db.execute(
            f"""
            SELECT * FROM documents
            WHERE category IN ({placeholders}) AND deleted_at IS NULL
            ORDER BY document_date ASC, created_at ASC
            LIMIT ?
            """,
            (*treatment_categories, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def get_document_by_gdrive_id(self, gdrive_id: str) -> Document | None:
        """Get a document by its Google Drive file ID."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE gdrive_id = ?", (gdrive_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def update_document_file_id(self, doc_id: int, file_id: str, size_bytes: int) -> None:
        """Update the Anthropic file_id and size for a re-uploaded document."""
        await self.db.execute(
            "UPDATE documents SET file_id = ?, size_bytes = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (file_id, size_bytes, doc_id),
        )
        await self.db.commit()

    async def update_document_ai_metadata(self, doc_id: int, ai_summary: str, ai_tags: str) -> None:
        """Update AI-generated summary and tags for a document."""
        await self.db.execute(
            "UPDATE documents SET ai_summary = ?, ai_tags = ?, "
            "ai_processed_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (ai_summary, ai_tags, doc_id),
        )
        await self.db.commit()

    async def get_documents_without_ai(self, limit: int = 100) -> list[Document]:
        """Get documents that haven't been AI-processed yet."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE ai_processed_at IS NULL AND deleted_at IS NULL "
            "ORDER BY document_date DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def update_gdrive_id(self, doc_id: int, gdrive_id: str, modified_time: str) -> None:
        """Set the Google Drive file ID and modified time for a document."""
        await self.db.execute(
            "UPDATE documents SET gdrive_id = ?, gdrive_modified_time = ? WHERE id = ?",
            (gdrive_id, modified_time, doc_id),
        )
        await self.db.commit()

    # ── OCR cache ─────────────────────────────────────────────────────────

    async def has_ocr_text(self, document_id: int) -> bool:
        """Check if OCR text is cached for a document."""
        async with self.db.execute(
            "SELECT 1 FROM document_pages WHERE document_id = ? LIMIT 1",
            (document_id,),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def get_ocr_pages(self, document_id: int) -> list[dict]:
        """Get cached OCR text for a document, ordered by page number."""
        async with self.db.execute(
            "SELECT page_number, extracted_text, model FROM document_pages "
            "WHERE document_id = ? ORDER BY page_number",
            (document_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def save_ocr_page(
        self, document_id: int, page_number: int, text: str, model: str
    ) -> None:
        """Save or update OCR text for a single page."""
        await self.db.execute(
            "INSERT OR REPLACE INTO document_pages "
            "(document_id, page_number, extracted_text, model) VALUES (?, ?, ?, ?)",
            (document_id, page_number, text, model),
        )
        await self.db.commit()

    async def delete_ocr_pages(self, document_id: int) -> bool:
        """Delete all cached OCR pages for a document. Returns True if any deleted."""
        cursor = await self.db.execute(
            "DELETE FROM document_pages WHERE document_id = ?", (document_id,)
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def update_structured_metadata(self, doc_id: int, metadata_json: str) -> None:
        """Update the structured_metadata JSON for a document."""
        await self.db.execute(
            "UPDATE documents SET structured_metadata = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (metadata_json, doc_id),
        )
        await self.db.commit()

    async def get_labs_before_date(self, before_date: str) -> list[Document]:
        """Get lab documents dated before a given date."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE category = 'labs' AND document_date < ? "
            "AND deleted_at IS NULL ORDER BY document_date DESC",
            (before_date,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def get_latest_labs(self, limit: int = 5) -> list[Document]:
        """Get the most recent lab result documents."""
        async with self.db.execute(
            """
            SELECT * FROM documents
            WHERE category = 'labs' AND deleted_at IS NULL
            ORDER BY document_date DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    # ── Conversation archive (#37) ────────────────────────────────────────

    async def insert_conversation_entry(self, entry: ConversationEntry) -> ConversationEntry:
        """Insert a conversation entry and return it with the generated ID."""
        import json

        cursor = await self.db.execute(
            """
            INSERT INTO conversation_entries
                (entry_date, entry_type, title, content, participant,
                 session_id, tags, document_ids, source, source_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_date.isoformat(),
                entry.entry_type,
                entry.title,
                entry.content,
                entry.participant,
                entry.session_id,
                json.dumps(entry.tags) if entry.tags else None,
                json.dumps(entry.document_ids) if entry.document_ids else None,
                entry.source,
                entry.source_ref,
            ),
        )
        await self.db.commit()
        entry.id = cursor.lastrowid
        return entry

    async def get_conversation_entry(self, entry_id: int) -> ConversationEntry | None:
        """Get a conversation entry by ID."""
        async with self.db.execute(
            "SELECT * FROM conversation_entries WHERE id = ?", (entry_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_conversation_entry(row) if row else None

    async def search_conversation_entries(
        self, query: ConversationQuery
    ) -> list[ConversationEntry]:
        """Search conversation entries using FTS5 and/or filters."""
        conditions: list[str] = []
        params: list[str | int] = []

        if query.text:
            conditions.append(
                "id IN (SELECT rowid FROM conversation_entries_fts "
                "WHERE conversation_entries_fts MATCH ?)"
            )
            params.append(query.text)

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
        sql = (
            f"SELECT * FROM conversation_entries WHERE {where} "
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
    ) -> list[ConversationEntry]:
        """Get conversation entries in chronological (ASC) order."""
        conditions: list[str] = []
        params: list[str | int] = []

        if date_from:
            conditions.append("entry_date >= ?")
            params.append(date_from.isoformat())
        if date_to:
            conditions.append("entry_date <= ?")
            params.append(date_to.isoformat())

        where = " AND ".join(conditions) if conditions else "1=1"
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

    async def get_entry_by_source_ref(self, source_ref: str) -> ConversationEntry | None:
        """Get a conversation entry by source_ref (for idempotent imports)."""
        async with self.db.execute(
            "SELECT * FROM conversation_entries WHERE source_ref = ?", (source_ref,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_conversation_entry(row) if row else None

    # ── Agent state (#32) ────────────────────────────────────────────────

    async def set_agent_state(self, state: AgentState) -> AgentState:
        """Upsert an agent state key-value pair. Returns the saved state."""
        await self.db.execute(
            """
            INSERT INTO agent_state (agent_id, "key", value, updated_at)
            VALUES (?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(agent_id, "key") DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (state.agent_id, state.key, state.value),
        )
        await self.db.commit()
        # Re-fetch (lastrowid unreliable on upsert)
        async with self.db.execute(
            _AGENT_STATE_SELECT + ' WHERE agent_id = ? AND "key" = ?',
            (state.agent_id, state.key),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_agent_state(row)

    async def get_agent_state(self, key: str, agent_id: str = "oncoteam") -> AgentState | None:
        """Get a single agent state value by key."""
        async with self.db.execute(
            _AGENT_STATE_SELECT + ' WHERE agent_id = ? AND "key" = ?',
            (agent_id, key),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_agent_state(row) if row else None

    async def list_agent_states(self, agent_id: str = "oncoteam") -> list[AgentState]:
        """List all state keys for an agent."""
        async with self.db.execute(
            _AGENT_STATE_SELECT + ' WHERE agent_id = ? ORDER BY "key"',
            (agent_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_agent_state(r) for r in rows]

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

    # ── Activity log (#38) ──────────────────────────────────────────────

    async def insert_activity_log(self, entry: ActivityLogEntry) -> ActivityLogEntry:
        """Append an activity log entry (immutable)."""
        cursor = await self.db.execute(
            """
            INSERT INTO activity_log
                (session_id, agent_id, tool_name, input_summary, output_summary,
                 duration_ms, status, error_message, tags)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.session_id,
                entry.agent_id,
                entry.tool_name,
                entry.input_summary,
                entry.output_summary,
                entry.duration_ms,
                entry.status,
                entry.error_message,
                entry.tags,
            ),
        )
        await self.db.commit()
        entry.id = cursor.lastrowid
        return entry

    async def search_activity_log(self, query: ActivityLogQuery) -> list[ActivityLogEntry]:
        """Search activity log with filters."""
        conditions: list[str] = []
        params: list[str | int] = []

        if query.session_id:
            conditions.append("session_id = ?")
            params.append(query.session_id)
        if query.agent_id:
            conditions.append("agent_id = ?")
            params.append(query.agent_id)
        if query.tool_name:
            conditions.append("tool_name = ?")
            params.append(query.tool_name)
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
            like_param = f"%{query.text}%"
            conditions.append("(input_summary LIKE ? OR output_summary LIKE ?)")
            params.extend([like_param, like_param])

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM activity_log WHERE {where} ORDER BY created_at DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_activity_log(r) for r in rows]

    async def get_activity_stats(
        self,
        session_id: str | None = None,
        agent_id: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> list[dict]:
        """Get aggregated activity counts grouped by tool_name and status."""
        conditions: list[str] = []
        params: list[str | int] = []

        if session_id:
            conditions.append("session_id = ?")
            params.append(session_id)
        if agent_id:
            conditions.append("agent_id = ?")
            params.append(agent_id)
        if date_from:
            conditions.append("created_at >= ?")
            params.append(date_from.isoformat())
        if date_to:
            conditions.append("created_at <= ?")
            params.append(date_to.isoformat() + "T23:59:59Z")

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT tool_name, status, COUNT(*) as count, "
            f"AVG(duration_ms) as avg_duration_ms "
            f"FROM activity_log WHERE {where} "
            f"GROUP BY tool_name, status ORDER BY count DESC"
        )

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    # ── OAuth tokens (#12) ─────────────────────────────────────────────

    async def upsert_oauth_token(self, token: OAuthToken) -> OAuthToken:
        """Insert or update OAuth tokens for a user/provider pair."""
        await self.db.execute(
            """
            INSERT INTO oauth_tokens
                (user_id, provider, access_token, refresh_token, token_expiry,
                 gdrive_folder_id, owner_email, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(user_id, provider) DO UPDATE SET
                access_token = excluded.access_token,
                refresh_token = excluded.refresh_token,
                token_expiry = excluded.token_expiry,
                gdrive_folder_id = excluded.gdrive_folder_id,
                owner_email = COALESCE(excluded.owner_email, oauth_tokens.owner_email),
                updated_at = excluded.updated_at
            """,
            (
                token.user_id,
                token.provider,
                token.access_token,
                token.refresh_token,
                token.token_expiry.isoformat() if token.token_expiry else None,
                token.gdrive_folder_id,
                token.owner_email,
            ),
        )
        await self.db.commit()
        return await self.get_oauth_token(token.user_id, token.provider)

    async def get_oauth_token(
        self, user_id: str = "default", provider: str = "google"
    ) -> OAuthToken | None:
        """Get OAuth tokens for a user/provider pair."""
        async with self.db.execute(
            "SELECT * FROM oauth_tokens WHERE user_id = ? AND provider = ?",
            (user_id, provider),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_oauth_token(row) if row else None

    async def update_oauth_folder(self, user_id: str, provider: str, folder_id: str) -> None:
        """Set the GDrive folder ID for a user's OAuth token."""
        await self.db.execute(
            "UPDATE oauth_tokens SET gdrive_folder_id = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE user_id = ? AND provider = ?",
            (folder_id, user_id, provider),
        )
        await self.db.commit()

    async def update_oauth_owner_email(self, user_id: str, provider: str, owner_email: str) -> None:
        """Store the GDrive folder owner's email for permission sharing."""
        await self.db.execute(
            "UPDATE oauth_tokens SET owner_email = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE user_id = ? AND provider = ?",
            (owner_email, user_id, provider),
        )
        await self.db.commit()

    # ── Sync state ───────────────────────────────────────────────────────

    async def update_sync_state(
        self, doc_id: int, sync_state: str, last_synced_at: str | None = None
    ) -> None:
        """Update the sync state of a document."""
        if last_synced_at:
            await self.db.execute(
                "UPDATE documents SET sync_state = ?, last_synced_at = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (sync_state, last_synced_at, doc_id),
            )
        else:
            await self.db.execute(
                "UPDATE documents SET sync_state = ?, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (sync_state, doc_id),
            )
        await self.db.commit()

    async def get_pending_sync_documents(self) -> list[Document]:
        """Get documents that need syncing (no gdrive_id or pending state)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE (gdrive_id IS NULL OR sync_state = 'pending') "
            "AND deleted_at IS NULL ORDER BY document_date DESC",
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def update_document_filename(self, doc_id: int, new_filename: str) -> None:
        """Update the display filename of a document (bilingual rename)."""
        await self.db.execute(
            "UPDATE documents SET filename = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (new_filename, doc_id),
        )
        await self.db.commit()

    async def update_document_category(self, doc_id: int, category: str) -> None:
        """Update the category of a document (e.g. when moved on GDrive)."""
        await self.db.execute(
            "UPDATE documents SET category = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (category, doc_id),
        )
        await self.db.commit()

    async def get_activity_timeline(self, hours: int = 24) -> list[ActivityLogEntry]:
        """Get recent activity log entries (last N hours)."""
        async with self.db.execute(
            """
            SELECT * FROM activity_log
            WHERE created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            ORDER BY created_at DESC LIMIT 200
            """,
            (f"-{hours} hours",),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_activity_log(r) for r in rows]


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


def _safe_get(row: aiosqlite.Row, key: str, default=None):
    """Get a column value from a row, returning default if column doesn't exist."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _row_to_oauth_token(row: aiosqlite.Row) -> OAuthToken:
    """Convert a database row to an OAuthToken model."""
    return OAuthToken(
        id=row["id"],
        user_id=row["user_id"],
        provider=row["provider"],
        access_token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_expiry=(datetime.fromisoformat(row["token_expiry"]) if row["token_expiry"] else None),
        gdrive_folder_id=row["gdrive_folder_id"],
        owner_email=_safe_get(row, "owner_email"),
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_agent_state(row: aiosqlite.Row) -> AgentState:
    """Convert a database row to an AgentState model.

    Uses aliased column 'state_key' to avoid reserved-word issues with Turso.
    """
    d = dict(row)
    return AgentState(
        id=d["id"],
        agent_id=d["agent_id"],
        key=d["state_key"],
        value=d["value"],
        created_at=datetime.fromisoformat(d["created_at"]) if d["created_at"] else None,
        updated_at=datetime.fromisoformat(d["updated_at"]) if d["updated_at"] else None,
    )


def _row_to_treatment_event(row: aiosqlite.Row) -> TreatmentEvent:
    """Convert a database row to a TreatmentEvent model."""
    return TreatmentEvent(
        id=row["id"],
        event_date=date.fromisoformat(row["event_date"]),
        event_type=row["event_type"],
        title=row["title"],
        notes=row["notes"],
        metadata=row["metadata"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_research_entry(row: aiosqlite.Row) -> ResearchEntry:
    """Convert a database row to a ResearchEntry model."""
    return ResearchEntry(
        id=row["id"],
        source=row["source"],
        external_id=row["external_id"],
        title=row["title"],
        summary=row["summary"],
        tags=row["tags"],
        raw_data=row["raw_data"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_activity_log(row: aiosqlite.Row) -> ActivityLogEntry:
    """Convert a database row to an ActivityLogEntry model."""
    return ActivityLogEntry(
        id=row["id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        tool_name=row["tool_name"],
        input_summary=row["input_summary"],
        output_summary=row["output_summary"],
        duration_ms=row["duration_ms"],
        status=row["status"],
        error_message=row["error_message"],
        tags=row["tags"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
    )


def _row_to_conversation_entry(row: aiosqlite.Row) -> ConversationEntry:
    """Convert a database row to a ConversationEntry model."""
    import json

    return ConversationEntry(
        id=row["id"],
        entry_date=date.fromisoformat(row["entry_date"]),
        entry_type=row["entry_type"],
        title=row["title"],
        content=row["content"],
        participant=row["participant"],
        session_id=row["session_id"],
        tags=json.loads(row["tags"]) if row["tags"] else None,
        document_ids=json.loads(row["document_ids"]) if row["document_ids"] else None,
        source=row["source"],
        source_ref=row["source_ref"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
    )


def _row_to_lab_value(row: aiosqlite.Row) -> LabValue:
    """Convert a database row to a LabValue model."""
    return LabValue(
        id=row["id"],
        document_id=row["document_id"],
        lab_date=date.fromisoformat(row["lab_date"]),
        parameter=row["parameter"],
        value=row["value"],
        unit=row["unit"],
        reference_low=row["reference_low"],
        reference_high=row["reference_high"],
        flag=row["flag"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
    )


def _row_to_document(row: aiosqlite.Row) -> Document:
    """Convert a database row to a Document model."""
    # Handle sync_state/last_synced_at which may not exist in older schemas
    row_dict = dict(row)
    return Document(
        id=row["id"],
        file_id=row["file_id"],
        filename=row["filename"],
        original_filename=row["original_filename"],
        document_date=date.fromisoformat(row["document_date"]) if row["document_date"] else None,
        institution=row["institution"],
        category=DocumentCategory(row["category"]),
        description=row["description"],
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
        updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
        gdrive_id=row["gdrive_id"],
        gdrive_modified_time=(
            datetime.fromisoformat(row["gdrive_modified_time"])
            if row["gdrive_modified_time"]
            else None
        ),
        sync_state=row_dict.get("sync_state", "synced") or "synced",
        last_synced_at=(
            datetime.fromisoformat(row_dict["last_synced_at"])
            if row_dict.get("last_synced_at")
            else None
        ),
        ai_summary=row["ai_summary"],
        ai_tags=row["ai_tags"],
        ai_processed_at=(
            datetime.fromisoformat(row["ai_processed_at"]) if row["ai_processed_at"] else None
        ),
        structured_metadata=row_dict.get("structured_metadata"),
        deleted_at=(
            datetime.fromisoformat(row_dict["deleted_at"]) if row_dict.get("deleted_at") else None
        ),
    )
