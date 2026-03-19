-- Add gdrive_md5 column for content change detection during sync.
-- Prevents re-import when only metadata (name, folder) changed on GDrive.
ALTER TABLE documents ADD COLUMN gdrive_md5 TEXT;

-- Add UNIQUE constraint on gdrive_id to prevent duplicate imports.
-- First clean up any existing duplicates (keep the one with the most OCR pages).
CREATE TEMPORARY TABLE _dedup_keep AS
SELECT MIN(d.id) AS keep_id, d.gdrive_id
FROM documents d
LEFT JOIN (
    SELECT document_id, COUNT(*) AS page_count
    FROM document_pages
    GROUP BY document_id
) p ON p.document_id = d.id
WHERE d.gdrive_id IS NOT NULL
  AND d.deleted_at IS NULL
GROUP BY d.gdrive_id
HAVING COUNT(*) > 1;

-- Soft-delete duplicates (keep the oldest record per gdrive_id)
UPDATE documents
SET deleted_at = datetime('now')
WHERE gdrive_id IN (SELECT gdrive_id FROM _dedup_keep)
  AND id NOT IN (SELECT keep_id FROM _dedup_keep)
  AND deleted_at IS NULL;

DROP TABLE _dedup_keep;

-- Now create unique index (only for non-deleted, non-null gdrive_id)
CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_gdrive_id_unique
    ON documents(gdrive_id) WHERE gdrive_id IS NOT NULL AND deleted_at IS NULL;
