-- Multi-patient support: patient registry and token-to-patient mapping (#134)

CREATE TABLE IF NOT EXISTS patients (
    patient_id TEXT PRIMARY KEY,                -- slug: "erika", "jan-novak"
    display_name TEXT NOT NULL,
    caregiver_email TEXT,                       -- primary contact email
    diagnosis_summary TEXT,                     -- e.g. "CRC, [BIOMARKER_REDACTED], [BIOMARKER_REDACTED]"
    is_active INTEGER NOT NULL DEFAULT 1,
    preferred_lang TEXT NOT NULL DEFAULT 'sk',  -- 'sk' or 'en'
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS patient_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    token_hash TEXT NOT NULL UNIQUE,            -- SHA-256 of bearer token (never store plaintext)
    label TEXT NOT NULL DEFAULT '',             -- "claude-connector", "chatgpt", "caregiver-mobile"
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_patient_tokens_hash ON patient_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_patient_tokens_patient ON patient_tokens(patient_id);

-- Seed default patient (idempotent). Real data set via dashboard or env vars.
INSERT OR IGNORE INTO patients (patient_id, display_name)
VALUES ('erika', 'Patient');
