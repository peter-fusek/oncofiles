-- Rename 'trigger' column to 'sync_trigger' to avoid SQL reserved word conflict
ALTER TABLE sync_history RENAME COLUMN "trigger" TO sync_trigger;
