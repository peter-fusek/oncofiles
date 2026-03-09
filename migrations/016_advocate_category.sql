-- Recategorize patient advocate notes from 'other' to 'advocate'
UPDATE documents
SET category = 'advocate'
WHERE institution = 'PacientAdvokat'
  AND category = 'other'
  AND deleted_at IS NULL;
