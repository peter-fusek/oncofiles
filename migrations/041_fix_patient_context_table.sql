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

-- Context data is seeded at runtime via update_patient_context MCP tool,
-- not in migrations (migrations run in test DBs and would break context tests).
