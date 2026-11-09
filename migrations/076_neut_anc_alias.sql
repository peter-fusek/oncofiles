-- Migration 076: "ANC" (absolute neutrophil count) as an alias of NEUT (#635).
-- Reference vocabulary only (no patient data).

UPDATE fact_codes SET aliases = '["NEUT","NEU","Neutrofily","Neutrophils","NEUT#","Neutrofily abs.","abs. počet NEUT","Neutrofily abs.počet","Neutrofilie abs.počet","ANC"]' WHERE code = 'NEUT';
