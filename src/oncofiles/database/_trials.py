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

# #999: the job predicates live in one import-free module, shared with the
# read-only dry run (oncofiles.job_dryrun), so its counts cannot drift.
from oncofiles.holding_sql import (
    HOLDING_DUE_FOR_READING_FROM,
    HOLDING_MOVED_COUNT_SQL,
    HOLDING_ROWS_TO_MOVE_SQL,
    SUPERSEDED_CANDIDATES_SQL,
    TRIALS_AWAITING_READY_MAIL_SQL,
    TRIALS_TO_CONVERT_SQL,
)

from ._base import DatabaseBase

TRIAL_SOURCES = ("welcome_email",)
HOLDING_PREFIX = "holding:"
_TRIAL_COLUMNS = (
    "id, patient_id, source, trial_ends_at, created_at, drive_connected_at, "
    "converted_at, first_reply_at, signed_in_at, ready_mail_sent_at, move_claimed_at"
)
# A holding row is written by the mover only while its trial's lease is the
# run's own (one parameter: the lease).
_LEASE_HOLDS_SQL = (
    "AND EXISTS (SELECT 1 FROM trial_accounts t "
    "WHERE t.patient_id = documents.patient_id AND t.move_claimed_at = ?)"
)
# #950 phase 3: the once-only trial instants a caller may set (allowlist, the
# column name is interpolated into SQL).
TRIAL_EVENT_COLUMNS = (
    "drive_connected_at",
    "first_reply_at",
    "signed_in_at",
    "ready_mail_sent_at",
)


