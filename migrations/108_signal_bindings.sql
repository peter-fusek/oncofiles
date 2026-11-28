-- #1032 chat over Signal (epic #1018). Additive only: three new tables, no backfill.
-- Nothing writes them until infra sets SIGNAL_ENABLED and the signal-cli
-- container exists; the adapter is oncofiles.channels.signal, the storage
-- oncofiles.channels.signal_bindings.
--
-- signal_bindings: one row per (sender, patient) the caregiver connected with
-- a one-time dashboard code. No default patient: a sender without an active
-- row gets nothing but a neutral reply.
--   caregiver_email_hash: keyed HMAC of the caregiver who created the code
--                         (secrets_keys.signal_caregiver_hash_key, own purpose);
--                         a binding stops working when the patient's current
--                         caregiver no longer hashes to it.
--   phone_hash:           keyed HMAC of the sender address (E.164 number, or
--                         the Signal account uuid when the number is hidden),
--                         secrets_keys.signal_phone_hash_key; never the raw
--                         number in the DB or in a log.
--   row_uuid:             random UUID per row, part of the AES-GCM associated data.
--   phone_enc:            the sender address, AES-GCM with
--                         secrets_keys.signal_phone_key, associated data
--                         signal|<patient_id>|<row_uuid>: needed to send replies.
--   conversation_uuid:    the current chat_history conversation of this binding
--                         (#1022); a new one after an idle gap.
--   conversation_at:      last turn of that conversation.
--   selected_at:          the newest selected active binding of a sender is its
--                         active patient (the `pacient <n>` switch).
--   active / revoked_at:  revoke = active 0 + revoked_at; rows are never deleted
--                         by a job (the #856 erasure removes them with the patient).
CREATE TABLE IF NOT EXISTS signal_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    caregiver_email_hash TEXT NOT NULL,
    phone_hash TEXT NOT NULL,
    row_uuid TEXT NOT NULL UNIQUE,
    phone_enc BLOB NOT NULL,
    conversation_uuid TEXT,
    conversation_at TEXT,
    selected_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    revoked_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_bindings_active
    ON signal_bindings(phone_hash, patient_id) WHERE active = 1;
CREATE INDEX IF NOT EXISTS idx_signal_bindings_patient
    ON signal_bindings(patient_id, active);

-- signal_link_codes: the one-time code a caregiver gets in the dashboard
-- ("Connect Signal"): 6 characters, 10 minutes, single use. Only a keyed HMAC
-- of the code is stored (secrets_keys.signal_link_code_key).
CREATE TABLE IF NOT EXISTS signal_link_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    code_hash TEXT NOT NULL UNIQUE,
    caregiver_email_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    expires_at TEXT NOT NULL,
    used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_signal_link_codes_patient
    ON signal_link_codes(patient_id, used_at);

-- signal_sender_limits: persisted per-sender throttle (survives a deploy, #623
-- lesson): messages in the current hour window, failed code attempts in it,
-- and when the last neutral "connect in the dashboard first" reply went out
-- (at most one per day). Keyed by the same keyed phone hash; no patient
-- reference, no text.
CREATE TABLE IF NOT EXISTS signal_sender_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone_hash TEXT NOT NULL UNIQUE,
    window_started_at TEXT,
    window_messages INTEGER NOT NULL DEFAULT 0,
    window_failed_codes INTEGER NOT NULL DEFAULT 0,
    neutral_reply_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
