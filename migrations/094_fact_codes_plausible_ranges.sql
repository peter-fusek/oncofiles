-- Migration 094: plausibility ranges as vocabulary data (#929).
-- The canonical-unit plausibility ranges of the lab rule parser and the
-- generic verifier lived in code (facts_rules.PLAUSIBLE, #637 review). They
-- move into fact_codes.plausible_low / plausible_high (columns since migration
-- 080, in unit_canonical), with exactly the values of the removed constant.
-- A value outside its range is never stored under the code.
-- Reference data only, no patient data. Additive and idempotent: a row is
-- written only while both columns are still empty, so a range set later by a
-- maintainer is never overwritten.
-- Effect on stored facts: none. The parsers read the same ranges from data
-- instead of code, so every extraction result is unchanged (no INDEX_VERSION
-- bump, see the PR of #929).
-- No semicolons inside comments: the best-effort fallback splits on them.

UPDATE fact_codes SET plausible_low = 0, plausible_high = 50
WHERE code = 'NEUT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 20
WHERE code = 'LYMPH' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 5
WHERE code = 'MONO' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 5
WHERE code = 'EOS' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 5
WHERE code = 'BASO' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 100
WHERE code = 'NEUT_PCT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 100
WHERE code = 'LYMPH_PCT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 100
WHERE code = 'MONO_PCT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 100
WHERE code = 'EOS_PCT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 100
WHERE code = 'BASO_PCT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 300
WHERE code = 'WBC' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 10
WHERE code = 'RBC' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 20, plausible_high = 250
WHERE code = 'HGB' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 1
WHERE code = 'HCT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 3000
WHERE code = 'PLT' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 40, plausible_high = 150
WHERE code = 'MCV' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 10, plausible_high = 60
WHERE code = 'MCH' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 200, plausible_high = 450
WHERE code = 'MCHC' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 5, plausible_high = 2000
WHERE code = 'CREA' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 100
WHERE code = 'UREA' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 60
WHERE code = 'GLU' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 90, plausible_high = 200
WHERE code = 'NA' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 1, plausible_high = 10
WHERE code = 'K' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 1000
WHERE code = 'CRP' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 5, plausible_high = 70
WHERE code = 'ALB' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 20, plausible_high = 120
WHERE code = 'TP' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 10
WHERE code = 'P' AND plausible_low IS NULL AND plausible_high IS NULL;
UPDATE fact_codes SET plausible_low = 0, plausible_high = 6
WHERE code = 'CA' AND plausible_low IS NULL AND plausible_high IS NULL;
