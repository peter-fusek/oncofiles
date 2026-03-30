-- Migration 036: Make patient_context per-patient instead of global singleton
-- Previously: single row with id=1 shared across all patients
-- Now: one row per patient_id with UNIQUE constraint

ALTER TABLE patient_context ADD COLUMN patient_id TEXT NOT NULL DEFAULT '';

-- Migrate existing row (id=1) to erika's UUID
UPDATE patient_context
SET patient_id = (SELECT patient_id FROM patients WHERE slug = 'erika' LIMIT 1)
WHERE id = 1 AND patient_id = '';

CREATE UNIQUE INDEX IF NOT EXISTS idx_patient_context_patient
ON patient_context(patient_id);
