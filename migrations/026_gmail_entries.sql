-- Gmail email entries
CREATE TABLE IF NOT EXISTS email_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default',
    gmail_message_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    recipients TEXT NOT NULL DEFAULT '[]',
    date TEXT NOT NULL,
    body_snippet TEXT NOT NULL DEFAULT '',
    body_text TEXT NOT NULL DEFAULT '',
    labels TEXT NOT NULL DEFAULT '[]',
    has_attachments INTEGER NOT NULL DEFAULT 0,
    ai_summary TEXT,
    ai_relevance_score REAL,
    structured_metadata TEXT,
    linked_document_ids TEXT NOT NULL DEFAULT '[]',
    is_medical INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, gmail_message_id)
);

CREATE INDEX IF NOT EXISTS idx_email_entries_date ON email_entries(date);
CREATE INDEX IF NOT EXISTS idx_email_entries_user ON email_entries(user_id);
CREATE INDEX IF NOT EXISTS idx_email_entries_thread ON email_entries(thread_id);
CREATE INDEX IF NOT EXISTS idx_email_entries_medical ON email_entries(is_medical);

-- Track which Google API scopes the user has granted
ALTER TABLE oauth_tokens ADD COLUMN granted_scopes TEXT NOT NULL DEFAULT '[]';

-- Track document source (email attachment, manual upload, etc.)
ALTER TABLE documents ADD COLUMN source TEXT;
ALTER TABLE documents ADD COLUMN source_ref TEXT;
