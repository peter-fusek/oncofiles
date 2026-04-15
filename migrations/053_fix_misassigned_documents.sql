-- Migration 053: Fix documents assigned to wrong patient (#375)
--
-- Root cause: GDrive sync imported files before the correct patient record
-- existed, assigning them to a different patient. Later renames updated the
-- filename but not the patient_id column.
--
-- This migration finds documents where the filename contains a patient's
-- display_name (compact, no spaces) but the document is NOT assigned to
-- that patient, and reassigns them.
--
-- Safe: uses WHERE EXISTS guards, only runs when mismatch is clear.
-- Turso note: uses simple UPDATE, no ALTER TABLE RENAME.

-- Reassign docs whose filename contains "NoraAntalov" to nora-antalova patient
-- (the specific case from #375, but using dynamic lookup not hardcoded UUID)
UPDATE documents
SET patient_id = (
    SELECT patient_id FROM patients WHERE slug = 'nora-antalova'
)
WHERE deleted_at IS NULL
  AND (filename LIKE '%NoraAntalov%')
  AND patient_id != (SELECT patient_id FROM patients WHERE slug = 'nora-antalova')
  AND EXISTS (SELECT 1 FROM patients WHERE slug = 'nora-antalova');
