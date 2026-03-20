-- Calendar entries
CREATE TABLE IF NOT EXISTS calendar_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default',
    google_event_id TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    start_time TEXT NOT NULL,
    end_time TEXT,
    location TEXT,
    attendees TEXT NOT NULL DEFAULT '[]',
    recurrence TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',
    ai_summary TEXT,
    treatment_event_id INTEGER,
    is_medical INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, google_event_id)
);

CREATE INDEX IF NOT EXISTS idx_calendar_entries_start ON calendar_entries(start_time);
CREATE INDEX IF NOT EXISTS idx_calendar_entries_user ON calendar_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_calendar_entries_medical ON calendar_entries(is_medical);
CREATE INDEX IF NOT EXISTS idx_calendar_entries_treatment ON calendar_entries(treatment_event_id);
