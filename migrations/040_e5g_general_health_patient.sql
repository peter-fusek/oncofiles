-- Migration 040: Configure e5g (Peter Fusek) as general health patient.
-- Sets profile to 'general' and creates patient context with DOB/sex for
-- preventive care screening. Merges with peter-fusek-2 (stays archived).

UPDATE patients SET profile = 'general' WHERE patient_id = '40a0e0c2-ddc7-4402-909a-0b0f09926917';

INSERT INTO patient_context (patient_id, context_json, updated_at)
VALUES (
    '40a0e0c2-ddc7-4402-909a-0b0f09926917',
    '{"name":"Peter Fusek","patient_type":"general","date_of_birth":"1980-05-06","sex":"male","diagnosis":"","staging":"","histology":"","tumor_site":"","diagnosis_date":"","biomarkers":{},"treatment":{},"metastases":[],"comorbidities":[],"surgeries":[{"date":"1998-06-25","procedure":"Laparoscopic varicocelectomy (left)"}],"physicians":{},"excluded_therapies":[],"note":"General health patient. Preventive care, periodic screenings."}',
    strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
)
ON CONFLICT(patient_id) DO UPDATE SET
    context_json = excluded.context_json,
    updated_at = excluded.updated_at;
