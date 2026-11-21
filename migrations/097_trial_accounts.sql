-- #950 phase 2: onboarding by e-mail via welcome@ (Flow B). Additive only.
--
-- patient_consents.kind: how the consent was given (vocab domain consent_kind).
--   wizard     = the #857 checkbox of the New Patient wizard (every existing
--                row; the DEFAULT keeps them as they are)
--   by_sending = consent by sending documents to welcome@ (maintainer
--                decision 2026-11-21); caller_email_hash is then the keyed
--                hash of the sending address, never the address.
ALTER TABLE patient_consents ADD COLUMN kind TEXT NOT NULL DEFAULT 'wizard';

-- trial_accounts: the 30-day trial a welcome@ mail starts (one per sender).
--
-- sender_hash:   keyed HMAC of the lowercased sending address
--                (secrets_keys.caller_email_hash), never the address. UNIQUE:
--                one trial patient per sender.
-- source:        how the trial started (vocab domain trial_source).
-- trial_ends_at: created_at + 30 days. Documents of an unclaimed trial stay in
--                the encrypted holding store at most until then. NOTHING
--                deletes them automatically yet: the end-of-trial purge needs
--                a RUNBOOK lifecycle entry and a #734 allowlist entry
--                (maintainer decision), see oncofiles.holding_store.
CREATE TABLE IF NOT EXISTS trial_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL UNIQUE REFERENCES patients(patient_id),
    sender_hash TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    trial_ends_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_trial_accounts_created ON trial_accounts(created_at);
