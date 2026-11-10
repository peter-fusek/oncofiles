-- Migration 079: verify-only re-reads (#678). One row per local re-read of a
-- curated document. result_json holds keys only — (code, value hash, day,
-- page) per confirmed / date-unconfirmed fact, never evidence text. A row is
-- valid only while the document's sidecar sha and source md5 still match.
-- Safe for Turso/libsql: plain CREATE TABLE / INDEX, no rename, no DML.

CREATE TABLE IF NOT EXISTS fact_rereads (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id   TEXT    NOT NULL,
    document_id  INTEGER NOT NULL,
    sidecar_sha  TEXT    NOT NULL,
    source_md5   TEXT    NOT NULL,
    engine       TEXT    NOT NULL,
    result_json  TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_fact_rereads_doc
    ON fact_rereads(patient_id, document_id, id);
