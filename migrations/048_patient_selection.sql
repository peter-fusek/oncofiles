-- Patient selection for OAuth sessions (#291).
-- Maps owner email to their preferred patient for stateless HTTP.
-- Used by verify_token to resolve patient for OAuth connections.

CREATE TABLE IF NOT EXISTS patient_selection (
    owner_email TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
