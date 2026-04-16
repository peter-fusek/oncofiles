-- Migration 056: NULL out 'Unknown' institution placeholders so the daily
-- institution-backfill job can re-infer them properly.
--
-- Background (#393): ingest can leave the institution column set to the
-- literal string 'Unknown' when the parser can't identify an institution
-- from filename or metadata. The daily institution-backfill job at
-- 03:35 only processes rows where institution IS NULL, so these rows
-- stay stuck forever.
--
-- Fix: treat 'Unknown' as equivalent to NULL — a one-time reset. The
-- daily job will then pick them up and try to infer the real institution
-- from structured_metadata.providers or AI re-classification.
--
-- Side-effect-free: no data loss, no cost, preserves all other columns.
-- Affects any patient (not patient-specific — see feedback_turso_migration_pattern).

UPDATE documents SET institution = NULL WHERE institution = 'Unknown';
UPDATE documents SET institution = NULL WHERE institution = 'unknown';
