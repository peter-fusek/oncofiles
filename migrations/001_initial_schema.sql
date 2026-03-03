-- Initial schema for erika-files-mcp
-- Compatible with both SQLite (local) and Turso libSQL (cloud)

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    document_date TEXT,          -- ISO 8601 date (YYYY-MM-DD)
    institution TEXT,
    category TEXT NOT NULL DEFAULT 'other',
    description TEXT,
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    gdrive_id TEXT,
    gdrive_modified_time TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_date ON documents(document_date);
CREATE INDEX IF NOT EXISTS idx_documents_institution ON documents(institution);
CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category);
CREATE INDEX IF NOT EXISTS idx_documents_gdrive_id ON documents(gdrive_id);

-- Full-text search index
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    filename,
    original_filename,
    institution,
    category,
    description,
    content='documents',
    content_rowid='id'
);

-- Triggers to keep FTS index in sync
CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN
    INSERT INTO documents_fts(rowid, filename, original_filename, institution, category, description)
    VALUES (new.id, new.filename, new.original_filename, new.institution, new.category, new.description);
END;

CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, filename, original_filename, institution, category, description)
    VALUES ('delete', old.id, old.filename, old.original_filename, old.institution, old.category, old.description);
END;

CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN
    INSERT INTO documents_fts(documents_fts, rowid, filename, original_filename, institution, category, description)
    VALUES ('delete', old.id, old.filename, old.original_filename, old.institution, old.category, old.description);
    INSERT INTO documents_fts(rowid, filename, original_filename, institution, category, description)
    VALUES (new.id, new.filename, new.original_filename, new.institution, new.category, new.description);
END;
