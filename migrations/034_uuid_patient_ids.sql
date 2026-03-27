-- UUID patient IDs: add slug column, assign deterministic UUIDs (#207)
--
-- Phase 1: Non-destructive — adds columns, backfills data.
-- Production continues using slugs until migration 035 swaps the PK.

-- Add slug column to patients (will hold the human-readable identifier)
ALTER TABLE patients ADD COLUMN slug TEXT;

-- Backfill slug from current patient_id (which IS the slug today)
UPDATE patients SET slug = patient_id;

-- Unique index on slug (enforces uniqueness before we make it a column constraint)
CREATE UNIQUE INDEX IF NOT EXISTS idx_patients_slug ON patients(slug);
