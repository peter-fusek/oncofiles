-- Activity log (#38)
-- Append-only audit trail of agent tool calls.

CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    input_summary TEXT NOT NULL DEFAULT '',
    output_summary TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER,
    status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_activity_log_session_id ON activity_log(session_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_agent_id ON activity_log(agent_id);
CREATE INDEX IF NOT EXISTS idx_activity_log_tool_name ON activity_log(tool_name);
CREATE INDEX IF NOT EXISTS idx_activity_log_status ON activity_log(status);
CREATE INDEX IF NOT EXISTS idx_activity_log_created_at ON activity_log(created_at);
