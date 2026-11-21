"""Durable queue of the Brevo inbound route (#952, migration 096).

Writers and readers live in ``oncofiles.inbound_queue``. Every helper runs on
the event loop (the app's single aiosqlite connection); the worker never calls
them from a thread. ``payload_enc`` is ciphertext only: this module never sees
an address, a subject or a download token in clear.

No helper deletes a row (#734): a processed job ends ``done`` or ``failed``.
TODO(#952 follow-up): removing ``done`` rows older than 30 days is a technical
artifact lifecycle that needs a RUNBOOK schedule and a reviewed entry in
``tests/test_no_automatic_deletion.py`` (maintainer decision) before any
DELETE is added here.
"""

from __future__ import annotations

from ._base import DatabaseBase
from ._tenant import require_patient_id

INBOUND_JOB_STATES = ("pending", "processing", "done", "failed")


class InboundJobsMixin(DatabaseBase):
    async def insert_inbound_jobs(self, rows: list[dict]) -> tuple[int, int]:
        """Queue ``rows`` in ONE transaction; returns (inserted, duplicates).

        Each row: ``patient_id``, ``token_hash``, ``message_id_hash`` (or None),
        ``sender_hash``, ``payload_enc``. A row whose (token_hash,
        message_id_hash, sender_hash) is already queued is a provider retry of
        the same mail and is ignored; the sender hash (From + auth verdict)
        keeps a forged mail with a stolen Message-ID from blocking the real
        one. Any error rolls everything back and raises (the route then
        answers 500, so the provider retries).
        """
        inserted = 0
        try:
            for row in rows:
                pid = require_patient_id(row["patient_id"])
                cursor = await self.db.execute(
                    # ON CONFLICT on the unique key only: OR IGNORE would also
                    # swallow a NOT NULL failure and lose the mail silently.
                    """INSERT INTO inbound_jobs
                       (patient_id, token_hash, message_id_hash, sender_hash, payload_enc,
                        state, attempts)
                       VALUES (?, ?, ?, ?, ?, 'pending', 0)
                       ON CONFLICT (token_hash, message_id_hash, sender_hash) DO NOTHING""",
                    (
                        pid,
                        row["token_hash"],
                        row.get("message_id_hash"),
                        row["sender_hash"],
                        row["payload_enc"],
                    ),
                )
                inserted += cursor.rowcount or 0
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            raise
        return inserted, len(rows) - inserted

    async def claimable_inbound_jobs(self, *, stale_before: str, limit: int = 100) -> list[dict]:
        """The oldest claimable jobs: ``pending``, or ``processing`` not touched
        since ``stale_before`` (a crash mid-way). System-wide on purpose: the
        worker serves every patient; each row carries its own ``patient_id``.
        The worker applies the retry backoff to this list.
        """
        async with self.db.execute(
            """SELECT id, patient_id, token_hash, payload_enc, state, attempts,
                      created_at, updated_at
               FROM inbound_jobs
               WHERE state = 'pending' OR (state = 'processing' AND updated_at < ?)
               ORDER BY id LIMIT ?""",
            (stale_before, int(limit)),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def claim_inbound_job(self, job: dict, *, now: str) -> bool:
        """``job`` → ``processing`` with attempts + 1, only if nobody changed it
        since it was read (state and updated_at unchanged). True when claimed.
        """
        cursor = await self.db.execute(
            """UPDATE inbound_jobs
               SET state = 'processing', attempts = attempts + 1, updated_at = ?
               WHERE id = ? AND patient_id = ? AND state = ? AND updated_at = ?""",
            (now, job["id"], job["patient_id"], job["state"], job["updated_at"]),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def finish_inbound_job(
        self, job_id: int, *, patient_id: str, state: str, reason: str | None, now: str
    ) -> None:
        """Set the job's final (or retry) ``state`` and ``reason`` code."""
        if state not in INBOUND_JOB_STATES:
            raise ValueError("unknown inbound job state")
        await self.db.execute(
            """UPDATE inbound_jobs SET state = ?, reason = ?, updated_at = ?
               WHERE id = ? AND patient_id = ?""",
            (state, reason, now, job_id, patient_id),
        )
        await self.db.commit()

    async def update_inbound_job_payload(
        self, job_id: int, *, patient_id: str, payload_enc: str, now: str
    ) -> None:
        """Store the re-encrypted payload (progress) and refresh ``updated_at``,
        which also tells the stale check that the job is alive.
        """
        await self.db.execute(
            """UPDATE inbound_jobs SET payload_enc = ?, updated_at = ?
               WHERE id = ? AND patient_id = ? AND state = 'processing'""",
            (payload_enc, now, job_id, patient_id),
        )
        await self.db.commit()

    async def inbound_job_counts(self, *, include_done: bool = True) -> dict:
        """Jobs per state, and pending / failed jobs per reason code (codes only).

        ``include_done=False`` (operator ``/readiness``) leaves out the ``done``
        rows, which only grow: the query then reads only the open and failed
        rows through ``idx_inbound_jobs_state``, so its cost stays bounded by
        the backlog, not by the history.
        """
        out: dict = {s: 0 for s in INBOUND_JOB_STATES if include_done or s != "done"}
        reasons: dict[str, dict[str, int]] = {"pending": {}, "failed": {}}
        sql = "SELECT state, reason, COUNT(*) AS n FROM inbound_jobs"
        if not include_done:
            sql += " WHERE state IN ('pending', 'processing', 'failed')"
        async with self.db.execute(sql + " GROUP BY state, reason") as cursor:
            for row in await cursor.fetchall():
                state, reason, n = row["state"], row["reason"], int(row["n"])
                out[state] = out.get(state, 0) + n
                if state in reasons:
                    # a pending job without a reason was never attempted
                    code = reason or ("new" if state == "pending" else "unknown")
                    reasons[state][code] = reasons[state].get(code, 0) + n
        out["pending_reasons"] = reasons["pending"]
        out["failed_reasons"] = reasons["failed"]
        return out
