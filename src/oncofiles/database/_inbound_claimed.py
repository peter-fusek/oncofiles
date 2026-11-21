"""The "claimed" bucket of the Brevo inbound route (#950, migration 099).

A mail whose From only CLAIMS to be the caregiver (or any welcome@ mail with a
DKIM-Signature) waits here until the queue worker has verified it by DKIM on
the raw message (``oncofiles.inbound_dkim``); only then is it queued into
``inbound_jobs`` with the real verdict. Writers and readers live in
``oncofiles.inbound_queue``. Every helper runs on the event loop.

States: ``pending`` / ``processing`` (open: counted by the open caps, worked
on by the worker), ``deferred`` (over an open cap: stored, no download, no
DNS, until :meth:`promote_deferred_claimed` moves it to ``pending``), ``done``
and ``failed``.

No helper deletes a row (#734): a verified job ends ``done`` or ``failed``.
"""

from __future__ import annotations

from ._base import DatabaseBase
from ._inbound_jobs import INBOUND_JOB_STATES

# The claimed table's states: the queue's four plus ``deferred`` (security
# review of #966: an over-cap mail is parked, never lost while there is room).
CLAIMED_JOB_STATES = ("pending", "processing", "deferred", "done", "failed")
_OPEN_STATES = "('pending', 'processing')"


def _checks(row: dict, *, deferred: bool) -> tuple:
    """(cap name, in-batch key, SQL filter, params) of every cap of ``row``.

    The welcome bucket and the address buckets are SEPARATE pools (security
    review of #966): a welcome@ flood from many Gmail senders can never use
    up the slots of the patients' addresses, nor the other way round.

    The sender caps count per (bucket, canonical sender) (infra review of
    #966): forgeries carrying the caregiver's From sent to the public welcome@
    never use the slots of that caregiver's real mail to the patient address.

    Open caps (``deferred=False``): ``sender`` (per bucket and canonical
    sender); for welcome@ ``welcome`` (its whole pool); for an address
    bucket ``address`` (per token), ``total`` (all address buckets) and
    ``domain`` (per From domain; never in welcome@: maintainer decision,
    every welcome@ sender is a Gmail user). Deferred caps:
    ``deferred_sender`` (per bucket and sender); for welcome@ ``deferred_welcome``; for an address
    bucket ``deferred_bucket`` (per token) and ``deferred_total`` (all
    address buckets).
    """
    bucket, sender, domain = row["bucket"], row["sender_hash"], row["domain_hash"]
    welcome = bucket == "welcome"
    if deferred:
        out = (
            (
                "deferred_sender",
                ("d_sender", bucket, sender),
                "bucket = ? AND sender_hash = ?",
                (bucket, sender),
            ),
        )
        if welcome:
            return out + (("deferred_welcome", ("d_welcome",), "bucket = 'welcome'", ()),)
        return out + (
            ("deferred_bucket", ("d_bucket", bucket), "bucket = ?", (bucket,)),
            ("deferred_total", ("d_total",), "bucket <> 'welcome'", ()),
        )
    out = (
        ("sender", ("sender", bucket, sender), "bucket = ? AND sender_hash = ?", (bucket, sender)),
    )
    if welcome:
        return out + (("welcome", ("welcome",), "bucket = 'welcome'", ()),)
    return out + (
        ("address", ("bucket", bucket), "bucket = ?", (bucket,)),
        ("total", ("total",), "bucket <> 'welcome'", ()),
        ("domain", ("domain", bucket, domain), "bucket = ? AND domain_hash = ?", (bucket, domain)),
    )


