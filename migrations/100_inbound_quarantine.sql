-- #950 quarantine (maintainer decision 2026-11-22). Additive only: two new
-- tables, no backfill, nothing existing changes.
--
-- A mail to a patient's document address from any sender other than the
-- caregiver (or claiming the caregiver without a passing DKIM verdict) is no
-- longer only listed: its screened attachments wait here, encrypted, until the
-- caregiver approves (filed through the phase-one path) or rejects them.
--
-- inbound_quarantine: one row per quarantined mail.
--   item_uuid:      random UUID; part of the AES-GCM associated data of every
--                   file and metadata blob of the row.
--   source_hash:    keyed digest of the delivery (address, Message-ID, sender,
--                   verdict, attachment references); UNIQUE per patient, so a
--                   queue retry or a duplicate delivery never quarantines twice.
--   sender_hash:    keyed HMAC of the canonical From (throttle per sender).
--   sender_enc:     the From address, AES-GCM (shown only to the caregiver: in
--                   the dashboard card, the notification and the confirm page).
--   files_enc:      AES-GCM JSON manifest (name, type, size, sha256, md5 per
--                   file); file names are shown only in the caregiver's
--                   dashboard, never in an e-mail, a page or a log.
--   refusal_reason: why the mail was not filed (unknown_sender | auth_failed).
--   state:          pending | approved | rejected | expired (domain
--                   inbound_quarantine_state). Reject and expiry only mark the
--                   row.
--   notified_at / notice_key: the notification e-mail that announced the item
--                   (one key per sent mail, for the per-day cap).
--   claimed_at:     short lease while an approval files the files.
--   decided_by:     keyed caller hash (never an address); decided_via:
--                   dashboard | email_link | expiry. decided_at of a rejected /
--                   expired item starts its 30-day trash: the caregiver can
--                   restore it to pending in the dashboard (restored_at; the
--                   expiry then counts from it). After the trash only the
--                   encrypted files go and the row stays 'purged' (separate
--                   PR with a #734 allowlist entry; nothing deleted here).
--
-- inbound_quarantine_tokens: single-use action links of the notification.
--   token_hash:     keyed HMAC of the random token (the token itself is only
--                   in the e-mail); recipient_hash = keyed hash of the
--                   canonical caregiver address the link was sent to;
--                   binding_hash binds the token to action + patient + item +
--                   recipient; a link resolves only while the patient's
--                   current caregiver address has the same recipient_hash, and
--                   a caregiver change marks every unused link used;
--                   expires_at = created + 7 days; used_at marks a used token.
--
-- Retention: no row and no file is ever deleted (#734). Removing rejected or
-- expired quarantine files needs a RUNBOOK lifecycle entry and a reviewed
-- tests/test_no_automatic_deletion.py allowlist entry (maintainer, TODO).
CREATE TABLE IF NOT EXISTS inbound_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    item_uuid TEXT NOT NULL UNIQUE,
    source_hash TEXT NOT NULL,
    sender_hash TEXT NOT NULL,
    sender_enc TEXT NOT NULL,
    files_enc TEXT NOT NULL,
    refusal_reason TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    received_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    notified_at TEXT,
    notice_key TEXT,
    claimed_at TEXT,
    decided_at TEXT,
    decided_by TEXT,
    decided_via TEXT,
    filed_count INTEGER NOT NULL DEFAULT 0,
    restored_at TEXT,
    UNIQUE (patient_id, source_hash)
);

CREATE INDEX IF NOT EXISTS idx_inbound_quarantine_patient
    ON inbound_quarantine(patient_id, state, received_at);

CREATE TABLE IF NOT EXISTS inbound_quarantine_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    quarantine_id INTEGER NOT NULL REFERENCES inbound_quarantine(id),
    action TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    binding_hash TEXT NOT NULL,
    recipient_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_inbound_quarantine_tokens_item
    ON inbound_quarantine_tokens(quarantine_id);
