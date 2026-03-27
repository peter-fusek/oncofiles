-- UUID patient IDs: swap PK from slug to UUID (#207)
--
-- Phase 2: Rebuild patients table with UUID as primary key.
-- Update all data table patient_id values from slug to UUID.

PRAGMA foreign_keys = OFF;

-- ── Generate UUIDs for all patients ───────────────────────────────────────

-- Deterministic UUIDs for known prod patients (reproducible across envs)
UPDATE patients SET slug = patient_id WHERE slug IS NULL;

-- Create mapping table with UUIDs
CREATE TABLE _patient_uuid_map (
    old_id TEXT PRIMARY KEY,
    new_id TEXT NOT NULL
);

-- Known patients get deterministic UUIDs
INSERT OR IGNORE INTO _patient_uuid_map (old_id, new_id)
VALUES
    ('erika',         '00000000-0000-4000-8000-000000000001'),
    ('test-patient',  '00000000-0000-4000-8000-000000000002'),
    ('peter-fusek-2', '00000000-0000-4000-8000-000000000003');

-- Any other patients get generated UUIDs
INSERT OR IGNORE INTO _patient_uuid_map (old_id, new_id)
SELECT patient_id,
    lower(
        substr(hex(randomblob(4)),1,8) || '-' ||
        substr(hex(randomblob(2)),1,4) || '-4' ||
        substr(hex(randomblob(2)),2,3) || '-' ||
        substr('89ab', abs(random()) % 4 + 1, 1) ||
        substr(hex(randomblob(2)),2,3) || '-' ||
        substr(hex(randomblob(6)),1,12)
    )
FROM patients
WHERE patient_id NOT IN (SELECT old_id FROM _patient_uuid_map);

-- ── Rebuild patients table ────────────────────────────────────────────────

CREATE TABLE patients_new (
    patient_id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    caregiver_email TEXT,
    diagnosis_summary TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    preferred_lang TEXT NOT NULL DEFAULT 'sk',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO patients_new
    (patient_id, slug, display_name, caregiver_email, diagnosis_summary,
     is_active, preferred_lang, created_at, updated_at)
SELECT m.new_id, p.patient_id, p.display_name, p.caregiver_email, p.diagnosis_summary,
       p.is_active, p.preferred_lang, p.created_at, p.updated_at
FROM patients p
JOIN _patient_uuid_map m ON m.old_id = p.patient_id;

DROP TABLE patients;
ALTER TABLE patients_new RENAME TO patients;

-- ── Rebuild patient_tokens (has FK reference) ─────────────────────────────

CREATE TABLE patient_tokens_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

INSERT INTO patient_tokens_new (id, patient_id, token_hash, label, is_active, created_at)
SELECT pt.id, m.new_id, pt.token_hash, pt.label, pt.is_active, pt.created_at
FROM patient_tokens pt
JOIN _patient_uuid_map m ON m.old_id = pt.patient_id;

DROP TABLE patient_tokens;
ALTER TABLE patient_tokens_new RENAME TO patient_tokens;
CREATE INDEX IF NOT EXISTS idx_patient_tokens_hash ON patient_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_patient_tokens_patient ON patient_tokens(patient_id);

-- ── Update data tables (patient_id TEXT → UUID value) ─────────────────────
-- No table rebuild needed — just UPDATE the column value via mapping table.

UPDATE documents SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = documents.patient_id);
UPDATE treatment_events SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = treatment_events.patient_id);
UPDATE conversation_entries SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = conversation_entries.patient_id);
UPDATE research_entries SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = research_entries.patient_id);
UPDATE agent_state SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = agent_state.patient_id);
UPDATE activity_log SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = activity_log.patient_id);
UPDATE patient_context SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = patient_context.patient_id);
UPDATE email_entries SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = email_entries.patient_id);
UPDATE calendar_entries SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = calendar_entries.patient_id);
UPDATE oauth_tokens SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = oauth_tokens.patient_id);
UPDATE prompt_log SET patient_id = (SELECT new_id FROM _patient_uuid_map WHERE old_id = prompt_log.patient_id);

-- ── Cleanup ───────────────────────────────────────────────────────────────

DROP TABLE _patient_uuid_map;

PRAGMA foreign_keys = ON;
