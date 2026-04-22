"""Clinical records CRUD + audit trail + personal notes (#450 Phase 1).

The canonical clinical fact store. Every ``clinical_records`` write produces a
matching ``clinical_record_audit`` row so the full change history is queryable.
Notes and analyses are separate tables — see ``clinical_record_notes`` and
``clinical_analyses``.

Deletion is always soft. A deleted record's audit row survives so the caregiver
(or Oncoteam, or a future dashboard timeline) can still see when / why a fact
was retracted.
"""

from __future__ import annotations

import json
from typing import Any

from oncofiles.models import (
    ClinicalAnalysis,
    ClinicalRecord,
    ClinicalRecordAudit,
    ClinicalRecordNote,
    ClinicalRecordQuery,
)

from ._converters import (
    _row_to_clinical_analysis,
    _row_to_clinical_record,
    _row_to_clinical_record_audit,
    _row_to_clinical_record_note,
)

_RECORD_FIELDS: tuple[str, ...] = (
    "patient_id",
    "record_type",
    "source_document_id",
    "occurred_at",
    "param",
    "value_num",
    "value_text",
    "unit",
    "status",
    "ref_range_low",
    "ref_range_high",
    "metadata_json",
    "source",
    "session_id",
    "caller_identity",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
    "deleted_at",
    "deleted_by",
)


def _record_to_snapshot(record: ClinicalRecord) -> dict[str, Any]:
    """Serialise a ClinicalRecord into a plain dict for audit storage."""
    return {f: getattr(record, f) for f in ("id", *_RECORD_FIELDS)}


