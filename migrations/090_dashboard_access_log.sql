-- #868: audit trail for dashboard access (#531 follow-up).
--
-- One row per dashboard sign-in, per ACL refusal of an authenticated caller,
-- and per admin read of a patient the admin is not caregiver of (deduped in
-- memory to once per session + patient per hour). Container logs reset on
-- every deploy, this table answers "who read which patient" afterwards.
--
-- Data minimisation: the caller is a keyed hash
-- (secrets_keys.caller_email_hash), never the e-mail, no document values,
-- no requested slug. target_patient_id is set only for admin reads and for
-- refusals of a known patient (NULL for sign-ins and unknown patients), so it
-- carries a role prefix: this is an operator log, not a patient-scoped table.
--
-- Retention: no automatic purge. The "No automatic deletion" lock (#734)
-- allowlists only prompt_log retention, a purge job here needs a maintainer
-- decision and an allowlist entry first. Rows are small.
--
-- Additive only: new table + index, no DML. Rollback: ignore the table.

CREATE TABLE IF NOT EXISTS dashboard_access_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event             TEXT    NOT NULL,
    caller_hash       TEXT,
    is_admin          INTEGER NOT NULL DEFAULT 0,
    target_patient_id TEXT REFERENCES patients(patient_id),
    route             TEXT    NOT NULL,
    created_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_dashboard_access_log_created
    ON dashboard_access_log(created_at);
