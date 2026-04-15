-- Migration 054: Set caregiver_email on all patients for access control (#377)
--
-- With admin bypass removed from patient listing, every patient must have
-- caregiver_email set to the email(s) of users who should see them.
-- Supports comma-separated emails for multiple caregivers.

-- q1b (Erika): Peter is caregiver via both emails
UPDATE patients
SET caregiver_email = 'peterfusek1980@gmail.com,peter.fusek@instarea.sk'
WHERE slug = 'q1b'
  AND EXISTS (SELECT 1 FROM patients WHERE slug = 'q1b');

-- e5g (Peter F.): Peter's own patient
UPDATE patients
SET caregiver_email = 'peterfusek1980@gmail.com,peter.fusek@instarea.sk'
WHERE slug = 'e5g'
  AND EXISTS (SELECT 1 FROM patients WHERE slug = 'e5g');

-- peter-fusek-2 (archived): Peter's old patient
UPDATE patients
SET caregiver_email = 'peterfusek1980@gmail.com,peter.fusek@instarea.sk'
WHERE slug = 'peter-fusek-2'
  AND EXISTS (SELECT 1 FROM patients WHERE slug = 'peter-fusek-2');

-- test-patient: Peter only (for testing)
UPDATE patients
SET caregiver_email = 'peterfusek1980@gmail.com'
WHERE slug = 'test-patient'
  AND EXISTS (SELECT 1 FROM patients WHERE slug = 'test-patient');

-- nora-antalova: already correct (nora.antalova@gmail.com), no change needed
