-- Migration 036: Make patient_context per-patient instead of global singleton
-- Previously: single row with id=1 and CHECK(id=1) enforcing singleton
-- Now: one row per patient_id with UNIQUE constraint, no singleton CHECK

-- Recreate table without CHECK(id=1) constraint
CREATE TABLE patient_context_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL DEFAULT '',
    context_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(patient_id)
);

-- Copy existing data, migrating erika's UUID from slug
INSERT INTO patient_context_new (id, patient_id, context_json, updated_at)
SELECT
    pc.id,
    COALESCE((SELECT p.patient_id FROM patients p WHERE p.slug = 'erika' LIMIT 1), ''),
    pc.context_json,
    pc.updated_at
FROM patient_context pc
WHERE pc.id = 1;

DROP TABLE patient_context;
ALTER TABLE patient_context_new RENAME TO patient_context;
