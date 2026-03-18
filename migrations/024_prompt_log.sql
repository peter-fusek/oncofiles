-- Prompt observability: log all AI calls (OCR, enhance, metadata, filename)
CREATE TABLE IF NOT EXISTS prompt_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    call_type TEXT NOT NULL,
    document_id INTEGER,
    model TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    user_prompt TEXT NOT NULL DEFAULT '',
    raw_response TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER,
    output_tokens INTEGER,
    duration_ms INTEGER,
    result_summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_prompt_log_call_type ON prompt_log(call_type);
CREATE INDEX IF NOT EXISTS idx_prompt_log_document_id ON prompt_log(document_id);
CREATE INDEX IF NOT EXISTS idx_prompt_log_created_at ON prompt_log(created_at);
CREATE INDEX IF NOT EXISTS idx_prompt_log_model ON prompt_log(model);
