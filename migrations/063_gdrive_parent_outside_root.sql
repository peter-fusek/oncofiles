-- Migration 063: flag documents whose GDrive file lives OUTSIDE the patient's sync root.
--
-- Context (#477 Issue 1): PacientAdvokat (and similar 3rd-party services) upload
-- markdown summaries into their own GDrive location, then register the gdrive_id in
-- oncofiles via bulk import. The IDs are valid, but the files are NOT children of
-- the patient's sync root — so every sync logs them as "missing from GDrive", even
-- though they still render fine via gdrive_url.
--
-- The fix reshapes sync_from_gdrive's "not in seen set" branch into a 3-way
-- classification: (a) file genuinely deleted → sync_state='deleted_remote',
-- (b) file exists but outside our root → set this flag, (c) otherwise noop.
--
-- DDL-only. Zero-row UPDATE. Safe per the post-#476 rule: bulk data fixes must run
-- from chunked MCP tools, not startup migrations. Existing rows default to 0 and
-- get backfilled by the first sync cycle after deploy (per-patient, per-doc,
-- chunked by definition).

ALTER TABLE documents ADD COLUMN gdrive_parent_outside_root INTEGER DEFAULT 0;
