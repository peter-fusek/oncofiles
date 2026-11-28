-- #1032 chat over Signal (epic #1018). Additive only: three new tables, no backfill.
-- Nothing writes them until infra sets SIGNAL_ENABLED and the signal-cli
-- container exists; the adapter is oncofiles.channels.signal, the storage
-- oncofiles.channels.signal_bindings.
--
-- Identity is the Signal account UUID (ACI), never the phone number: a number
-- can be recycled to another person or re-registered, the ACI cannot.
--
-- signal_bindings: one row per (sender, patient) the caregiver connected with
-- a one-time dashboard code. No default patient: a sender without an active
-- row gets nothing but a neutral reply.
--   caregiver_email_hash: keyed HMAC of the caregiver who created the code
--                         (secrets_keys.signal_caregiver_hash_key, own purpose);
--                         a binding stops working when the patient's current
--                         caregiver no longer hashes to it.
--   sender_uuid_hash:     keyed HMAC of uuid:<ACI> (secrets_keys.
--                         signal_sender_hash_key): THE lookup key. Never raw.
--   phone_hash:           keyed HMAC of the E.164 number when signal-cli showed
--                         one at bind time (secrets_keys.signal_phone_hash_key);
--                         informational only, never a lookup key. Never raw.
--   identity_hash:        keyed HMAC of the sender's identity-key fingerprint as
--                         signal-cli's listIdentities reported it at bind time
--                         (secrets_keys.signal_identity_hash_key). Re-checked
--                         before every answer: a different fingerprint suspends
--                         the binding; a failed check answers nothing. Never raw.
--   identity_trust:       signal-cli trust level at bind time (TRUSTED_VERIFIED |
--                         TRUSTED_UNVERIFIED; UNTRUSTED never binds).
--   row_uuid:             random UUID per row, part of the AES-GCM associated data.
--   reply_enc:            the reply address uuid:<ACI>, AES-GCM with
--                         secrets_keys.signal_phone_key, associated data
--                         signal|<patient_id>|<row_uuid>.
--   conversation_uuid:    the current chat_history conversation of this binding
--                         (#1022); a new one after an idle gap.
--   conversation_at:      last turn of that conversation.
--   selected_at:          the newest selected active binding of a sender is its
--                         active patient (the `pacient <n>` switch).
--   last_seen_at:         last use; idle past SIGNAL_BINDING_IDLE_DAYS (default
--                         90) → active 0 + expired_at, a new code is needed.
--   suspended_at:         signal-cli reported a changed / untrusted identity key
--                         for the sender → active 0, a new code is needed.
--   active / revoked_at:  revoke = active 0 + revoked_at. Rows are never deleted
--                         by a job (the #856 erasure removes them with the patient).
CREATE TABLE IF NOT EXISTS signal_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    caregiver_email_hash TEXT NOT NULL,
    sender_uuid_hash TEXT NOT NULL,
    phone_hash TEXT,
    identity_hash TEXT NOT NULL,
    identity_trust TEXT NOT NULL,
    row_uuid TEXT NOT NULL UNIQUE,
    reply_enc BLOB NOT NULL,
    conversation_uuid TEXT,
    conversation_at TEXT,
    selected_at TEXT,
    last_seen_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    revoked_at TEXT,
    suspended_at TEXT,
    expired_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_bindings_active
    ON signal_bindings(sender_uuid_hash, patient_id) WHERE active = 1;
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
-- (at most one per day), and the last "temporarily unavailable" line (identity
-- not confirmable, at most one per hour). Keyed by the same keyed ACI hash; no patient
-- reference, no text.
CREATE TABLE IF NOT EXISTS signal_sender_limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sender_uuid_hash TEXT NOT NULL UNIQUE,
    window_started_at TEXT,
    window_messages INTEGER NOT NULL DEFAULT 0,
    window_failed_codes INTEGER NOT NULL DEFAULT 0,
    neutral_reply_at TEXT,
    unavailable_reply_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
