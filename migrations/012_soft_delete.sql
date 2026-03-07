-- Soft delete support: deleted_at column
-- Uses ALTER TABLE since SQLite doesn't support IF NOT EXISTS for columns
-- The migrate() method handles "column already exists" errors gracefully
ALTER TABLE documents ADD COLUMN deleted_at TEXT;
