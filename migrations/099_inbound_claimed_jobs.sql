-- #950: the "claimed" bucket of the Brevo inbound route (DKIM verification).
-- Additive only: one new table, no backfill.
--
-- Brevo's inbound payload carries no Authentication-Results header, so the
-- sender is verified by DKIM on the raw message (oncofiles.inbound_dkim). That
-- needs a download of the raw EML and DNS key lookups, which must not run in
-- the webhook request. A mail whose From CLAIMS to be the patient's caregiver
-- (an unverified claim), or any mail to welcome@ that carries a DKIM-Signature,
-- is written here first instead of to inbound_jobs. The queue worker
-- (oncofiles.inbound_queue) verifies it and only then queues it into
-- inbound_jobs with the real verdict (trusted when the DKIM check passed).
-- A forged flood that claims the caregiver's From can therefore fill only this
-- small bucket (own caps per bucket and in total), never the trusted slots.
--
-- bucket:          the address token hash (the same value as
--                  inbound_jobs.token_hash) or the literal 'welcome' for mail
--                  to welcome@ (no patient yet: the trial is created only
--                  after the verification, so this table has no patient_id).
-- job_uuid:        random UUID per row, part of the AES-GCM associated data
--                  ('claimed'|bucket|job_uuid).
-- message_id_hash: keyed HMAC of the Message-ID (or of the payload digest),
--                  as in inbound_jobs.
-- sender_hash:     keyed HMAC of the lowercased From; UNIQUE together with the
--                  bucket and the Message-ID hash, so a Brevo retry of a mail
--                  already waiting here is a no-op.
-- payload_enc:     AES-GCM ciphertext of the same minimal payload as
--                  inbound_jobs plus the Brevo EMLDownloadToken; never a
--                  subject, a body or the EML itself.
-- state:           pending | processing | done | failed (domain
--                  inbound_job_state, shared with inbound_jobs).
-- reason:          last outcome code: verified, a DKIM refusal code
--                  (dkim_none, dkim_invalid, dkim_not_aligned, ...), or a
--                  temporary code (fetch_failed, dns_failed, no_api_key,
--                  queue_full) on a pending row.
--
-- Retention: no row is ever deleted (#734); same TODO as inbound_jobs.
CREATE TABLE IF NOT EXISTS inbound_claimed_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_uuid TEXT NOT NULL UNIQUE,
    bucket TEXT NOT NULL,
    message_id_hash TEXT NOT NULL,
    sender_hash TEXT NOT NULL,
    payload_enc TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (bucket, message_id_hash, sender_hash)
);

CREATE INDEX IF NOT EXISTS idx_inbound_claimed_due ON inbound_claimed_jobs(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_inbound_claimed_bucket ON inbound_claimed_jobs(bucket, state);
