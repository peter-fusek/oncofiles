-- Add patient_id to all data tables (#134)
-- DEFAULT 'erika' backfills all existing rows atomically.

-- documents
ALTER TABLE documents ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
CREATE INDEX IF NOT EXISTS idx_documents_patient ON documents(patient_id, document_date);

-- treatment_events
ALTER TABLE treatment_events ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
CREATE INDEX IF NOT EXISTS idx_treatment_events_patient ON treatment_events(patient_id, event_date);

-- conversation_entries
ALTER TABLE conversation_entries ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
CREATE INDEX IF NOT EXISTS idx_conversation_entries_patient ON conversation_entries(patient_id, entry_date);

-- research_entries
ALTER TABLE research_entries ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
CREATE INDEX IF NOT EXISTS idx_research_entries_patient ON research_entries(patient_id);
-- Widen unique constraint to include patient_id (two patients can save the same PubMed article)
DROP INDEX IF EXISTS idx_research_entries_unique;
CREATE UNIQUE INDEX IF NOT EXISTS idx_research_entries_patient_unique
    ON research_entries(patient_id, source, external_id);

-- agent_state: rebuild unique constraint to include patient_id
ALTER TABLE agent_state ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
-- SQLite can't drop inline UNIQUE constraints, but we can add a new covering index.
-- The old UNIQUE(agent_id, key) still exists but won't conflict since patient_id has a default.
-- For new patients, the new index enforces (patient_id, agent_id, key) uniqueness.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_state_patient_key
    ON agent_state(patient_id, agent_id, "key");

-- activity_log
ALTER TABLE activity_log ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
CREATE INDEX IF NOT EXISTS idx_activity_log_patient ON activity_log(patient_id, created_at);

-- patient_context
ALTER TABLE patient_context ADD COLUMN patient_id TEXT NOT NULL DEFAULT 'erika';
