-- Migration 070: onboarding event trail for #468.
--
-- Captures funnel events as a new caregiver joins (T0 created, later T1
-- oauth_ok, T2 folder_set, T3 first_sync, T4 first_ai). Admin notification
-- dispatcher reads `admin_notified_at IS NULL` to find unsent events.
--
-- v5.19 Session 2 ships only the T0 hook + real-time admin email; later
-- events are wired up in v5.20.
--
-- Partial UNIQUE index enforces one-time-only semantics for events that
-- should fire exactly once per patient (created/oauth_ok/folder_set/
-- first_sync/first_ai). Repeatable events (stuck_24h/oauth_failure/
-- doc_limit_hit) are intentionally NOT covered by the partial index — they
-- can have many rows per patient and will get a separate per-day uniqueness
-- guard when wired up.
--
-- INSERT OR IGNORE on a (patient_id, event_type) collision is the canonical
-- write path — silently dedupes on retry without raising.
--
-- Safe for Turso: plain CREATE TABLE + CREATE INDEX, no rename, no DML.

CREATE TABLE IF NOT EXISTS onboarding_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id         TEXT    NOT NULL,
    event_type         TEXT    NOT NULL,
    occurred_at        TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    meta_json          TEXT,
    admin_notified_at  TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_onboarding_unique_once
    ON onboarding_events(patient_id, event_type)
    WHERE event_type IN ('created', 'oauth_ok', 'folder_set', 'first_sync', 'first_ai');

CREATE INDEX IF NOT EXISTS idx_onboarding_patient
    ON onboarding_events(patient_id);

CREATE INDEX IF NOT EXISTS idx_onboarding_pending
    ON onboarding_events(admin_notified_at, event_type);