class InboundClaimedJobsMixin(DatabaseBase):
    async def _count_claimed(self, states: str, where: str = "", params: tuple = ()) -> int:
        sql = f"SELECT COUNT(*) FROM inbound_claimed_jobs WHERE state IN {states}"
        if where:
            sql += " AND " + where
        async with self.db.execute(sql, params) as cursor:
            return int((await cursor.fetchone())[0])

    async def _over(self, row: dict, caps: dict, added: dict, *, deferred: bool) -> str | None:
        """The first cap ``row`` would pass, or None."""
        states = "('deferred')" if deferred else _OPEN_STATES
        for cap, key, where, params in _checks(row, deferred=deferred):
            if await self._count_claimed(states, where, params) + added.get(key, 0) >= caps[cap]:
                return cap
        return None

    async def insert_inbound_claimed_jobs(
        self, rows: list[dict], *, now: str | None = None, caps: dict[str, int]
    ) -> dict:
        """Queue claimed ``rows`` in ONE transaction.

        Returns ``{"inserted", "duplicate", "deferred": {cap: n}, "dropped":
        {cap: n}, "over_bound": [bucket, ...]}``. Each row: ``job_uuid``,
        ``bucket``, ``message_id_hash``, ``sender_hash``, ``domain_hash``,
        ``delivery_hash``, ``payload_enc``. A row already queued (a Brevo
        retry of the same delivery) is never counted. A row within the open
        caps (:func:`_checks`) is ``pending``; over one it is stored
        ``deferred`` (no download, no DNS) while the deferred caps allow.
        Beyond those: a welcome@ row is DROPPED and counted; an ADDRESS bucket
        row is not written and its bucket is listed in ``over_bound``. Every
        other row of the batch is written and committed either way; the caller
        answers 503 for the batch when any address row is over-bound
        (``inbound_queue``, fail closed: never dropped).
        """
        keep: list[tuple[dict, str]] = []
        deferred: dict[str, int] = {}
        dropped: dict[str, int] = {}
        over_bound: list[str] = []
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
                    keep.append((row, "pending"))  # a retry: a no-op insert
                    continue
            over = await self._over(row, caps, added, deferred=False)
            if over is None:
                state, checks = "pending", _checks(row, deferred=False)
            else:
                over_deferred = await self._over(row, caps, added, deferred=True)
                if over_deferred is not None:
                    if row["bucket"] != "welcome":
                        over_bound.append(row["bucket"])
                        continue
                    dropped[over_deferred] = dropped.get(over_deferred, 0) + 1
                    continue
                deferred[over] = deferred.get(over, 0) + 1
                state, checks = "deferred", _checks(row, deferred=True)
            for _cap, key, _w, _p in checks:
                added[key] = added.get(key, 0) + 1
            keep.append((row, state))
        inserted = 0
        try:
            for row, state in keep:
                cursor = await self.db.execute(
                    """INSERT INTO inbound_claimed_jobs
                       (job_uuid, bucket, message_id_hash, sender_hash, domain_hash,
                        delivery_hash, payload_enc, state, attempts, next_attempt_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0,
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
                        state,
                        now,
                    ),
                )
                inserted += cursor.rowcount or 0
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return {
            "inserted": inserted,
            "duplicate": len(keep) - inserted,
            "deferred": deferred,
            "dropped": dropped,
            "over_bound": over_bound,
        }

    async def promote_deferred_claimed(
        self, *, caps: dict[str, int], now: str, expired_before: str
    ) -> dict[str, int]:
        """Move deferred rows to ``pending`` while the open caps allow, oldest
        first, so the oldest deferred row of a sender / domain takes the slot a
        finished row freed. A deferred row created before ``expired_before``
        (the Brevo attachment retention) ends ``failed`` / ``download_expired``
        instead. Returns ``{"promoted": n, "expired": n}``.
        """
        cursor = await self.db.execute(
            """UPDATE inbound_claimed_jobs
               SET state = 'failed', reason = 'download_expired', updated_at = ?
               WHERE state = 'deferred' AND created_at < ?""",
            (now, expired_before),
        )
        expired = cursor.rowcount or 0
        async with self.db.execute(
            """SELECT id, bucket, sender_hash, domain_hash FROM inbound_claimed_jobs
               WHERE state = 'deferred' ORDER BY created_at, id"""
        ) as cur:
            waiting = [dict(r) for r in await cur.fetchall()]
        promoted = 0
        for row in waiting:
            if await self._over(row, caps, {}, deferred=False) is not None:
                continue
            cursor = await self.db.execute(
                """UPDATE inbound_claimed_jobs
                   SET state = 'pending', next_attempt_at = ?, updated_at = ?
                   WHERE id = ? AND state = 'deferred'""",
                (now, now, row["id"]),
            )
            promoted += cursor.rowcount or 0
        await self.db.commit()
        return {"promoted": promoted, "expired": expired}

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
        out: dict = {s: 0 for s in CLAIMED_JOB_STATES}
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
