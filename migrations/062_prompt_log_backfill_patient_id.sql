-- Migration 062: backfill prompt_log.patient_id for orphan rows (#476).
--
-- Background: 19,707 prompt_log rows have patient_id = '' (empty string)
-- because log_ai_call in src/oncofiles/prompt_logger.py defaulted to "" when
-- get_current_patient_id() returned empty or raised (background jobs don't
-- set the ContextVar). This was the supporting vector for the #476 P0
-- cross-patient leak: under the broken `if patient_id:` filter in DB
-- helpers, an empty-string caller bypassed scoping and saw all of these
-- rows' user_prompts — which contain OCR'd text from real patients'
-- medical documents.
--
-- Recovery strategy:
--
-- Layer 1 — recover via document_id JOIN (16,303 of 19,707, ~82%):
--   Most orphan rows DO know which document triggered the AI call. The
--   document row has patient_id. Join and backfill.
--
-- Layer 2 — remaining rows that have document_id but whose document has
-- been hard-deleted or never existed: set to sentinel '__system_no_patient__'
-- so cross-patient queries under `WHERE patient_id = <real-uuid>` can't match them.
--
-- Layer 3 — rows with no document_id (3,398 rows): these are pure
-- system-level AI calls (startup sweeps, batch backfills) with no
-- per-document linkage. Mark with sentinel '__system_no_patient__'.
--
-- Side effects: UPDATE only, no DROP. All prompt_log rows remain.
-- patient_id transitions from '' to either a real UUID or the sentinel.

-- Layer 1 — recover from document_id.
UPDATE prompt_log
SET patient_id = (SELECT d.patient_id FROM documents d WHERE d.id = prompt_log.document_id)
WHERE patient_id = ''
  AND document_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM documents d WHERE d.id = prompt_log.document_id);

-- Layer 2+3 — remaining orphans go to the sentinel.
UPDATE prompt_log
SET patient_id = '__system_no_patient__'
WHERE patient_id = '';