class ClinicalRecordsMixin:
    """CRUD + audit + notes for the canonical clinical fact store (#450)."""

    # ── clinical_records CRUD ────────────────────────────────────────────

    async def insert_clinical_record(
        self,
        record: ClinicalRecord,
        *,
        reason: str | None = None,
    ) -> ClinicalRecord:
        """Insert a clinical record and emit the matching audit row."""
        cursor = await self.db.execute(
            """
            INSERT INTO clinical_records (
                patient_id, record_type, source_document_id, occurred_at,
                param, value_num, value_text, unit, status,
                ref_range_low, ref_range_high, metadata_json,
                source, session_id, caller_identity, created_by, updated_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.patient_id,
                record.record_type,
                record.source_document_id,
                record.occurred_at,
                record.param,
                record.value_num,
                record.value_text,
                record.unit,
                record.status,
                record.ref_range_low,
                record.ref_range_high,
                record.metadata_json,
                record.source,
                record.session_id,
                record.caller_identity,
                record.created_by,
                record.updated_by or record.created_by,
            ),
        )
        await self.db.commit()
        record.id = cursor.lastrowid
        stored = await self.get_clinical_record(record.id)
        # ``stored`` is never None here: we just inserted it. The cast keeps
        # type checkers happy and guards against silent insert failures.
        if stored is None:
            raise RuntimeError(f"insert_clinical_record: row {record.id} vanished after INSERT")
        await self._write_audit(
            record_id=stored.id,
            action="create",
            before=None,
            after=_record_to_snapshot(stored),
            changed_fields=None,
            reason=reason,
            source=stored.source,
            session_id=stored.session_id,
            caller_identity=stored.caller_identity,
            changed_by=stored.created_by,
        )
        return stored

    async def get_clinical_record(
        self, record_id: int, *, include_deleted: bool = False
    ) -> ClinicalRecord | None:
        """Fetch a single clinical record by id."""
        if include_deleted:
            sql = "SELECT * FROM clinical_records WHERE id = ?"
            params: tuple = (record_id,)
        else:
            sql = "SELECT * FROM clinical_records WHERE id = ? AND deleted_at IS NULL"
            params = (record_id,)
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
            return _row_to_clinical_record(row) if row else None

    async def list_clinical_records(
        self, query: ClinicalRecordQuery, *, patient_id: str
    ) -> list[ClinicalRecord]:
        """List clinical records for a patient with optional filters."""
        conditions: list[str] = ["patient_id = ?"]
        params: list[Any] = [patient_id]

        if not query.include_deleted:
            conditions.append("deleted_at IS NULL")
        if query.record_type:
            conditions.append("record_type = ?")
            params.append(query.record_type)
        if query.param:
            conditions.append("param = ?")
            params.append(query.param)
        if query.since:
            conditions.append("occurred_at >= ?")
            params.append(query.since)
        if query.until:
            conditions.append("occurred_at <= ?")
            params.append(query.until)

        where = " AND ".join(conditions)
        sql = (
            f"SELECT * FROM clinical_records WHERE {where} "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?"
        )
        params.append(query.limit)
        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_clinical_record(r) for r in rows]

    async def update_clinical_record(
        self,
        record_id: int,
        updates: dict[str, Any],
        *,
        changed_by: str | None = None,
        source: str,
        session_id: str | None = None,
        caller_identity: str | None = None,
        reason: str | None = None,
    ) -> ClinicalRecord | None:
        """Apply a partial update and emit an audit row.

        Only fields in ``_RECORD_FIELDS`` are settable. Attempts to update
        ``patient_id``, ``created_at``, or audit-owned fields are silently
        dropped — those are controlled by other code paths (soft-delete writes
        ``deleted_at`` directly via ``delete_clinical_record``).
        """
        before = await self.get_clinical_record(record_id, include_deleted=True)
        if before is None:
            return None

        allowed = {
            "record_type",
            "source_document_id",
            "occurred_at",
            "param",
            "value_num",
            "value_text",
            "unit",
            "status",
            "ref_range_low",
            "ref_range_high",
            "metadata_json",
        }
        sets: list[str] = []
        params: list[Any] = []
        changed_fields: list[str] = []
        for key, val in updates.items():
            if key not in allowed:
                continue
            if getattr(before, key) == val:
                continue
            sets.append(f"{key} = ?")
            params.append(val)
            changed_fields.append(key)

        if not sets:
            return before

        sets.append("updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')")
        sets.append("updated_by = ?")
        params.append(changed_by)
        params.append(record_id)
        await self.db.execute(
            f"UPDATE clinical_records SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        await self.db.commit()

        after = await self.get_clinical_record(record_id, include_deleted=True)
        await self._write_audit(
            record_id=record_id,
            action="update",
            before=_record_to_snapshot(before),
            after=_record_to_snapshot(after) if after else None,
            changed_fields=",".join(changed_fields),
            reason=reason,
            source=source,
            session_id=session_id,
            caller_identity=caller_identity,
            changed_by=changed_by,
        )
        return after

    async def delete_clinical_record(
        self,
        record_id: int,
        *,
        deleted_by: str | None = None,
        source: str,
        session_id: str | None = None,
        caller_identity: str | None = None,
        reason: str | None = None,
    ) -> bool:
        """Soft-delete a clinical record. Idempotent — already-deleted returns False."""
        before = await self.get_clinical_record(record_id, include_deleted=True)
        if before is None or before.deleted_at is not None:
            return False
        await self.db.execute(
            "UPDATE clinical_records "
            "   SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "       deleted_by = ? "
            " WHERE id = ?",
            (deleted_by, record_id),
        )
        await self.db.commit()
        after = await self.get_clinical_record(record_id, include_deleted=True)
        await self._write_audit(
            record_id=record_id,
            action="delete",
            before=_record_to_snapshot(before),
            after=_record_to_snapshot(after) if after else None,
            changed_fields="deleted_at,deleted_by",
            reason=reason,
            source=source,
            session_id=session_id,
            caller_identity=caller_identity,
            changed_by=deleted_by,
        )
        return True

    async def restore_clinical_record(
        self,
        record_id: int,
        *,
        restored_by: str | None = None,
        source: str,
        session_id: str | None = None,
        caller_identity: str | None = None,
        reason: str | None = None,
    ) -> ClinicalRecord | None:
        """Undo a soft-delete. Returns the restored record or None if not found."""
        before = await self.get_clinical_record(record_id, include_deleted=True)
        if before is None or before.deleted_at is None:
            return None
        await self.db.execute(
            "UPDATE clinical_records "
            "   SET deleted_at = NULL, deleted_by = NULL, "
            "       updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "       updated_by = ? "
            " WHERE id = ?",
            (restored_by, record_id),
        )
        await self.db.commit()
        after = await self.get_clinical_record(record_id, include_deleted=True)
        await self._write_audit(
            record_id=record_id,
            action="restore",
            before=_record_to_snapshot(before),
            after=_record_to_snapshot(after) if after else None,
            changed_fields="deleted_at,deleted_by",
            reason=reason,
            source=source,
            session_id=session_id,
            caller_identity=caller_identity,
            changed_by=restored_by,
        )
        return after

    # ── clinical_record_audit reads ─────────────────────────────────────

    async def list_clinical_record_audit(
        self, record_id: int, limit: int = 200
    ) -> list[ClinicalRecordAudit]:
        """Full chronological audit history for a single record (newest first)."""
        async with self.db.execute(
            "SELECT * FROM clinical_record_audit WHERE record_id = ? "
            "ORDER BY changed_at DESC, id DESC LIMIT ?",
            (record_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_clinical_record_audit(r) for r in rows]

    # ── clinical_record_notes CRUD ──────────────────────────────────────

    async def insert_clinical_record_note(self, note: ClinicalRecordNote) -> ClinicalRecordNote:
        """Add a note to a clinical record."""
        cursor = await self.db.execute(
            """
            INSERT INTO clinical_record_notes (
                record_id, note_text, tags, source, session_id,
                mcp_conversation_ref, caller_identity, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                note.record_id,
                note.note_text,
                note.tags,
                note.source,
                note.session_id,
                note.mcp_conversation_ref,
                note.caller_identity,
                note.created_by,
            ),
        )
        await self.db.commit()
        note.id = cursor.lastrowid
        stored = await self.get_clinical_record_note(note.id)
        if stored is None:
            raise RuntimeError(f"insert_clinical_record_note: row {note.id} vanished after INSERT")
        return stored

    async def get_clinical_record_note(
        self, note_id: int, *, include_deleted: bool = False
    ) -> ClinicalRecordNote | None:
        """Fetch a single note by id."""
        if include_deleted:
            sql = "SELECT * FROM clinical_record_notes WHERE id = ?"
        else:
            sql = "SELECT * FROM clinical_record_notes WHERE id = ? AND deleted_at IS NULL"
        async with self.db.execute(sql, (note_id,)) as cursor:
            row = await cursor.fetchone()
            return _row_to_clinical_record_note(row) if row else None

    async def list_clinical_record_notes(
        self,
        record_id: int | None = None,
        *,
        patient_id: str | None = None,
        tags_any: list[str] | None = None,
        include_deleted: bool = False,
        limit: int = 200,
    ) -> list[ClinicalRecordNote]:
        """List notes.

        Either ``record_id`` (notes on a specific record) or ``patient_id``
        (all notes for a patient, via join to ``clinical_records``) must be
        provided. ``tags_any`` filters to notes whose ``tags`` JSON array
        contains at least one of the given tag strings.
        """
        if record_id is None and patient_id is None:
            raise ValueError("list_clinical_record_notes: record_id or patient_id required")

        conditions: list[str] = []
        params: list[Any] = []
        join = ""
        if record_id is not None:
            conditions.append("n.record_id = ?")
            params.append(record_id)
        if patient_id is not None:
            join = "JOIN clinical_records r ON r.id = n.record_id"
            conditions.append("r.patient_id = ?")
            params.append(patient_id)
        if not include_deleted:
            conditions.append("n.deleted_at IS NULL")

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = (
            f"SELECT n.* FROM clinical_record_notes n {join} "
            f"WHERE {where} ORDER BY n.created_at DESC, n.id DESC LIMIT ?"
        )
        params.append(limit)
        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            notes = [_row_to_clinical_record_note(r) for r in rows]

        if tags_any:
            requested = set(tags_any)
            filtered: list[ClinicalRecordNote] = []
            for note in notes:
                if not note.tags:
                    continue
                try:
                    note_tags = set(json.loads(note.tags))
                except (ValueError, TypeError):
                    continue
                if requested & note_tags:
                    filtered.append(note)
            return filtered
        return notes

    async def delete_clinical_record_note(
        self, note_id: int, *, deleted_by: str | None = None
    ) -> bool:
        """Soft-delete a note."""
        note = await self.get_clinical_record_note(note_id, include_deleted=True)
        if note is None or note.deleted_at is not None:
            return False
        await self.db.execute(
            "UPDATE clinical_record_notes "
            "   SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), "
            "       deleted_by = ? "
            " WHERE id = ?",
            (deleted_by, note_id),
        )
        await self.db.commit()
        return True

    # ── internal helpers ─────────────────────────────────────────────────

    async def _write_audit(
        self,
        *,
        record_id: int,
        action: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
        changed_fields: str | None,
        reason: str | None,
        source: str,
        session_id: str | None,
        caller_identity: str | None,
        changed_by: str | None,
    ) -> None:
        before_json = json.dumps(before, default=str) if before is not None else None
        after_json = json.dumps(after, default=str) if after is not None else None
        await self.db.execute(
            """
            INSERT INTO clinical_record_audit (
                record_id, action, before_json, after_json, changed_fields,
                reason, source, session_id, caller_identity, changed_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record_id,
                action,
                before_json,
                after_json,
                changed_fields,
                reason,
                source,
                session_id,
                caller_identity,
                changed_by,
            ),
        )
        await self.db.commit()

    # ── clinical_analyses (#450 Phase 2) ───────────────────────────────

    async def insert_clinical_analysis(self, analysis: ClinicalAnalysis) -> ClinicalAnalysis:
        """Persist an analytic output over one or more clinical records."""
        cursor = await self.db.execute(
            """
            INSERT INTO clinical_analyses (
                patient_id, record_ids, analysis_type, result_json,
                result_summary, tags, produced_by, session_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                analysis.patient_id,
                analysis.record_ids,
                analysis.analysis_type,
                analysis.result_json,
                analysis.result_summary,
                analysis.tags,
                analysis.produced_by,
                analysis.session_id,
            ),
        )
        await self.db.commit()
        analysis.id = cursor.lastrowid
        stored = await self.get_clinical_analysis(analysis.id)
        if stored is None:
            raise RuntimeError(f"insert_clinical_analysis: row {analysis.id} vanished after INSERT")
        return stored

    async def get_clinical_analysis(self, analysis_id: int) -> ClinicalAnalysis | None:
        """Fetch a single clinical_analyses row by id."""
        async with self.db.execute(
            "SELECT * FROM clinical_analyses WHERE id = ?", (analysis_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return _row_to_clinical_analysis(row) if row else None

    async def list_clinical_analyses(
        self,
        *,
        patient_id: str,
        analysis_type: str | None = None,
        limit: int = 100,
    ) -> list[ClinicalAnalysis]:
        """List clinical_analyses rows for a patient, newest first."""
        conditions = ["patient_id = ?"]
        params: list[Any] = [patient_id]
        if analysis_type:
            conditions.append("analysis_type = ?")
            params.append(analysis_type)
        where = " AND ".join(conditions)
        params.append(limit)
        async with self.db.execute(
            f"SELECT * FROM clinical_analyses WHERE {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_clinical_analysis(r) for r in rows]

    # ── clinical_record_notes full-text-ish search (#450 Phase 2) ─────────

    async def search_clinical_record_notes(
        self,
        *,
        patient_id: str,
        query: str,
        limit: int = 100,
    ) -> list[ClinicalRecordNote]:
        """LIKE-based search on note_text scoped to a patient.

        FTS5 upgrade deferred until >500 notes/patient. Current use case
        (caregiver recall: "What did I tag about CEA?") works with LIKE
        and the existing (record_id, created_at DESC) index.
        """
        if not query.strip():
            return []
        like = f"%{query}%"
        async with self.db.execute(
            """
            SELECT n.* FROM clinical_record_notes n
            JOIN clinical_records r ON r.id = n.record_id
            WHERE r.patient_id = ?
              AND n.deleted_at IS NULL
              AND n.note_text LIKE ?
            ORDER BY n.created_at DESC, n.id DESC
            LIMIT ?
            """,
            (patient_id, like, limit),
        ) as cursor:
            rows = await cursor.fetchall()
            return [_row_to_clinical_record_note(r) for r in rows]
