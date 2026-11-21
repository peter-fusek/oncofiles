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

    async def count_inbound_jobs_since(self, *, patient_id: str, since: str) -> int:
        """Queued mails of a patient since ``since`` (every state): the welcome@
        per-sender mail cap, one trial per sender, durable across deploys."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM inbound_jobs WHERE patient_id = ? AND created_at >= ?",
            (patient_id, since),
        ) as cursor:
            return int((await cursor.fetchone())[0])

    # ── holding-store documents ─────────────────────────────────────────

    async def count_holding_documents_since(self, *, patient_id: str, since: str) -> int:
        """Holding documents a patient got since ``since`` (the per-sender file cap)."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM documents WHERE patient_id = ? "
            "AND file_id LIKE 'holding:%' AND created_at >= ?",
            (patient_id, since),
        ) as cursor:
            return int((await cursor.fetchone())[0])

    async def holding_bytes(self, *, patient_id: str | None = None) -> int:
        """Bytes in the holding store: one patient's, or all (``patient_id=None``,
        the global cap; system-wide on purpose, a number only). Soft-deleted rows
        count too: their encrypted files stay on disk (#734)."""
        sql = "SELECT COALESCE(SUM(size_bytes), 0) FROM documents WHERE file_id LIKE 'holding:%'"
        params: tuple = ()
        if patient_id is not None:
            sql += " AND patient_id = ?"
            params = (patient_id,)
        async with self.db.execute(sql, params) as cursor:
            return int((await cursor.fetchone())[0] or 0)

    async def next_holding_extraction(
        self, *, exclude_patients: list[str], now: str, stale_before: str
    ) -> dict | None:
        """The oldest active holding document that may be read now.

        System-wide on purpose (the reader serves every trial; each row carries
        its own ``patient_id``). A document is due when it is ``pending``, or
        ``reading`` with a claim older than ``stale_before`` (a reader killed
        mid-way), and its trial has not ended (``trial_ends_at > now``: after
        the end nothing is read). ``exclude_patients`` are skipped (over a
        budget, or retried later, in this run).
        """
        sql = (
            "SELECT d.id, d.patient_id, d.file_id, d.mime_type, d.structured_metadata "
            "FROM documents d JOIN trial_accounts t ON t.patient_id = d.patient_id "
            "WHERE d.file_id LIKE 'holding:%' AND d.deleted_at IS NULL "
            "AND t.trial_ends_at > ? AND json_valid(d.structured_metadata) "
            "AND (json_extract(d.structured_metadata, '$.holding.extract') = 'pending' "
            "  OR (json_extract(d.structured_metadata, '$.holding.extract') = 'reading' "
            "      AND COALESCE(json_extract(d.structured_metadata, '$.holding.claimed_at'), '')"
            "          < ?))"
        )
        params: list = [now, stale_before]
        if exclude_patients:
            sql += f" AND d.patient_id NOT IN ({','.join('?' for _ in exclude_patients)})"
            params.extend(exclude_patients)
        sql += " ORDER BY d.id LIMIT 1"
        async with self.db.execute(sql, params) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def claim_holding_extraction(
        self, doc_id: int, *, patient_id: str, claimed_at: str, stale_before: str
    ) -> bool:
        """Conditional claim (the #952 queue pattern): ``pending`` (or a stale
        ``reading``) → ``reading`` with ``claimed_at``, in ONE UPDATE, so two
        containers of a rolling deploy never read the same file. False = lost."""
        cursor = await self.db.execute(
            "UPDATE documents SET structured_metadata = json_set(structured_metadata, "
            "'$.holding.extract', 'reading', '$.holding.claimed_at', ?) "
            "WHERE id = ? AND patient_id = ? AND file_id LIKE 'holding:%' "
            "AND json_valid(structured_metadata) "
            "AND (json_extract(structured_metadata, '$.holding.extract') = 'pending' "
            "  OR (json_extract(structured_metadata, '$.holding.extract') = 'reading' "
            "      AND COALESCE(json_extract(structured_metadata, '$.holding.claimed_at'), '')"
            "          < ?))",
            (claimed_at, doc_id, patient_id, stale_before),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def holding_model_pages_since(self, *, patient_id: str | None, since: str) -> int:
        """Model pages the holding reader read since ``since``: one patient's,
        or all trials together (``patient_id=None``, the global daily cap)."""
        sql = (
            "SELECT COALESCE(SUM(CAST(json_extract(structured_metadata, "
            "'$.holding.model_pages') AS INTEGER)), 0) FROM documents "
            "WHERE file_id LIKE 'holding:%' AND json_valid(structured_metadata) "
            "AND json_extract(structured_metadata, '$.holding.extracted_at') >= ?"
        )
        params: tuple = (since,)
        if patient_id is not None:
            sql += " AND patient_id = ?"
            params = (since, patient_id)
        async with self.db.execute(sql, params) as cursor:
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

    async def set_holding_state(
        self, doc_id: int, *, patient_id: str, holding: dict, claimed_at: str | None = None
    ) -> bool:
        """Replace ``structured_metadata.holding`` of one holding document.

        With ``claimed_at`` only while that claim still holds (the reader's
        lease): a document taken over as stale is not overwritten. False = not
        written.
        """
        async with self.db.execute(
            "SELECT structured_metadata FROM documents "
            "WHERE id = ? AND patient_id = ? AND file_id LIKE 'holding:%'",
            (doc_id, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return False
        try:
            meta = json.loads(row[0] or "{}")
        except ValueError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        if claimed_at is not None and (meta.get("holding") or {}).get("claimed_at") != claimed_at:
            return False
        meta["holding"] = holding
        await self.db.execute(
            "UPDATE documents SET structured_metadata = ?, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND patient_id = ?",
            (json.dumps(meta, sort_keys=True), doc_id, patient_id),
        )
        await self.db.commit()
        return True
