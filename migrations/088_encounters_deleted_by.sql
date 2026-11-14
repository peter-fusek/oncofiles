-- #795: record WHY a planned encounter was soft-deleted (re-extraction or a
-- user-invoked curated_clinical_retract), like the other hard-fact tables.
-- Additive only: one nullable column, no table rebuild, no data change.
-- Existing rows keep deleted_by = NULL.
-- Idempotent: if the column already exists, Database.migrate() falls back to
-- best-effort per-statement execution and logs the "duplicate column" error.
-- Rollback: the column can simply be ignored; dropping it would need a table
-- rebuild, so leave it in place.
ALTER TABLE encounters ADD COLUMN deleted_by TEXT;
