-- Remove duplicate patient created with slug "erika" (0 docs, test artifact).
-- The real patient was already renamed to "q1b" in migration 049.
-- Guard: only delete if it has 0 documents. (#325)

DELETE FROM patient_tokens
WHERE patient_id = '5546aa1e-93ff-46a1-8e1e-688325c77db0';

DELETE FROM patients
WHERE patient_id = '5546aa1e-93ff-46a1-8e1e-688325c77db0'
  AND slug = 'erika'
  AND NOT EXISTS (
      SELECT 1 FROM documents WHERE patient_id = '5546aa1e-93ff-46a1-8e1e-688325c77db0'
  );
