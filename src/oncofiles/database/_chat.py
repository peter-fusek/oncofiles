"""Encrypted per-patient chat history (#1022, migration 106).

Raw rows only: the text arrives here already sealed (``body_enc``) and leaves
sealed. Encryption, validation and decryption live in
``oncofiles.chat_history`` (the storage API the chat core and the dashboard
call); nothing else writes or reads ``chat_messages``.

Every patient helper takes a keyword-only ``patient_id``, passes it through
the #791 tenant guard on writes and filters on it in SQL, so a foreign patient
or conversation simply matches no row ("not found"). The two cross-patient
statements are the retention sweeps of the ``chat_history_retention`` loop
(``oncofiles.chat_retention``), whose predicates are ``chat_retention_sql``.
Nothing here hard-deletes a row (#734): the user's delete and the retention's
step one set ``deleted_at`` (the trash), step two only NULLs the content.
"""

from __future__ import annotations

from oncofiles import chat_retention_sql as q

from ._base import DatabaseBase
from ._tenant import require_patient_id

# vocab domains ``chat_channel`` / ``chat_role`` (oncofiles.vocab); the table's
# CHECK constraints hold the same values.
CHAT_CHANNELS = ("dashboard", "signal", "whatsapp")
CHAT_ROLES = ("user", "assistant")

_MSG_COLS = """id, conversation_id, channel, role, row_uuid, body_enc, citations_json, model,
               grounding_json, created_at, deleted_at, purged_at"""


