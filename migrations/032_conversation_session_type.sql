-- Migration 032: Add session_type column for patient vs technical split (#169)

ALTER TABLE conversation_entries ADD COLUMN session_type TEXT
    NOT NULL DEFAULT 'patient';

-- Classify existing entries based on participant
UPDATE conversation_entries SET session_type = 'technical'
WHERE participant = 'claude-code';

UPDATE conversation_entries SET session_type = 'patient'
WHERE participant IN ('claude.ai', 'oncoteam');

CREATE INDEX IF NOT EXISTS idx_conversation_entries_session_type
    ON conversation_entries(session_type);
