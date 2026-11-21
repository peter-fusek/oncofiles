"""The "claimed" bucket of the Brevo inbound route (#950, migration 099).

A mail whose From only CLAIMS to be the caregiver (or any welcome@ mail with a
DKIM-Signature) waits here until the queue worker has verified it by DKIM on
the raw message (``oncofiles.inbound_dkim``); only then is it queued into
``inbound_jobs`` with the real verdict. Writers and readers live in
``oncofiles.inbound_queue``. Every helper runs on the event loop.

No helper deletes a row (#734): a verified job ends ``done`` or ``failed``.
"""

from __future__ import annotations

from ._base import DatabaseBase
from ._inbound_jobs import INBOUND_JOB_STATES

_OPEN_STATES = "('pending', 'processing')"


class InboundClaimedJobsMixin(DatabaseBase):
    async def _open_claimed(self, where: str = "", params: tuple = ()) -> int:
        sql = f"SELECT COUNT(*) FROM inbound_claimed_jobs WHERE state IN {_OPEN_STATES}"
        if where:
            sql += " AND " + where
        async with self.db.execute(sql, params) as cursor:
            return int((await cursor.fetchone())[0])

    async def insert_inbound_claimed_jobs(
        self, rows: list[dict], *, now: str | None = None, caps: dict[str, int]
    ) -> tuple[int, int, dict[str, int]]:
        """Queue claimed ``rows`` in ONE transaction.

        Returns (inserted, duplicates, dropped per cap). Each row:
        ``job_uuid``, ``bucket``, ``message_id_hash``, ``sender_hash``,
        ``domain_hash``, ``delivery_hash``, ``payload_enc``. Caps over open
        (pending + processing) rows, keys of ``caps``: ``address`` / ``welcome``
        (per bucket of that kind), ``total``, ``sender`` (per sender hash, all
        buckets), ``domain`` (per From domain within a bucket). A row already
        queued (a Brevo retry of the same delivery) is never counted. A row
        over a cap is DROPPED and counted (infra review of #966): the rest of
        the batch is still queued and the route still answers 202, so one
        flooded address never makes the whole Brevo batch fail.
        """
        keep: list[dict] = []
        dropped: dict[str, int] = {}
        added: dict[tuple, int] = {}
        # Reads only, outside the try (never roll back the shared connection).
        for row in rows:
            async with self.db.execute(
                """SELECT 1 FROM inbound_claimed_jobs WHERE bucket = ?
                   AND message_id_hash = ? AND sender_hash = ? AND delivery_hash = ?
                   LIMIT 1""",
                (row["bucket"], row["message_id_hash"], row["sender_hash"], row["delivery_hash"]),
            ) as cursor:
                if await cursor.fetchone() is not None:
                    keep.append(row)
                    continue
            bucket, sender, domain = row["bucket"], row["sender_hash"], row["domain_hash"]
            checks = (
                (
                    "welcome" if bucket == "welcome" else "address",
                    ("bucket", bucket),
                    "bucket = ?",
                    (bucket,),
                ),
                ("total", ("total",), "", ()),
                ("sender", ("sender", sender), "sender_hash = ?", (sender,)),
                (
                    "domain",
                    ("domain", bucket, domain),
                    "bucket = ? AND domain_hash = ?",
                    (bucket, domain),
                ),
            )
            over = None
            for cap, key, where, params in checks:
                if await self._open_claimed(where, params) + added.get(key, 0) >= caps[cap]:
                    over = cap
                    break
            if over:
                dropped[over] = dropped.get(over, 0) + 1
                continue
            for _cap, key, _w, _p in checks:
                added[key] = added.get(key, 0) + 1
            keep.append(row)
        inserted = 0
        try:
            for row in keep:
                cursor = await self.db.execute(
                    """INSERT INTO inbound_claimed_jobs
                       (job_uuid, bucket, message_id_hash, sender_hash, domain_hash,
                        delivery_hash, payload_enc, state, attempts, next_attempt_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0,
                               COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))
                       ON CONFLICT (bucket, message_id_hash, sender_hash, delivery_hash)
                       DO NOTHING""",
                    (
                        row["job_uuid"],
                        row["bucket"],
                        row["message_id_hash"],
                        row["sender_hash"],
                        row["domain_hash"],
                        row["delivery_hash"],
                        row["payload_enc"],
                        now,
                    ),
                )
                inserted += cursor.rowcount or 0
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return inserted, len(keep) - inserted, dropped

    async def next_inbound_claimed_job(self, *, now: str, stale_before: str) -> dict | None:
        """The next claimable row (pending and due, or a stale processing one)."""
        async with self.db.execute(
            """SELECT id, job_uuid, bucket, payload_enc, state, attempts,
                      next_attempt_at, created_at, updated_at
               FROM inbound_claimed_jobs
               WHERE (state = 'pending' AND next_attempt_at <= ?)
                  OR (state = 'processing' AND updated_at < ?)
               ORDER BY next_attempt_at, id LIMIT 1""",
            (now, stale_before),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def claim_inbound_claimed_job(self, job: dict, *, now: str) -> bool:
        """``job`` → ``processing`` with attempts + 1, if nobody changed it."""
        cursor = await self.db.execute(
            """UPDATE inbound_claimed_jobs
               SET state = 'processing', attempts = attempts + 1, updated_at = ?
               WHERE id = ? AND state = ? AND updated_at = ?""",
            (now, job["id"], job["state"], job["updated_at"]),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def touch_inbound_claimed_job(self, job_id: int, *, lease: str, now: str) -> bool:
        """Refresh the lease of a row this worker holds; False = lease lost."""
        cursor = await self.db.execute(
            """UPDATE inbound_claimed_jobs SET updated_at = ?
               WHERE id = ? AND state = 'processing' AND updated_at = ?""",
            (now, job_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def finish_inbound_claimed_job(
        self,
        job_id: int,
        *,
        state: str,
        reason: str | None,
        now: str,
        lease: str,
        next_attempt_at: str | None = None,
    ) -> bool:
        """Final (or retry) ``state`` + ``reason`` code, only under the lease."""
        if state not in INBOUND_JOB_STATES:
            raise ValueError("unknown inbound job state")
        cursor = await self.db.execute(
            """UPDATE inbound_claimed_jobs
               SET state = ?, reason = ?, updated_at = ?,
                   next_attempt_at = COALESCE(?, next_attempt_at)
               WHERE id = ? AND state = 'processing' AND updated_at = ?""",
            (state, reason, now, next_attempt_at, job_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def inbound_claimed_counts(self) -> dict:
        """Rows per state, pending / failed rows per reason code (codes only)."""
        out: dict = {s: 0 for s in INBOUND_JOB_STATES}
        reasons: dict[str, dict[str, int]] = {"pending": {}, "failed": {}, "done": {}}
        async with self.db.execute(
            "SELECT state, reason, COUNT(*) AS n FROM inbound_claimed_jobs GROUP BY state, reason"
        ) as cursor:
            for row in await cursor.fetchall():
                state, reason, n = row["state"], row["reason"], int(row["n"])
                out[state] = out.get(state, 0) + n
                if state in reasons:
                    code = reason or ("new" if state == "pending" else "unknown")
                    reasons[state][code] = reasons[state].get(code, 0) + n
        out["pending_reasons"] = reasons["pending"]
        out["failed_reasons"] = reasons["failed"]
        out["done_reasons"] = reasons["done"]
        return out
