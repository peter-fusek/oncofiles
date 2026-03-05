-- Treatment events timeline (#34)
-- Structured treatment milestones: chemo cycles, surgeries, scans, etc.

CREATE TABLE IF NOT EXISTS treatment_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_date TEXT NOT NULL,
    event_type TEXT NOT NULL,
    title TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_treatment_events_date ON treatment_events(event_date);
CREATE INDEX IF NOT EXISTS idx_treatment_events_type ON treatment_events(event_type);
