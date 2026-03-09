-- Lab values for trend tracking
-- One row per parameter per lab date, linked to source document
CREATE TABLE IF NOT EXISTS lab_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    lab_date TEXT NOT NULL,
    parameter TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL DEFAULT '',
    reference_low REAL,
    reference_high REAL,
    flag TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_lab_values_parameter ON lab_values(parameter, lab_date);
CREATE INDEX IF NOT EXISTS idx_lab_values_date ON lab_values(lab_date);
CREATE INDEX IF NOT EXISTS idx_lab_values_document ON lab_values(document_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lab_values_unique ON lab_values(document_id, parameter);
