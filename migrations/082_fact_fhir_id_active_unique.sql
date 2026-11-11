-- Migration 082: clinical_records.fhir_id is unique among ACTIVE rows (#726).
-- The fact id is stable (fact_identity.py). A re-index soft-deletes a
-- document's machine facts and inserts them again with the same fhir_id and
-- the next version_id, so the soft-deleted row and the new row share the
-- logical id, as FHIR versions do. Migration 080 made fhir_id unique over all
-- rows, which would reject that insert. Same partial-index pattern as codings
-- and identifiers.
-- Schema only. No writer set clinical_records.fhir_id before this release.
-- No semicolons inside comments: the best-effort fallback splits on them.

DROP INDEX IF EXISTS uq_cr_fhir_id;

CREATE UNIQUE INDEX IF NOT EXISTS uq_cr_fhir_id_active
    ON clinical_records(fhir_id) WHERE fhir_id IS NOT NULL AND deleted_at IS NULL;
