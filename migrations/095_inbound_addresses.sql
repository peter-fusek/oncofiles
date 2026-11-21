-- #950 phase 1: a private document e-mail address per patient.
-- Additive only: one new table, no backfill. A patient gets a row the first
-- time the dashboard shows its address (oncofiles.inbound_email.ensure_address).
--
-- The address is <first name, optional>-<token>@<INBOUND_DOMAIN>. The token is
-- derived (HMAC of patient id + version with a purpose-bound key, #502), never
-- stored: token_hash is HMAC-SHA256(server-side key, token) as hex (key from
-- secrets_keys.inbound_token_hash_key), so a copy of the database does
-- not reveal any address. The webhook routes a mail by hashing the token of
-- its recipient and looking the hash up here.
--
-- version: rotation counter per patient (1, 2, ...). Rotating writes a new
--          row with version + 1 and sets active = 0 on the old one, so an old
--          address stops routing (an unknown or inactive token is dropped).
CREATE TABLE IF NOT EXISTS inbound_addresses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    token_hash TEXT NOT NULL UNIQUE,
    version INTEGER NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    UNIQUE (patient_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbound_addresses_one_active
    ON inbound_addresses(patient_id) WHERE active = 1;
