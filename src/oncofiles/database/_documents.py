"""Document CRUD, search, OCR cache, and metadata operations."""

from __future__ import annotations

import logging

from oncofiles.models import Document, SearchQuery

from ._converters import _row_to_document

logger = logging.getLogger(__name__)


def _safe_row_to_document(row) -> Document | None:
    """Convert a row to Document, returning None on conversion errors."""
    try:
        return _row_to_document(row)
    except Exception:
        row_id = row["id"] if row else "?"
        logger.warning("Skipping corrupt document row id=%s", row_id, exc_info=True)
        return None


class DocumentMixin:
    """Document-related database operations."""

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def insert_document(self, doc: Document, *, patient_id: str) -> Document:
        """Insert a document and return it with the generated ID.

        Raises ValueError if the patient has reached the document limit.
        """
        from oncofiles.config import MAX_DOCUMENTS_PER_PATIENT

        if MAX_DOCUMENTS_PER_PATIENT > 0:
            count = await self.count_documents(patient_id=patient_id)
            if count >= MAX_DOCUMENTS_PER_PATIENT:
                raise ValueError(
                    f"Document limit reached ({MAX_DOCUMENTS_PER_PATIENT}). "
                    f"Patient has {count} documents. "
                    "Contact support or self-host for unlimited documents."
                )

        cursor = await self.db.execute(
            """
            INSERT INTO documents
                (file_id, filename, original_filename, document_date,
                 institution, category, description, mime_type, size_bytes,
                 gdrive_id, gdrive_modified_time, gdrive_md5,
                 version, previous_version_id, patient_id,
                 group_id, part_number, total_parts, split_source_doc_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                doc.gdrive_md5,
                doc.version,
                doc.previous_version_id,
                patient_id,
                doc.group_id,
                doc.part_number,
                doc.total_parts,
                doc.split_source_doc_id,
            ),
        )
        await self.db.commit()
        doc.id = cursor.lastrowid
        return doc

    async def get_document(self, doc_id: int, *, patient_id: str) -> Document | None:
        """Get a document by its local ID, scoped to ``patient_id``.

        ``patient_id`` is required (#499/#516): the SQL filter prevents cross-patient
        disclosure if a caller forgets a separate ownership check.
        """
        async with self.db.execute(
            "SELECT * FROM documents WHERE id = ? AND patient_id = ?",
            (doc_id, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def check_document_ownership(self, doc_id: int, patient_id: str) -> bool:
        """Check if a document belongs to the given patient. Returns False if not found."""
        async with self.db.execute(
            "SELECT patient_id FROM documents WHERE id = ?", (doc_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return bool(row and row["patient_id"] == patient_id)

    async def get_documents_by_ids(
        self, doc_ids: set[int], *, patient_id: str
    ) -> dict[int, Document]:
        """Get multiple documents by their IDs in a single query, scoped to ``patient_id``.

        Returns ``{id: Document}``. ``patient_id`` is required (#516): rows from
        other patients are filtered out at the SQL layer so a relationship row
        or cached id list can never leak cross-patient document metadata.
        """
        if not doc_ids:
            return {}
        placeholders = ",".join("?" for _ in doc_ids)
        async with self.db.execute(
            f"SELECT * FROM documents WHERE id IN ({placeholders}) AND patient_id = ?",
            (*doc_ids, patient_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return {doc.id: doc for row in rows if (doc := _safe_row_to_document(row))}

    async def get_documents_by_group(self, group_id: str) -> list[Document]:
        """Get all documents sharing a group_id, ordered by part_number."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE group_id = ? "
            "AND deleted_at IS NULL ORDER BY part_number",
            (group_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [doc for row in rows if (doc := _safe_row_to_document(row))]

    async def get_document_by_file_id(self, file_id: str, *, patient_id: str) -> Document | None:
        """Get a document by its Anthropic Files API file_id."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE file_id = ? AND patient_id = ?",
            (file_id, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def get_document_by_original_filename(
        self, original_filename: str, *, patient_id: str
    ) -> Document | None:
        """Get a document by its original filename (for idempotent imports)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE original_filename = ? AND patient_id = ?",
            (original_filename, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def get_document_by_gdrive_id(
        self, gdrive_id: str, *, patient_id: str
    ) -> Document | None:
        """Get a document by its Google Drive file ID."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE gdrive_id = ? AND patient_id = ?",
            (gdrive_id, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def list_documents(
        self,
        limit: int = 50,
        offset: int = 0,
        *,
        patient_id: str,
    ) -> list[Document]:
        """List documents ordered by date descending."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE deleted_at IS NULL AND patient_id = ? "
            "ORDER BY document_date DESC, created_at DESC LIMIT ? OFFSET ?",
            (patient_id, limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def search_documents(self, query: SearchQuery, *, patient_id: str) -> list[Document]:
        """Search documents with relevance scoring and multi-term matching.

        When text is provided, terms are split on whitespace and ALL must
        match somewhere across the searchable fields (AND semantics).
        Results are ranked by field weight: filename/description (3),
        ai_summary (2), ai_tags/structured_metadata (1).
        """
        conditions: list[str] = ["patient_id = ?"]
        params: list[str | int] = [patient_id]
        score_parts: list[str] = []
        score_params: list[str | int] = []

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
            # Score params are separate because they bind to SELECT clause
            # placeholders, which precede WHERE clause placeholders.
            for field, weight in search_fields:
                term_cases = " OR ".join(f"{field} LIKE ?" for _ in terms)
                score_parts.append(f"(CASE WHEN ({term_cases}) THEN {weight} ELSE 0 END)")
                score_params.extend(f"%{t}%" for t in terms)

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

        # Score params bind to SELECT clause (before WHERE), so prepend them
        all_params = score_params + params if score_params else params

        async with self.db.execute(sql, all_params) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def delete_document(self, doc_id: int, *, patient_id: str | None = None) -> bool:
        """Soft-delete a document by local ID. Returns True if updated."""
        sql = (
            "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND deleted_at IS NULL"
        )
        params: list = [doc_id]
        if patient_id is not None:
            sql += " AND patient_id = ?"
            params.append(patient_id)
        cursor = await self.db.execute(sql, tuple(params))
        await self.db.commit()
        return cursor.rowcount > 0

    async def delete_document_by_file_id(self, file_id: str, *, patient_id: str) -> bool:
        """Soft-delete a document by file_id. Returns True if updated."""
        cursor = await self.db.execute(
            "UPDATE documents SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE file_id = ? AND deleted_at IS NULL AND patient_id = ?",
            (file_id, patient_id),
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

    async def list_trash(self, limit: int = 50, *, patient_id: str) -> list[Document]:
        """List soft-deleted documents (recoverable within 30 days)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE deleted_at IS NOT NULL AND patient_id = ? "
            "ORDER BY deleted_at DESC LIMIT ?",
            (patient_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def find_duplicates(self, *, patient_id: str) -> list[list[Document]]:
        """Find potential duplicate documents based on original_filename + size_bytes.

        Returns groups of documents that share the same original filename and
        file size. Each group has 2+ documents. Only active (non-deleted) docs.
        """
        async with self.db.execute(
            """
            SELECT original_filename, size_bytes, COUNT(*) as cnt
            FROM documents
            WHERE deleted_at IS NULL AND patient_id = ?
            GROUP BY original_filename, size_bytes
            HAVING cnt > 1
            ORDER BY cnt DESC
            """,
            (patient_id,),
        ) as cursor:
            groups = await cursor.fetchall()

        result = []
        for g in groups:
            async with self.db.execute(
                "SELECT * FROM documents WHERE original_filename = ? "
                "AND size_bytes = ? AND deleted_at IS NULL AND patient_id = ? "
                "ORDER BY created_at ASC",
                (g["original_filename"], g["size_bytes"], patient_id),
            ) as cursor:
                rows = await cursor.fetchall()
                result.append([d for r in rows if (d := _safe_row_to_document(r)) is not None])
        return result

    async def purge_expired_trash(self, days: int = 30, *, patient_id: str) -> int:
        """Permanently delete documents that have been in trash for over N days.

        Also deletes associated OCR pages. Returns count of purged documents.
        """
        # Find expired documents
        async with self.db.execute(
            """
            SELECT id FROM documents
            WHERE deleted_at IS NOT NULL AND patient_id = ?
            AND deleted_at < strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
            """,
            (patient_id, f"-{days} days"),
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

    async def count_documents(self, *, patient_id: str) -> int:
        """Count all active (non-deleted) documents."""
        async with self.db.execute(
            "SELECT COUNT(*) as cnt FROM documents WHERE deleted_at IS NULL AND patient_id = ?",
            (patient_id,),
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

    async def get_documents_without_metadata(
        self, limit: int = 100, *, patient_id: str
    ) -> list[Document]:
        """Get documents that have AI summaries but no useful structured_metadata.

        Matches documents where structured_metadata is NULL, empty string,
        or contains only empty-default values (no findings/diagnoses extracted).
        """
        async with self.db.execute(
            "SELECT * FROM documents WHERE ai_processed_at IS NOT NULL "
            "AND (structured_metadata IS NULL OR structured_metadata = '' "
            '  OR (structured_metadata NOT LIKE \'%"findings": ["%%\' '
            "      AND structured_metadata NOT LIKE '%\"diagnoses\": [{%%')) "
            "AND deleted_at IS NULL AND patient_id = ? "
            "ORDER BY document_date DESC LIMIT ?",
            (patient_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def get_documents_without_ai(
        self, limit: int = 100, *, patient_id: str
    ) -> list[Document]:
        """Get documents that haven't been AI-processed yet."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE ai_processed_at IS NULL AND deleted_at IS NULL "
            "AND patient_id = ? ORDER BY document_date DESC LIMIT ?",
            (patient_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def update_gdrive_id(self, doc_id: int, gdrive_id: str, modified_time: str) -> None:
        """Set the Google Drive file ID and modified time for a document."""
        await self.db.execute(
            "UPDATE documents SET gdrive_id = ?, gdrive_modified_time = ? WHERE id = ?",
            (gdrive_id, modified_time, doc_id),
        )
        await self.db.commit()

    async def update_gdrive_md5(self, doc_id: int, md5: str) -> None:
        """Set the Google Drive md5Checksum for content change detection."""
        await self.db.execute(
            "UPDATE documents SET gdrive_md5 = ? WHERE id = ?",
            (md5, doc_id),
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

    async def backfill_document_fields(
        self,
        doc_id: int,
        *,
        document_date: str | None = None,
        institution: str | None = None,
        description: str | None = None,
        force_description: bool = False,
    ) -> None:
        """Backfill top-level fields from structured metadata.

        Date and institution only update NULL values (COALESCE).
        Description updates NULL by default; set force_description=True
        to overwrite existing (e.g. replacing a Slovak description with
        an English CamelCase one for non-standard filenames).
        """
        sets = []
        params: list = []
        if document_date is not None:
            # Validate date string before writing — prevents AI-hallucinated dates (#258)
            from datetime import date as _date

            try:
                _date.fromisoformat(document_date)
            except (ValueError, TypeError):
                logger.warning(
                    "backfill_document_fields: rejecting invalid date %r for doc %d",
                    document_date,
                    doc_id,
                )
                document_date = None
        if document_date is not None:
            sets.append("document_date = COALESCE(document_date, ?)")
            params.append(document_date)
        if institution is not None:
            sets.append("institution = COALESCE(institution, ?)")
            params.append(institution)
        if description is not None:
            if force_description:
                sets.append("description = ?")
            else:
                sets.append("description = COALESCE(description, ?)")
            params.append(description)
        if not sets:
            return
        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        params.append(doc_id)
        sql = f"UPDATE documents SET {', '.join(sets)} WHERE id = ?"
        await self.db.execute(sql, tuple(params))
        await self.db.commit()

    # ── OCR cache ─────────────────────────────────────────────────────────

    async def has_ocr_text(self, document_id: int, *, patient_id: str | None = None) -> bool:
        """Check if OCR text is cached for a document."""
        if patient_id is not None:
            sql = (
                "SELECT 1 FROM document_pages dp "
                "JOIN documents d ON d.id = dp.document_id "
                "WHERE dp.document_id = ? AND d.patient_id = ? LIMIT 1"
            )
            params: tuple = (document_id, patient_id)
        else:
            sql = "SELECT 1 FROM document_pages WHERE document_id = ? LIMIT 1"
            params = (document_id,)
        async with self.db.execute(sql, params) as cursor:
            return await cursor.fetchone() is not None

    async def get_ocr_document_ids(self, *, patient_id: str) -> set[int]:
        """Get document IDs with cached OCR text — scoped to one patient (#504/#505).

        Patient-isolation gap fix: the original implementation queried
        ``document_pages`` with no join to ``documents`` and no filter,
        so callers consumed a global cross-patient set. Status / pipeline
        code that compared a per-patient document list against this global
        set was both incorrect (count drift if any cross-patient id
        accidentally matched) and a structural leak class.

        ``patient_id`` is now keyword-only and required. Admin jobs that
        truly need a global view should call
        ``get_ocr_document_ids_unscoped_for_admin`` and document the reason.
        """
        async with self.db.execute(
            "SELECT DISTINCT dp.document_id "
            "FROM document_pages dp "
            "JOIN documents d ON d.id = dp.document_id "
            "WHERE d.patient_id = ?",
            (patient_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return {(r["document_id"] if isinstance(r, dict) else r[0]) for r in rows}

    async def get_ocr_document_ids_unscoped_for_admin(self) -> set[int]:
        """Cross-patient OCR ids — admin/operator jobs only.

        Reserved for explicit admin paths that genuinely need a global view
        (e.g. one-off integrity audits run by the operator). Patient-facing
        MCP tools and dashboard endpoints MUST use ``get_ocr_document_ids``
        (patient-scoped) — calling this from a per-patient code path is the
        exact regression #504/#505 fixes.
        """
        async with self.db.execute("SELECT DISTINCT document_id FROM document_pages") as cursor:
            rows = await cursor.fetchall()
            return {(r["document_id"] if isinstance(r, dict) else r[0]) for r in rows}

    async def get_ocr_pages(self, document_id: int, *, patient_id: str | None = None) -> list[dict]:
        """Get cached OCR text for a document, ordered by page number."""
        if patient_id is not None:
            sql = (
                "SELECT dp.page_number, dp.extracted_text, dp.model FROM document_pages dp "
                "JOIN documents d ON d.id = dp.document_id "
                "WHERE dp.document_id = ? AND d.patient_id = ? ORDER BY dp.page_number"
            )
            params: tuple = (document_id, patient_id)
        else:
            sql = (
                "SELECT page_number, extracted_text, model FROM document_pages "
                "WHERE document_id = ? ORDER BY page_number"
            )
            params = (document_id,)
        async with self.db.execute(sql, params) as cursor:
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

    async def get_labs_before_date(self, before_date: str, *, patient_id: str) -> list[Document]:
        """Get lab documents dated before a given date."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE category = 'labs' AND document_date < ? "
            "AND deleted_at IS NULL AND patient_id = ? ORDER BY document_date DESC",
            (before_date, patient_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def get_latest_labs(self, limit: int = 5, *, patient_id: str) -> list[Document]:
        """Get the most recent lab result documents."""
        async with self.db.execute(
            """
            SELECT * FROM documents
            WHERE category = 'labs' AND deleted_at IS NULL AND patient_id = ?
            ORDER BY document_date DESC, created_at DESC
            LIMIT ?
            """,
            (patient_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

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

    async def set_gdrive_parent_outside_root(self, doc_id: int, outside: bool) -> None:
        """Mark whether a document's GDrive file lives outside the patient's sync root.

        Set by sync when it discovers a gdrive_id that's valid in Drive but not
        in the sync-root listing (#477). Cleared when the file is moved back in,
        either by the user or by ``consolidate_external_gdrive_files``.
        """
        await self.db.execute(
            "UPDATE documents SET gdrive_parent_outside_root = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (1 if outside else 0, doc_id),
        )
        await self.db.commit()

    # ── Versioning ─────────────────────────────────────────────────────

    async def get_active_document_by_filename(
        self, original_filename: str, *, patient_id: str
    ) -> Document | None:
        """Get an active (non-deleted) document by its original filename."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE original_filename = ? AND deleted_at IS NULL "
            "AND patient_id = ? ORDER BY version DESC LIMIT 1",
            (original_filename, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_document(row) if row else None

    async def get_document_version_chain(self, doc_id: int, *, patient_id: str) -> list[Document]:
        """Walk the version chain for a document, newest first.

        Starting from the given document, finds the latest version (if any
        newer version points back to it), then walks previous_version_id
        backwards to build the full chain. Both walks are patient-scoped so
        version chains can never leak across patients (#499 collateral —
        ``get_document`` is now required to be scoped).
        """
        current_id = doc_id
        while True:
            async with self.db.execute(
                "SELECT id FROM documents WHERE previous_version_id = ? AND patient_id = ?",
                (current_id, patient_id),
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    current_id = row["id"]
                else:
                    break

        chain: list[Document] = []
        visited: set[int] = set()
        while current_id and current_id not in visited:
            visited.add(current_id)
            doc = await self.get_document(current_id, patient_id=patient_id)
            if not doc:
                break
            chain.append(doc)
            current_id = doc.previous_version_id
        return chain

    # ── Cross-references ─────────────────────────────────────────────────

    async def insert_cross_reference(
        self,
        source_id: int,
        target_id: int,
        relationship: str = "related",
        confidence: float = 1.0,
    ) -> None:
        """Insert a cross-reference between two documents (idempotent)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO document_cross_references "
            "(source_document_id, target_document_id, relationship, confidence) "
            "VALUES (?, ?, ?, ?)",
            (source_id, target_id, relationship, confidence),
        )
        await self.db.commit()

    async def bulk_insert_cross_references(self, refs: list[tuple[int, int, str, float]]) -> int:
        """Bulk insert cross-references. Tuples: (src_id, tgt_id, rel, conf).

        Returns count of inserted rows.
        """
        count = 0
        for source_id, target_id, relationship, confidence in refs:
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO document_cross_references "
                "(source_document_id, target_document_id, relationship, confidence) "
                "VALUES (?, ?, ?, ?)",
                (source_id, target_id, relationship, confidence),
            )
            count += cursor.rowcount
        await self.db.commit()
        return count

    async def get_cross_references(self, doc_id: int, *, patient_id: str) -> list[dict]:
        """Get cross-references where BOTH ends belong to ``patient_id`` (#517).

        Joins ``document_cross_references`` with ``documents`` on both
        ``source_document_id`` and ``target_document_id`` and filters both
        sides by ``patient_id``. This blocks the cross-patient relationship
        leak: even if a stray relationship row links a doc to another
        patient's doc, the join + filter drops the row entirely so callers
        can never observe (or follow) the cross-patient edge.
        """
        async with self.db.execute(
            "SELECT r.* FROM document_cross_references r "
            "JOIN documents d_src ON d_src.id = r.source_document_id "
            "JOIN documents d_tgt ON d_tgt.id = r.target_document_id "
            "WHERE (r.source_document_id = ? OR r.target_document_id = ?) "
            "AND d_src.patient_id = ? AND d_tgt.patient_id = ? "
            "ORDER BY r.confidence DESC",
            (doc_id, doc_id, patient_id, patient_id),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def get_pending_sync_documents(self, *, patient_id: str) -> list[Document]:
        """Get documents that need syncing (no gdrive_id or pending state)."""
        async with self.db.execute(
            "SELECT * FROM documents WHERE (gdrive_id IS NULL OR sync_state = 'pending') "
            "AND deleted_at IS NULL AND patient_id = ? ORDER BY document_date DESC",
            (patient_id,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]

    async def get_treatment_timeline(self, limit: int = 200, *, patient_id: str) -> list[Document]:
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
            "chemo_sheet",
            "prescription",
            "referral",
        )
        placeholders = ", ".join("?" for _ in treatment_categories)
        async with self.db.execute(
            f"""
            SELECT * FROM documents
            WHERE category IN ({placeholders}) AND deleted_at IS NULL
            AND patient_id = ?
            ORDER BY document_date ASC, created_at ASC
            LIMIT ?
            """,
            (*treatment_categories, patient_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [d for r in rows if (d := _safe_row_to_document(r)) is not None]
