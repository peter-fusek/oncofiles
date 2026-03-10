-- Add sync state tracking to documents
ALTER TABLE documents ADD COLUMN sync_state TEXT NOT NULL DEFAULT 'synced';
ALTER TABLE documents ADD COLUMN last_synced_at TEXT;
