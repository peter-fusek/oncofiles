-- #952: durable queue for the Brevo inbound route (#950 go-live blocker).
-- Additive only: one new table, no backfill.
--
-- /inbound/brevo writes one row per mail BEFORE it answers 202; a worker
-- (oncofiles.inbound_queue) processes the rows one at a time under
-- inbound_email.PROCESS_LOCK, at startup after STARTUP_GRACE_S, every few
-- minutes and right after an enqueue. A deploy or restart no longer loses a
-- mail Brevo already got its 202 for.
--
-- patient_id:      resolved from the address token at enqueue time.
-- token_hash:      the keyed routing hash of the address token (the same value
--                  as inbound_addresses.token_hash), never the token.
-- job_uuid:        random UUID per job (#952 infra review), part of the AES-GCM
--                  associated data (patient_id|token_hash|job_uuid), so a
--                  payload copied onto another row never decrypts.
-- message_id_hash: keyed HMAC of the Message-ID; a mail without one gets a
--                  keyed HMAC of its payload digest instead (never NULL: NULL
--                  is distinct in a UNIQUE key and would defeat the dedupe).
-- sender_hash:     keyed HMAC of the lowercased From address + the auth verdict
--                  (security review): part of the dedupe key, so a forged or
--                  unmatched mail sent first with a real mail's Message-ID can
--                  never make the real one look like a duplicate. UNIQUE
--                  (token_hash, message_id_hash, sender_hash): a genuine
--                  provider retry (same sender, verdict, Message-ID) is not
--                  queued twice.
-- payload_enc:     AES-GCM ciphertext (base64, "v1:" prefix) of the minimal
--                  data the worker needs: the address token, the sender address
--                  and its auth verdict, the loop-check header values, the
--                  Message-ID, and per attachment the Brevo download token,
--                  name, content type and declared size; plus the job's
--                  progress (sha256 of files already filed, receipt sent).
--                  Key: secrets_keys.inbound_jobs_key (purpose
--                  oncofiles.inbound_jobs.v1), from the MANDATORY runtime env
--                  INBOUND_JOBS_SECRET, never derived from MCP_BEARER_TOKEN;
--                  unset = the route queues nothing (503). No subject, no
--                  body, ever.
-- state:           pending | processing | done | failed (vocab domain
--                  inbound_job_state).
-- attempts:        claims so far; at MAX_ATTEMPTS the job ends failed.
-- next_attempt_at: earliest time a pending job may be claimed (the retry
--                  backoff after a temporary failure); the worker picks
--                  WHERE next_attempt_at <= now ORDER BY next_attempt_at.
-- reason:          last outcome / failure code (filed, unmatched,
--                  download_expired, max_attempts, error:<class>, and the
--                  temporary codes rate_limited / fetch_failed / store_failed /
--                  no_api_key: on a pending row a retry is due, on a failed
--                  row the attempts ran out on it).
--
-- Retention: rows are never deleted by the processing path. Removing done
-- rows older than 30 days needs a RUNBOOK lifecycle schedule and a reviewed
-- #734 allowlist entry (maintainer decision); not part of #952.
CREATE TABLE IF NOT EXISTS inbound_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL UNIQUE,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    token_hash TEXT NOT NULL,
    message_id_hash TEXT NOT NULL,
    sender_hash TEXT NOT NULL,
    payload_enc TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (token_hash, message_id_hash, sender_hash)
);

CREATE INDEX IF NOT EXISTS idx_inbound_jobs_state ON inbound_jobs(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_inbound_jobs_due ON inbound_jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_inbound_jobs_token ON inbound_jobs(token_hash, state);
