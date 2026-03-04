-- Conversation archive / worklog diary (#37)
-- Stores diary entries: summaries, decisions, progress notes, questions.
-- Complements documents with narrative content for the oncology journey.

CREATE TABLE IF NOT EXISTS conversation_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_date TEXT NOT NULL,                -- YYYY-MM-DD, date the entry is about
    entry_type TEXT NOT NULL DEFAULT 'note', -- summary, decision, progress, question, note
    title TEXT NOT NULL,
    content TEXT NOT NULL,                   -- markdown body
    participant TEXT NOT NULL DEFAULT 'claude.ai',  -- claude.ai, claude-code, oncoteam
    session_id TEXT,                         -- Claude session UUID
    tags TEXT,                               -- JSON array: ["chemo","FOLFOX"]
    document_ids TEXT,                       -- JSON array: [3, 15]
    source TEXT,                             -- 'live' or 'import'
    source_ref TEXT,                         -- JSONL filename for idempotent imports
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_conversation_entries_date ON conversation_entries(entry_date);
CREATE INDEX IF NOT EXISTS idx_conversation_entries_type ON conversation_entries(entry_type);
CREATE INDEX IF NOT EXISTS idx_conversation_entries_participant ON conversation_entries(participant);
CREATE INDEX IF NOT EXISTS idx_conversation_entries_source_ref ON conversation_entries(source_ref);

-- Full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS conversation_entries_fts USING fts5(
    title,
    content,
    tags,
    content='conversation_entries',
    content_rowid='id'
);

-- Triggers to keep FTS index in sync (same pattern as documents_fts in 001)
CREATE TRIGGER IF NOT EXISTS conversation_entries_ai AFTER INSERT ON conversation_entries BEGIN
    INSERT INTO conversation_entries_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS conversation_entries_ad AFTER DELETE ON conversation_entries BEGIN
    INSERT INTO conversation_entries_fts(conversation_entries_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS conversation_entries_au AFTER UPDATE ON conversation_entries BEGIN
    INSERT INTO conversation_entries_fts(conversation_entries_fts, rowid, title, content, tags)
    VALUES ('delete', old.id, old.title, old.content, old.tags);
    INSERT INTO conversation_entries_fts(rowid, title, content, tags)
    VALUES (new.id, new.title, new.content, new.tags);
END;
