-- Multi-event document cloning (#460). Represents a "virtual copy" of a
-- source document filed in a specific YYYY-MM folder for a specific clinical
-- event (initially scoped to vaccination logs).
--
-- The source document remains canonical — these are pointers with their
-- own event_date + event_label, and optional GDrive shortcut file id.
--
-- Peter's design picks (2026-04-21): (A) separate table not virtual rows;
-- (A) GDrive shortcuts as physical manifestation; (B) explicit MCP trigger
-- rather than auto-on-enhance; and (yes) UNIQUE constraint to prevent
-- re-cloning on repeated calls.

CREATE TABLE IF NOT EXISTS document_references (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id           TEXT    NOT NULL,
    source_document_id   INTEGER NOT NULL REFERENCES documents(id),
    event_date           TEXT    NOT NULL,  -- YYYY-MM-DD
    event_label          TEXT    NOT NULL DEFAULT '',  -- e.g. "HepatitisB" / "MMR_booster"
    kind                 TEXT    NOT NULL DEFAULT 'vaccination',  -- widen if #460 expands beyond vaccines
    gdrive_shortcut_id   TEXT,   -- GDrive file_id of the shortcut file (nullable — DB-only clones OK)
    target_folder_id     TEXT,   -- YYYY-MM folder the shortcut lives in
    metadata_json        TEXT    NOT NULL DEFAULT '{}',
    created_at           TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE(source_document_id, event_date, event_label)
);

CREATE INDEX IF NOT EXISTS idx_document_references_patient
    ON document_references(patient_id);

CREATE INDEX IF NOT EXISTS idx_document_references_event_date
    ON document_references(event_date);

CREATE INDEX IF NOT EXISTS idx_document_references_source
    ON document_references(source_document_id);
