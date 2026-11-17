-- Migration 092: HCT printed as a ratio (#814).
-- Slovak lab printouts give the hematocrit with the unit "pomer" (ratio),
-- which is the canonical L/L, so the factor is 1. Reference data only, no
-- patient data. Additive and idempotent: the key is added only when absent,
-- no other key of the HCT conversion map changes.
-- Effect on stored facts: HCT facts printed in "pomer" were unconvertible
-- (canonical value NULL) and get canonical value = printed value at the
-- re-index (INDEX_VERSION bump). Values, codes and verdicts do not change.
-- No semicolons inside comments: the best-effort fallback splits on them.

UPDATE fact_codes SET unit_conversions = json_set(unit_conversions, '$.pomer', 1)
WHERE code = 'HCT' AND json_extract(unit_conversions, '$.pomer') IS NULL;
