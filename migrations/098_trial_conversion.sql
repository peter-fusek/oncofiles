-- #950 phase 3: holding -> Drive on connect, the "ready" e-mail and the
-- time-to-value funnel. Additive only: nullable instants on trial_accounts,
-- no DML, no index change. Rollback: ignore the columns.
--
-- drive_connected_at: the trial patient connected a Drive folder (set once,
--                     by drive_folder_link.connect_folder).
-- converted_at:       every held document of the trial has a verified copy in
--                     the patient's curated inbox (or was already there); the
--                     trial is over: holding_mode is false, the phase-1 caps
--                     apply. Set once by oncofiles.holding_move.
-- first_reply_at:     the first welcome@ reply was accepted by the relay.
-- signed_in_at:       the first dashboard sign-in with the sending address.
-- ready_mail_sent_at: the one "your documents are ready" e-mail (claimed
--                     before sending, so it is sent at most once per trial).
-- move_claimed_at:    lease of the holding mover on this trial (the
--                     inbound_jobs pattern): claimed with ONE conditional
--                     UPDATE (unset or older than the stale cutoff),
--                     refreshed before every file copy, every per-row record
--                     written only while it holds, released at the end. Two
--                     containers of a rolling deploy never copy one trial at
--                     once.
--
-- received_at is created_at, first_doc_ready comes from the holding rows'
-- extracted_at and first_mcp_call from activity_log: no column for them.
ALTER TABLE trial_accounts ADD COLUMN drive_connected_at TEXT;
ALTER TABLE trial_accounts ADD COLUMN converted_at TEXT;
ALTER TABLE trial_accounts ADD COLUMN first_reply_at TEXT;
ALTER TABLE trial_accounts ADD COLUMN signed_in_at TEXT;
ALTER TABLE trial_accounts ADD COLUMN ready_mail_sent_at TEXT;
ALTER TABLE trial_accounts ADD COLUMN move_claimed_at TEXT;
