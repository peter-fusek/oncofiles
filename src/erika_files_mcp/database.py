"""SQLite database for document metadata with FTS5 search."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import aiosqlite

from erika_files_mcp.models import Document, DocumentCategory, SearchQuery

MIGRATIONS_DIR = Path(__file__).parent.parent.parent / "migrations"


class Database:
    """Async SQLite database for document metadata."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
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
