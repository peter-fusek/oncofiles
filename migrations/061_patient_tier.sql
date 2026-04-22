-- Migration 061: Patient tier + onboarding window + upgrade tracking.
--
-- Billing foundation for cost-limiter v2 (#442). Pairs with migration 058's
-- prompt_log.estimated_cost_usd column to enforce a rolling 30-day monthly
-- EUR budget per patient (see memory/project_cost_budget.md).
--
-- Locked cost model (Peter 2026-04-21):
--   - Monthly bucket, NOT daily linear.
--   - free_onboarding: €30 for first 30 days (cold-start room).
--   - free: €10 steady state after onboarding ends.
--   - paid_basic: €30, paid_pro: €100 (Stripe integration later — separate PR).
--   - admin: no cap. All existing prod patients grandfathered.
--
-- Safe for Turso: ADD COLUMN only, no RENAME, no DROP+CREATE. CHECK constraint
-- on tier guards against typos in future UPDATEs.
--
-- Grandfather rule: every patient row that exists on migration day goes to
-- 'admin'. New patients default to 'free_onboarding'. The nightly
-- _tier_transition_job moves free_onboarding → free when the window expires.

ALTER TABLE patients ADD COLUMN tier TEXT NOT NULL DEFAULT 'free_onboarding'
    CHECK (tier IN ('free_onboarding','free','paid_basic','paid_pro','admin'));

ALTER TABLE patients ADD COLUMN onboarding_ends_at TEXT;

ALTER TABLE patients ADD COLUMN upgraded_at TEXT;

ALTER TABLE patients ADD COLUMN tier_notes TEXT;

-- Grandfather every existing patient as 'admin' so v5.13 rollout doesn't
-- suddenly budget-gate anybody. New-patient onboarding uses the default.
UPDATE patients SET tier = 'admin'
 WHERE tier = 'free_onboarding';

-- Seed onboarding_ends_at for any future rows (migration-run timestamp + 30d)
-- but only when not already set. Admin rows still get it populated for
-- consistency, even though their cap isn't enforced.
UPDATE patients
   SET onboarding_ends_at = strftime('%Y-%m-%dT%H:%M:%SZ', created_at, '+30 days')
 WHERE onboarding_ends_at IS NULL;

-- Index to keep _tier_transition_job cheap
CREATE INDEX IF NOT EXISTS idx_patients_tier_onboarding
    ON patients(tier, onboarding_ends_at);
