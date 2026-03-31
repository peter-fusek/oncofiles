-- Patient bearer tokens for multi-patient MCP authentication.
-- Plaintext tokens are never stored — only SHA-256 hashes.
-- Created manually in Turso on 2026-03-31 (Sprint 63); this migration
-- ensures the table exists on fresh deployments and test databases.

CREATE TABLE IF NOT EXISTS patient_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_patient_tokens_hash ON patient_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_patient_tokens_patient ON patient_tokens(patient_id);
