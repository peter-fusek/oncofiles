"""SQLite database for document metadata with FTS5 search."""

from __future__ import annotations

import asyncio
from datetime import date, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from erika_files_mcp.models import Document, DocumentCategory, SearchQuery

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


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

    def execute(self, sql: str, params: tuple = ()) -> _TursoExecProxy:
        return _TursoExecProxy(self._conn, sql, params)

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
        """Run SQL migration files in order."""
        for sql_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = sql_file.read_text()
            await self.db.executescript(sql)

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
            "SELECT * FROM documents ORDER BY document_date DESC, created_at DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def search_documents(self, query: SearchQuery) -> list[Document]:
        """Search documents using FTS5 and/or filters."""
        conditions: list[str] = []
        params: list[str | int] = []

        if query.text:
            conditions.append("id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)")
            params.append(query.text)

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

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM documents WHERE {where} ORDER BY document_date DESC LIMIT ?"
        params.append(query.limit)

        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def delete_document(self, doc_id: int) -> bool:
        """Delete a document by local ID. Returns True if deleted."""
        cursor = await self.db.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_document_by_file_id(self, file_id: str) -> bool:
        """Delete a document by Anthropic file_id. Returns True if deleted."""
        cursor = await self.db.execute("DELETE FROM documents WHERE file_id = ?", (file_id,))
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_treatment_timeline(self, limit: int = 200) -> list[Document]:
        """Get treatment documents in chronological (ASC) order."""
        treatment_categories = (
            "surgery",
            "discharge",
            "report",
            "pathology",
            "labs",
            "imaging",
            "prescription",
            "referral",
        )
        placeholders = ", ".join("?" for _ in treatment_categories)
        async with self.db.execute(
            f"""
            SELECT * FROM documents
            WHERE category IN ({placeholders})
            ORDER BY document_date ASC, created_at ASC
            LIMIT ?
            """,
            (*treatment_categories, limit),
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

    async def get_latest_labs(self, limit: int = 5) -> list[Document]:
        """Get the most recent lab result documents."""
        async with self.db.execute(
            """
            SELECT * FROM documents
            WHERE category = 'labs'
            ORDER BY document_date DESC, created_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]


def _row_to_document(row: aiosqlite.Row) -> Document:
    """Convert a database row to a Document model."""
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
    )
