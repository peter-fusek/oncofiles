-- Store the GDrive folder owner's email for automatic permission sharing
-- When service account creates files, it grants writer access to this email
ALTER TABLE oauth_tokens ADD COLUMN owner_email TEXT;
