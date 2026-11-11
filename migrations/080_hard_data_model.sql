-- Migration 080: hard-data model, phase 1 (#711, design docs/architecture/hard_data_model.md).
-- Additive only: CREATE TABLE / ADD COLUMN / INDEX. No rename, no DML, no
-- data moved, no patient data. Existing readers and writers are unaffected.
--
-- Shared building blocks: codings, provenance, devices, identifiers.
-- Fact tables: conditions, cancer_stages, treatment_courses, treatment_cycles,
-- medication_events, procedures, specimens, reports.
-- Every fact table carries fhir_id (UUID set by the writer), version_id,
-- last_updated, verification (QA state, default unverified), soft delete
-- (deleted_at / deleted_by), created_at and patient_id NOT NULL, plus the
-- denormalised current provenance (source_document_id, page_number, evidence,
-- extracted_by). The full history lives in provenance.
--
-- CHECK constraints only for small closed vocabularies: verification and the
-- FHIR R4 status sets. Our own growing vocabularies (owner_type, target_type,
-- activity, agent_role, procedure kind, stage system) are validated in Python,
-- because widening a CHECK later would need a table rebuild and Turso cannot
-- rename tables.
-- fact_codes.plausible_low / plausible_high (in unit_canonical, nullable, not
-- seeded here) let the generic verifier (#714) read plausibility from data.
-- No semicolons inside comments: the best-effort fallback splits on them.

CREATE TABLE IF NOT EXISTS devices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id         TEXT    NOT NULL UNIQUE,
    kind            TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    version         TEXT    NOT NULL DEFAULT '',
    model           TEXT,
    note            TEXT,
    version_id      INTEGER NOT NULL DEFAULT 1,
    last_updated    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at      TEXT,
    deleted_by      TEXT,
    UNIQUE (kind, name, version)
);

CREATE TABLE IF NOT EXISTS codings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      TEXT    REFERENCES patients(patient_id),
    owner_type      TEXT    NOT NULL,
    owner_id        TEXT    NOT NULL,
    system          TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    display         TEXT,
    system_version  TEXT,
    is_original     INTEGER NOT NULL DEFAULT 0 CHECK (is_original IN (0, 1)),
    mapped_by       TEXT,
    equivalence     TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at      TEXT,
    deleted_by      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_codings_owner_system_code
    ON codings(owner_type, owner_id, system, code) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_codings_patient ON codings(patient_id);
CREATE INDEX IF NOT EXISTS idx_codings_system_code ON codings(system, code);

CREATE TABLE IF NOT EXISTS identifiers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id      TEXT    REFERENCES patients(patient_id),
    owner_type      TEXT    NOT NULL,
    owner_id        INTEGER NOT NULL,
    system          TEXT    NOT NULL,
    value           TEXT    NOT NULL,
    type_text       TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at      TEXT,
    deleted_by      TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_identifiers_owner_system_value
    ON identifiers(owner_type, owner_id, system, value) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_identifiers_patient ON identifiers(patient_id);
CREATE INDEX IF NOT EXISTS idx_identifiers_system_value ON identifiers(system, value);

CREATE TABLE IF NOT EXISTS specimens (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id             TEXT    NOT NULL UNIQUE,
    patient_id          TEXT    NOT NULL REFERENCES patients(patient_id),
    type_printed        TEXT,
    collected_at        TEXT,
    received_at         TEXT,
    accession           TEXT,
    body_site           TEXT,
    encounter_id        INTEGER REFERENCES encounters(id),
    organization_id     INTEGER REFERENCES organizations(id),
    source_document_id  INTEGER REFERENCES documents(id),
    page_number         INTEGER,
    evidence            TEXT,
    extracted_by        TEXT,
    verification        TEXT    NOT NULL DEFAULT 'unverified'
                        CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id          INTEGER NOT NULL DEFAULT 1,
    last_updated        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at          TEXT,
    deleted_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_specimens_patient_collected ON specimens(patient_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_specimens_patient_accession ON specimens(patient_id, accession);
CREATE INDEX IF NOT EXISTS idx_specimens_source_document ON specimens(source_document_id);

CREATE TABLE IF NOT EXISTS reports (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id                     TEXT    NOT NULL UNIQUE,
    patient_id                  TEXT    NOT NULL REFERENCES patients(patient_id),
    panel_printed               TEXT,
    category                    TEXT,
    status                      TEXT    CHECK (status IN (
                                    'registered', 'partial', 'preliminary', 'final',
                                    'amended', 'corrected', 'appended', 'cancelled',
                                    'entered-in-error', 'unknown')),
    effective_at                TEXT,
    issued_at                   TEXT,
    specimen_id                 INTEGER REFERENCES specimens(id),
    interpreter_practitioner_id INTEGER REFERENCES practitioners(id),
    organization_id             INTEGER REFERENCES organizations(id),
    department_id               INTEGER REFERENCES departments(id),
    encounter_id                INTEGER REFERENCES encounters(id),
    conclusion                  TEXT,
    source_document_id          INTEGER REFERENCES documents(id),
    page_number                 INTEGER,
    evidence                    TEXT,
    extracted_by                TEXT,
    verification                TEXT    NOT NULL DEFAULT 'unverified'
                                CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id                  INTEGER NOT NULL DEFAULT 1,
    last_updated                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at                  TEXT,
    deleted_by                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_reports_patient_issued ON reports(patient_id, issued_at);
CREATE INDEX IF NOT EXISTS idx_reports_specimen ON reports(specimen_id);
CREATE INDEX IF NOT EXISTS idx_reports_source_document ON reports(source_document_id);

CREATE TABLE IF NOT EXISTS conditions (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id                  TEXT    NOT NULL UNIQUE,
    patient_id               TEXT    NOT NULL REFERENCES patients(patient_id),
    printed_text             TEXT    NOT NULL,
    category                 TEXT    CHECK (category IN ('problem-list-item', 'encounter-diagnosis')),
    clinical_status          TEXT    CHECK (clinical_status IN (
                                 'active', 'recurrence', 'relapse', 'inactive',
                                 'remission', 'resolved')),
    clinical_certainty       TEXT    CHECK (clinical_certainty IN (
                                 'unconfirmed', 'provisional', 'differential',
                                 'confirmed', 'refuted', 'entered-in-error')),
    onset_at                 TEXT,
    asserted_at              TEXT,
    abatement_at             TEXT,
    recorded_at              TEXT,
    body_site                TEXT,
    laterality               TEXT,
    is_primary_cancer        INTEGER NOT NULL DEFAULT 0 CHECK (is_primary_cancer IN (0, 1)),
    primary_condition_id     INTEGER REFERENCES conditions(id),
    grade                    TEXT,
    basis_of_diagnosis       TEXT,
    asserter_practitioner_id INTEGER REFERENCES practitioners(id),
    encounter_id             INTEGER REFERENCES encounters(id),
    source_document_id       INTEGER REFERENCES documents(id),
    page_number              INTEGER,
    evidence                 TEXT,
    extracted_by             TEXT,
    verification             TEXT    NOT NULL DEFAULT 'unverified'
                             CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id               INTEGER NOT NULL DEFAULT 1,
    last_updated             TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at               TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at               TEXT,
    deleted_by               TEXT
);

CREATE INDEX IF NOT EXISTS idx_conditions_patient_asserted ON conditions(patient_id, asserted_at);
CREATE INDEX IF NOT EXISTS idx_conditions_primary ON conditions(primary_condition_id);
CREATE INDEX IF NOT EXISTS idx_conditions_source_document ON conditions(source_document_id);

CREATE TABLE IF NOT EXISTS cancer_stages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id             TEXT    NOT NULL UNIQUE,
    patient_id          TEXT    NOT NULL REFERENCES patients(patient_id),
    condition_id        INTEGER NOT NULL REFERENCES conditions(id),
    printed             TEXT    NOT NULL,
    system              TEXT,
    edition             TEXT,
    prefix              TEXT,
    t                   TEXT,
    n                   TEXT,
    m                   TEXT,
    stage_group         TEXT,
    assessed_at         TEXT,
    source_document_id  INTEGER REFERENCES documents(id),
    page_number         INTEGER,
    evidence            TEXT,
    extracted_by        TEXT,
    verification        TEXT    NOT NULL DEFAULT 'unverified'
                        CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id          INTEGER NOT NULL DEFAULT 1,
    last_updated        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at          TEXT,
    deleted_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_cancer_stages_patient_condition
    ON cancer_stages(patient_id, condition_id, assessed_at);

CREATE TABLE IF NOT EXISTS treatment_courses (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id             TEXT    NOT NULL UNIQUE,
    patient_id          TEXT    NOT NULL REFERENCES patients(patient_id),
    regimen_printed     TEXT    NOT NULL,
    regimen_normalised  TEXT,
    line_of_therapy     INTEGER,
    intent              TEXT,
    setting             TEXT,
    start_at            TEXT,
    end_at              TEXT,
    status              TEXT    CHECK (status IN (
                            'draft', 'active', 'on-hold', 'revoked', 'completed',
                            'entered-in-error', 'unknown')),
    termination_reason  TEXT,
    condition_id        INTEGER REFERENCES conditions(id),
    source_document_id  INTEGER REFERENCES documents(id),
    page_number         INTEGER,
    evidence            TEXT,
    extracted_by        TEXT,
    verification        TEXT    NOT NULL DEFAULT 'unverified'
                        CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id          INTEGER NOT NULL DEFAULT 1,
    last_updated        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at          TEXT,
    deleted_by          TEXT
);

CREATE INDEX IF NOT EXISTS idx_treatment_courses_patient_start ON treatment_courses(patient_id, start_at);
CREATE INDEX IF NOT EXISTS idx_treatment_courses_source_document ON treatment_courses(source_document_id);

CREATE TABLE IF NOT EXISTS treatment_cycles (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id                   TEXT    NOT NULL UNIQUE,
    patient_id                TEXT    NOT NULL REFERENCES patients(patient_id),
    course_id                 INTEGER NOT NULL REFERENCES treatment_courses(id),
    cycle_number_printed      TEXT,
    planned_at                TEXT,
    actual_at                 TEXT,
    dose_modification_reason  TEXT,
    encounter_id              INTEGER REFERENCES encounters(id),
    source_document_id        INTEGER REFERENCES documents(id),
    page_number               INTEGER,
    evidence                  TEXT,
    extracted_by              TEXT,
    verification              TEXT    NOT NULL DEFAULT 'unverified'
                              CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id                INTEGER NOT NULL DEFAULT 1,
    last_updated              TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at                TEXT,
    deleted_by                TEXT
);

CREATE INDEX IF NOT EXISTS idx_treatment_cycles_patient_course
    ON treatment_cycles(patient_id, course_id, planned_at);

CREATE TABLE IF NOT EXISTS medication_events (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id                     TEXT    NOT NULL UNIQUE,
    patient_id                  TEXT    NOT NULL REFERENCES patients(patient_id),
    kind                        TEXT    NOT NULL
                                CHECK (kind IN ('request', 'administration', 'statement', 'dispense')),
    course_id                   INTEGER REFERENCES treatment_courses(id),
    cycle_id                    INTEGER REFERENCES treatment_cycles(id),
    request_id                  INTEGER REFERENCES medication_events(id),
    ingredient_printed          TEXT    NOT NULL,
    inn                         TEXT,
    dose_value                  REAL,
    dose_unit_ucum              TEXT,
    dose_printed                TEXT,
    normalization_basis         TEXT,
    route                       TEXT,
    timing_text                 TEXT,
    effective_start             TEXT,
    effective_end               TEXT,
    authored_at                 TEXT,
    prescriber_practitioner_id  INTEGER REFERENCES practitioners(id),
    status                      TEXT,
    encounter_id                INTEGER REFERENCES encounters(id),
    source_document_id          INTEGER REFERENCES documents(id),
    page_number                 INTEGER,
    evidence                    TEXT,
    extracted_by                TEXT,
    verification                TEXT    NOT NULL DEFAULT 'unverified'
                                CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id                  INTEGER NOT NULL DEFAULT 1,
    last_updated                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at                  TEXT,
    deleted_by                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_medication_events_patient_start
    ON medication_events(patient_id, effective_start);
CREATE INDEX IF NOT EXISTS idx_medication_events_course ON medication_events(course_id);
CREATE INDEX IF NOT EXISTS idx_medication_events_cycle ON medication_events(cycle_id);
CREATE INDEX IF NOT EXISTS idx_medication_events_request ON medication_events(request_id);
CREATE INDEX IF NOT EXISTS idx_medication_events_source_document
    ON medication_events(source_document_id);

CREATE TABLE IF NOT EXISTS procedures (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id                     TEXT    NOT NULL UNIQUE,
    patient_id                  TEXT    NOT NULL REFERENCES patients(patient_id),
    kind                        TEXT    NOT NULL,
    printed_text                TEXT    NOT NULL,
    performed_at                TEXT,
    performed_end               TEXT,
    body_site                   TEXT,
    laterality                  TEXT,
    performer_practitioner_id   INTEGER REFERENCES practitioners(id),
    organization_id             INTEGER REFERENCES organizations(id),
    outcome                     TEXT,
    condition_id                INTEGER REFERENCES conditions(id),
    encounter_id                INTEGER REFERENCES encounters(id),
    source_document_id          INTEGER REFERENCES documents(id),
    page_number                 INTEGER,
    evidence                    TEXT,
    extracted_by                TEXT,
    verification                TEXT    NOT NULL DEFAULT 'unverified'
                                CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected')),
    version_id                  INTEGER NOT NULL DEFAULT 1,
    last_updated                TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at                  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at                  TEXT,
    deleted_by                  TEXT
);

CREATE INDEX IF NOT EXISTS idx_procedures_patient_performed ON procedures(patient_id, performed_at);
CREATE INDEX IF NOT EXISTS idx_procedures_source_document ON procedures(source_document_id);

CREATE TABLE IF NOT EXISTS provenance (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    fhir_id             TEXT    NOT NULL UNIQUE,
    patient_id          TEXT    NOT NULL REFERENCES patients(patient_id),
    target_type         TEXT    NOT NULL,
    target_id           INTEGER NOT NULL,
    target_version      INTEGER,
    activity            TEXT    NOT NULL,
    agent_role          TEXT    NOT NULL,
    agent_device_id     INTEGER REFERENCES devices(id),
    agent_user          TEXT,
    source_document_id  INTEGER REFERENCES documents(id),
    page_number         INTEGER,
    char_offset         INTEGER,
    char_length         INTEGER,
    quote               TEXT,
    confidence          REAL,
    reason              TEXT,
    recorded_at         TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    created_at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_provenance_target ON provenance(patient_id, target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_provenance_source_document ON provenance(source_document_id);
CREATE INDEX IF NOT EXISTS idx_provenance_device ON provenance(agent_device_id);

ALTER TABLE clinical_records ADD COLUMN result_status TEXT CHECK (result_status IN (
    'registered', 'preliminary', 'final', 'amended', 'corrected', 'cancelled',
    'entered-in-error', 'unknown'));
ALTER TABLE clinical_records ADD COLUMN value_comparator TEXT
    CHECK (value_comparator IN ('<', '>', '<=', '>='));
ALTER TABLE clinical_records ADD COLUMN unit_printed TEXT;
ALTER TABLE clinical_records ADD COLUMN unit_ucum TEXT;
ALTER TABLE clinical_records ADD COLUMN interpretation_code TEXT;
ALTER TABLE clinical_records ADD COLUMN effective_start TEXT;
ALTER TABLE clinical_records ADD COLUMN effective_end TEXT;
ALTER TABLE clinical_records ADD COLUMN effective_precision TEXT
    CHECK (effective_precision IN ('year', 'month', 'day', 'datetime'));
ALTER TABLE clinical_records ADD COLUMN issued_at TEXT;
ALTER TABLE clinical_records ADD COLUMN specimen_id INTEGER REFERENCES specimens(id);
ALTER TABLE clinical_records ADD COLUMN report_id INTEGER REFERENCES reports(id);
ALTER TABLE clinical_records ADD COLUMN fhir_id TEXT;
ALTER TABLE clinical_records ADD COLUMN version_id INTEGER NOT NULL DEFAULT 1;
ALTER TABLE clinical_records ADD COLUMN last_updated TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_cr_fhir_id ON clinical_records(fhir_id) WHERE fhir_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cr_specimen ON clinical_records(specimen_id);
CREATE INDEX IF NOT EXISTS idx_cr_report ON clinical_records(report_id);

ALTER TABLE fact_codes ADD COLUMN plausible_low REAL;
ALTER TABLE fact_codes ADD COLUMN plausible_high REAL;
