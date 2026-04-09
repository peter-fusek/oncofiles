-- Anonymize patient slug: "erika" → "q1b" (#319).
-- Patient ID (UUID) stays the same — only the human-readable slug changes.
-- This prevents PII exposure in the public repo and API responses.

UPDATE patients SET slug = 'q1b' WHERE slug = 'erika';
