"""Clinical facts model (#603, migration 072): entities, facts, links, views.

Rules enforced here:
* every read takes a ``patient_id`` and filters by it in SQL;
* ``links`` never join two patients (both ends are checked before insert);
* re-extraction soft-deletes machine/unverified facts of a document with an
  audit row, and never touches ``verified`` / ``corrected`` ones;
* aggregates exclude ``unverified`` values unless asked to include them.
"""

from __future__ import annotations

import json

_NOW = "strftime('%Y-%m-%dT%H:%M:%SZ', 'now')"

# entity type → (table, id column) for link ownership checks
_LINK_TABLES = {
    "document": ("documents", "id"),
    "fact": ("clinical_records", "id"),
    "encounter": ("encounters", "id"),
    "treatment_event": ("treatment_events", "id"),
    "conversation": ("conversation_entries", "id"),
}
PROTECTED_VERIFICATION = ("verified", "corrected")
EXTRACT_SOURCE = "curated-extract"


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


class FactsMixin:
    """Entities, facts and the timeline/series views."""

    # ── entities ──────────────────────────────────────────────────────

    async def _one(self, sql: str, params: tuple):
        async with self.db.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_organization(self, code: str, *, name: str | None = None) -> int:
        await self.db.execute(
            "INSERT INTO organizations (code, name) VALUES (?, ?) "
            "ON CONFLICT(code) DO UPDATE SET name = COALESCE(organizations.name, excluded.name)",
            (code, name),
        )
        await self.db.commit()
        return (await self._one("SELECT id FROM organizations WHERE code = ?", (code,)))["id"]

    async def upsert_department(self, organization_id: int, code: str) -> int:
        await self.db.execute(
            "INSERT OR IGNORE INTO departments (organization_id, code) VALUES (?, ?)",
            (organization_id, code),
        )
        await self.db.commit()
        row = await self._one(
            "SELECT id FROM departments WHERE organization_id = ? AND code = ?",
            (organization_id, code),
        )
        return row["id"]

    async def upsert_practitioner(
        self,
        *,
        patient_id: str,
        surname_token: str,
        display_name: str | None = None,
        organization_id: int | None = None,
        department_id: int | None = None,
    ) -> int:
        await self.db.execute(
            "INSERT INTO practitioners (patient_id, surname_token, display_name, "
            "organization_id, department_id) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(patient_id, surname_token) DO UPDATE SET "
            "display_name = COALESCE(excluded.display_name, practitioners.display_name), "
            "organization_id = COALESCE(practitioners.organization_id, excluded.organization_id), "
            "department_id = COALESCE(practitioners.department_id, excluded.department_id)",
            (patient_id, surname_token, display_name, organization_id, department_id),
        )
        await self.db.commit()
        row = await self._one(
            "SELECT id FROM practitioners WHERE patient_id = ? AND surname_token = ?",
            (patient_id, surname_token),
        )
        return row["id"]

    async def upsert_encounter(
        self,
        *,
        patient_id: str,
        natural_key: str,
        start_at: str,
        kind: str,
        title: str | None,
        organization_id: int | None,
        department_id: int | None,
        practitioner_id: int | None,
    ) -> int:
        await self.db.execute(
            "INSERT INTO encounters (patient_id, natural_key, start_at, kind, title, "
            "organization_id, department_id, practitioner_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(patient_id, natural_key) DO UPDATE SET deleted_at = NULL, "
            "practitioner_id = COALESCE(encounters.practitioner_id, excluded.practitioner_id), "
            f"updated_at = {_NOW}",
            (
                patient_id, natural_key, start_at, kind, title,
                organization_id, department_id, practitioner_id,
            ),
        )  # fmt: skip
        await self.db.commit()
        row = await self._one(
            "SELECT id FROM encounters WHERE patient_id = ? AND natural_key = ?",
            (patient_id, natural_key),
        )
        return row["id"]

    async def set_document_entities(
        self,
        doc_id: int,
        *,
        patient_id: str,
        title: str | None,
        source_url: str | None,
        encounter_id: int | None,
        practitioner_id: int | None,
        organization_id: int | None,
        department_id: int | None,
    ) -> None:
        await self.db.execute(
            "UPDATE documents SET title = ?, source_url = ?, encounter_id = ?, "
            "practitioner_id = ?, organization_id = ?, department_id = ? "
            "WHERE id = ? AND patient_id = ?",
            (
                title, source_url, encounter_id, practitioner_id, organization_id,
                department_id, doc_id, patient_id,
            ),
        )  # fmt: skip
        await self.db.commit()

    async def fact_code_vocabulary(self) -> list[dict]:
        async with self.db.execute(
            "SELECT code, category, name_sk, name_en, unit_canonical, aliases, unit_conversions "
            "FROM fact_codes"
        ) as cur:
            rows = _rows(await cur.fetchall())
        for r in rows:
            r["aliases"] = json.loads(r["aliases"] or "[]")
            r["unit_conversions"] = json.loads(r["unit_conversions"] or "{}")
        return rows

    # ── facts ─────────────────────────────────────────────────────────

    async def _audit(self, record_id: int, action: str, after: dict | None, reason: str) -> None:
        await self.db.execute(
            "INSERT INTO clinical_record_audit (record_id, action, after_json, reason, source, "
            "changed_by) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record_id, action,
                json.dumps(after, ensure_ascii=False, default=str) if after else None,
                reason, EXTRACT_SOURCE, "oncofiles",
            ),
        )  # fmt: skip

    async def replace_document_facts(
        self, *, patient_id: str, document_id: int, facts: list[dict], extracted_by: str
    ) -> dict:
        """Re-extraction of one document: soft-delete its machine facts, insert new ones.

        Facts a person verified or corrected are kept, and a new machine fact
        with the same code and time is not added next to them.
        """
        async with self.db.execute(
            "SELECT id, code, occurred_at, verification FROM clinical_records "
            "WHERE patient_id = ? AND source_document_id = ? AND deleted_at IS NULL",
            (patient_id, document_id),
        ) as cur:
            existing = _rows(await cur.fetchall())
        protected = {
            (r["code"], r["occurred_at"])
            for r in existing
            if r["verification"] in PROTECTED_VERIFICATION
        }
        removed = 0
        for r in existing:
            if r["verification"] in PROTECTED_VERIFICATION:
                continue
            await self.db.execute(
                f"UPDATE clinical_records SET deleted_at = {_NOW}, deleted_by = ? "
                "WHERE id = ? AND patient_id = ?",
                ("oncofiles", r["id"], patient_id),
            )
            await self._audit(r["id"], "delete", None, f"re-extraction by {extracted_by}")
            removed += 1
        added = 0
        for f in facts:
            if (f.get("code"), f.get("occurred_at")) in protected:
                continue
            cur = await self.db.execute(
                "INSERT INTO clinical_records (patient_id, record_type, source_document_id, "
                "occurred_at, param, value_num, value_text, unit, ref_range_low, ref_range_high, "
                "ref_text, code, page_number, evidence, value_canonical, unit_canonical, flag, "
                "encounter_id, practitioner_id, organization_id, department_id, verification, "
                "extracted_by, confidence, source, metadata_json) "
                f"VALUES ({', '.join('?' * 26)})",
                (
                    patient_id, f.get("category", "lab"), document_id, f.get("occurred_at"),
                    f.get("label"), f.get("value_num"), f.get("value_text"), f.get("unit"),
                    f.get("ref_low"), f.get("ref_high"), f.get("ref_text"), f.get("code"),
                    f.get("page_number"), f.get("evidence"), f.get("value_canonical"),
                    f.get("unit_canonical"), f.get("flag"), f.get("encounter_id"),
                    f.get("practitioner_id"), f.get("organization_id"), f.get("department_id"),
                    f.get("verification", "machine"), extracted_by, f.get("confidence"),
                    EXTRACT_SOURCE, json.dumps(f.get("metadata") or {}, ensure_ascii=False),
                ),
            )  # fmt: skip
            await self._audit(cur.lastrowid, "create", f, f"extracted by {extracted_by}")
            added += 1
        await self.db.commit()
        return {"added": added, "removed": removed, "kept_verified": len(protected)}

    async def fact_verification_rows(self, *, patient_id: str) -> list[dict]:
        """Active facts with what corroboration and the unverified metric need."""
        async with self.db.execute(
            "SELECT id, source_document_id, code, occurred_at, value_canonical, "
            "unit_canonical, verification, metadata_json FROM clinical_records "
            "WHERE patient_id = ? AND deleted_at IS NULL AND code IS NOT NULL",
            (patient_id,),
        ) as cur:
            return _rows(await cur.fetchall())

    async def corroborate_fact(
        self, *, patient_id: str, fact_id: int, occurred_at: str, metadata: dict
    ) -> bool:
        """Date a report-quoted value by the draw it quotes (#659). Only an
        unverified machine-extracted row changes; human-verified rows never."""
        cur = await self.db.execute(
            "UPDATE clinical_records SET occurred_at = ?, verification = 'machine', "
            f"metadata_json = ?, updated_at = {_NOW}, updated_by = 'oncofiles' "
            "WHERE id = ? AND patient_id = ? AND verification = 'unverified' "
            "AND deleted_at IS NULL",
            (occurred_at, json.dumps(metadata, ensure_ascii=False), fact_id, patient_id),
        )
        if cur.rowcount:
            await self._audit(fact_id, "update", metadata, "corroborated by another document")
        await self.db.commit()
        return bool(cur.rowcount)

    async def flag_date_conflict(self, *, patient_id: str, fact_id: int, metadata: dict) -> bool:
        """A machine fact whose header date conflicts with another document's
        identical draw becomes unverified (#672). Never touches human-verified
        or corrected rows."""
        cur = await self.db.execute(
            "UPDATE clinical_records SET verification = 'unverified', "
            f"metadata_json = ?, updated_at = {_NOW}, updated_by = 'oncofiles' "
            "WHERE id = ? AND patient_id = ? AND verification = 'machine' "
            "AND deleted_at IS NULL",
            (json.dumps(metadata, ensure_ascii=False), fact_id, patient_id),
        )
        if cur.rowcount:
            await self._audit(fact_id, "update", metadata, "date conflict with another document")
        await self.db.commit()
        return bool(cur.rowcount)

    # ── links (never across patients) ─────────────────────────────────

    async def _owned(self, entity: str, entity_id: int, patient_id: str) -> bool:
        if entity not in _LINK_TABLES:
            raise ValueError(f"unknown link entity type {entity!r}")
        table, col = _LINK_TABLES[entity]
        row = await self._one(
            f"SELECT 1 AS ok FROM {table} WHERE {col} = ? AND patient_id = ?",
            (entity_id, patient_id),
        )
        return row is not None

    async def add_link(
        self,
        *,
        patient_id: str,
        from_type: str,
        from_id: int,
        to_type: str,
        to_id: int,
        relation: str,
        source: str = "rule",
        confidence: float | None = None,
    ) -> None:
        if not (
            await self._owned(from_type, from_id, patient_id)
            and await self._owned(to_type, to_id, patient_id)
        ):
            raise ValueError("link ends must both belong to the same patient")
        await self.db.execute(
            "INSERT OR IGNORE INTO links (patient_id, from_type, from_id, to_type, to_id, "
            "relation, source, confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (patient_id, from_type, from_id, to_type, to_id, relation, source, confidence),
        )
        await self.db.commit()

    async def links_for(self, *, patient_id: str, entity: str, entity_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT from_type, from_id, to_type, to_id, relation, source, confidence FROM links "
            "WHERE patient_id = ? AND deleted_at IS NULL AND "
            "((from_type = ? AND from_id = ?) OR (to_type = ? AND to_id = ?))",
            (patient_id, entity, entity_id, entity, entity_id),
        ) as cur:
            return _rows(await cur.fetchall())

    # ── reads for the MCP tools (all patient-scoped) ──────────────────

    async def timeline_rows(
        self,
        *,
        patient_id: str,
        date_from: str | None = None,
        date_to: str | None = None,
        kinds: list[str] | None = None,
        limit: int = 200,
    ) -> list[dict]:
        sql = "SELECT * FROM timeline_v WHERE patient_id = ? AND at IS NOT NULL"
        params: list = [patient_id]
        if date_from:
            sql += " AND at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND at <= ?"
            params.append(date_to + "~")  # include the whole end day
        if kinds:
            sql += f" AND kind IN ({','.join('?' * len(kinds))})"
            params += kinds
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        async with self.db.execute(sql, tuple(params)) as cur:
            return _rows(await cur.fetchall())

    async def fact_catalog_rows(self, *, patient_id: str) -> list[dict]:
        async with self.db.execute(
            "SELECT code, MAX(name_en) AS name_en, MAX(name_sk) AS name_sk, "
            "MAX(category) AS category, COUNT(*) AS count, "
            "SUM(CASE WHEN verification = 'unverified' THEN 1 ELSE 0 END) AS unverified, "
            "MIN(effective_at) AS first_at, MAX(effective_at) AS last_at, "
            "MAX(label_original) AS label_example "
            "FROM fact_series_v WHERE patient_id = ? "
            "GROUP BY COALESCE(code, label_original) ORDER BY category, name_en",
            (patient_id,),
        ) as cur:
            return _rows(await cur.fetchall())

    async def fact_series_rows(
        self,
        *,
        patient_id: str,
        codes: list[str],
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> list[dict]:
        sql = (
            "SELECT s.*, d.title AS source_title, d.document_date AS source_date, "
            "d.source_url FROM fact_series_v s "
            "LEFT JOIN documents d ON d.id = s.source_document_id AND d.patient_id = s.patient_id "
            f"WHERE s.patient_id = ? AND s.code IN ({','.join('?' * len(codes))})"
        )
        params: list = [patient_id, *codes]
        if date_from:
            sql += " AND s.effective_at >= ?"
            params.append(date_from)
        if date_to:
            sql += " AND s.effective_at <= ?"
            params.append(date_to + "~")
        sql += " ORDER BY s.code, s.effective_at"
        async with self.db.execute(sql, tuple(params)) as cur:
            return _rows(await cur.fetchall())

    async def latest_fact_rows(self, *, patient_id: str) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM latest_fact_v WHERE patient_id = ? ORDER BY category, code",
            (patient_id,),
        ) as cur:
            return _rows(await cur.fetchall())

    async def facts_of_document(self, *, patient_id: str, document_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM fact_series_v WHERE patient_id = ? AND source_document_id = ? "
            "ORDER BY page_number, id",
            (patient_id, document_id),
        ) as cur:
            return _rows(await cur.fetchall())

    async def encounter_row(self, *, patient_id: str, encounter_id: int) -> dict | None:
        return await self._one(
            "SELECT e.*, o.code AS organization_code, o.name AS organization_name, "
            "dp.code AS department_code, p.display_name AS practitioner_name "
            "FROM encounters e LEFT JOIN organizations o ON o.id = e.organization_id "
            "LEFT JOIN departments dp ON dp.id = e.department_id "
            "LEFT JOIN practitioners p ON p.id = e.practitioner_id AND p.patient_id = e.patient_id "
            "WHERE e.id = ? AND e.patient_id = ? AND e.deleted_at IS NULL",
            (encounter_id, patient_id),
        )

    async def encounter_documents(self, *, patient_id: str, encounter_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT id, title, filename, category, document_date, source_url FROM documents "
            "WHERE patient_id = ? AND encounter_id = ? AND deleted_at IS NULL ORDER BY id",
            (patient_id, encounter_id),
        ) as cur:
            return _rows(await cur.fetchall())

    async def people_and_places_rows(self, *, patient_id: str) -> dict:
        async with self.db.execute(
            "SELECT p.id, p.surname_token, p.display_name, o.code AS organization, "
            "dp.code AS department, COUNT(d.id) AS documents, MIN(d.document_date) AS first_at, "
            "MAX(d.document_date) AS last_at FROM practitioners p "
            "LEFT JOIN organizations o ON o.id = p.organization_id "
            "LEFT JOIN departments dp ON dp.id = p.department_id "
            "LEFT JOIN documents d ON d.practitioner_id = p.id AND d.patient_id = p.patient_id "
            "AND d.deleted_at IS NULL WHERE p.patient_id = ? GROUP BY p.id ORDER BY documents DESC",
            (patient_id,),
        ) as cur:
            people = _rows(await cur.fetchall())
        async with self.db.execute(
            "SELECT o.code, o.name, COUNT(d.id) AS documents, MIN(d.document_date) AS first_at, "
            "MAX(d.document_date) AS last_at FROM documents d "
            "JOIN organizations o ON o.id = d.organization_id "
            "WHERE d.patient_id = ? AND d.deleted_at IS NULL GROUP BY o.id ORDER BY documents DESC",
            (patient_id,),
        ) as cur:
            places = _rows(await cur.fetchall())
        return {"practitioners": people, "organizations": places}

    async def document_category_counts(self, *, patient_id: str) -> dict:
        async with self.db.execute(
            "SELECT category, COUNT(*) AS n, MIN(document_date) AS first_at, "
            "MAX(document_date) AS last_at FROM documents "
            "WHERE patient_id = ? AND deleted_at IS NULL GROUP BY category ORDER BY n DESC",
            (patient_id,),
        ) as cur:
            return {r["category"]: dict(r) for r in _rows(await cur.fetchall())}

    async def document_row(self, *, patient_id: str, document_id: int) -> dict | None:
        return await self._one(
            "SELECT d.id, d.title, d.filename, d.category, d.document_date, d.institution, "
            "d.ai_summary, d.source_url, d.encounter_id, d.mime_type, d.structured_metadata, "
            "o.code AS organization, dp.code AS department, p.display_name AS practitioner "
            "FROM documents d LEFT JOIN organizations o ON o.id = d.organization_id "
            "LEFT JOIN departments dp ON dp.id = d.department_id "
            "LEFT JOIN practitioners p ON p.id = d.practitioner_id AND p.patient_id = d.patient_id "
            "WHERE d.id = ? AND d.patient_id = ? AND d.deleted_at IS NULL",
            (document_id, patient_id),
        )

    async def latest_any_rows(self, *, patient_id: str) -> list[dict]:
        """Newest value per code, whatever its verification (shown with it)."""
        async with self.db.execute(
            "SELECT s.* FROM fact_series_v s WHERE s.patient_id = ? AND s.code IS NOT NULL "
            "AND s.effective_at = (SELECT MAX(s2.effective_at) FROM fact_series_v s2 "
            "WHERE s2.patient_id = s.patient_id AND s2.code = s.code) "
            "ORDER BY s.category, s.code",
            (patient_id,),
        ) as cur:
            return _rows(await cur.fetchall())
