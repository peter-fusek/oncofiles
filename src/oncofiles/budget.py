"""AI cost budget per patient (#442).

Rolling 30-day monthly EUR bucket, NOT a daily linear rate. Peter's locked
cost model (2026-04-21):

- free_onboarding: €30 for the first 30 days after patient creation
- free: €10/month steady state after onboarding ends
- paid_basic: €30/month, paid_pro: €100/month (Stripe — separate PR)
- admin: no cap (grandfathered prod patients)

Rationale for monthly bucket: a typical user spends ~€0 most days and 1-2
days near the cap (onboarding, batch upload). A daily sub-cap would break
a 500-doc cold start in one afternoon, which is a valid spend pattern.

The budget query is a single SUM() over `prompt_log` filtered by
patient_id + rolling 30-day window, with cost computed inline from
input_tokens + output_tokens × Haiku 4.5 pricing — sidesteps the need to
populate `prompt_log.estimated_cost_usd` on every insert (migration 058's
column remains the intended long-term home if Haiku pricing moves).
"""

from __future__ import annotations

from dataclasses import dataclass

# Haiku 4.5 pricing per 1M tokens (mirror of _analytics._HAIKU_*). Keep in
# sync if pricing changes — or switch to a table keyed by model name.
_HAIKU_INPUT_PER_M = 0.80
_HAIKU_OUTPUT_PER_M = 4.00

# Fixed USD→EUR conversion for budget math. Slightly conservative so small
# FX drift doesn't silently break the cap. Updated manually when the spot
# rate moves > ±5% over 30 days.
USD_TO_EUR = 0.92

# Tier → monthly EUR cap. None = no cap (admin).
TIER_CAPS_EUR: dict[str, float | None] = {
    "free_onboarding": 30.0,
    "free": 10.0,
    "paid_basic": 30.0,
    "paid_pro": 100.0,
    "admin": None,
}


@dataclass
class BudgetStatus:
    """Per-patient AI spend position."""

    patient_id: str
    tier: str
    used_eur: float  # rolling 30-day spend
    cap_eur: float | None  # None for tier='admin'
    within_budget: bool
    in_onboarding: bool  # tier == 'free_onboarding'

    def as_dict(self) -> dict:
        return {
            "patient_id": self.patient_id,
            "tier": self.tier,
            "used_eur": round(self.used_eur, 4),
            "cap_eur": self.cap_eur,
            "within_budget": self.within_budget,
            "in_onboarding": self.in_onboarding,
        }


async def check_patient_budget(db, patient_id: str) -> BudgetStatus:
    """Compute rolling 30-day AI spend for a patient and compare to tier cap.

    Single DB round-trip: one aggregate query on `prompt_log`. The cost is
    computed inline from `input_tokens` + `output_tokens` × Haiku pricing,
    so this works even when `prompt_log.estimated_cost_usd` hasn't been
    populated yet.

    Callers:
    - `_run_nightly_ai_pipeline` — at per-patient entry, skip if over
    - MCP `enhance_documents(force=True)` — warn but still run (explicit
      override)
    - `/api/patient-budget` — dashboard pill
    """
    patient = await db.get_patient(patient_id)
    tier = patient.tier if patient else "free_onboarding"
    cap = TIER_CAPS_EUR.get(tier, TIER_CAPS_EUR["free_onboarding"])

    # One aggregate query, status='ok' only — failed calls don't charge.
    sql = """
        SELECT COALESCE(SUM(
            (COALESCE(input_tokens, 0)  * ?
           + COALESCE(output_tokens, 0) * ?) / 1000000.0
        ), 0.0) AS usd
        FROM prompt_log
        WHERE patient_id = ?
          AND status = 'ok'
          AND created_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-30 days')
    """
    async with db.db.execute(sql, (_HAIKU_INPUT_PER_M, _HAIKU_OUTPUT_PER_M, patient_id)) as cursor:
        row = await cursor.fetchone()
        used_usd = float(row["usd"] or 0.0) if row is not None else 0.0

    used_eur = used_usd * USD_TO_EUR
    within = True if cap is None else used_eur < cap
    return BudgetStatus(
        patient_id=patient_id,
        tier=tier,
        used_eur=used_eur,
        cap_eur=cap,
        within_budget=within,
        in_onboarding=(tier == "free_onboarding"),
    )
