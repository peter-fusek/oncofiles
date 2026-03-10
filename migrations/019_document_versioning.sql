-- Document versioning: track version number and link to previous version
ALTER TABLE documents ADD COLUMN version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE documents ADD COLUMN previous_version_id INTEGER REFERENCES documents(id);
