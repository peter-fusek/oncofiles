-- Migration 045: Fix e5g document dates incorrectly set to 2026-04-05 (bulk upload date)
-- Fix for #262: ~40 Peter Fusek (e5g) docs have document_date='2026-04-05', which is
-- the GDrive scan/import date. The actual medical dates are in
-- structured_metadata.dates_mentioned, extracted by AI enhancement, but
-- backfill_document_fields uses COALESCE so it never overwrote the already-set wrong value.
--
-- Strategy: for each affected doc, find MIN (earliest) valid date in dates_mentioned
-- that is not '2026-04-05' and falls in a plausible historical range (1970-2025).
-- Uses json_each() to iterate the JSON array. Only updates if a valid candidate exists.

UPDATE documents
SET
    document_date = (
        SELECT MIN(je.value)
        FROM json_each(structured_metadata, '$.dates_mentioned') AS je
        WHERE date(je.value) IS NOT NULL
          AND je.value != '2026-04-05'
          AND je.value >= '1970-01-01'
          AND je.value <= '2025-12-31'
    ),
    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE document_date = '2026-04-05'
  AND structured_metadata IS NOT NULL
  AND json_valid(structured_metadata)
  AND (
        SELECT MIN(je.value)
        FROM json_each(structured_metadata, '$.dates_mentioned') AS je
        WHERE date(je.value) IS NOT NULL
          AND je.value != '2026-04-05'
          AND je.value >= '1970-01-01'
          AND je.value <= '2025-12-31'
  ) IS NOT NULL;
