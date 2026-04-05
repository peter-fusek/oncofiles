-- Migration 042: Re-seed e5g patient context after migration 041 wiped it.
-- Only inserts if e5g patient exists in the patients table (safe for test DBs).

INSERT OR IGNORE INTO patient_context (patient_id, context_json, updated_at)
SELECT
    '40a0e0c2-ddc7-4402-909a-0b0f09926917',
    '{"name":"Peter Fusek","patient_type":"general","date_of_birth":"1980-05-06","sex":"male","diagnosis":"","staging":"","histology":"","tumor_site":"","diagnosis_date":"","biomarkers":{},"treatment":{},"metastases":[],"comorbidities":[],"surgeries":[{"date":"1998-06-25","procedure":"Laparoscopic varicocelectomy (left)"}],"physicians":{},"excluded_therapies":[],"note":"General health patient. Preventive care, periodic screenings."}',
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
WHERE EXISTS (SELECT 1 FROM patients WHERE patient_id = '40a0e0c2-ddc7-4402-909a-0b0f09926917');
