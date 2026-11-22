"""Quarantined inbound attachments and their action links (#950, migration 100).

Writers and readers live in ``oncofiles.inbound_quarantine``. Every helper
takes a keyword-only ``patient_id`` and filters on it in SQL, except the
token lookup by its keyed hash (the token IS the routing key of the e-mail
button, like ``find_inbound_address``) and the cross-patient sweeps of the
scheduler (expiry, due notifications). Nothing here deletes a row (#734).
"""

from __future__ import annotations

from ._base import DatabaseBase
from ._tenant import require_patient_id

QUARANTINE_STATES = ("pending", "approved", "rejected", "expired")
QUARANTINE_ACTIONS = ("approve", "reject")

_COLS = """id, patient_id, item_uuid, source_hash, sender_hash, sender_enc, files_enc,
           refusal_reason, file_count, total_bytes, state, received_at, notified_at,
           notice_key, claimed_at, decided_at, decided_by, decided_via, filed_count,
           restored_at"""


class InboundQuarantineMixin(DatabaseBase):
    async def quarantine_source_exists(self, source_hash: str, *, patient_id: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM inbound_quarantine WHERE patient_id = ? AND source_hash = ?",
            (patient_id, source_hash),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def quarantine_usage(self, *, patient_id: str, since: str, since_30d: str) -> dict:
        """Counts for the caps: items and files received since ``since``,
        bytes received since ``since_30d`` (every state), pending items and
        their bytes."""
        async with self.db.execute(
            """SELECT
                 COALESCE(SUM(CASE WHEN received_at >= ? THEN 1 ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN received_at >= ? THEN file_count ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN state = 'pending' THEN 1 ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN state = 'pending' THEN total_bytes ELSE 0 END), 0),
                 COALESCE(SUM(CASE WHEN received_at >= ? THEN total_bytes ELSE 0 END), 0)
               FROM inbound_quarantine WHERE patient_id = ?""",
            (since, since, since_30d, patient_id),
        ) as cursor:
            row = await cursor.fetchone()
        return {
            "items_today": int(row[0]),
            "files_today": int(row[1]),
            "pending_items": int(row[2]),
            "pending_bytes": int(row[3]),
            "received_bytes_30d": int(row[4]),
        }

    async def quarantine_pending_bytes_total(self) -> int:
        """Bytes of every PENDING item (all patients), for the quarantine's
        own global budget."""
        async with self.db.execute(
            "SELECT COALESCE(SUM(total_bytes), 0) FROM inbound_quarantine WHERE state = 'pending'"
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    async def quarantine_state_totals(self) -> dict:
        """{state: {items, files, bytes}} over all patients (admin overview)."""
        async with self.db.execute(
            """SELECT state, COUNT(*), COALESCE(SUM(file_count), 0),
                      COALESCE(SUM(total_bytes), 0)
               FROM inbound_quarantine GROUP BY state"""
        ) as cursor:
            rows = await cursor.fetchall()
        return {r[0]: {"items": int(r[1]), "files": int(r[2]), "bytes": int(r[3])} for r in rows}

    async def void_quarantine_tokens(self, *, patient_id: str, at: str) -> int:
        """Mark every unused action link of the patient used (the caregiver
        address changed: links sent to the old address must stop working)."""
        cursor = await self.db.execute(
            """UPDATE inbound_quarantine_tokens SET used_at = ?
               WHERE patient_id = ? AND used_at IS NULL""",
            (at, patient_id),
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)

    async def insert_quarantine_item(
        self,
        *,
        patient_id: str,
        item_uuid: str,
        source_hash: str,
        sender_hash: str,
        sender_enc: str,
        files_enc: str,
        refusal_reason: str,
        file_count: int,
        total_bytes: int,
    ) -> int | None:
        """Insert one pending item; None when the same delivery is already
        quarantined (``ON CONFLICT DO NOTHING``)."""
        pid = require_patient_id(patient_id)
        cursor = await self.db.execute(
            """INSERT INTO inbound_quarantine
                 (patient_id, item_uuid, source_hash, sender_hash, sender_enc, files_enc,
                  refusal_reason, file_count, total_bytes, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
               ON CONFLICT (patient_id, source_hash) DO NOTHING""",
            (
                pid,
                item_uuid,
                source_hash,
                sender_hash,
                sender_enc,
                files_enc,
                refusal_reason,
                int(file_count),
                int(total_bytes),
            ),
        )
        await self.db.commit()
        if not cursor.rowcount:
            return None
        return int(cursor.lastrowid)

    async def get_quarantine_item(self, item_id: int, *, patient_id: str) -> dict | None:
        async with self.db.execute(
            f"SELECT {_COLS} FROM inbound_quarantine WHERE id = ? AND patient_id = ?",
            (int(item_id), patient_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_quarantine_items(
        self, *, patient_id: str, state: str = "pending", limit: int = 50
    ) -> list[dict]:
        async with self.db.execute(
            f"""SELECT {_COLS} FROM inbound_quarantine
                WHERE patient_id = ? AND state = ?
                ORDER BY received_at DESC, id DESC LIMIT ?""",
            (patient_id, state, int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def count_quarantine_pending(self, *, patient_id: str) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM inbound_quarantine WHERE patient_id = ? AND state = 'pending'",
            (patient_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    # ── notifications ────────────────────────────────────────────────────

    async def quarantine_patients_due(self, *, since: str) -> list[str]:
        """Patients with pending items received since ``since`` that no
        notification announced yet (the scheduler's digest sweep)."""
        async with self.db.execute(
            """SELECT DISTINCT patient_id FROM inbound_quarantine
               WHERE state = 'pending' AND notified_at IS NULL AND received_at >= ?""",
            (since,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [r[0] for r in rows]

    async def quarantine_unnotified(self, *, patient_id: str, since: str) -> list[dict]:
        async with self.db.execute(
            f"""SELECT {_COLS} FROM inbound_quarantine
                WHERE patient_id = ? AND state = 'pending' AND notified_at IS NULL
                  AND received_at >= ?
                ORDER BY received_at, id""",
            (patient_id, since),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def quarantine_last_notice_for_sender(
        self, *, patient_id: str, sender_hash: str
    ) -> str | None:
        async with self.db.execute(
            """SELECT MAX(notified_at) FROM inbound_quarantine
               WHERE patient_id = ? AND sender_hash = ? AND notified_at IS NOT NULL""",
            (patient_id, sender_hash),
        ) as cursor:
            row = await cursor.fetchone()
        return row[0] if row and row[0] else None

    async def quarantine_notices_since(self, *, patient_id: str, since: str) -> int:
        """Notification e-mails sent for the patient since ``since``."""
        async with self.db.execute(
            """SELECT COUNT(DISTINCT notice_key) FROM inbound_quarantine
               WHERE patient_id = ? AND notified_at >= ? AND notice_key IS NOT NULL""",
            (patient_id, since),
        ) as cursor:
            row = await cursor.fetchone()
        return int(row[0] or 0)

    async def mark_quarantine_notified(
        self, item_ids: list[int], *, patient_id: str, at: str, notice_key: str
    ) -> int:
        """Claim the items for one notification (only those still unannounced)."""
        if not item_ids:
            return 0
        marks = ",".join("?" for _ in item_ids)
        cursor = await self.db.execute(
            f"""UPDATE inbound_quarantine SET notified_at = ?, notice_key = ?
                WHERE patient_id = ? AND notified_at IS NULL AND id IN ({marks})""",
            (at, notice_key, patient_id, *[int(i) for i in item_ids]),
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)

    async def clear_quarantine_notice(self, *, patient_id: str, notice_key: str) -> None:
        """A notification the relay refused: its items are announced again later.
        The tokens made for it were never sent; they stay unused and expire."""
        await self.db.execute(
            """UPDATE inbound_quarantine SET notified_at = NULL, notice_key = NULL
               WHERE patient_id = ? AND notice_key = ?""",
            (patient_id, notice_key),
        )
        await self.db.commit()

    # ── action tokens ────────────────────────────────────────────────────

    async def insert_quarantine_token(
        self,
        *,
        patient_id: str,
        quarantine_id: int,
        action: str,
        token_hash: str,
        binding_hash: str,
        recipient_hash: str,
        expires_at: str,
    ) -> None:
        if action not in QUARANTINE_ACTIONS:
            raise ValueError("unknown quarantine action")
        pid = require_patient_id(patient_id)
        await self.db.execute(
            """INSERT INTO inbound_quarantine_tokens
                 (patient_id, quarantine_id, action, token_hash, binding_hash,
                  recipient_hash, expires_at)
               SELECT ?, id, ?, ?, ?, ?, ? FROM inbound_quarantine
               WHERE id = ? AND patient_id = ?""",
            (
                pid,
                action,
                token_hash,
                binding_hash,
                recipient_hash,
                expires_at,
                int(quarantine_id),
                pid,
            ),
        )
        await self.db.commit()

    async def find_quarantine_token(self, token_hash: str) -> dict | None:
        """The token row with its item (any state), or None. The only lookup
        without a patient filter: the token is the routing key of the link."""
        async with self.db.execute(
            """SELECT t.id AS token_id, t.patient_id, t.quarantine_id, t.action,
                      t.binding_hash, t.recipient_hash, t.expires_at, t.used_at,
                      q.item_uuid, q.state, q.refusal_reason
               FROM inbound_quarantine_tokens t
               JOIN inbound_quarantine q ON q.id = t.quarantine_id
                                        AND q.patient_id = t.patient_id
               WHERE t.token_hash = ?""",
            (token_hash,),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def use_quarantine_token(self, token_id: int, *, patient_id: str, at: str) -> bool:
        """Mark a token used; False when it was already used (single use)."""
        cursor = await self.db.execute(
            """UPDATE inbound_quarantine_tokens SET used_at = ?
               WHERE id = ? AND patient_id = ? AND used_at IS NULL""",
            (at, int(token_id), patient_id),
        )
        await self.db.commit()
        return bool(cursor.rowcount)

    async def release_quarantine_token(self, token_id: int, *, patient_id: str, at: str) -> None:
        """Give a token back after an approval that could not finish now."""
        await self.db.execute(
            """UPDATE inbound_quarantine_tokens SET used_at = NULL
               WHERE id = ? AND patient_id = ? AND used_at = ?""",
            (int(token_id), patient_id, at),
        )
        await self.db.commit()

    # ── decisions ────────────────────────────────────────────────────────

    async def claim_quarantine_item(
        self, item_id: int, *, patient_id: str, now: str, stale_before: str
    ) -> bool:
        """Approval lease: a pending item not claimed (or claimed long ago)."""
        cursor = await self.db.execute(
            """UPDATE inbound_quarantine SET claimed_at = ?
               WHERE id = ? AND patient_id = ? AND state = 'pending'
                 AND (claimed_at IS NULL OR claimed_at < ?)""",
            (now, int(item_id), patient_id, stale_before),
        )
        await self.db.commit()
        return bool(cursor.rowcount)

    async def add_quarantine_filed(self, item_id: int, *, patient_id: str, filed: int) -> None:
        """A partial approve: count the files it filed (the item stays pending)."""
        await self.db.execute(
            """UPDATE inbound_quarantine SET filed_count = filed_count + ?
               WHERE id = ? AND patient_id = ? AND state = 'pending'""",
            (int(filed), int(item_id), patient_id),
        )
        await self.db.commit()

    async def release_quarantine_claim(self, item_id: int, *, patient_id: str, lease: str) -> None:
        await self.db.execute(
            """UPDATE inbound_quarantine SET claimed_at = NULL
               WHERE id = ? AND patient_id = ? AND claimed_at = ?""",
            (int(item_id), patient_id, lease),
        )
        await self.db.commit()

    async def decide_quarantine_item(
        self,
        item_id: int,
        *,
        patient_id: str,
        state: str,
        at: str,
        decided_by: str,
        decided_via: str,
        filed_count: int | None = None,
        lease: str | None = None,
        stale_before: str | None = None,
    ) -> bool:
        """pending → ``state``. With ``lease`` (approve) only under that claim;
        without it (reject) only while no fresh approval claim holds the item.
        ``filed_count`` None keeps the stored count (a reject after a partial
        approve)."""
        if state not in ("approved", "rejected"):
            raise ValueError("bad decision state")
        if lease is not None:
            cond, extra = "claimed_at = ?", (lease,)
        else:
            cond, extra = "(claimed_at IS NULL OR claimed_at < ?)", (stale_before or "",)
        cursor = await self.db.execute(
            f"""UPDATE inbound_quarantine
                SET state = ?, decided_at = ?, decided_by = ?, decided_via = ?,
                    filed_count = COALESCE(?, filed_count), claimed_at = NULL
                WHERE id = ? AND patient_id = ? AND state = 'pending' AND {cond}""",
            (
                state,
                at,
                decided_by,
                decided_via,
                None if filed_count is None else int(filed_count),
                int(item_id),
                patient_id,
                *extra,
            ),
        )
        await self.db.commit()
        return bool(cursor.rowcount)

    async def list_quarantine_trash(self, *, patient_id: str, since: str, limit: int = 50):
        """Rejected / expired items decided since ``since`` (the trash)."""
        async with self.db.execute(
            f"""SELECT {_COLS} FROM inbound_quarantine
                WHERE patient_id = ? AND state IN ('rejected', 'expired') AND decided_at >= ?
                ORDER BY decided_at DESC, id DESC LIMIT ?""",
            (patient_id, since, int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def restore_quarantine_item(
        self, item_id: int, *, patient_id: str, since: str, at: str
    ) -> bool:
        """Trash → pending (rejected / expired, decided since ``since``). Every
        unused e-mail link of the item is voided first: a restored item is
        decided in the dashboard only."""
        cursor = await self.db.execute(
            """UPDATE inbound_quarantine
               SET state = 'pending', decided_at = NULL, decided_by = NULL,
                   decided_via = NULL, restored_at = ?, claimed_at = NULL
               WHERE id = ? AND patient_id = ? AND state IN ('rejected', 'expired')
                 AND decided_at >= ?""",
            (at, int(item_id), patient_id, since),
        )
        if cursor.rowcount:
            await self.db.execute(
                """UPDATE inbound_quarantine_tokens SET used_at = ?
                   WHERE patient_id = ? AND quarantine_id = ? AND used_at IS NULL""",
                (at, patient_id, int(item_id)),
            )
        await self.db.commit()
        return bool(cursor.rowcount)

    async def expire_quarantine_items(self, *, before: str, stale_before: str, at: str) -> int:
        """Mark pending items received (or restored) before ``before`` expired;
        ``decided_at`` = ``at`` starts their trash period. Nothing deleted."""
        cursor = await self.db.execute(
            """UPDATE inbound_quarantine
               SET state = 'expired', decided_at = ?, decided_by = NULL, decided_via = 'expiry'
               WHERE state = 'pending' AND COALESCE(restored_at, received_at) < ?
                 AND (claimed_at IS NULL OR claimed_at < ?)""",
            (at, before, stale_before),
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)
