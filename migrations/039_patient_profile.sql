-- Migration 039: Add profile column to patients table for patient-type-aware features.
-- Supports "oncology" (default, backward-compatible) and "general" patient types.

ALTER TABLE patients ADD COLUMN profile TEXT DEFAULT 'oncology';

-- Set peter-fusek-2 as general health patient
UPDATE patients SET profile = 'general' WHERE slug = 'peter-fusek-2';
