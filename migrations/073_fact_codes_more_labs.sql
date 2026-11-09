-- Migration 073: more lab codes and aliases (#603 review, 2026-11-09).
-- Reference vocabulary only (no patient data): real lab labels seen in the
-- first index run that had no code (differential counts written as
-- "abs. počet …", inflammation ratios, platelet indices, urine strip and
-- sediment). Aliases are matched after normalisation (diacritics, dots,
-- spacing, "U- " vs "U-"), so variants need no separate entries.

UPDATE fact_codes SET aliases = '["NEUT","NEU","Neutrofily","Neutrophils","NEUT#","Neutrofily abs.","abs. počet NEUT","Neutrofily abs.počet","Neutrofilie abs.počet"]' WHERE code = 'NEUT';
UPDATE fact_codes SET aliases = '["NEUT%","Neutrofily %","Neutrofilie %"]' WHERE code = 'NEUT_PCT';
UPDATE fact_codes SET aliases = '["LYMPH","LYM","Lymfocyty","LYMPH#","abs. počet LYMPH","Lymfocyty abs.počet"]' WHERE code = 'LYMPH';
UPDATE fact_codes SET aliases = '["MONO","Monocyty","MONO#","abs. počet MONO","Monocyty abs.počet"]' WHERE code = 'MONO';
UPDATE fact_codes SET aliases = '["EOS","Eozinofily","EOS#","abs. počet EO","Eozinofily abs.počet"]' WHERE code = 'EOS';
UPDATE fact_codes SET aliases = '["BASO","Bazofily","BASO#","abs. počet BASO","Bazofily abs.počet"]' WHERE code = 'BASO';
UPDATE fact_codes SET aliases = '["Cl","Chloridy","Chloride","S-Chloridy"]' WHERE code = 'CL';
UPDATE fact_codes SET aliases = '["U- Krv","U-Krv","USED - Erytrocyty"]' WHERE code = 'U_ERY';

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('NLR', 'lab', 'Pomer neutrofily/lymfocyty', 'Neutrophil-to-lymphocyte ratio', NULL, NULL, '["Ne/Ly","Ne/Ly ratio","NLR","Pomer Ne/Ly"]', '{}'),
('SII', 'lab', 'Systémový zápalový index', 'Systemic immune-inflammation index', NULL, NULL, '["Syst. inflam. index","Syst. inflamát. index","SII"]', '{}'),
('MPV', 'lab', 'Stredný objem trombocytu', 'Mean platelet volume', NULL, 'fL', '["MPV","Stredný objem PLT"]', '{}'),
('PDW', 'lab', 'Distribučná šírka trombocytov', 'Platelet distribution width', NULL, 'fL', '["PDW","Šírka distr. PLT"]', '{}'),
('CA_CORR', 'lab', 'Korigovaný vápnik', 'Corrected calcium', NULL, 'mmol/L', '["Korigovaný vápnik","Korigovaný vápnik v sére"]', '{}'),
('OSMO', 'lab', 'Osmolarita séra (výpočet)', 'Serum osmolarity (calculated)', NULL, 'mosm/kg', '["Osmolarita séra výpočtom","Osmolarita séra"]', '{}'),
('U_BIL', 'lab', 'Moč - bilirubín', 'Urine bilirubin', NULL, NULL, '["U-Bilirubín"]', '{}'),
('U_GLU', 'lab', 'Moč - glukóza', 'Urine glucose', NULL, NULL, '["U-Glukóza"]', '{}'),
('U_KET', 'lab', 'Moč - ketóny', 'Urine ketones', NULL, NULL, '["U-Ketóny"]', '{}'),
('U_PROT', 'lab', 'Moč - bielkoviny', 'Urine protein', NULL, NULL, '["U-Bielkoviny"]', '{}'),
('U_UBG', 'lab', 'Moč - urobilinogén', 'Urine urobilinogen', NULL, NULL, '["U-Urobilinogén","Urobilinogén"]', '{}'),
('U_NIT', 'lab', 'Moč - dusitany', 'Urine nitrite', NULL, NULL, '["U-Dusitany","U- Dusitany"]', '{}'),
('U_SG', 'lab', 'Moč - špecifická hmotnosť', 'Urine specific gravity', NULL, 'g/cm3', '["U-Špecifická hmotnosť"]', '{}'),
('USED_BACT', 'lab', 'Močový sediment - baktérie', 'Urine sediment bacteria', NULL, '/µl', '["USED - Baktérie"]', '{}'),
('USED_EPI', 'lab', 'Močový sediment - epitelie ploché', 'Urine sediment squamous epithelia', NULL, '/µl', '["USED - Epitelie ploché"]', '{}'),
('USED_MUCUS', 'lab', 'Močový sediment - hlienové vlákna', 'Urine sediment mucus threads', NULL, '/µl', '["USED - Hlienové vlákna"]', '{}'),
('USED_CRYST', 'lab', 'Močový sediment - kryštály', 'Urine sediment crystals', NULL, '/µl', '["USED - Kryštály"]', '{}'),
('USED_YEAST', 'lab', 'Močový sediment - kvasinky', 'Urine sediment yeast', NULL, '/µl', '["USED - Kvasinky"]', '{}'),
('USED_PCAST', 'lab', 'Močový sediment - patologické valce', 'Urine sediment pathological casts', NULL, '/µl', '["USED - Patologické valce"]', '{}'),
('USED_CAST', 'lab', 'Močový sediment - valce', 'Urine sediment casts', NULL, '/µl', '["USED - Valce"]', '{}'),
('USED_ERY_TYPE', 'lab', 'Močový sediment - typizácia erytrocytov', 'Urine sediment erythrocyte morphology', NULL, NULL, '["USED - Erytrocyty - typizácia"]', '{}');
