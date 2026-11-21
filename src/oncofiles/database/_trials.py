"""Trial accounts and holding-store documents (#950 phase 2, migration 097).

``trial_accounts`` holds one row per welcome@ sender (keyed sender hash, never
the address). The holding-store documents are ordinary ``documents`` rows
with ``file_id = "holding:<uuid>"`` (``oncofiles.holding_store``); their state
lives in ``structured_metadata.holding``.

No helper deletes anything (#734). TODO(#950): the end-of-trial purge of an
unclaimed trial needs a RUNBOOK lifecycle entry and a reviewed entry in
``tests/test_no_automatic_deletion.py`` (maintainer decision) first.
"""

from __future__ import annotations

import json

from ._base import DatabaseBase

TRIAL_SOURCES = ("welcome_email",)
HOLDING_PREFIX = "holding:"


class TrialsMixin(DatabaseBase):
    async def get_trial_by_sender_hash(self, sender_hash: str) -> dict | None:
        """The trial of one sender (system-wide: the hash IS the lookup key)."""
        async with self.db.execute(
            """SELECT id, patient_id, source, trial_ends_at, created_at
               FROM trial_accounts WHERE sender_hash = ?""",
            (sender_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_trial(self, *, patient_id: str) -> dict | None:
        """The patient's trial row (id, source, trial_ends_at, created_at), or None."""
        async with self.db.execute(
            """SELECT id, patient_id, source, trial_ends_at, created_at
               FROM trial_accounts WHERE patient_id = ?""",
            (patient_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_trials_since(self, since: str) -> int:
        """New trials created at or after ``since`` (the global daily cap)."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM trial_accounts WHERE created_at >= ?", (since,)
        ) as cursor:
            return int((await cursor.fetchone())[0])

    # ── holding-store documents ─────────────────────────────────────────

    async def next_holding_extraction(self, *, exclude_patients: list[str]) -> dict | None:
        """The oldest active holding document still waiting for its reading.

        System-wide on purpose (the extraction worker serves every patient;
        each row carries its own ``patient_id``). ``exclude_patients`` are
        skipped (over their daily page budget in this run).
        """
        sql = (
            "SELECT id, patient_id, file_id, mime_type, structured_metadata FROM documents "
            "WHERE file_id LIKE 'holding:%' AND deleted_at IS NULL "
            "AND json_valid(structured_metadata) "
            "AND json_extract(structured_metadata, '$.holding.extract') = 'pending'"
        )
        params: list = []
        if exclude_patients:
            sql += f" AND patient_id NOT IN ({','.join('?' for _ in exclude_patients)})"
            params.extend(exclude_patients)
        sql += " ORDER BY id LIMIT 1"
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def holding_model_pages_since(self, *, patient_id: str, since: str) -> int:
        """Model pages the holding extraction read for a patient since ``since``."""
        async with self.db.execute(
            "SELECT COALESCE(SUM(CAST(json_extract(structured_metadata, "
            "'$.holding.model_pages') AS INTEGER)), 0) FROM documents "
            "WHERE patient_id = ? AND file_id LIKE 'holding:%' "
            "AND json_valid(structured_metadata) "
            "AND json_extract(structured_metadata, '$.holding.extracted_at') >= ?",
            (patient_id, since),
        ) as cursor:
            return int((await cursor.fetchone())[0] or 0)

    async def holding_document_counts(self, *, patient_id: str) -> dict[str, int]:
        """Active holding documents of a patient per extraction state."""
        async with self.db.execute(
            "SELECT structured_metadata FROM documents "
            "WHERE patient_id = ? AND file_id LIKE 'holding:%' AND deleted_at IS NULL",
            (patient_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        counts: dict[str, int] = {}
        for r in rows:
            try:
                state = (json.loads(r[0] or "{}").get("holding") or {}).get("extract")
            except (TypeError, ValueError, AttributeError):
                state = None
            key = str(state or "unknown")
            counts[key] = counts.get(key, 0) + 1
        return counts

    async def set_holding_state(self, doc_id: int, *, patient_id: str, holding: dict) -> None:
        """Replace ``structured_metadata.holding`` of one holding document."""
        async with self.db.execute(
            "SELECT structured_metadata FROM documents "
            "WHERE id = ? AND patient_id = ? AND file_id LIKE 'holding:%'",
            (doc_id, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        try:
            meta = json.loads(row[0] or "{}")
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["holding"] = holding
        await self.db.execute(
            "UPDATE documents SET structured_metadata = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND patient_id = ?",
            (json.dumps(meta, sort_keys=True), doc_id, patient_id),
        )
        await self.db.commit()
