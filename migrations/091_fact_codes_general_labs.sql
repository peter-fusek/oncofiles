-- Migration 091: general-health lab vocabulary for fact_codes (#814, #831).
-- Reference data only (generic Slovak lab test names and public lab
-- nomenclature), no patient data, no dependency on any patient row. Built
-- from public knowledge and from the label review list of the shadow preview
-- (label strings only), never from document lines.
-- Additive and idempotent: INSERT OR IGNORE for new codes, aliases appended
-- only when not present yet, nothing renamed, moved or removed.
-- Codes and aliases are category lab, so the lab rule parsers read them too
-- (INDEX_VERSION bump in the same change).
-- No semicolons inside comments: the best-effort fallback splits on them.

INSERT OR IGNORE INTO fact_codes (code, category, name_sk, name_en, loinc, unit_canonical, aliases, unit_conversions) VALUES
('ESR', 'lab', 'Sedimentácia erytrocytov (FW)', 'Erythrocyte sedimentation rate', NULL, 'mm/h', '["ESR","FW","FW po 1. hodine","FW po 1 hod.","Sedimentácia erytrocytov","Sedimentácia"]', '{"mm": 1}'),
('CHOL', 'lab', 'Cholesterol celkový', 'Total cholesterol', NULL, 'mmol/L', '["Cholesterol","Cholesterol celkový","Celkový cholesterol","Total cholesterol"]', '{"mg/dl": 0.02586}'),
('TRIG', 'lab', 'Triacylglyceroly', 'Triglycerides', NULL, 'mmol/L', '["Triacylglyceroly","Triglyceridy","Triglycerides","TAG"]', '{"mg/dl": 0.01129}'),
('BILI_DIRECT', 'lab', 'Bilirubín konjugovaný', 'Direct (conjugated) bilirubin', NULL, 'µmol/L', '["Bilirubín konjugovaný","Konjugovaný bilirubín","Bilirubín priamy","Priamy bilirubín","Direct bilirubin","Conjugated bilirubin"]', '{"mg/dl": 17.1}'),
('LUC', 'lab', 'Veľké nezafarbené bunky (LUC)', 'Large unstained cells', NULL, '%', '["LUC","LUC%","Veľké nezafarbené bunky","Large unstained cells"]', '{}'),
('PDW_PCT', 'lab', 'Šírka distribúcie trombocytov (%)', 'Platelet distribution width (%)', NULL, '%', '["PDW%","Šírka distr. PLT %"]', '{}'),
('USED_SMALL_EPI', 'lab', 'Močový sediment - malé guľaté epitélie', 'Urine sediment small round epithelia', NULL, '/µl', '["USED - Malé guľaté epitélie","U-SED - Malé guľaté epitélie","U-SED-Malé guľaté epitélie"]', '{}');

-- aliases for existing codes: one row per (code, alias), appended when absent
CREATE TEMP TABLE IF NOT EXISTS _m091_aliases (code TEXT NOT NULL, alias TEXT NOT NULL);
DELETE FROM _m091_aliases;
INSERT INTO _m091_aliases (code, alias) VALUES
('U_ERY', 'U-SED - Erytrocyty'), ('U_ERY', 'U-SED-Erytrocyty'),
('USED_BACT', 'U-SED - Baktérie'), ('USED_BACT', 'U-SED-Baktérie'),
('USED_EPI', 'U-SED - Epitélie ploché'), ('USED_EPI', 'U-SED-Epitélie ploché'),
('USED_MUCUS', 'U-SED - Hlienové vlákna'), ('USED_MUCUS', 'U-SED-Hlienové vlákna'),
('USED_CRYST', 'U-SED - Kryštály'), ('USED_CRYST', 'U-SED-Kryštály'),
('USED_CAST', 'U-SED - Valce'), ('USED_CAST', 'U-SED-Valce'),
('USED_YEAST', 'U-SED - Kvasinky'), ('USED_YEAST', 'U-SED-Kvasinky'),
('USED_PCAST', 'U-SED - Patologické valce'), ('USED_PCAST', 'U-SED-Patologické valce'),
('MCHC', 'Stred.farebná konc. HGB v RBC'), ('MCHC', 'Stredná farebná koncentrácia HGB v RBC'),
('MCH', 'Stred.hmot.HGB v RBC'), ('MCH', 'Stredná hmotnosť HGB v RBC'),
('MCV', 'Stred.obj.RBC'), ('MCV', 'Stredný objem RBC'),
('RDW', 'Šírka distr. RBC'), ('RDW', 'Šírka distribúcie RBC'),
('PDW', 'Šírka distribúcie PLT'),
('EGFR', 'GF podľa rovnice CKD EPI'), ('EGFR', 'GF podľ''a rovnice CKD EPI'), ('EGFR', 'eGFR CKD-EPI'),
('INR', 'PT-INR (liečení pacienti)'), ('INR', 'PT- INR (liečení pacienti)'), ('INR', 'INR (liečení pacienti)');
UPDATE fact_codes SET aliases = (
    SELECT json_group_array(value) FROM (
        SELECT value FROM json_each(fact_codes.aliases)
        UNION ALL
        SELECT a.alias FROM _m091_aliases a
        WHERE a.code = fact_codes.code
          AND a.alias NOT IN (SELECT value FROM json_each(fact_codes.aliases))
    )
)
WHERE code IN (SELECT code FROM _m091_aliases);
DROP TABLE IF EXISTS _m091_aliases;

-- No unit conversion of an existing code changes here (a separate change
-- decides the HCT ratio unit, because it would set canonical values of
-- existing facts).
