-- Migration 078: short labels NOU reports use for values whose long forms are
-- already aliased (#672): "CA KOR V" (corrected calcium), "BI C S" (total
-- bilirubin), "CB S" (total protein). Reference vocabulary only, no patient
-- data. Appended idempotently, existing aliases are kept.

UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'CA KOR V')
WHERE code = 'CA_CORR'
  AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) WHERE value = 'CA KOR V');

UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'BI C S')
WHERE code = 'BILI'
  AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) WHERE value = 'BI C S');

UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'CB S')
WHERE code = 'TP'
  AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) WHERE value = 'CB S');
