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

from oncofiles import inbound_sql as q

from ._base import DatabaseBase
from ._tenant import require_patient_id

INBOUND_JOB_STATES = ("pending", "processing", "done", "failed")
# A job still to be processed (the enqueue caps count these).
_OPEN_STATES = "('pending', 'processing')"


class InboundQueueFullError(RuntimeError):
    """An enqueue would pass a queue cap (#952 infra review): nothing written."""


class InboundJobsMixin(DatabaseBase):
    async def _open_jobs(self, *, trusted: bool, token_hash: str | None = None) -> int:
        """Open (pending + processing) jobs of one trust class, for one token
        hash or in total (indexed COUNT)."""
        sql = f"SELECT COUNT(*) FROM inbound_jobs WHERE trusted = ? AND state IN {_OPEN_STATES}"
        params: tuple = (1 if trusted else 0,)
        if token_hash is not None:
            sql += " AND token_hash = ?"
            params += (token_hash,)
        async with self.db.execute(sql, params) as cursor:
            return int((await cursor.fetchone())[0])

    async def insert_inbound_jobs(
        self,
        rows: list[dict],
        *,
        now: str | None = None,
        caps: dict[bool, tuple[int, int]] | None = None,
    ) -> tuple[int, int, int]:
        """Queue ``rows`` in ONE transaction; returns (inserted, duplicates, dropped).

        Each row: ``job_uuid``, ``patient_id``, ``token_hash``,
        ``message_id_hash``, ``sender_hash``, ``trusted``, ``payload_enc``. A
        row whose (token_hash, message_id_hash, sender_hash) is already queued
        is a provider retry of the same mail and is ignored (never counted
        against a cap); the sender hash (From + auth verdict) keeps a forged
        mail with a stolen Message-ID from blocking the real one. ``now`` is the
        row's first ``next_attempt_at``.

        Caps (#952 infra + security review), ``caps[trusted] = (per token hash,
        total)`` over open (pending + processing) jobs, counted per trust class
        so untrusted mail can never use up the caregiver's slots:

        * trusted (caregiver From + auth pass): a batch that would pass a cap
          raises ``InboundQueueFullError`` and NOTHING is written (the route
          answers 503 + Retry-After: Brevo retries the real mail later);
        * untrusted: a row over its cap is DROPPED (not written, counted in
          ``dropped``); the rest of the batch is still queued and the route
          still answers 202, since this is a flood, not a retry case.

        Any other error rolls everything back and raises (the route then
        answers 500, so the provider retries).
        """
        inserted = 0
        keep: list[dict] = []
        dropped = 0
        # The cap check only READS, outside the try: a refusal (or a read error)
        # must never roll back the single shared connection, which could drop
        # another coroutine's uncommitted writes (infra review of #953).
        if caps:
            added: dict[tuple[bool, str | None], int] = {}
            for row in rows:
                async with self.db.execute(
                    """SELECT 1 FROM inbound_jobs WHERE token_hash = ?
                       AND message_id_hash = ? AND sender_hash = ? LIMIT 1""",
                    (row["token_hash"], row["message_id_hash"], row["sender_hash"]),
                ) as cursor:
                    if await cursor.fetchone() is not None:
                        keep.append(row)  # a retry: a no-op insert, never capped
                        continue
                trusted = bool(row["trusted"])
                per_token, total = caps[trusted]
                th = row["token_hash"]
                open_token = await self._open_jobs(trusted=trusted, token_hash=th)
                open_total = await self._open_jobs(trusted=trusted)
                over = (
                    open_token + added.get((trusted, th), 0) >= per_token
                    or open_total + added.get((trusted, None), 0) >= total
                )
                if over:
                    if trusted:
                        raise InboundQueueFullError("trusted")
                    dropped += 1
                    continue
                added[(trusted, th)] = added.get((trusted, th), 0) + 1
                added[(trusted, None)] = added.get((trusted, None), 0) + 1
                keep.append(row)
        else:
            keep = list(rows)
        try:
            for row in keep:
                pid = require_patient_id(row["patient_id"])
                cursor = await self.db.execute(
                    # ON CONFLICT on the dedupe key only: OR IGNORE would also
                    # swallow a NOT NULL failure and lose the mail silently.
                    """INSERT INTO inbound_jobs
                       (job_uuid, patient_id, token_hash, message_id_hash, sender_hash,
                        trusted, payload_enc, state, attempts, next_attempt_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0,
                               COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))
                       ON CONFLICT (token_hash, message_id_hash, sender_hash) DO NOTHING""",
                    (
                        row["job_uuid"],
                        pid,
                        row["token_hash"],
                        row["message_id_hash"],
                        row["sender_hash"],
                        1 if row.get("trusted") else 0,
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

    async def next_inbound_job(self, *, now: str, stale_before: str) -> dict | None:
        """The next claimable job, or None: a ``pending`` job whose
        ``next_attempt_at`` has come, or a ``processing`` job not touched since
        ``stale_before`` (a crash or kill mid-way), earliest ``next_attempt_at``
        first. System-wide on purpose: the worker serves every patient; each
        row carries its own ``patient_id``.
        """
        async with self.db.execute(
            """SELECT id, job_uuid, patient_id, token_hash, payload_enc, state, attempts,
                      next_attempt_at, created_at, updated_at
               FROM inbound_jobs
               WHERE """
            + q.JOB_CLAIMABLE_WHERE
            + " "
            + q.JOB_CLAIM_ORDER
            + " LIMIT 1",
            (now, stale_before),
        ) as cursor:
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def claim_inbound_job(self, job: dict, *, now: str) -> bool:
        """``job`` → ``processing`` with attempts + 1, only if nobody changed it
        since it was read (state and updated_at unchanged). True when claimed;
        ``now`` is then the claimer's lease (the row's ``updated_at``).
        """
        cursor = await self.db.execute(
            """UPDATE inbound_jobs
               SET state = 'processing', attempts = attempts + 1, updated_at = ?
               WHERE id = ? AND patient_id = ? AND state = ? AND updated_at = ?""",
            (now, job["id"], job["patient_id"], job["state"], job["updated_at"]),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def touch_inbound_job(
        self, job_id: int, *, patient_id: str, lease: str, now: str
    ) -> bool:
        """Refresh the lease (``updated_at`` = ``now``) of a job this worker
        holds (``processing`` with ``updated_at`` = ``lease``). False = the
        lease is lost (another worker took the job over).
        """
        cursor = await self.db.execute(
            """UPDATE inbound_jobs SET updated_at = ?
               WHERE id = ? AND patient_id = ? AND state = 'processing' AND updated_at = ?""",
            (now, job_id, patient_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def finish_inbound_job(
        self,
        job_id: int,
        *,
        patient_id: str,
        state: str,
        reason: str | None,
        now: str,
        lease: str,
        next_attempt_at: str | None = None,
        refund_attempt: bool = False,
        payload_enc: str | None = None,
    ) -> bool:
        """Set the job's final (or retry) ``state`` and ``reason`` code, only
        while this worker still holds it (``processing`` with ``updated_at`` =
        ``lease``; #952 infra review). ``next_attempt_at`` (a retry) is when it
        may be claimed again. ``refund_attempt`` gives back the attempt the
        claim counted (a wait for the filing quota is not an attempt, #952
        follow-up). ``payload_enc`` (#976) replaces the payload in the same
        update (the payload without the mail text once the job ends). False =
        the lease was lost, nothing changed.
        """
        if state not in INBOUND_JOB_STATES:
            raise ValueError("unknown inbound job state")
        cursor = await self.db.execute(
            """UPDATE inbound_jobs
               SET state = ?, reason = ?, updated_at = ?,
                   next_attempt_at = COALESCE(?, next_attempt_at),
                   attempts = MAX(attempts - ?, 0),
                   payload_enc = COALESCE(?, payload_enc)
               WHERE id = ? AND patient_id = ? AND state = 'processing' AND updated_at = ?""",
            (
                state,
                reason,
                now,
                next_attempt_at,
                1 if refund_attempt else 0,
                payload_enc,
                job_id,
                patient_id,
                lease,
            ),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

    async def update_inbound_job_payload(
        self, job_id: int, *, patient_id: str, payload_enc: str, lease: str, now: str
    ) -> bool:
        """Store the re-encrypted payload (progress) and refresh the lease
        (``updated_at`` = ``now``), only while this worker holds it. False =
        the lease was lost, nothing changed.
        """
        cursor = await self.db.execute(
            """UPDATE inbound_jobs SET payload_enc = ?, updated_at = ?
               WHERE id = ? AND patient_id = ? AND state = 'processing' AND updated_at = ?""",
            (payload_enc, now, job_id, patient_id, lease),
        )
        await self.db.commit()
        return (cursor.rowcount or 0) == 1

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
