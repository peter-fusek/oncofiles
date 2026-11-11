-- #590 / #604: record HOW each audited tool call was authenticated.
-- Additive only. Rows written before this migration keep auth_method = ''
-- (unknown) and caller_email_hash = NULL.
--
-- auth_method: oauth | patient_bearer | static_bearer | stdio | none
-- caller_email_hash: keyed HMAC-SHA256 (secrets_keys.caller_email_hash) of the
--   OAuth-bound Google email, never the email itself; NULL for non-OAuth calls.
ALTER TABLE activity_log ADD COLUMN auth_method TEXT NOT NULL DEFAULT '';
ALTER TABLE activity_log ADD COLUMN caller_email_hash TEXT;
