-- Add patient_id to prompt_log for multi-patient data isolation.
ALTER TABLE prompt_log ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
CREATE INDEX idx_prompt_log_patient_id ON prompt_log(patient_id);
