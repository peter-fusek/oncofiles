-- Migration 069: prompt-hash dedup column for #441 Layer 5.
--
-- Adds a `prompt_hash` column that holds a SHA-256 of (system_prompt ||
-- user_prompt || model). Successful AI calls record their hash; future
-- calls compute the same hash and check `prompt_log` for a match in the
-- last 30 days. On a hit the persisted `raw_response` is reused, no
-- Anthropic round-trip happens.
--
-- The index is non-unique because legitimate cache hits will produce
-- multiple rows with the same hash (one entry per HIT — we still log
-- so cache hit rate is observable).
--
-- Pre-#441-Layer-5 rows have NULL prompt_hash and won't dedup. That's
-- intentional: the dedup window is rolling 30 days anyway, so the cold
-- backfill cost would amortize to zero quickly. Per the global "no bulk
-- startup migrations" rule (CLAUDE.md), no backfill SQL here — new rows
-- get hashes, old rows don't, and within ~30 days the table self-heals.
--
-- Safe for Turso: simple ADD COLUMN + CREATE INDEX, no rename, no DML.

ALTER TABLE prompt_log ADD COLUMN prompt_hash TEXT DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_prompt_log_hash ON prompt_log(prompt_hash);
