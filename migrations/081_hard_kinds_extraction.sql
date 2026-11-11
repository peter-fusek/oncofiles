-- Migration 081: LLM-first extraction of the non-lab kinds (#726 item 4, #711 phase 2b).
-- Additive only: ADD COLUMN, one vocabulary row, one index, and the timeline
-- view redefined. No rename, no patient data, nothing moved.
--
-- * metadata_json on every hard-fact table (verifier reasons, printed forms,
--   date precision, links such as the imaging procedure of a lesion size)
-- * treatment_cycles.status (planned | administered | postponed | cancelled,
--   validated in Python) + status_reason (the printed postponement reason)
-- * encounters get provenance columns so a PLANNED visit or examination read
--   from a document carries its source, page, evidence and verification.
--   Existing encounter rows keep NULL there (they come from the indexer).
-- * fact_codes LESION_SIZE (category imaging, canonical unit mm, cm converts).
--   Its only alias is the code itself, so no printed lab label can match it.
-- * timeline_v adds page_number and date_source columns and unions the hard
--   tables: conditions, treatment courses, cycles, medication events and
--   procedures. Planned encounters and planned cycles get kind planned.
-- No semicolons inside comments: the best-effort fallback splits on them.

ALTER TABLE conditions ADD COLUMN metadata_json TEXT;
ALTER TABLE cancer_stages ADD COLUMN metadata_json TEXT;
ALTER TABLE treatment_courses ADD COLUMN metadata_json TEXT;
ALTER TABLE treatment_cycles ADD COLUMN metadata_json TEXT;
ALTER TABLE medication_events ADD COLUMN metadata_json TEXT;
ALTER TABLE procedures ADD COLUMN metadata_json TEXT;
ALTER TABLE specimens ADD COLUMN metadata_json TEXT;
ALTER TABLE reports ADD COLUMN metadata_json TEXT;

ALTER TABLE treatment_cycles ADD COLUMN status TEXT;
ALTER TABLE treatment_cycles ADD COLUMN status_reason TEXT;

ALTER TABLE encounters ADD COLUMN status TEXT;
ALTER TABLE encounters ADD COLUMN source_document_id INTEGER REFERENCES documents(id);
ALTER TABLE encounters ADD COLUMN page_number INTEGER;
ALTER TABLE encounters ADD COLUMN evidence TEXT;
ALTER TABLE encounters ADD COLUMN extracted_by TEXT;
ALTER TABLE encounters ADD COLUMN verification TEXT
    CHECK (verification IN ('machine', 'unverified', 'verified', 'corrected'));
ALTER TABLE encounters ADD COLUMN metadata_json TEXT;

CREATE INDEX IF NOT EXISTS idx_encounters_source_document ON encounters(source_document_id);

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('LESION_SIZE', 'imaging', 'Veľkosť ložiska', 'Lesion size', NULL, 'mm', '[]', '{"cm": 10}');

DROP VIEW IF EXISTS timeline_v;

CREATE VIEW timeline_v AS
SELECT e.patient_id, e.start_at AS at,
       CASE WHEN e.status = 'planned' THEN 'planned' ELSE 'encounter' END AS kind,
       e.title AS title, NULL AS value, NULL AS unit, NULL AS flag, e.verification AS verification,
       COALESCE(e.source_document_id,
                (SELECT MIN(d.id) FROM documents d WHERE d.encounter_id = e.id
                    AND d.deleted_at IS NULL)) AS source_document_id,
       e.page_number AS page_number, 'encounter' AS entity, e.id AS entity_id,
       e.organization_id AS organization_id, e.practitioner_id AS practitioner_id,
       CASE WHEN e.source_document_id IS NOT NULL THEN 'printed' END AS date_source
  FROM encounters e WHERE e.deleted_at IS NULL
UNION ALL
SELECT d.patient_id, d.document_date, 'document', COALESCE(d.title, d.filename),
       NULL, NULL, NULL, NULL, d.id, NULL, 'document', d.id, d.organization_id,
       d.practitioner_id, NULL
  FROM documents d WHERE d.deleted_at IS NULL
UNION ALL
SELECT t.patient_id, t.event_date, 'treatment', t.title, NULL, NULL, NULL, NULL,
       t.source_document_id, NULL, 'treatment_event', t.id, NULL, NULL, NULL
  FROM treatment_events t
UNION ALL
SELECT s.patient_id, s.effective_at, s.category,
       CASE WHEN s.category = 'imaging' AND s.label_original IS NOT NULL
            THEN s.name_en || ' · ' || s.label_original ELSE s.name_en END,
       COALESCE(s.value_text, CAST(s.value_num AS TEXT)), s.unit, s.flag, s.verification,
       s.source_document_id, s.page_number, 'fact', s.id, s.organization_id,
       s.practitioner_id, NULL
  FROM fact_series_v s
 WHERE s.category IN ('condition', 'procedure', 'genetic_variant', 'biomarker',
                      'medication', 'imaging')
    OR s.flag IS NOT NULL
UNION ALL
SELECT c.patient_id, COALESCE(c.asserted_at, c.onset_at, c.recorded_at, d.document_date),
       'condition', c.printed_text,
       (SELECT st.printed FROM cancer_stages st WHERE st.condition_id = c.id
           AND st.patient_id = c.patient_id AND st.deleted_at IS NULL
         ORDER BY st.id DESC LIMIT 1),
       NULL, NULL, c.verification, c.source_document_id, c.page_number, 'condition', c.id,
       NULL, NULL,
       CASE WHEN COALESCE(c.asserted_at, c.onset_at) IS NOT NULL THEN 'printed'
            ELSE 'document' END
  FROM conditions c
  LEFT JOIN documents d ON d.id = c.source_document_id AND d.patient_id = c.patient_id
 WHERE c.deleted_at IS NULL
UNION ALL
SELECT tc.patient_id, COALESCE(tc.start_at, d.document_date), 'treatment', tc.regimen_printed,
       tc.intent, NULL, NULL, tc.verification, tc.source_document_id, tc.page_number,
       'treatment_course', tc.id, NULL, NULL,
       CASE WHEN tc.start_at IS NOT NULL THEN 'printed' ELSE 'document' END
  FROM treatment_courses tc
  LEFT JOIN documents d ON d.id = tc.source_document_id AND d.patient_id = tc.patient_id
 WHERE tc.deleted_at IS NULL
UNION ALL
SELECT cy.patient_id, COALESCE(cy.actual_at, cy.planned_at, d.document_date),
       CASE WHEN cy.status = 'planned' THEN 'planned' ELSE 'treatment' END,
       TRIM(COALESCE(tc.regimen_printed, '') || ' ' || COALESCE(cy.cycle_number_printed, '')),
       cy.status, NULL, NULL, cy.verification, cy.source_document_id, cy.page_number,
       'treatment_cycle', cy.id, NULL, NULL,
       CASE WHEN COALESCE(cy.actual_at, cy.planned_at) IS NOT NULL THEN 'printed'
            ELSE 'document' END
  FROM treatment_cycles cy
  LEFT JOIN treatment_courses tc ON tc.id = cy.course_id AND tc.patient_id = cy.patient_id
  LEFT JOIN documents d ON d.id = cy.source_document_id AND d.patient_id = cy.patient_id
 WHERE cy.deleted_at IS NULL
UNION ALL
SELECT m.patient_id, COALESCE(m.effective_start, m.authored_at, d.document_date), 'medication',
       m.ingredient_printed, m.dose_printed, m.route, NULL, m.verification,
       m.source_document_id, m.page_number, 'medication_event', m.id, NULL, NULL,
       CASE WHEN COALESCE(m.effective_start, m.authored_at) IS NOT NULL THEN 'printed'
            ELSE 'document' END
  FROM medication_events m
  LEFT JOIN documents d ON d.id = m.source_document_id AND d.patient_id = m.patient_id
 WHERE m.deleted_at IS NULL
UNION ALL
SELECT p.patient_id, COALESCE(p.performed_at, d.document_date), 'procedure', p.printed_text,
       p.kind, NULL, NULL, p.verification, p.source_document_id, p.page_number, 'procedure',
       p.id, p.organization_id, p.performer_practitioner_id,
       CASE WHEN p.performed_at IS NOT NULL THEN 'printed' ELSE 'document' END
  FROM procedures p
  LEFT JOIN documents d ON d.id = p.source_document_id AND d.patient_id = p.patient_id
 WHERE p.deleted_at IS NULL;
