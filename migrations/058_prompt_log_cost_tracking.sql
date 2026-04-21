-- Migration 058: Per-call cost tracking in USD.
--
-- Foundation for cost-limiter v2 (#441) and tier-based budget gating (#442).
-- This migration is SCHEMA-ONLY — no behavior change. Populating the column
-- and adding the budget check happens in v5.10.2.
--
-- Cost formula (computed at insert time in v5.10.2):
--   estimated_cost_usd = (input_tokens  × INPUT_PRICE_PER_MTOK  / 1_000_000)
--                     + (output_tokens × OUTPUT_PRICE_PER_MTOK / 1_000_000)
-- Prices per model from a constant table in config.py. Haiku 4.5 today:
--   input=$1/MTok, output=$5/MTok. Cache hit input tokens priced at 10% of input.
--
-- Why REAL (not INTEGER cents): Haiku calls are often sub-cent. Using cents
-- would round everything to 0 and break budget math. REAL with precision to
-- ~6 decimal places gives us $0.000005 granularity, well under one Haiku call.
--
-- Budget queries (v5.10.2) will use:
--   SELECT SUM(estimated_cost_usd) FROM prompt_log
--    WHERE patient_id = ? AND created_at > datetime('now', '-30 days')
-- Rolling 30-day per-patient spend. See memory/project_cost_budget.md for the
-- tiered free/paid model (€30 onboarding, €10 steady, monthly bucket).
--
-- Safe for Turso: simple ADD COLUMN, no rename, no data migration.
-- Indexed because the daily budget check runs once per patient per nightly
-- pipeline — needs to be fast.

ALTER TABLE prompt_log ADD COLUMN estimated_cost_usd REAL DEFAULT NULL;

CREATE INDEX IF NOT EXISTS idx_prompt_log_patient_cost
    ON prompt_log(patient_id, created_at, estimated_cost_usd);
