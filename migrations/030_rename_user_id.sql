-- Rename user_id → patient_id in tables that already have user_id (#134)
-- Turso/libSQL supports RENAME COLUMN (libSQL ≥ 0.1.11)

-- email_entries
ALTER TABLE email_entries RENAME COLUMN user_id TO patient_id;
UPDATE email_entries SET patient_id = 'erika' WHERE patient_id = 'default';

-- calendar_entries
ALTER TABLE calendar_entries RENAME COLUMN user_id TO patient_id;
UPDATE calendar_entries SET patient_id = 'erika' WHERE patient_id = 'default';

-- oauth_tokens
ALTER TABLE oauth_tokens RENAME COLUMN user_id TO patient_id;
UPDATE oauth_tokens SET patient_id = 'erika' WHERE patient_id = 'default';
