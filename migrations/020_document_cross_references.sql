-- Cross-references between related documents (same visit, follow-up, etc.)
CREATE TABLE IF NOT EXISTS document_cross_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER NOT NULL REFERENCES documents(id),
    target_document_id INTEGER NOT NULL REFERENCES documents(id),
    relationship TEXT NOT NULL DEFAULT 'related',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(source_document_id, target_document_id, relationship)
);

CREATE INDEX IF NOT EXISTS idx_cross_refs_source ON document_cross_references(source_document_id);
CREATE INDEX IF NOT EXISTS idx_cross_refs_target ON document_cross_references(target_document_id);
