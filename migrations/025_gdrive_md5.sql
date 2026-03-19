-- Add gdrive_md5 column for content change detection during sync.
-- Prevents re-import when only metadata (name, folder) changed on GDrive.
ALTER TABLE documents ADD COLUMN gdrive_md5 TEXT;

-- Soft-delete duplicate gdrive_ids (keep oldest record per gdrive_id).
-- Uses strftime for Turso compatibility (datetime('now') not supported).
UPDATE documents
SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE id NOT IN (
    SELECT MIN(id) FROM documents
    WHERE gdrive_id IS NOT NULL AND deleted_at IS NULL
    GROUP BY gdrive_id
)
AND gdrive_id IS NOT NULL
AND deleted_at IS NULL;

-- Unique index on gdrive_id (only for non-deleted, non-null).
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_gdrive_id_unique
    ON documents(gdrive_id) WHERE gdrive_id IS NOT NULL AND deleted_at IS NULL;
