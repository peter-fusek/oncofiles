-- Migration 075: iron panel, ferritin, vitamin D, more aliases; views expose
-- fact metadata and give encounter rows a source document (#635).
-- Reference vocabulary + view definitions only; no patient data.

UPDATE fact_codes SET aliases = '["CREA","Kreatinín","Kreatinin","Creatinine","S-Kreatinín","KREA"]' WHERE code = 'CREA';
UPDATE fact_codes SET aliases = '["GLU","Glukóza","Glucose","S-Glukóza","GLUK"]' WHERE code = 'GLU';
UPDATE fact_codes SET aliases = '["Kyselina močová","Uric acid","URIC","KM"]' WHERE code = 'URIC';
UPDATE fact_codes SET aliases = '["Osmolarita séra výpočtom","Osmolarita séra","Osmolalita séra výpočtom","Osmolalita séra","OSM"]' WHERE code = 'OSMO';
UPDATE fact_codes SET aliases = '["NEUT%","Neutrofily %","Neutrofilie %","Neutrofilie%"]' WHERE code = 'NEUT_PCT';

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('FE', 'lab', 'Železo', 'Serum iron', NULL, 'µmol/L', '["Železo","Železo v sére","Fe"]', '{}'),
('TRF', 'lab', 'Transferín', 'Transferrin', NULL, 'g/L', '["Transferín","Transferín v sére","TRF"]', '{}'),
('TSAT', 'lab', 'Saturácia transferínu', 'Transferrin saturation', NULL, '%', '["Sat.transferínu","Saturácia transferínu","Saturácia železa","Saturácia železa v sére","TSAT"]', '{}'),
('TIBC', 'lab', 'Celková väzobná kapacita železa', 'Total iron-binding capacity', NULL, 'µmol/L', '["Celk.väzobná kapacita železa","Celková väzobná kapacita železa","CVK výp. z TRF","CVK","TIBC"]', '{}'),
('UIBC', 'lab', 'Voľná väzobná kapacita železa', 'Unsaturated iron-binding capacity', NULL, 'µmol/L', '["Voľná väzobná kapacita železa","VVK","UIBC"]', '{}'),
('FERR', 'lab', 'Feritín', 'Ferritin', NULL, 'µg/L', '["Feritín","Ferritin","FER","FERR"]', '{"ng/ml": 1}'),
('VITD', 'lab', 'Vitamín D celkový', 'Total vitamin D (25-OH)', NULL, 'nmol/L', '["Vitamín D celkový","Vitamín D","25-OH vitamín D","VITD"]', '{}');

DROP VIEW IF EXISTS timeline_v;
DROP VIEW IF EXISTS latest_fact_v;
DROP VIEW IF EXISTS fact_series_v;

CREATE VIEW fact_series_v AS
SELECT f.id, f.patient_id, f.code, f.record_type AS category,
       COALESCE(fc.name_en, f.param) AS name_en, COALESCE(fc.name_sk, f.param) AS name_sk,
       f.param AS label_original, f.occurred_at AS effective_at,
       f.value_num, f.value_text, f.unit,
       COALESCE(f.value_canonical, f.value_num) AS value_canonical,
       COALESCE(f.unit_canonical, fc.unit_canonical, f.unit) AS unit_canonical,
       f.ref_range_low, f.ref_range_high, f.ref_text, f.flag, f.verification,
       f.source_document_id, f.page_number, f.evidence, f.encounter_id,
       f.organization_id, f.department_id, f.practitioner_id, f.extracted_by, f.confidence,
       f.metadata_json
  FROM clinical_records f LEFT JOIN fact_codes fc ON fc.code = f.code
 WHERE f.deleted_at IS NULL;

CREATE VIEW latest_fact_v AS
SELECT s.* FROM fact_series_v s
 WHERE s.code IS NOT NULL AND s.verification != 'unverified'
   AND s.effective_at = (
       SELECT MAX(s2.effective_at) FROM fact_series_v s2
        WHERE s2.patient_id = s.patient_id AND s2.code = s.code
          AND s2.verification != 'unverified');

CREATE VIEW timeline_v AS
SELECT e.patient_id, e.start_at AS at, 'encounter' AS kind, e.title AS title,
       NULL AS value, NULL AS unit, NULL AS flag, NULL AS verification,
       (SELECT MIN(d.id) FROM documents d WHERE d.encounter_id = e.id AND d.deleted_at IS NULL)
           AS source_document_id,
       'encounter' AS entity, e.id AS entity_id, e.organization_id, e.practitioner_id
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
