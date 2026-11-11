-- Migration 081: an institution's local lab abbreviations for our fact codes (#726).
-- Shared vocabulary in codings (owner_type fact_code, owner_id = our code,
-- patient_id NULL), system urn:oncofiles:local:nou = the NOU laboratory's own
-- abbreviations. Exposed by fact_catalog as local_codes.
-- Reference data only, no patient data. A row is added only when our code
-- exists in fact_codes, and only once (the partial unique index on
-- owner_type, owner_id, system, code ignores a repeat).
-- No semicolons inside comments: the best-effort fallback splits on them.

INSERT OR IGNORE INTO codings
    (patient_id, owner_type, owner_id, system, code, is_original, mapped_by, equivalence)
SELECT NULL, 'fact_code', m.ours, 'urn:oncofiles:local:nou', m.local, 0, 'maintainer', 'equivalent'
  FROM (
        SELECT 'NEUT' AS ours, 'ANC' AS local
        UNION ALL SELECT 'CA19_9', 'CA199'
        UNION ALL SELECT 'GGT', 'GMT'
        UNION ALL SELECT 'BILI', 'BI'
        UNION ALL SELECT 'CREA', 'KREA'
        UNION ALL SELECT 'LDH', 'LD'
        UNION ALL SELECT 'TP', 'CB'
        UNION ALL SELECT 'URIC', 'KM'
        UNION ALL SELECT 'EGFR', 'EGF'
        UNION ALL SELECT 'CA_CORR', 'CAKOR'
        UNION ALL SELECT 'OSMO', 'OSM'
        UNION ALL SELECT 'GLU', 'GLUK'
       ) m
 WHERE EXISTS (SELECT 1 FROM fact_codes fc WHERE fc.code = m.ours)
   AND NOT EXISTS (
        SELECT 1 FROM codings c
         WHERE c.owner_type = 'fact_code' AND c.owner_id = m.ours
           AND c.system = 'urn:oncofiles:local:nou' AND c.code = m.local
           AND c.deleted_at IS NULL);
