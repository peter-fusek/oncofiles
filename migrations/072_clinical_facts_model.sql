-- Migration 072: clinical facts model (#603).
-- Every fact (lab value, marker, variant, diagnosis, medication, procedure)
-- lives in clinical_records with provenance (document, page, evidence quote,
-- extractor), a verification state, a normalized code, and links to the
-- doctor, institution, department and visit. Organizations and departments
-- are shared reference data. Practitioners are scoped per patient so nothing
-- reveals which other patients a doctor treats. Views do the stitching for
-- timelines and series. Additive only: CREATE / ADD COLUMN / VIEW, no rename,
-- no patient data. Seed rows are reference vocabulary only.

CREATE TABLE IF NOT EXISTS organizations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    code        TEXT    NOT NULL UNIQUE,
    name        TEXT,
    address     TEXT,
    kind        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS departments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id  INTEGER NOT NULL REFERENCES organizations(id),
    code             TEXT    NOT NULL,
    name             TEXT,
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (organization_id, code)
);

CREATE TABLE IF NOT EXISTS practitioners (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id       TEXT    NOT NULL REFERENCES patients(patient_id),
    surname_token    TEXT    NOT NULL,
    display_name     TEXT,
    role             TEXT,
    organization_id  INTEGER REFERENCES organizations(id),
    department_id    INTEGER REFERENCES departments(id),
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    UNIQUE (patient_id, surname_token)
);

CREATE TABLE IF NOT EXISTS encounters (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id       TEXT    NOT NULL REFERENCES patients(patient_id),
    start_at         TEXT    NOT NULL,
    end_at           TEXT,
    kind             TEXT    NOT NULL DEFAULT 'visit',
    title            TEXT,
    organization_id  INTEGER REFERENCES organizations(id),
    department_id    INTEGER REFERENCES departments(id),
    practitioner_id  INTEGER REFERENCES practitioners(id),
    source           TEXT    NOT NULL DEFAULT 'rule',
    created_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    updated_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at       TEXT,
    natural_key      TEXT    NOT NULL,
    UNIQUE (patient_id, natural_key)
);

CREATE INDEX IF NOT EXISTS idx_encounters_patient_start ON encounters(patient_id, start_at);

