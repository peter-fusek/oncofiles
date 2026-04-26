-- Migration 067: Anthropic prompt-cache observability columns (#441 Layer 6).
--
-- Records the two cache-counter fields the Messages API returns in
-- `response.usage` so we can measure cache hit rate empirically:
--
--   cache_creation_input_tokens — tokens written to the cache (cost ~1.25× input)
--   cache_read_input_tokens     — tokens served from the cache (cost ~0.1× input)
--
-- Both default NULL — historical rows pre-#441 didn't capture these and we don't
-- want to invent zeros there (a real "0 cache hits" entry must be distinguishable
-- from "we didn't capture it"). New rows from prompt_logger.py / enhance.py /
-- doc_analysis.py populate them via getattr(response.usage, "cache_*_input_tokens", None).
--
-- Why this matters even before prompt-padding lands: Haiku 4.5's minimum
-- cacheable prefix is 4096 tokens. Every system prompt in oncofiles is currently
-- 145–1173 tokens, so cache_control: {type: "ephemeral"} is a guaranteed no-op
-- right now (silent — no error, just zeros). Without these telemetry columns
-- we cannot tell whether a future prompt change crosses the threshold and
-- starts paying off, so they're load-bearing for the Layer 4 follow-up.
--
-- Safe for Turso: simple ADD COLUMN, no rename, no data migration.

ALTER TABLE prompt_log ADD COLUMN cache_creation_input_tokens INTEGER DEFAULT NULL;
ALTER TABLE prompt_log ADD COLUMN cache_read_input_tokens INTEGER DEFAULT NULL;
