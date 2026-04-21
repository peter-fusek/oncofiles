-- Migration 059: Clinical records foundation (#450 Phase 1)
--
-- Introduces three new tables for a Chartfold-inspired clinical data model:
--
-- 1. `clinical_records`          — canonical clinical facts (labs, biomarkers,
--                                  medications, findings, procedures, etc.)
-- 2. `clinical_record_audit`     — append-only change history
-- 3. `clinical_record_notes`     — personal annotations from any source
--                                  (caregiver via dashboard, Claude.ai chat,
--                                   ChatGPT, Oncoteam, manual)
-- 4. `clinical_analyses`         — Oncoteam / AI analytic outputs referencing
--                                  one or more records
--
-- These tables live alongside the existing `patient_context` JSON blob and
-- `lab_values` / `treatment_events` tables. This migration is SCHEMA-ONLY —
-- no existing data is migrated. That is a separate, later migration (v5.12).
--
-- Turso safety:
--   * No ALTER TABLE RENAME (Hrana protocol silently drops it — per CLAUDE.md)
--   * No patient-specific seed data (the migration runs in test DBs too)
--   * Every index uses IF NOT EXISTS so re-runs are safe
--
-- Provenance columns on every row:
--   * `source`           — 'manual' | 'ai-extract' | 'oncoteam' | 'mcp-claude'
--                         | 'mcp-chatgpt' | 'import-*' | 'migration'
--   * `session_id`       — MCP/Claude.ai/ChatGPT conversation UUID if available
--   * `caller_identity`  — bearer-token hash OR OAuth sub
--   * `created_by` / `updated_by` / `deleted_by` — email of actor or 'system'
--   * Soft-delete only (30-day retention per global deletion policy)

CREATE TABLE IF NOT EXISTS clinical_records (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id        TEXT    NOT NULL REFERENCES patients(patient_id),
    record_type       TEXT    NOT NULL,
    -- 'lab', 'finding', 'medication', 'treatment_event', 'biomarker',
    -- 'condition', 'procedure', 'imaging', 'pathology', 'genetic_variant',
    -- 'allergy', 'vital'
    source_document_id INTEGER REFERENCES documents(id),
    occurred_at       TEXT,
    param             TEXT,
    value_num         REAL,
    value_text        TEXT,
    unit              TEXT,
    status            TEXT,
    ref_range_low     REAL,
    ref_range_high    REAL,
    metadata_json     TEXT,
    source            TEXT    NOT NULL,
    session_id        TEXT,
    caller_identity   TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_by        TEXT,
    updated_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_by        TEXT,
    deleted_at        TEXT,
    deleted_by        TEXT
);

CREATE INDEX IF NOT EXISTS idx_cr_patient_type
    ON clinical_records(patient_id, record_type)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_cr_patient_occurred
    ON clinical_records(patient_id, occurred_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_cr_patient_param
    ON clinical_records(patient_id, param)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_cr_source_doc
    ON clinical_records(source_document_id);


CREATE TABLE IF NOT EXISTS clinical_record_audit (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id         INTEGER NOT NULL REFERENCES clinical_records(id),
    action            TEXT    NOT NULL,  -- 'create' | 'update' | 'delete' | 'restore'
    before_json       TEXT,
    after_json        TEXT,
    changed_fields    TEXT,              -- comma-separated field names
    reason            TEXT,
    source            TEXT    NOT NULL,
    session_id        TEXT,
    caller_identity   TEXT,
    changed_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    changed_by        TEXT
);

CREATE INDEX IF NOT EXISTS idx_cra_record
    ON clinical_record_audit(record_id, changed_at DESC);

CREATE INDEX IF NOT EXISTS idx_cra_session
    ON clinical_record_audit(session_id);


CREATE TABLE IF NOT EXISTS clinical_record_notes (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id              INTEGER NOT NULL REFERENCES clinical_records(id),
    note_text              TEXT    NOT NULL,
    tags                   TEXT,              -- JSON array of tag strings
    source                 TEXT    NOT NULL,  -- 'dashboard' | 'mcp-claude' | ...
    session_id             TEXT,
    mcp_conversation_ref   TEXT,
    caller_identity        TEXT,
    created_at             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_by             TEXT,
    updated_at             TEXT,
    updated_by             TEXT,
    deleted_at             TEXT,
    deleted_by             TEXT
);

CREATE INDEX IF NOT EXISTS idx_crn_record
    ON clinical_record_notes(record_id, created_at DESC)
    WHERE deleted_at IS NULL;


CREATE TABLE IF NOT EXISTS clinical_analyses (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id        TEXT    NOT NULL REFERENCES patients(patient_id),
    record_ids        TEXT,              -- JSON array of clinical_records.id
    analysis_type     TEXT    NOT NULL,
    -- 'sii_trend' | 'ne_ly_ratio' | 'lab_delta' | 'biomarker_safety_check' |
    -- 'trial_eligibility' | 'session_note' | 'precycle_checklist' | ...
    result_json       TEXT    NOT NULL,
    result_summary    TEXT,
    tags              TEXT,              -- JSON array
    produced_by       TEXT    NOT NULL,  -- 'oncoteam' | 'oncofiles-internal' | 'external-ai'
    session_id        TEXT,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_ca_patient_type
    ON clinical_analyses(patient_id, analysis_type);

CREATE INDEX IF NOT EXISTS idx_ca_patient_time
    ON clinical_analyses(patient_id, created_at DESC);
