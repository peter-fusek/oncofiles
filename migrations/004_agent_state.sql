-- Agent state key-value store (#32)
-- Allows agents (oncoteam etc.) to persist state across sessions.

CREATE TABLE IF NOT EXISTS agent_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL DEFAULT 'oncoteam',
    key TEXT NOT NULL,
    value TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(agent_id, key)
);
CREATE INDEX IF NOT EXISTS idx_agent_state_agent_id ON agent_state(agent_id);
