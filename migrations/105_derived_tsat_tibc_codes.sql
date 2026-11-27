-- Migration 105: the calculated transferrin saturation and the TIBC calculated
-- from transferrin get their own codes (#672). Until now "Sat.transferínu" and
-- "CVK výp. z TRF" (labels of migration 075) shared TSAT / TIBC with the
-- directly reported values, so one draw printing both gave a series two points
-- at one timestamp. Reference vocabulary only, no patient data. The aliases
-- move (removed from TSAT / TIBC, the first alias match wins in the
-- vocabulary); every other alias is kept. Idempotent.
-- Stored facts change code only through a re-index (INDEX_VERSION 27).

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('TSAT_CALC', 'lab', 'Saturácia transferínu (výpočet)', 'Transferrin saturation (calculated)', NULL, '%', '["Sat.transferínu","TSAT_CALC"]', '{}'),
('TIBC_CALC', 'lab', 'Celková väzobná kapacita železa (výpočet z transferínu)', 'Total iron-binding capacity (calculated from transferrin)', NULL, 'µmol/L', '["CVK výp. z TRF","TIBC_CALC"]', '{}');

UPDATE fact_codes
   SET aliases = (SELECT json_group_array(value) FROM json_each(fact_codes.aliases)
                   WHERE value != 'Sat.transferínu')
 WHERE code = 'TSAT'
   AND EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) WHERE value = 'Sat.transferínu');

UPDATE fact_codes
   SET aliases = (SELECT json_group_array(value) FROM json_each(fact_codes.aliases)
                   WHERE value != 'CVK výp. z TRF')
 WHERE code = 'TIBC'
   AND EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) WHERE value = 'CVK výp. z TRF');
