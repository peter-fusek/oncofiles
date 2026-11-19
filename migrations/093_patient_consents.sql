-- #857: the consent a caregiver gives when a patient is created (GDPR Art. 9).
-- Additive only. Existing patients get no row (no backfill): they were created
-- before the consent step existed.
--
-- consent_version:   CONSENT_VERSION of the privacy + terms text the caller saw
--                    (oncofiles.patient_consent), e.g. '2026-11-17'.
-- caller_email_hash: keyed HMAC-SHA256 (secrets_keys.caller_email_hash) of the
--                    signed-in caller's e-mail, never the e-mail itself; NULL
--                    for the operator (static bearer), who has no e-mail.
-- is_admin:          1 when an admin or the operator created the patient.
--
-- Renumbered 091 -> 093 at the merge with main (091 and 092 were taken); the
-- runner tracks migrations by file name.
CREATE TABLE IF NOT EXISTS patient_consents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    consent_version TEXT NOT NULL,
    consented_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    caller_email_hash TEXT,
    is_admin INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_patient_consents_patient ON patient_consents(patient_id);
