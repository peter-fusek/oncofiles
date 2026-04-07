-- Migration 046: Fix docs that got DOB (1980-05-06) as document_date
-- Fix for #262 follow-up: migration 045 used MIN(dates_mentioned) which picked
-- the patient's date of birth when it was the earliest date in the array.
-- This migration uses MAX to pick the latest plausible date instead.
-- Only affects docs where document_date is exactly '1980-05-06' (Peter Fusek's DOB).

UPDATE documents
SET
    document_date = (
        SELECT MAX(je.value)
        FROM json_each(structured_metadata, '$.dates_mentioned') AS je
        WHERE date(je.value) IS NOT NULL
          AND je.value != '1980-05-06'
          AND je.value >= '1970-01-01'
          AND je.value <= '2025-12-31'
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE document_date = '1980-05-06'
  AND structured_metadata IS NOT NULL
  AND json_valid(structured_metadata)
  AND (
        SELECT MAX(je.value)
        FROM json_each(structured_metadata, '$.dates_mentioned') AS je
        WHERE date(je.value) IS NOT NULL
          AND je.value != '1980-05-06'
          AND je.value >= '1970-01-01'
          AND je.value <= '2025-12-31'
  ) IS NOT NULL;