class TrialsMixin(DatabaseBase):
    async def get_trial_by_sender_hash(self, sender_hash: str) -> dict | None:
        """The trial of one sender (system-wide: the hash IS the lookup key)."""
        async with self.db.execute(
            f"SELECT {_TRIAL_COLUMNS} FROM trial_accounts WHERE sender_hash = ?",
            (sender_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_trial(self, *, patient_id: str) -> dict | None:
        """The patient's trial row (id, source, trial_ends_at, created_at), or None."""
        async with self.db.execute(
            f"SELECT {_TRIAL_COLUMNS} FROM trial_accounts WHERE patient_id = ?",
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

    async def count_documents_since(self, *, patient_id: str, since: str) -> int:
        """Documents of a patient created since ``since``, every file id and
        state (the welcome@ per-sender file cap)."""
        async with self.db.execute(
            "SELECT COUNT(*) FROM documents WHERE patient_id = ? AND created_at >= ?",
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
            + HOLDING_DUE_FOR_READING_FROM
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
        """Replace ``structured_metadata.holding`` of one holding document in ONE
        UPDATE. With ``claimed_at`` only while that claim still holds: the
        reader's lease is part of the WHERE, so a document taken over as stale
        is never overwritten. False = not written."""
        sql = (
            "UPDATE documents SET structured_metadata = json_set(structured_metadata, "
            "'$.holding', json(?)), updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND patient_id = ? AND file_id LIKE 'holding:%' "
            "AND json_valid(structured_metadata)"
        )
        params: list = [json.dumps(holding, sort_keys=True), doc_id, patient_id]
        if claimed_at is not None:
            sql += " AND json_extract(structured_metadata, '$.holding.claimed_at') = ?"
            params.append(claimed_at)
        cursor = await self.db.execute(sql, params)
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def refresh_holding_claim(
        self, doc_id: int, *, patient_id: str, claimed_at: str, new_claimed_at: str
    ) -> bool:
        """Move the reader's lease forward, conditional on the current one, so a
        long document is never taken over as stale while it is being read."""
        cursor = await self.db.execute(
            "UPDATE documents SET structured_metadata = json_set(structured_metadata, "
            "'$.holding.claimed_at', ?) WHERE id = ? AND patient_id = ? "
            "AND file_id LIKE 'holding:%' AND json_valid(structured_metadata) "
            "AND json_extract(structured_metadata, '$.holding.extract') = 'reading' "
            "AND json_extract(structured_metadata, '$.holding.claimed_at') = ?",
            (new_claimed_at, doc_id, patient_id, claimed_at),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    # ── phase 3: holding -> Drive, ready mail, funnel (#950) ─────────────

    async def note_trial_event(self, column: str, *, patient_id: str, at: str) -> bool:
        """Set one once-only trial instant (``TRIAL_EVENT_COLUMNS``) when still
        unset, in ONE conditional UPDATE. True = this call set it (so a caller
        can claim a once-per-trial action with it); False = no trial or set."""
        if column not in TRIAL_EVENT_COLUMNS:
            raise ValueError(f"not a trial event column: {column}")
        cursor = await self.db.execute(
            f"UPDATE trial_accounts SET {column} = ? WHERE patient_id = ? AND {column} IS NULL",
            (at, patient_id),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def clear_trial_event(self, column: str, *, patient_id: str, at: str) -> None:
        """Undo a claim made by :meth:`note_trial_event` (the claimed action
        failed, e.g. the relay refused the ready mail), only if still ``at``."""
        if column not in TRIAL_EVENT_COLUMNS:
            raise ValueError(f"not a trial event column: {column}")
        await self.db.execute(
            f"UPDATE trial_accounts SET {column} = NULL WHERE patient_id = ? AND {column} = ?",
            (patient_id, at),
        )
        await self.db.commit()

    async def list_trials_to_convert(self) -> list[str]:
        """Patient ids of trials that are not converted yet and have a connected
        Drive folder. System-wide on purpose (the mover serves every trial;
        ids only, no patient data)."""
        async with self.db.execute(TRIALS_TO_CONVERT_SQL) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def holding_rows_to_move(self, *, patient_id: str) -> list[dict]:
        """Active holding documents of a patient without a verified Drive copy
        yet, oldest first (id, file_id, filename, original_filename, mime_type,
        structured_metadata)."""
        async with self.db.execute(HOLDING_ROWS_TO_MOVE_SQL, (patient_id,)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    # The mover's per-trial lease (the inbound_jobs pattern): every write of
    # the mover below is conditional on ``trial_accounts.move_claimed_at`` still
    # being this run's lease, so a run whose claim was taken over as stale (a
    # second container of a rolling deploy) changes nothing more.

    async def claim_trial_move(self, *, patient_id: str, lease: str, stale_before: str) -> bool:
        """Claim the trial for one mover run in ONE conditional UPDATE: free,
        or a claim older than ``stale_before`` (a run that died). False = held
        by a live run (or converted). ``lease`` starts with the ISO time, so
        the stale check compares it as text."""
        cursor = await self.db.execute(
            "UPDATE trial_accounts SET move_claimed_at = ? WHERE patient_id = ? "
            "AND converted_at IS NULL "
            "AND (move_claimed_at IS NULL OR move_claimed_at < ?)",
            (lease, patient_id, stale_before),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def refresh_trial_move(self, *, patient_id: str, lease: str, new_lease: str) -> bool:
        """Move the lease forward, only while ``lease`` still holds."""
        cursor = await self.db.execute(
            "UPDATE trial_accounts SET move_claimed_at = ? "
            "WHERE patient_id = ? AND move_claimed_at = ?",
            (new_lease, patient_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def release_trial_move(self, *, patient_id: str, lease: str) -> None:
        """End of the run: free the trial, only if the lease is still ours."""
        await self.db.execute(
            "UPDATE trial_accounts SET move_claimed_at = NULL "
            "WHERE patient_id = ? AND move_claimed_at = ?",
            (patient_id, lease),
        )
        await self.db.commit()

    async def convert_trial(self, *, patient_id: str, lease: str, at: str) -> bool:
        """Set ``converted_at`` once, only while the mover's ``lease`` holds."""
        cursor = await self.db.execute(
            "UPDATE trial_accounts SET converted_at = ? WHERE patient_id = ? "
            "AND converted_at IS NULL AND move_claimed_at = ?",
            (at, patient_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def mark_holding_moved(
        self, doc_id: int, *, patient_id: str, drive_id: str, at: str, via: str, lease: str
    ) -> bool:
        """Record ``holding.moved_to_drive = {drive_id, at, via}`` once (a row
        already marked is left as it is), only while the mover's ``lease``
        holds. ``via``: ``copied`` (written and read back by size + md5) or
        ``present`` (the same md5 was already in the inbox or the filed tree).
        Clears a previous ``move_error``. False = not written."""
        cursor = await self.db.execute(
            "UPDATE documents SET structured_metadata = json_remove(json_set(structured_metadata, "
            "'$.holding.moved_to_drive', json(?)), '$.holding.move_error'), "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') "
            "WHERE id = ? AND patient_id = ? AND file_id LIKE 'holding:%' "
            "AND json_valid(structured_metadata) "
            "AND json_extract(structured_metadata, '$.holding.moved_to_drive') IS NULL "
            f"{_LEASE_HOLDS_SQL}",
            (
                json.dumps({"drive_id": drive_id, "at": at, "via": via}, sort_keys=True),
                doc_id,
                patient_id,
                lease,
            ),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def note_holding_move_error(
        self, doc_id: int, *, patient_id: str, code: str, lease: str
    ) -> bool:
        """A failed copy: ``holding.move_error`` = code, ``move_attempts`` + 1,
        only while the mover's ``lease`` holds. The held copy is untouched; a
        later run tries again. False = not written."""
        cursor = await self.db.execute(
            "UPDATE documents SET structured_metadata = json_set(structured_metadata, "
            "'$.holding.move_error', ?, '$.holding.move_attempts', "
            "COALESCE(CAST(json_extract(structured_metadata, '$.holding.move_attempts') "
            "AS INTEGER), 0) + 1) "
            "WHERE id = ? AND patient_id = ? AND file_id LIKE 'holding:%' "
            f"AND json_valid(structured_metadata) {_LEASE_HOLDS_SQL}",
            (code, doc_id, patient_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def mark_superseded_holding_rows(self) -> int:
        """Flag moved holding rows whose curated mirror row now exists
        (``holding.superseded_by`` = the mirror's id), matched by the Drive id
        of the copy or by the source md5. The flag hides the held row from
        the document lists (``HOLDING_VISIBLE_SQL``); nothing is deleted.
        System-wide on purpose; returns how many rows were flagged."""
        async with self.db.execute(SUPERSEDED_CANDIDATES_SQL) as cursor:
            rows = [tuple(r) for r in await cursor.fetchall()]
        for doc_id, pid, mirror_id in rows:
            await self.db.execute(
                "UPDATE documents SET structured_metadata = json_set(structured_metadata, "
                "'$.holding.superseded_by', ?) WHERE id = ? AND patient_id = ? "
                "AND file_id LIKE 'holding:%' AND json_valid(structured_metadata)",
                (int(mirror_id), doc_id, pid),
            )
        if rows:
            await self.db.commit()
        return len(rows)

    async def holding_moved_count(self, *, patient_id: str) -> int:
        """Active holding documents of a patient with a verified Drive copy."""
        async with self.db.execute(HOLDING_MOVED_COUNT_SQL, (patient_id,)) as cursor:
            return int((await cursor.fetchone())[0])

    async def trial_funnel_rows(self) -> list[dict]:
        """Every trial's funnel instants, for the admin overview (system-wide,
        instants only: no id, address, name or file). ``received_at`` is the
        trial's ``created_at`` (the first admitted mail), ``first_doc_ready_at``
        the earliest ``extracted_at`` of a read holding document and
        ``first_mcp_call_at`` the earliest audited MCP tool call of the patient
        (``activity_log``, agent ``auto``)."""
        async with self.db.execute(
            "SELECT t.created_at AS received_at, t.first_reply_at, t.signed_in_at, "
            "  (SELECT MIN(json_extract(d.structured_metadata, '$.holding.extracted_at')) "
            "     FROM documents d WHERE d.patient_id = t.patient_id "
            "     AND d.file_id LIKE 'holding:%' AND json_valid(d.structured_metadata) "
            "     AND json_extract(d.structured_metadata, '$.holding.extract') = 'done') "
            "    AS first_doc_ready_at, "
            "  (SELECT MIN(a.created_at) FROM activity_log a "
            "     WHERE a.patient_id = t.patient_id AND a.agent_id = 'auto') "
            "    AS first_mcp_call_at, "
            "  t.drive_connected_at, t.converted_at, t.ready_mail_sent_at "
            "FROM trial_accounts t ORDER BY t.id"
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def trials_awaiting_ready_mail(self) -> list[dict]:
        """Trials due for the one "ready" e-mail (#950 phase 3): not sent yet,
        at least one held document read (``done``), none still waiting to be
        read by the holding reader (``pending`` / ``reading`` and not yet
        copied into Drive) and no mail of the patient still queued or being
        filed (#952 ``inbound_jobs``). System-wide; ``patient_id`` + ``done``."""
        async with self.db.execute(TRIALS_AWAITING_READY_MAIL_SQL) as cursor:
            return [{"patient_id": r[0], "done": int(r[1] or 0)} for r in await cursor.fetchall()]
