"""Document CRUD, search, OCR cache, and metadata operations."""

from __future__ import annotations

from oncofiles.models import Document, SearchQuery

from ._converters import _row_to_document


class DocumentMixin:
    """Document-related database operations."""

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def insert_document(self, doc: Document) -> Document:
        """Insert a document and return it with the generated ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO documents
                (file_id, filename, original_filename, document_date,
                 institution, category, description, mime_type, size_bytes,
                 gdrive_id, gdrive_modified_time, version, previous_version_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                doc.version,
                doc.previous_version_id,
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

    async def get_document_by_gdrive_id(self, gdrive_id: str) -> Document | None:
        """Get a document by its Google Drive file ID."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE gdrive_id = ?", (gdrive_id,)
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
                term_cases = " OR ".join(f"{field} LIKE ?" for _ in terms)
                score_parts.append(f"(CASE WHEN ({term_cases}) THEN {weight} ELSE 0 END)")
                params.extend(f"%{t}%" for t in terms)

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
        """Soft-delete a document by file_id. Returns True if updated."""
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
        """List soft-deleted documents (recoverable within 30 days)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

    async def find_duplicates(self) -> list[list[Document]]:
        """Find potential duplicate documents based on original_filename + size_bytes.

        Returns groups of documents that share the same original filename and
        file size. Each group has 2+ documents. Only active (non-deleted) docs.
        """
        async with self.db.execute(
            """
            SELECT original_filename, size_bytes, COUNT(*) as cnt
            FROM documents
            WHERE deleted_at IS NULL
            GROUP BY original_filename, size_bytes
            HAVING cnt > 1
            ORDER BY cnt DESC
            """,
        ) as cursor:
            groups = await cursor.fetchall()

        result = []
        for g in groups:
            async with self.db.execute(
                "SELECT * FROM documents WHERE original_filename = ? "
                "AND size_bytes = ? AND deleted_at IS NULL "
                "ORDER BY created_at ASC",
                (g["original_filename"], g["size_bytes"]),
            ) as cursor:
                rows = await cursor.fetchall()
                result.append([_row_to_document(r) for r in rows])
        return result

    async def purge_expired_trash(self, days: int = 30) -> int:
        """Permanently delete documents that have been in trash for over N days.

        Also deletes associated OCR pages. Returns count of purged documents.
        """
        # Find expired documents
        async with self.db.execute(
            """
            SELECT id FROM documents
            WHERE deleted_at IS NOT NULL
            AND deleted_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            """,
            (f"-{days} days",),
        ) as cursor:
            rows = await cursor.fetchall()

        if not rows:
            return 0

        doc_ids = [r["id"] for r in rows]

        # Delete OCR pages for expired documents
        for doc_id in doc_ids:
            await self.db.execute(
                "DELETE FROM document_pages WHERE document_id = ?",
                (doc_id,),
            )

        # Delete lab values for expired documents
        for doc_id in doc_ids:
            await self.db.execute(
                "DELETE FROM lab_values WHERE document_id = ?",
                (doc_id,),
            )

        # Permanently delete documents
        placeholders = ", ".join("?" for _ in doc_ids)
        await self.db.execute(
            f"DELETE FROM documents WHERE id IN ({placeholders})",
            doc_ids,
        )
        await self.db.commit()
        return len(doc_ids)

    async def count_documents(self) -> int:
        """Count all active (non-deleted) documents."""
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM documents WHERE deleted_at IS NULL"
        ) as cursor:
            row = await cursor.fetchone()
            return row["cnt"] if row else 0

    # ── Document metadata ─────────────────────────────────────────────────

    async def update_document_file_id(self, doc_id: int, file_id: str, size_bytes: int) -> None:
        """Set the Anthropic Files API file_id and size for a document."""
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

    async def get_documents_without_metadata(self, limit: int = 100) -> list[Document]:
        """Get documents that have AI summaries but no useful structured_metadata.

        Matches documents where structured_metadata is NULL, empty string,
        or contains only empty-default values (no findings/diagnoses extracted).
        """
        async with self.db.execute(
            "SELECT * FROM documents WHERE ai_processed_at IS NOT NULL "
            "AND (structured_metadata IS NULL OR structured_metadata = '' "
            '  OR (structured_metadata NOT LIKE \'%"findings": ["%%\' '
            "      AND structured_metadata NOT LIKE '%\"diagnoses\": [{%%')) "
            "AND deleted_at IS NULL "
            "ORDER BY document_date DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

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

    async def update_structured_metadata(self, doc_id: int, metadata_json: str) -> None:
        """Update the structured_metadata JSON for a document."""
        await self.db.execute(
            "UPDATE documents SET structured_metadata = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (metadata_json, doc_id),
        )
        await self.db.commit()

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

    # ── Lab-related document queries ──────────────────────────────────────

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

    # ── Sync state ────────────────────────────────────────────────────────

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

    # ── Versioning ─────────────────────────────────────────────────────

    async def get_active_document_by_filename(self, original_filename: str) -> Document | None:
        """Get an active (non-deleted) document by its original filename."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE original_filename = ? AND deleted_at IS NULL "
            "ORDER BY version DESC LIMIT 1",
            (original_filename,),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def get_document_version_chain(self, doc_id: int) -> list[Document]:
        """Walk the version chain for a document, newest first.

        Starting from the given document, finds the latest version (if any
        newer version points back to it), then walks previous_version_id
        backwards to build the full chain.
        """
        # First, find the latest version that traces back through this doc
        # by walking forward: find any doc whose previous_version_id = doc_id
        current_id = doc_id
        while True:
            async with self.db.execute(
                "SELECT id FROM documents WHERE previous_version_id = ?",
                (current_id,),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    current_id = row["id"]
                else:
                    break

        # Now walk backwards from the latest version
        chain: list[Document] = []
        visited: set[int] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            doc = await self.get_document(current_id)
            if not doc:
                break
            chain.append(doc)
            current_id = doc.previous_version_id
        return chain

    async def get_pending_sync_documents(self) -> list[Document]:
        """Get documents that need syncing (no gdrive_id or pending state)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE (gdrive_id IS NULL OR sync_state = 'pending') "
            "AND deleted_at IS NULL ORDER BY document_date DESC",
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_document(r) for r in rows]

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
