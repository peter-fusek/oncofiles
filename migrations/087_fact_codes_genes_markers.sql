-- Migration 087: oncology gene and marker vocabulary for fact_codes.
-- Reference data only (public HGNC gene symbols and standard marker names),
-- no patient data, no dependency on any patient row. Built from public
-- knowledge, never from documents. Additive and idempotent: INSERT OR IGNORE
-- for new codes, and each new alias of an existing code is appended only when
-- it is not in the list yet.
-- Lab rule parsers never read genetic_variant or biomarker rows (the
-- Vocabulary keeps them on a separate biomarker side), so short symbols such
-- as MET, KIT or RET cannot become lab facts.
-- The gene EGFR gets code EGFR_GENE because EGFR is already the eGFR lab code.
-- No semicolons inside comments: the best-effort fallback splits on them.

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('PIK3CA', 'genetic_variant', 'PIK3CA', 'PIK3CA', NULL, NULL, '["PIK3CA"]', '{}'),
('APC', 'genetic_variant', 'APC', 'APC', NULL, NULL, '["APC"]', '{}'),
('TP53', 'genetic_variant', 'TP53', 'TP53', NULL, NULL, '["TP53"]', '{}'),
('SMAD4', 'genetic_variant', 'SMAD4', 'SMAD4', NULL, NULL, '["SMAD4","DPC4"]', '{}'),
('MLH1', 'genetic_variant', 'MLH1', 'MLH1', NULL, NULL, '["MLH1"]', '{}'),
('MSH2', 'genetic_variant', 'MSH2', 'MSH2', NULL, NULL, '["MSH2"]', '{}'),
('MSH6', 'genetic_variant', 'MSH6', 'MSH6', NULL, NULL, '["MSH6"]', '{}'),
('PMS2', 'genetic_variant', 'PMS2', 'PMS2', NULL, NULL, '["PMS2"]', '{}'),
('EPCAM', 'genetic_variant', 'EPCAM', 'EPCAM', NULL, NULL, '["EPCAM","EpCAM"]', '{}'),
('POLE', 'genetic_variant', 'POLE', 'POLE', NULL, NULL, '["POLE"]', '{}'),
('POLD1', 'genetic_variant', 'POLD1', 'POLD1', NULL, NULL, '["POLD1"]', '{}'),
('MUTYH', 'genetic_variant', 'MUTYH', 'MUTYH', NULL, NULL, '["MUTYH"]', '{}'),
('STK11', 'genetic_variant', 'STK11', 'STK11', NULL, NULL, '["STK11","LKB1"]', '{}'),
('PTEN', 'genetic_variant', 'PTEN', 'PTEN', NULL, NULL, '["PTEN"]', '{}'),
('CDH1', 'genetic_variant', 'CDH1', 'CDH1', NULL, NULL, '["CDH1"]', '{}'),
('ATM', 'genetic_variant', 'ATM', 'ATM', NULL, NULL, '["ATM"]', '{}'),
('CHEK2', 'genetic_variant', 'CHEK2', 'CHEK2', NULL, NULL, '["CHEK2"]', '{}'),
('BRCA1', 'genetic_variant', 'BRCA1', 'BRCA1', NULL, NULL, '["BRCA1"]', '{}'),
('BRCA2', 'genetic_variant', 'BRCA2', 'BRCA2', NULL, NULL, '["BRCA2"]', '{}'),
('PALB2', 'genetic_variant', 'PALB2', 'PALB2', NULL, NULL, '["PALB2"]', '{}'),
('AKT1', 'genetic_variant', 'AKT1', 'AKT1', NULL, NULL, '["AKT1"]', '{}'),
('FBXW7', 'genetic_variant', 'FBXW7', 'FBXW7', NULL, NULL, '["FBXW7"]', '{}'),
('CTNNB1', 'genetic_variant', 'CTNNB1', 'CTNNB1', NULL, NULL, '["CTNNB1"]', '{}'),
('ARID1A', 'genetic_variant', 'ARID1A', 'ARID1A', NULL, NULL, '["ARID1A"]', '{}'),
('SOX9', 'genetic_variant', 'SOX9', 'SOX9', NULL, NULL, '["SOX9"]', '{}'),
('TCF7L2', 'genetic_variant', 'TCF7L2', 'TCF7L2', NULL, NULL, '["TCF7L2"]', '{}'),
('NTRK1', 'genetic_variant', 'NTRK1', 'NTRK1', NULL, NULL, '["NTRK1"]', '{}'),
('NTRK2', 'genetic_variant', 'NTRK2', 'NTRK2', NULL, NULL, '["NTRK2"]', '{}'),
('NTRK3', 'genetic_variant', 'NTRK3', 'NTRK3', NULL, NULL, '["NTRK3"]', '{}'),
('ALK', 'genetic_variant', 'ALK', 'ALK', NULL, NULL, '["ALK"]', '{}'),
('ROS1', 'genetic_variant', 'ROS1', 'ROS1', NULL, NULL, '["ROS1"]', '{}'),
('RET', 'genetic_variant', 'RET', 'RET', NULL, NULL, '["RET"]', '{}'),
('MET', 'genetic_variant', 'MET', 'MET', NULL, NULL, '["MET","c-MET","cMET"]', '{}'),
('FGFR1', 'genetic_variant', 'FGFR1', 'FGFR1', NULL, NULL, '["FGFR1"]', '{}'),
('FGFR2', 'genetic_variant', 'FGFR2', 'FGFR2', NULL, NULL, '["FGFR2"]', '{}'),
('FGFR3', 'genetic_variant', 'FGFR3', 'FGFR3', NULL, NULL, '["FGFR3"]', '{}'),
('EGFR_GENE', 'genetic_variant', 'EGFR (gén)', 'EGFR (gene)', NULL, NULL, '["EGFR","ERBB1","HER1"]', '{}'),
('KIT', 'genetic_variant', 'KIT', 'KIT', NULL, NULL, '["KIT","c-KIT","cKIT"]', '{}'),
('PDGFRA', 'genetic_variant', 'PDGFRA', 'PDGFRA', NULL, NULL, '["PDGFRA"]', '{}'),
('IDH1', 'genetic_variant', 'IDH1', 'IDH1', NULL, NULL, '["IDH1"]', '{}'),
('IDH2', 'genetic_variant', 'IDH2', 'IDH2', NULL, NULL, '["IDH2"]', '{}'),
('GNAS', 'genetic_variant', 'GNAS', 'GNAS', NULL, NULL, '["GNAS"]', '{}'),
('RNF43', 'genetic_variant', 'RNF43', 'RNF43', NULL, NULL, '["RNF43"]', '{}'),
('ZNRF3', 'genetic_variant', 'ZNRF3', 'ZNRF3', NULL, NULL, '["ZNRF3"]', '{}'),
('HRAS', 'genetic_variant', 'HRAS', 'HRAS', NULL, NULL, '["HRAS"]', '{}'),
('ERBB3', 'genetic_variant', 'ERBB3', 'ERBB3', NULL, NULL, '["ERBB3","HER3"]', '{}'),
('MAP2K1', 'genetic_variant', 'MAP2K1', 'MAP2K1', NULL, NULL, '["MAP2K1","MEK1"]', '{}'),
('NF1', 'genetic_variant', 'NF1', 'NF1', NULL, NULL, '["NF1"]', '{}'),
('CDKN2A', 'genetic_variant', 'CDKN2A', 'CDKN2A', NULL, NULL, '["CDKN2A"]', '{}'),
('SMARCA4', 'genetic_variant', 'SMARCA4', 'SMARCA4', NULL, NULL, '["SMARCA4"]', '{}'),
('KEAP1', 'genetic_variant', 'KEAP1', 'KEAP1', NULL, NULL, '["KEAP1"]', '{}'),
('ESR1', 'genetic_variant', 'ESR1', 'ESR1', NULL, NULL, '["ESR1"]', '{}'),
('RAD51C', 'genetic_variant', 'RAD51C', 'RAD51C', NULL, NULL, '["RAD51C"]', '{}'),
('RAD51D', 'genetic_variant', 'RAD51D', 'RAD51D', NULL, NULL, '["RAD51D"]', '{}'),
('BRIP1', 'genetic_variant', 'BRIP1', 'BRIP1', NULL, NULL, '["BRIP1"]', '{}'),
('CDK12', 'genetic_variant', 'CDK12', 'CDK12', NULL, NULL, '["CDK12"]', '{}'),
('TMB', 'biomarker', 'Mutačná záťaž nádoru', 'Tumour mutational burden', NULL, NULL, '["TMB","Tumor mutational burden","Tumour mutational burden","Mutačná záťaž nádoru","Tumor mutation burden"]', '{}'),
('PDL1', 'biomarker', 'PD-L1', 'PD-L1', NULL, NULL, '["PD-L1","PDL1","PD-L1 IHC","PD-L1 TPS","PD-L1 CPS","CD274"]', '{}'),
('KI67', 'biomarker', 'Proliferačný index Ki-67', 'Ki-67 proliferation index', NULL, NULL, '["Ki-67","Ki67","MIB-1","MIB1"]', '{}'),
('ER', 'biomarker', 'Estrogénové receptory', 'Estrogen receptor', NULL, NULL, '["ER","Estrogen receptor","Estrogénové receptory","Estrogénový receptor"]', '{}'),
('PR', 'biomarker', 'Progesterónové receptory', 'Progesterone receptor', NULL, NULL, '["PR","PgR","Progesterone receptor","Progesterónové receptory","Progesterónový receptor"]', '{}'),
('HRD', 'biomarker', 'Deficit homologickej rekombinácie', 'Homologous recombination deficiency', NULL, NULL, '["HRD","Homologous recombination deficiency"]', '{}');

-- New alias spellings for codes seeded in 072 (MSI, HER2), one statement per
-- alias, appended only when missing, so a rerun changes nothing.

UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'MSS') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'MSS');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'MSI-H') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'MSI-H');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'MSI-L') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'MSI-L');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'MSI-high') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'MSI-high');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'MSI-low') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'MSI-low');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'MMR status') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'MMR status');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'Mismatch repair') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'Mismatch repair');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'Microsatellite instability') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'Microsatellite instability');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'Mikrosatelitová instabilita') WHERE code = 'MSI' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'Mikrosatelitová instabilita');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'HER-2') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'HER-2');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'HER2/neu') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'HER2/neu');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'Her-2/neu') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'Her-2/neu');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'c-erbB-2') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'c-erbB-2');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'HER2 IHC') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'HER2 IHC');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'HER2 ISH') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'HER2 ISH');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'HER2 FISH') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'HER2 FISH');
UPDATE fact_codes SET aliases = json_insert(aliases, '$[#]', 'HER2 SISH') WHERE code = 'HER2' AND NOT EXISTS (SELECT 1 FROM json_each(fact_codes.aliases) j WHERE j.value = 'HER2 SISH');