class ChatHistoryMixin(DatabaseBase):
    async def chat_conversation_owner_mismatch(
        self, conversation_id: str, *, patient_id: str
    ) -> bool:
        """True when ``conversation_id`` already holds rows of ANOTHER patient."""
        async with self.db.execute(
            "SELECT 1 FROM chat_messages WHERE conversation_id = ? AND patient_id != ? LIMIT 1",
            (conversation_id, patient_id),
        ) as cursor:
            return await cursor.fetchone() is not None

    async def add_chat_message(
        self,
        *,
        patient_id: str,
        conversation_id: str,
        channel: str,
        role: str,
        row_uuid: str,
        body_enc: bytes,
        citations_json: str | None,
        model: str | None,
        grounding_json: str | None,
        caller_email_hash: str | None,
        created_at: str | None = None,
    ) -> int:
        """Insert one sealed message; returns its id. ValueError on an unknown
        channel / role, a missing patient, or a conversation of another patient."""
        pid = require_patient_id(patient_id)
        if channel not in CHAT_CHANNELS:
            raise ValueError(f"unknown chat channel {channel!r}")
        if role not in CHAT_ROLES:
            raise ValueError(f"unknown chat role {role!r}")
        if await self.chat_conversation_owner_mismatch(conversation_id, patient_id=pid):
            raise ValueError("conversation belongs to another patient")
        cursor = await self.db.execute(
            """INSERT INTO chat_messages
                 (patient_id, conversation_id, channel, role, row_uuid, body_enc,
                  citations_json, model, grounding_json, caller_email_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                       COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))""",
            (
                pid,
                conversation_id,
                channel,
                role,
                row_uuid,
                body_enc,
                citations_json,
                model,
                grounding_json,
                caller_email_hash,
                created_at,
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def get_chat_conversation_messages(
        self, *, patient_id: str, conversation_id: str, limit: int = 20
    ) -> list[dict]:
        """The LAST ``limit`` live (not trashed) messages of one conversation of
        one patient, oldest first. A foreign patient or conversation → []."""
        limit = max(1, min(int(limit), 500))
        async with self.db.execute(
            f"""SELECT {_MSG_COLS} FROM chat_messages
                WHERE patient_id = ? AND conversation_id = ?
                  AND deleted_at IS NULL AND purged_at IS NULL
                ORDER BY id DESC LIMIT ?""",
            (patient_id, conversation_id, limit),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        rows.reverse()
        return rows

    async def get_chat_first_user_message(
        self, *, patient_id: str, conversation_id: str, trashed: bool = False
    ) -> dict | None:
        """The first question of a conversation (its title in the list)."""
        state = "deleted_at IS NOT NULL" if trashed else "deleted_at IS NULL"
        async with self.db.execute(
            f"""SELECT {_MSG_COLS} FROM chat_messages
                WHERE patient_id = ? AND conversation_id = ? AND role = 'user'
                  AND purged_at IS NULL AND {state}
                ORDER BY id LIMIT 1""",
            (patient_id, conversation_id),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_chat_conversations(
        self, *, patient_id: str, trashed: bool = False, limit: int = 50
    ) -> list[dict]:
        """Conversations of one patient, newest first: id, channel, counts and
        times, no text. ``trashed`` lists the trash (restorable) instead."""
        limit = max(1, min(int(limit), 500))
        state = "deleted_at IS NOT NULL" if trashed else "deleted_at IS NULL"
        async with self.db.execute(
            f"""SELECT conversation_id, MIN(channel) AS channel, COUNT(*) AS messages,
                       MIN(created_at) AS started_at, MAX(created_at) AS last_at,
                       MAX(deleted_at) AS deleted_at
                FROM chat_messages
                WHERE patient_id = ? AND purged_at IS NULL AND {state}
                GROUP BY conversation_id
                ORDER BY MAX(id) DESC LIMIT ?""",
            (patient_id, limit),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def soft_delete_chat_conversation(
        self, *, patient_id: str, conversation_id: str, at: str
    ) -> int:
        """The user's delete: every live row of the conversation moves to the
        trash. Returns the rows moved (0 = not found / foreign / already)."""
        pid = require_patient_id(patient_id)
        cursor = await self.db.execute(
            """UPDATE chat_messages SET deleted_at = ?, deleted_by = ?
               WHERE patient_id = ? AND conversation_id = ?
                 AND deleted_at IS NULL AND purged_at IS NULL""",
            (at, q.USER_DELETED_BY, pid, conversation_id),
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)

    async def restore_chat_conversation(
        self, *, patient_id: str, conversation_id: str, at: str
    ) -> int:
        """Back from the trash (rows not yet purged); the 90 days then count
        from ``at``. Returns the rows restored (0 = not found / purged)."""
        pid = require_patient_id(patient_id)
        cursor = await self.db.execute(
            """UPDATE chat_messages SET deleted_at = NULL, deleted_by = NULL, restored_at = ?
               WHERE patient_id = ? AND conversation_id = ?
                 AND deleted_at IS NOT NULL AND purged_at IS NULL""",
            (at, pid, conversation_id),
        )
        await self.db.commit()
        return int(cursor.rowcount or 0)

    # ── retention (cross-patient sweeps of the chat_history_retention loop) ──

    async def count_chat_retention(self, *, trash_cutoff: str, purge_cutoff: str) -> dict:
        """Rows step 1 would trash and step 2 would purge (report mode)."""
        out = {}
        for key, where, cut in (
            ("would_trash", q.TRASH_WHERE, trash_cutoff),
            ("would_purge", q.PURGE_WHERE, purge_cutoff),
        ):
            async with self.db.execute(
                f"SELECT COUNT(*) FROM chat_messages WHERE {where}", (cut,)
            ) as cursor:
                out[key] = int((await cursor.fetchone())[0] or 0)
        return out

    async def trash_expired_chat_messages(self, *, now: str, trash_cutoff: str) -> int:
        """Step 1: live rows past the 90 days → trash, in chunks, committed each."""
        total = 0
        while True:
            cursor = await self.db.execute(
                q.trash_sql(), (now, q.RETENTION_DELETED_BY, trash_cutoff, q.CHUNK_ROWS)
            )
            n = int(cursor.rowcount or 0)
            await self.db.commit()
            total += n
            if n < q.CHUNK_ROWS:
                return total

    async def purge_trashed_chat_messages(self, *, now: str, purge_cutoff: str) -> int:
        """Step 2: rows 30 days in the trash lose their content (row kept)."""
        total = 0
        while True:
            cursor = await self.db.execute(q.purge_sql(), (now, purge_cutoff, q.CHUNK_ROWS))
            n = int(cursor.rowcount or 0)
            await self.db.commit()
            total += n
            if n < q.CHUNK_ROWS:
                return total
