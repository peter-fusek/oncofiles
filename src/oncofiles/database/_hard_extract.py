"""Storage and reads for the LLM-extracted non-lab kinds (#726 item 4, migration 081).

The hard-fact tables of #725 are written through ``HardFactsMixin``; this mixin
adds what those helpers do not cover:

* ``clinical_records`` rows for biomarkers / genomic results and CT lesion
  sizes (with an audit row, like ``replace_document_facts``);
* planned encounters (a next visit or examination printed in a document);
* per-document listings for re-extraction, and the read helpers of the MCP
  tools (overview, treatment listing).

Rules: keyword-only ``patient_id`` filtered in SQL; references checked to be
the same patient; deletion is soft and never touches ``verified`` /
``corrected`` rows; nothing here deletes a row another extractor wrote.
"""

from __future__ import annotations

import json
import uuid

from oncofiles.database._hard_facts import FACT_ENTITIES

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"
_PROTECTED_SQL = "('verified', 'corrected')"
PLANNED_ENCOUNTER_KINDS = ("chemo_cycle", "imaging", "visit", "other")


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


class HardExtractMixin:
    """Re-extraction listings, clinical_records / planned-encounter writes, reads."""

    async def _hx_all(self, sql: str, params: tuple) -> list[dict]:
        async with self.db.execute(sql, params) as cur:
            return _rows(await cur.fetchall())

    # ── per-document listings (re-extraction) ─────────────────────────

    async def hard_rows_of_document(
        self, entity: str, *, patient_id: str, document_id: int
    ) -> list[dict]:
        """Active rows of one hard-fact table that cite this document."""
        table, _ = FACT_ENTITIES[entity]
        return await self._hx_all(
            f"SELECT * FROM {table} WHERE patient_id = ? AND source_document_id = ? "
            "AND deleted_at IS NULL ORDER BY id",
            (patient_id, document_id),
        )

    async def extracted_records_of_document(
        self, *, patient_id: str, document_id: int, family: str
    ) -> list[dict]:
        """Active clinical_records of this document written by one extractor family."""
        return await self._hx_all(
            "SELECT * FROM clinical_records WHERE patient_id = ? AND source_document_id = ? "
            "AND deleted_at IS NULL AND extracted_by LIKE ? ORDER BY id",
            (patient_id, document_id, family + "%"),
        )

    async def planned_encounters_of_document(
        self, *, patient_id: str, document_id: int, family: str
    ) -> list[dict]:
        return await self._hx_all(
            "SELECT * FROM encounters WHERE patient_id = ? AND source_document_id = ? "
            "AND status = 'planned' AND deleted_at IS NULL AND extracted_by LIKE ? ORDER BY id",
            (patient_id, document_id, family + "%"),
        )

    # ── clinical_records (biomarkers, lesion sizes) ───────────────────

    async def insert_extracted_record(
        self, *, patient_id: str, document_id: int, record: dict, extracted_by: str
    ) -> int:
        """One clinical_records row from an extractor, with its audit row."""
        await self._require_owned("documents", document_id, patient_id, "source_document_id")
        cols = {
            "patient_id": patient_id,
            "record_type": record["record_type"],
            "source_document_id": document_id,
            "occurred_at": record.get("occurred_at"),
            "param": record.get("param"),
            "value_num": record.get("value_num"),
            "value_text": record.get("value_text"),
            "unit": record.get("unit"),
            "unit_printed": record.get("unit"),
            "code": record.get("code"),
            "page_number": record.get("page_number"),
            "evidence": record.get("evidence"),
            "value_canonical": record.get("value_canonical"),
            "unit_canonical": record.get("unit_canonical"),
            "effective_precision": record.get("effective_precision"),
            "verification": record.get("verification", "unverified"),
            "extracted_by": extracted_by,
            "source": "curated-llm-extract",
            "metadata_json": json.dumps(record.get("metadata") or {}, ensure_ascii=False),
            "fhir_id": str(uuid.uuid4()),
        }
        cur = await self.db.execute(
            f"INSERT INTO clinical_records ({', '.join(cols)}, last_updated) "
            f"VALUES ({', '.join('?' for _ in cols)}, {_NOW})",
            tuple(cols.values()),
        )
        await self._audit(cur.lastrowid, "create", record, f"extracted by {extracted_by}")
        await self.db.commit()
        return cur.lastrowid

    async def soft_delete_extracted_record(
        self, *, patient_id: str, record_id: int, reason: str
    ) -> bool:
        """Soft-delete one extracted row; verified / corrected rows never change."""
        cur = await self.db.execute(
            f"UPDATE clinical_records SET deleted_at = {_NOW}, deleted_by = 'oncofiles' "
            f"WHERE id = ? AND patient_id = ? AND deleted_at IS NULL "
            f"AND verification NOT IN {_PROTECTED_SQL}",
            (record_id, patient_id),
        )
        if cur.rowcount:
            await self._audit(record_id, "delete", None, reason)
        await self.db.commit()
        return bool(cur.rowcount)

    async def soft_delete_codings_of(
        self, owner_type: str, owner_id: int, *, patient_id: str, deleted_by: str
    ) -> int:
        cur = await self.db.execute(
            f"UPDATE codings SET deleted_at = {_NOW}, deleted_by = ? WHERE owner_type = ? "
            "AND owner_id = ? AND patient_id = ? AND deleted_at IS NULL",
            (deleted_by, owner_type, str(owner_id), patient_id),
        )
        await self.db.commit()
        return cur.rowcount or 0

    # ── planned encounters ────────────────────────────────────────────

    async def insert_planned_encounter(
        self,
        *,
        patient_id: str,
        document_id: int,
        start_at: str,
        kind: str,
        title: str | None,
        page_number: int | None,
        evidence: str | None,
        extracted_by: str,
        verification: str,
        metadata: dict | None = None,
    ) -> int:
        if kind not in PLANNED_ENCOUNTER_KINDS:
            raise ValueError(f"planned encounter kind {kind!r} is not allowed")
        await self._require_owned("documents", document_id, patient_id, "source_document_id")
        cur = await self.db.execute(
            "INSERT INTO encounters (patient_id, natural_key, start_at, kind, title, source, "
            "status, source_document_id, page_number, evidence, extracted_by, verification, "
            "metadata_json) VALUES (?, ?, ?, ?, ?, 'llm', 'planned', ?, ?, ?, ?, ?, ?)",
            (
                patient_id, f"planned|{start_at}|{kind}|{uuid.uuid4()}", start_at, kind, title,
                document_id, page_number, evidence, extracted_by, verification,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )  # fmt: skip
        await self.db.commit()
        return cur.lastrowid

    async def soft_delete_planned_encounter(self, *, patient_id: str, encounter_id: int) -> bool:
        cur = await self.db.execute(
            f"UPDATE encounters SET deleted_at = {_NOW}, updated_at = {_NOW} "
            "WHERE id = ? AND patient_id = ? AND status = 'planned' AND deleted_at IS NULL "
            f"AND COALESCE(verification, '') NOT IN {_PROTECTED_SQL}",
            (encounter_id, patient_id),
        )
        await self.db.commit()
        return bool(cur.rowcount)

    # ── reads for the MCP tools (patient-scoped) ──────────────────────

    async def condition_rows(self, *, patient_id: str) -> list[dict]:
        """Active conditions with their newest stage and the printed code."""
        return await self._hx_all(
            "SELECT c.id, c.printed_text, c.asserted_at, c.recorded_at, c.body_site, "
            "c.is_primary_cancer, c.primary_condition_id, c.clinical_certainty, c.verification, "
            "c.source_document_id, c.page_number, c.evidence, "
            "(SELECT st.printed FROM cancer_stages st WHERE st.condition_id = c.id "
            " AND st.patient_id = c.patient_id AND st.deleted_at IS NULL "
            " ORDER BY st.id DESC LIMIT 1) AS stage, "
            "(SELECT cd.code FROM codings cd WHERE cd.owner_type = 'condition' "
            " AND cd.owner_id = CAST(c.id AS TEXT) AND cd.patient_id = c.patient_id "
            " AND cd.deleted_at IS NULL ORDER BY cd.is_original DESC, cd.id LIMIT 1) AS code "
            "FROM conditions c WHERE c.patient_id = ? AND c.deleted_at IS NULL "
            "ORDER BY c.is_primary_cancer DESC, COALESCE(c.asserted_at, c.recorded_at) DESC, c.id",
            (patient_id,),
        )

    async def marker_rows(self, *, patient_id: str) -> list[dict]:
        """Newest biomarker / genomic / condition fact per code (or label when
        unmapped), whatever its verification (shown with it)."""
        return await self._hx_all(
            "SELECT s.* FROM fact_series_v s WHERE s.patient_id = ? "
            "AND s.category IN ('condition', 'genetic_variant', 'biomarker') "
            "AND s.id = (SELECT s2.id FROM fact_series_v s2 WHERE s2.patient_id = s.patient_id "
            " AND s2.category = s.category "
            " AND COALESCE(s2.code, s2.label_original) = COALESCE(s.code, s.label_original) "
            " ORDER BY s2.effective_at DESC, s2.id DESC LIMIT 1) "
            "ORDER BY s.category, COALESCE(s.code, s.label_original)",
            (patient_id,),
        )

    async def planned_rows(self, *, patient_id: str, from_day: str, limit: int = 10) -> list[dict]:
        return await self._hx_all(
            "SELECT * FROM timeline_v WHERE patient_id = ? AND kind = 'planned' AND at >= ? "
            "ORDER BY at LIMIT ?",
            (patient_id, from_day, limit),
        )

    async def treatment_rows(
        self,
        *,
        patient_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 200,
    ) -> dict[str, list[dict]]:
        """Courses, cycles and medication events of a patient (hard tables)."""

        def window(col: str) -> tuple[str, list]:
            sql, params = "", []
            if date_from:
                sql += f" AND {col} >= ?"
                params.append(date_from)
            if date_to:
                sql += f" AND {col} <= ?"
                params.append(date_to + "~")
            return sql, params

        doc_day = (
            "(SELECT d.document_date FROM documents d WHERE d.id = x.source_document_id "
            "AND d.patient_id = x.patient_id)"
        )
        out: dict[str, list[dict]] = {}
        at = f"COALESCE(x.start_at, {doc_day})"
        w, p = window(at)
        out["courses"] = await self._hx_all(
            f"SELECT x.id, x.regimen_printed, x.line_of_therapy, x.intent, x.start_at, x.end_at, "
            f"x.status, x.verification, x.source_document_id, x.page_number, x.evidence, "
            f"{at} AS at FROM treatment_courses x WHERE x.patient_id = ? "
            f"AND x.deleted_at IS NULL{w} ORDER BY at DESC, x.id LIMIT ?",
            (patient_id, *p, limit),
        )
        at = f"COALESCE(x.actual_at, x.planned_at, {doc_day})"
        w, p = window(at)
        out["cycles"] = await self._hx_all(
            f"SELECT x.id, x.course_id, x.cycle_number_printed, x.status, x.status_reason, "
            f"x.actual_at, x.planned_at, x.verification, x.source_document_id, x.page_number, "
            f"x.evidence, {at} AS at FROM treatment_cycles x WHERE x.patient_id = ? "
            f"AND x.deleted_at IS NULL{w} ORDER BY at DESC, x.id LIMIT ?",
            (patient_id, *p, limit),
        )
        at = f"COALESCE(x.effective_start, x.authored_at, {doc_day})"
        w, p = window(at)
        out["medication_events"] = await self._hx_all(
            f"SELECT x.id, x.kind, x.course_id, x.cycle_id, x.ingredient_printed, x.dose_value, "
            f"x.dose_unit_ucum, x.dose_printed, x.normalization_basis, x.route, "
            f"x.effective_start, x.effective_end, x.verification, x.source_document_id, "
            f"x.page_number, x.evidence, {at} AS at FROM medication_events x "
            f"WHERE x.patient_id = ? AND x.deleted_at IS NULL{w} ORDER BY at DESC, x.id LIMIT ?",
            (patient_id, *p, limit),
        )
        return out
