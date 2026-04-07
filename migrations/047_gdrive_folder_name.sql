-- Cache the GDrive folder display name alongside folder_id
ALTER TABLE oauth_tokens ADD COLUMN gdrive_folder_name TEXT;
