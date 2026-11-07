-- Migration 071: curated-folder pipeline runs + per-patient nightly switch (#573).
--
-- curated_runs: one row per run (dry or real). The in-memory state in
-- tools/curated.py is lost on every deploy; the dashboard and MCP status read
-- the last finished run from here. report_json holds RunReport.to_dict().
-- curated_settings: admin-controlled nightly switch per patient.
--
-- Safe for Turso/libsql: plain CREATE TABLE / INDEX, no rename, no DML.

CREATE TABLE IF NOT EXISTS curated_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id   TEXT    NOT NULL,
    run_id       TEXT,
    trigger      TEXT    NOT NULL DEFAULT 'manual',
    dry_run      INTEGER NOT NULL DEFAULT 1,
    status       TEXT    NOT NULL DEFAULT 'running',
    message      TEXT,
    started_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    finished_at  TEXT,
    report_json  TEXT
);

CREATE INDEX IF NOT EXISTS idx_curated_runs_patient
    ON curated_runs(patient_id, id);

CREATE TABLE IF NOT EXISTS curated_settings (
    patient_id       TEXT PRIMARY KEY,
    nightly_enabled  INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_by       TEXT
);
