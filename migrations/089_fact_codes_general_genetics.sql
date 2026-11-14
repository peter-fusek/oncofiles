-- Migration 089: general-health gene vocabulary for fact_codes (#815).
-- Reference data only (public HGNC gene symbols and standard names of
-- inherited and pharmacogenomic markers), no patient data, no dependency on
-- any patient row. Built from public knowledge, never from documents.
-- Additive and idempotent: INSERT OR IGNORE, nothing renamed or moved.
-- Category genetic_variant: the lab rule parsers and the lab verifier read
-- only the lab side of the Vocabulary, so none of these symbols can become a
-- lab fact (same rule as migration 087). The clinical biomarker verifier maps
-- a printed gene label to one of these codes for a carrier, pharmacogenomic
-- or risk-variant result of a general-health patient.
-- No semicolons inside comments: the best-effort fallback splits on them.

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('CYP2C19', 'genetic_variant', 'CYP2C19', 'CYP2C19', NULL, NULL, '["CYP2C19"]', '{}'),
('CYP2C9', 'genetic_variant', 'CYP2C9', 'CYP2C9', NULL, NULL, '["CYP2C9"]', '{}'),
('CYP2D6', 'genetic_variant', 'CYP2D6', 'CYP2D6', NULL, NULL, '["CYP2D6"]', '{}'),
('CYP3A5', 'genetic_variant', 'CYP3A5', 'CYP3A5', NULL, NULL, '["CYP3A5"]', '{}'),
('VKORC1', 'genetic_variant', 'VKORC1', 'VKORC1', NULL, NULL, '["VKORC1"]', '{}'),
('SLCO1B1', 'genetic_variant', 'SLCO1B1', 'SLCO1B1', NULL, NULL, '["SLCO1B1"]', '{}'),
('DPYD', 'genetic_variant', 'DPYD', 'DPYD', NULL, NULL, '["DPYD"]', '{}'),
('TPMT', 'genetic_variant', 'TPMT', 'TPMT', NULL, NULL, '["TPMT"]', '{}'),
('NUDT15', 'genetic_variant', 'NUDT15', 'NUDT15', NULL, NULL, '["NUDT15"]', '{}'),
('UGT1A1', 'genetic_variant', 'UGT1A1', 'UGT1A1', NULL, NULL, '["UGT1A1"]', '{}'),
('HLA_B', 'genetic_variant', 'HLA-B', 'HLA-B', NULL, NULL, '["HLA-B"]', '{}'),
('F5', 'genetic_variant', 'Faktor V Leiden', 'Factor V Leiden', NULL, NULL, '["F5","Faktor V Leiden","Factor V Leiden","FV Leiden","FVL","F5 Leiden"]', '{}'),
('F2', 'genetic_variant', 'Protrombín G20210A', 'Prothrombin G20210A', NULL, NULL, '["F2","F2 G20210A","Protrombín G20210A","Prothrombin G20210A","Faktor II G20210A"]', '{}'),
('MTHFR', 'genetic_variant', 'MTHFR', 'MTHFR', NULL, NULL, '["MTHFR"]', '{}'),
('HFE', 'genetic_variant', 'HFE', 'HFE', NULL, NULL, '["HFE"]', '{}'),
('CFTR', 'genetic_variant', 'CFTR', 'CFTR', NULL, NULL, '["CFTR"]', '{}'),
('APOE', 'genetic_variant', 'APOE', 'APOE', NULL, NULL, '["APOE","ApoE"]', '{}'),
('SERPINA1', 'genetic_variant', 'SERPINA1', 'SERPINA1', NULL, NULL, '["SERPINA1"]', '{}'),
('HBB', 'genetic_variant', 'HBB', 'HBB', NULL, NULL, '["HBB"]', '{}');
