-- Migration tracking table for idempotent migration runner
-- Records which migrations have been applied to prevent re-running
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
