-- #1022 chat history (epic #1018). Additive only: one new table, no backfill.
--
-- One row per chat message (a question of the caregiver or an answer of the
-- local model), always for ONE patient. Written only through
-- oncofiles.chat_history (the storage API the chat core and the dashboard
-- call); read back by the same module for follow-up questions, the
-- conversation list, the data export (decrypted) and nothing else.
--
-- conversation_id: random UUID chosen by the chat core; one conversation
--                  belongs to exactly one patient (a write naming a
--                  conversation of another patient is refused).
-- channel:         dashboard | signal | whatsapp (vocab domain chat_channel).
-- role:            user | assistant (vocab domain chat_role).
-- row_uuid:        random UUID per row; part of the AES-GCM associated data.
-- body_enc:        the message text, AES-GCM with secrets_keys.chat_history_key()
--                  (own purpose string; from CHAT_HISTORY_SECRET when set,
--                  else derived from the mandatory HOLDING_STORE_SECRET),
--                  associated data chat|<patient_id>|<conversation_id>|<row_uuid>,
--                  so a body copied onto any other row never decrypts.
--                  NULL once purged. Never logged, never sent to a cloud model.
-- citations_json:  [{"document_id": n, "page": n|null}, ...]: ids and pages
--                  only, never text.
-- model:           the local model id that wrote an answer (NULL for questions).
-- grounding_json:  {"code": count} of the grounding guard (counts only).
-- caller_email_hash: keyed hash of the signed-in caller (never the address).
-- restored_at:     the conversation was restored from the trash; the 90-day
--                  clock then counts from here (like inbound_quarantine).
--
-- Retention (maintainer decision 2026-11-28, RUNBOOK lifecycle; job
-- chat_history_retention, oncofiles.chat_retention, CHAT_RETENTION_MODE
-- default off): 90 days after created_at (or restored_at) a row moves to the
-- trash (deleted_at, deleted_by = 'retention'); 30 days later its content is
-- purged (body_enc, citations_json, grounding_json, caller_email_hash set
-- NULL, purged_at set). The row itself is never hard-deleted by a job; the
-- #856 erasure of a patient on request removes it with the patient.
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    conversation_id TEXT NOT NULL,
    channel TEXT NOT NULL CHECK (channel IN ('dashboard', 'signal', 'whatsapp')),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    row_uuid TEXT NOT NULL UNIQUE,
    body_enc BLOB,
    citations_json TEXT,
    model TEXT,
    grounding_json TEXT,
    caller_email_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    restored_at TEXT,
    deleted_at TEXT,
    deleted_by TEXT,
    purged_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation
    ON chat_messages(patient_id, conversation_id, id);

CREATE INDEX IF NOT EXISTS idx_chat_messages_retention
    ON chat_messages(purged_at, deleted_at, created_at);
