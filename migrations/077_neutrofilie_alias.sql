-- Migration 077: "Neutrofilie" (a common spelling in one lab's sheets) maps to
-- NEUT (#635 v10). Reference vocabulary only, no patient data. The unit decides
-- the code at extraction: "%" -> NEUT_PCT, a count unit -> NEUT (#659 tests).

UPDATE fact_codes SET aliases = '["NEUT","NEU","Neutrofily","Neutrofilie","Neutrophils","NEUT#","Neutrofily abs.","abs. počet NEUT","Neutrofily abs.počet","Neutrofilie abs.počet","ANC"]' WHERE code = 'NEUT';
