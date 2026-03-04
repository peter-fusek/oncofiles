-- Per-page OCR text cache for document analysis (#36)
-- Stores extracted text from Claude Vision API alongside page images.
-- Per-page granularity because PDFs have multiple pages.
-- Model column enables future re-extraction with better models.

CREATE TABLE IF NOT EXISTS document_pages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL DEFAULT 1,
    extracted_text TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'claude-haiku-4-5-20251001',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(document_id, page_number)
);

CREATE INDEX IF NOT EXISTS idx_document_pages_document_id ON document_pages(document_id);
