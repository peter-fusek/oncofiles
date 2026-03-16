-- Sync history: persistent record of every sync run for observability
CREATE TABLE IF NOT EXISTS sync_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    trigger TEXT NOT NULL DEFAULT 'scheduled',  -- scheduled, manual, startup
    status TEXT NOT NULL DEFAULT 'running',       -- running, completed, failed
    duration_s REAL,
    from_gdrive_new INTEGER DEFAULT 0,
    from_gdrive_updated INTEGER DEFAULT 0,
    from_gdrive_errors INTEGER DEFAULT 0,
    to_gdrive_exported INTEGER DEFAULT 0,
    to_gdrive_organized INTEGER DEFAULT 0,
    to_gdrive_renamed INTEGER DEFAULT 0,
    to_gdrive_errors INTEGER DEFAULT 0,
    error_message TEXT,
    stats_json TEXT  -- full stats dict for detailed inspection
);

CREATE INDEX IF NOT EXISTS idx_sync_history_started ON sync_history(started_at);
