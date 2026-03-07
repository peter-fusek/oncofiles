-- OAuth tokens for per-user Google Drive access
CREATE TABLE IF NOT EXISTS oauth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL DEFAULT 'default',
    provider TEXT NOT NULL DEFAULT 'google',
    access_token TEXT NOT NULL,
    refresh_token TEXT NOT NULL,
    token_expiry TEXT,
    gdrive_folder_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(user_id, provider)
);
