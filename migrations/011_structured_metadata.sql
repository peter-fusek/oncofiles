-- Add structured_metadata column for extracted medical metadata (JSON)
-- Uses ALTER TABLE since SQLite doesn't support IF NOT EXISTS for columns
-- The migrate() method handles "column already exists" errors gracefully
ALTER TABLE documents ADD COLUMN structured_metadata TEXT;