CREATE TABLE IF NOT EXISTS fact_codes (
    code              TEXT PRIMARY KEY,
    category          TEXT NOT NULL,
    name_sk           TEXT NOT NULL,
    name_en           TEXT NOT NULL,
    loinc             TEXT,
    unit_canonical    TEXT,
    aliases           TEXT NOT NULL DEFAULT '[]',
    unit_conversions  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id  TEXT    NOT NULL REFERENCES patients(patient_id),
    from_type   TEXT    NOT NULL,
    from_id     INTEGER NOT NULL,
    to_type     TEXT    NOT NULL,
    to_id       INTEGER NOT NULL,
    relation    TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'rule',
    confidence  REAL,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    deleted_at  TEXT,
    UNIQUE (patient_id, from_type, from_id, to_type, to_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_links_from ON links(patient_id, from_type, from_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(patient_id, to_type, to_id);

ALTER TABLE documents ADD COLUMN title TEXT;
ALTER TABLE documents ADD COLUMN source_url TEXT;
ALTER TABLE documents ADD COLUMN encounter_id INTEGER REFERENCES encounters(id);
ALTER TABLE documents ADD COLUMN practitioner_id INTEGER REFERENCES practitioners(id);
ALTER TABLE documents ADD COLUMN organization_id INTEGER REFERENCES organizations(id);
ALTER TABLE documents ADD COLUMN department_id INTEGER REFERENCES departments(id);

ALTER TABLE clinical_records ADD COLUMN code TEXT;
ALTER TABLE clinical_records ADD COLUMN page_number INTEGER;
ALTER TABLE clinical_records ADD COLUMN evidence TEXT;
ALTER TABLE clinical_records ADD COLUMN value_canonical REAL;
ALTER TABLE clinical_records ADD COLUMN unit_canonical TEXT;
ALTER TABLE clinical_records ADD COLUMN flag TEXT;
ALTER TABLE clinical_records ADD COLUMN ref_text TEXT;
ALTER TABLE clinical_records ADD COLUMN encounter_id INTEGER REFERENCES encounters(id);
ALTER TABLE clinical_records ADD COLUMN practitioner_id INTEGER REFERENCES practitioners(id);
ALTER TABLE clinical_records ADD COLUMN organization_id INTEGER REFERENCES organizations(id);
ALTER TABLE clinical_records ADD COLUMN department_id INTEGER REFERENCES departments(id);
ALTER TABLE clinical_records ADD COLUMN verification TEXT NOT NULL DEFAULT 'machine';
ALTER TABLE clinical_records ADD COLUMN extracted_by TEXT;
ALTER TABLE clinical_records ADD COLUMN confidence REAL;

ALTER TABLE treatment_events ADD COLUMN source_document_id INTEGER REFERENCES documents(id);
ALTER TABLE treatment_events ADD COLUMN encounter_id INTEGER REFERENCES encounters(id);

CREATE INDEX IF NOT EXISTS idx_cr_patient_code_time ON clinical_records(patient_id, code, occurred_at);
CREATE INDEX IF NOT EXISTS idx_cr_source_document ON clinical_records(source_document_id);
CREATE INDEX IF NOT EXISTS idx_documents_encounter ON documents(encounter_id);

CREATE VIEW IF NOT EXISTS fact_series_v AS
SELECT f.id, f.patient_id, f.code, f.record_type AS category,
       COALESCE(fc.name_en, f.param) AS name_en, COALESCE(fc.name_sk, f.param) AS name_sk,
       f.param AS label_original, f.occurred_at AS effective_at,
       f.value_num, f.value_text, f.unit,
       COALESCE(f.value_canonical, f.value_num) AS value_canonical,
       COALESCE(f.unit_canonical, fc.unit_canonical, f.unit) AS unit_canonical,
       f.ref_range_low, f.ref_range_high, f.ref_text, f.flag, f.verification,
       f.source_document_id, f.page_number, f.evidence, f.encounter_id,
       f.organization_id, f.department_id, f.practitioner_id, f.extracted_by, f.confidence
  FROM clinical_records f LEFT JOIN fact_codes fc ON fc.code = f.code
 WHERE f.deleted_at IS NULL;

CREATE VIEW IF NOT EXISTS latest_fact_v AS
SELECT s.* FROM fact_series_v s
 WHERE s.code IS NOT NULL AND s.verification != 'unverified'
   AND s.effective_at = (
       SELECT MAX(s2.effective_at) FROM fact_series_v s2
        WHERE s2.patient_id = s.patient_id AND s2.code = s.code
          AND s2.verification != 'unverified');

CREATE VIEW IF NOT EXISTS timeline_v AS
SELECT e.patient_id, e.start_at AS at, 'encounter' AS kind, e.title AS title,
       NULL AS value, NULL AS unit, NULL AS flag, NULL AS verification,
       NULL AS source_document_id, 'encounter' AS entity, e.id AS entity_id,
       e.organization_id, e.practitioner_id
  FROM encounters e WHERE e.deleted_at IS NULL
UNION ALL
SELECT d.patient_id, d.document_date, 'document', COALESCE(d.title, d.filename),
       NULL, NULL, NULL, NULL, d.id, 'document', d.id, d.organization_id, d.practitioner_id
  FROM documents d WHERE d.deleted_at IS NULL
UNION ALL
SELECT t.patient_id, t.event_date, 'treatment', t.title, NULL, NULL, NULL, NULL,
       t.source_document_id, 'treatment_event', t.id, NULL, NULL
  FROM treatment_events t
UNION ALL
SELECT s.patient_id, s.effective_at, s.category, s.name_en,
       COALESCE(s.value_text, CAST(s.value_num AS TEXT)), s.unit, s.flag, s.verification,
       s.source_document_id, 'fact', s.id, s.organization_id, s.practitioner_id
  FROM fact_series_v s
 WHERE s.category IN ('condition', 'procedure', 'genetic_variant', 'biomarker', 'medication')
    OR s.flag IS NOT NULL;

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('WBC', 'lab', 'Leukocyty', 'White blood cells', '6690-2', '10^9/L', '["WBC","Leukocyty","Leu","White blood cells"]', '{"g/l": 1, "/nl": 1}'),
('RBC', 'lab', 'Erytrocyty', 'Red blood cells', '789-8', '10^12/L', '["RBC","Erytrocyty","Ery"]', '{"t/l": 1}'),
('HGB', 'lab', 'Hemoglobín', 'Hemoglobin', '718-7', 'g/L', '["HGB","Hb","Hemoglobin","Hemoglobín"]', '{"g/dl": 10}'),
('HCT', 'lab', 'Hematokrit', 'Hematocrit', '4544-3', 'L/L', '["HCT","Hematokrit","Hematocrit"]', '{"%": 0.01}'),
('PLT', 'lab', 'Trombocyty', 'Platelets', '777-3', '10^9/L', '["PLT","Trombocyty","Platelets","Trombo"]', '{"g/l": 1, "/nl": 1}'),
('MCV', 'lab', 'Stredný objem erytrocytu', 'Mean corpuscular volume', '787-2', 'fL', '["MCV"]', '{}'),
('MCH', 'lab', 'Stredné množstvo hemoglobínu', 'Mean corpuscular hemoglobin', NULL, 'pg', '["MCH"]', '{}'),
('MCHC', 'lab', 'Stredná koncentrácia hemoglobínu', 'Mean corpuscular hemoglobin concentration', NULL, 'g/L', '["MCHC"]', '{"g/dl": 10}'),
('RDW', 'lab', 'Distribučná šírka erytrocytov', 'Red cell distribution width', NULL, '%', '["RDW","RDW-CV"]', '{}'),
('NEUT', 'lab', 'Neutrofily (absolútne)', 'Neutrophils (absolute)', '751-8', '10^9/L', '["NEUT","NEU","Neutrofily","Neutrophils","NEUT#","Neutrofily abs."]', '{"g/l": 1}'),
('NEUT_PCT', 'lab', 'Neutrofily (%)', 'Neutrophils (%)', NULL, '%', '["NEUT%","Neutrofily %"]', '{}'),
('LYMPH', 'lab', 'Lymfocyty (absolútne)', 'Lymphocytes (absolute)', '731-0', '10^9/L', '["LYMPH","LYM","Lymfocyty","LYMPH#"]', '{"g/l": 1}'),
('LYMPH_PCT', 'lab', 'Lymfocyty (%)', 'Lymphocytes (%)', NULL, '%', '["LYMPH%","Lymfocyty %"]', '{}'),
('MONO', 'lab', 'Monocyty (absolútne)', 'Monocytes (absolute)', '742-7', '10^9/L', '["MONO","Monocyty","MONO#"]', '{"g/l": 1}'),
('EOS', 'lab', 'Eozinofily (absolútne)', 'Eosinophils (absolute)', '711-2', '10^9/L', '["EOS","Eozinofily","EOS#"]', '{"g/l": 1}'),
('BASO', 'lab', 'Bazofily (absolútne)', 'Basophils (absolute)', '704-7', '10^9/L', '["BASO","Bazofily","BASO#"]', '{"g/l": 1}'),
('INR', 'lab', 'INR', 'INR', '6301-6', NULL, '["INR","PT-INR"]', '{}'),
('APTT', 'lab', 'aPTT', 'Activated partial thromboplastin time', NULL, 's', '["APTT","aPTT","APTC"]', '{}'),
('FIB', 'lab', 'Fibrinogén', 'Fibrinogen', NULL, 'g/L', '["Fibrinogen","Fibrinogén","FIB"]', '{}'),
('GLU', 'lab', 'Glukóza', 'Glucose', '2345-7', 'mmol/L', '["GLU","Glukóza","Glucose","S-Glukóza"]', '{"mg/dl": 0.0555}'),
('UREA', 'lab', 'Močovina', 'Urea', NULL, 'mmol/L', '["Urea","Močovina","Močovina (urea)","S-Močovina"]', '{}'),
('CREA', 'lab', 'Kreatinín', 'Creatinine', NULL, 'µmol/L', '["CREA","Kreatinín","Kreatinin","Creatinine","S-Kreatinín"]', '{"mg/dl": 88.4}'),
('EGFR', 'lab', 'Odhadovaná glomerulárna filtrácia', 'Estimated GFR', NULL, 'ml/s', '["eGFR","eGF CKD-EPI","GFR"]', '{}'),
('NA', 'lab', 'Sodík', 'Sodium', '2951-2', 'mmol/L', '["Na","Sodík","Sodium","S-Sodík"]', '{}'),
('K', 'lab', 'Draslík', 'Potassium', '2823-3', 'mmol/L', '["K","Draslík","Potassium","S-Draslík"]', '{}'),
('CL', 'lab', 'Chloridy', 'Chloride', NULL, 'mmol/L', '["Cl","Chloridy","Chloride"]', '{}'),
('CA', 'lab', 'Vápnik', 'Calcium', NULL, 'mmol/L', '["Ca","Vápnik","Calcium"]', '{}'),
('MG', 'lab', 'Horčík', 'Magnesium', NULL, 'mmol/L', '["Mg","Horčík","Magnesium"]', '{}'),
('P', 'lab', 'Fosfor', 'Phosphate', NULL, 'mmol/L', '["P","Fosfor","Fosfáty","Phosphate"]', '{}'),
('ALT', 'lab', 'ALT', 'Alanine aminotransferase', '1742-6', 'µkat/L', '["ALT","ALAT"]', '{}'),
('AST', 'lab', 'AST', 'Aspartate aminotransferase', '1920-8', 'µkat/L', '["AST","ASAT"]', '{}'),
('GGT', 'lab', 'GMT', 'Gamma-glutamyl transferase', NULL, 'µkat/L', '["GGT","GMT"]', '{}'),
('ALP', 'lab', 'Alkalická fosfatáza', 'Alkaline phosphatase', NULL, 'µkat/L', '["ALP","ALP","Alkalická fosfatáza"]', '{}'),
('BILI', 'lab', 'Bilirubín celkový', 'Total bilirubin', NULL, 'µmol/L', '["Bilirubín","Bilirubin","Bilirubín celkový","BILI"]', '{}'),
('ALB', 'lab', 'Albumín', 'Albumin', '1751-7', 'g/L', '["ALB","Albumín","Albumin"]', '{"g/dl": 10}'),
('TP', 'lab', 'Celkové bielkoviny', 'Total protein', NULL, 'g/L', '["Celkové bielkoviny","Total protein","TP"]', '{"g/dl": 10}'),
('LDH', 'lab', 'LDH', 'Lactate dehydrogenase', NULL, 'µkat/L', '["LDH","LD"]', '{}'),
('URIC', 'lab', 'Kyselina močová', 'Uric acid', NULL, 'µmol/L', '["Kyselina močová","Uric acid","URIC"]', '{}'),
('CRP', 'lab', 'C-reaktívny proteín', 'C-reactive protein', '1988-5', 'mg/L', '["CRP","C-reaktívny proteín"]', '{"mg/dl": 10}'),
('PCT', 'lab', 'Prokalcitonín', 'Procalcitonin', NULL, 'µg/L', '["PCT","Prokalcitonín"]', '{"ng/ml": 1}'),
('CEA', 'tumor_marker', 'CEA', 'Carcinoembryonic antigen', '2039-6', 'µg/L', '["CEA","CEA v sére"]', '{"ng/ml": 1}'),
('CA19_9', 'tumor_marker', 'CA 19-9', 'CA 19-9', '24108-3', 'kU/L', '["CA 19-9","CA19-9","CA 19-9 v sére"]', '{"u/ml": 1}'),
('CA125', 'tumor_marker', 'CA 125', 'CA 125', '10334-1', 'kU/L', '["CA 125","CA125"]', '{"u/ml": 1}'),
('CA15_3', 'tumor_marker', 'CA 15-3', 'CA 15-3', NULL, 'kU/L', '["CA 15-3","CA15-3"]', '{"u/ml": 1}'),
('AFP', 'tumor_marker', 'AFP', 'Alpha-fetoprotein', NULL, 'µg/L', '["AFP"]', '{"ng/ml": 1}'),
('U_PH', 'lab', 'Moč - pH', 'Urine pH', NULL, NULL, '["U- pH","U-pH","pH moču"]', '{}'),
('U_LEU', 'lab', 'Moč - leukocyty', 'Urine leukocytes', NULL, '/µl', '["U-Leukocyty","USED - Leukocyty"]', '{}'),
('U_ERY', 'lab', 'Moč - erytrocyty', 'Urine erythrocytes', NULL, '/µl', '["U- Krv","USED - Erytrocyty"]', '{}'),
('WEIGHT', 'vital', 'Hmotnosť', 'Body weight', NULL, 'kg', '["Hmotnosť","Váha","Weight"]', '{}'),
('HEIGHT', 'vital', 'Výška', 'Body height', NULL, 'cm', '["Výška","Height"]', '{}'),
('BSA', 'vital', 'Povrch tela', 'Body surface area', NULL, 'm2', '["BSA","Povrch tela"]', '{}'),
('BP_SYS', 'vital', 'Krvný tlak systolický', 'Systolic blood pressure', NULL, 'mmHg', '["TK sys","Systolický tlak"]', '{}'),
('BP_DIA', 'vital', 'Krvný tlak diastolický', 'Diastolic blood pressure', NULL, 'mmHg', '["TK dia","Diastolický tlak"]', '{}'),
('TEMP', 'vital', 'Telesná teplota', 'Body temperature', NULL, '°C', '["TT","Teplota","Temperature"]', '{}'),
('ECOG', 'vital', 'ECOG výkonnostný stav', 'ECOG performance status', NULL, NULL, '["ECOG","PS ECOG"]', '{}'),
('KRAS', 'genetic_variant', 'KRAS', 'KRAS', NULL, NULL, '["KRAS"]', '{}'),
('NRAS', 'genetic_variant', 'NRAS', 'NRAS', NULL, NULL, '["NRAS"]', '{}'),
('BRAF', 'genetic_variant', 'BRAF', 'BRAF', NULL, NULL, '["BRAF"]', '{}'),
('MSI', 'biomarker', 'Mikrosatelitová instabilita', 'Microsatellite instability', NULL, NULL, '["MSI","MMR","dMMR","pMMR"]', '{}'),
('HER2', 'biomarker', 'HER2', 'HER2', NULL, NULL, '["HER2","ERBB2"]', '{}');
