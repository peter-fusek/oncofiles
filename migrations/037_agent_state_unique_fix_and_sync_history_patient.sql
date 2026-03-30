-- Migration 037: Fix agent_state UNIQUE constraint + add patient_id to sync_history
--
-- agent_state: Drop legacy UNIQUE(agent_id, key) that prevents per-patient isolation.
-- Recreate table with only UNIQUE(patient_id, agent_id, key).
--
-- sync_history: Add patient_id column for per-patient sync tracking.

-- ── agent_state: recreate without legacy UNIQUE ──

CREATE TABLE agent_state_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL DEFAULT 'oncoteam',
    "key" TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    patient_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(patient_id, agent_id, "key")
);

INSERT INTO agent_state_new (id, agent_id, "key", value, patient_id, created_at, updated_at)
SELECT id, agent_id, "key", value, patient_id, created_at, updated_at
FROM agent_state;

DROP TABLE agent_state;
ALTER TABLE agent_state_new RENAME TO agent_state;

CREATE INDEX IF NOT EXISTS idx_agent_state_agent_id ON agent_state(agent_id);

-- ── sync_history: add patient_id ──

ALTER TABLE sync_history ADD COLUMN patient_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_sync_history_patient ON sync_history(patient_id);
