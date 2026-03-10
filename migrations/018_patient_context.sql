-- Patient context configuration (single-row key-value store)
CREATE TABLE IF NOT EXISTS patient_context (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    context_json TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
