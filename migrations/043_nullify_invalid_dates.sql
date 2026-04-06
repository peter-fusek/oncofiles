-- Migration 043: NULL out invalid document_date values
-- Fix for #258: AI-hallucinated dates like '2222-14-81' crash _row_to_document()
-- SQLite's date() returns NULL for invalid date strings, so we use that as the validator.

UPDATE documents
SET document_date = NULL,
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE document_date IS NOT NULL
  AND date(document_date) IS NULL;

-- Also fix any invalid dates in treatment_events, conversations, and lab_values
UPDATE treatment_events
SET event_date = NULL
WHERE event_date IS NOT NULL
  AND date(event_date) IS NULL;

UPDATE conversations
SET entry_date = NULL
WHERE entry_date IS NOT NULL
  AND date(entry_date) IS NULL;

UPDATE lab_values
SET lab_date = NULL
WHERE lab_date IS NOT NULL
  AND date(lab_date) IS NULL;
