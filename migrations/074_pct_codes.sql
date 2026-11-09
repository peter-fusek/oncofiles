-- Migration 074: percentage codes for the white-cell differential (#635).
-- Reference vocabulary only. A value with unit "%" is coded as <CODE>_PCT;
-- without such a code it stays uncoded (never filed under the absolute count).

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('MONO_PCT', 'lab', 'Monocyty (%)', 'Monocytes (%)', NULL, '%', '["MONO%","Monocyty %"]', '{}'),
('EOS_PCT', 'lab', 'Eozinofily (%)', 'Eosinophils (%)', NULL, '%', '["EOS%","Eozinofily %"]', '{}'),
('BASO_PCT', 'lab', 'Bazofily (%)', 'Basophils (%)', NULL, '%', '["BASO%","Bazofily %"]', '{}');
