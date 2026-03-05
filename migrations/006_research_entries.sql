-- Research entries (#33)
-- PubMed articles, clinical trials, and other research saved by agents.

CREATE TABLE IF NOT EXISTS research_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    raw_data TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_research_entries_source ON research_entries(source);
CREATE INDEX IF NOT EXISTS idx_research_entries_external_id ON research_entries(external_id);
