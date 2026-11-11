-- Migration 084: soft delete for conversation_entries and treatment_events (#723).
-- Additive only: ADD COLUMN plus a view rebuild. No rename, no DML, no patient data.
--
-- delete_conversation_entry / delete_treatment_event used to issue a hard DELETE,
-- which breaks the deletion policy (trash only, 30 days to review). They now set
-- deleted_at / deleted_by, every reader filters deleted_at IS NULL, and the
-- restore_* helpers clear both columns again.
-- If a column already exists the ADD COLUMN fails and the migration runner falls
-- back to running each statement best-effort, so the remaining ones still apply.
--
-- timeline_v is rebuilt from the migration 075 definition with one change: the
-- treatment branch excludes soft-deleted events. PR 729 (migration 083) also
-- rebuilds timeline_v, so whichever of the two merges second must carry both
-- changes in its view definition.
-- No semicolons inside comments: the best-effort fallback splits on them.

ALTER TABLE conversation_entries ADD COLUMN deleted_at TEXT;
ALTER TABLE conversation_entries ADD COLUMN deleted_by TEXT;
ALTER TABLE treatment_events ADD COLUMN deleted_at TEXT;
ALTER TABLE treatment_events ADD COLUMN deleted_by TEXT;

DROP VIEW IF EXISTS timeline_v;

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
  FROM treatment_events t WHERE t.deleted_at IS NULL
UNION ALL
SELECT s.patient_id, s.effective_at, s.category, s.name_en,
       COALESCE(s.value_text, CAST(s.value_num AS TEXT)), s.unit, s.flag, s.verification,
       s.source_document_id, 'fact', s.id, s.organization_id, s.practitioner_id
  FROM fact_series_v s
 WHERE s.category IN ('condition', 'procedure', 'genetic_variant', 'biomarker', 'medication')
    OR s.flag IS NOT NULL;
