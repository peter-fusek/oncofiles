-- Migration 041: Fix patient_context table — remove CHECK(id=1) constraint.
-- Migration 036 failed silently on Turso (DROP+RENAME not supported).
-- Table is empty (context loaded from memory), safe to recreate.

DROP TABLE IF EXISTS patient_context;

CREATE TABLE patient_context (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(patient_id)
);

-- Erika's context is managed by the app (JSON/env/update_patient_context).
-- Only seed e5g (Peter Fusek) who has no other context source.
INSERT INTO patient_context (patient_id, context_json, updated_at)
VALUES (
    '40a0e0c2-ddc7-4402-909a-0b0f09926917',
    '{"name":"Peter Fusek","patient_type":"general","date_of_birth":"1980-05-06","sex":"male","diagnosis":"","staging":"","histology":"","tumor_site":"","diagnosis_date":"","biomarkers":{},"treatment":{},"metastases":[],"comorbidities":[],"surgeries":[{"date":"1998-06-25","procedure":"Laparoscopic varicocelectomy (left)"}],"physicians":{},"excluded_therapies":[],"note":"General health patient. Preventive care, periodic screenings."}',
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
);
