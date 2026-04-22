"""Tests for per-patient AI cost budget gating (#442)."""

from __future__ import annotations

from oncofiles.budget import (
    TIER_CAPS_EUR,
    USD_TO_EUR,
    check_patient_budget,
)
from oncofiles.database import Database
from oncofiles.models import PromptCallType, PromptLogEntry
from tests.helpers import ERIKA_UUID


async def _log_ai_call(db: Database, *, input_tokens: int, output_tokens: int) -> None:
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.SUMMARY_TAGS,
            patient_id=ERIKA_UUID,
            model="claude-haiku-4-5",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            status="ok",
            result_summary="test",
        )
    )


async def _set_tier(db: Database, tier: str) -> None:
    await db.db.execute(
        "UPDATE patients SET tier = ? WHERE patient_id = ?",
        (tier, ERIKA_UUID),
    )
    await db.db.commit()


# ── Tier caps ────────────────────────────────────────────────────────────


def test_tier_caps_shape():
    """All expected tiers present; admin has None cap."""
    assert TIER_CAPS_EUR["free_onboarding"] == 30.0
    assert TIER_CAPS_EUR["free"] == 10.0
    assert TIER_CAPS_EUR["paid_basic"] == 30.0
    assert TIER_CAPS_EUR["paid_pro"] == 100.0
    assert TIER_CAPS_EUR["admin"] is None


# ── check_patient_budget ─────────────────────────────────────────────────


async def test_budget_empty_usage_within(db: Database):
    """Fresh patient (zero prompt_log rows) is within budget."""
    await _set_tier(db, "free")
    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.tier == "free"
    assert status.used_eur == 0.0
    assert status.cap_eur == 10.0
    assert status.within_budget is True
    assert status.in_onboarding is False


async def test_budget_admin_never_capped(db: Database):
    """tier='admin' returns cap_eur=None and within_budget=True even with
    large spend. Protects the 6 grandfathered prod patients."""
    await _set_tier(db, "admin")
    # Simulate 50M input + 50M output tokens → way past any cap
    await _log_ai_call(db, input_tokens=50_000_000, output_tokens=50_000_000)

    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.tier == "admin"
    assert status.cap_eur is None
    assert status.within_budget is True
    assert status.used_eur > 0


async def test_budget_under_cap(db: Database):
    """Small spend stays within 'free' tier's €10 cap."""
    await _set_tier(db, "free")
    # 100k input + 50k output tokens = $0.08 + $0.20 = $0.28 → ~€0.26
    await _log_ai_call(db, input_tokens=100_000, output_tokens=50_000)

    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.within_budget is True
    assert status.used_eur < 10.0
    assert status.used_eur > 0.0


async def test_budget_over_cap_blocks(db: Database):
    """Heavy spend past 'free' €10 cap flips within_budget=False."""
    await _set_tier(db, "free")
    # 20M input + 1M output = $16 + $4 = $20 USD → ~€18.40 — past €10 cap
    await _log_ai_call(db, input_tokens=20_000_000, output_tokens=1_000_000)

    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.within_budget is False
    assert status.used_eur > 10.0
    assert status.cap_eur == 10.0


async def test_budget_onboarding_flag(db: Database):
    """tier='free_onboarding' sets in_onboarding=True + €30 cap."""
    await _set_tier(db, "free_onboarding")
    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.in_onboarding is True
    assert status.cap_eur == 30.0
    assert status.within_budget is True


async def test_budget_excludes_error_calls(db: Database):
    """Failed prompt_log rows (status='error') do NOT count toward spend."""
    await _set_tier(db, "free")
    # Insert an error-status call with high token counts
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.SUMMARY_TAGS,
            patient_id=ERIKA_UUID,
            model="claude-haiku-4-5",
            input_tokens=50_000_000,
            output_tokens=5_000_000,
            status="error",
            result_summary="boom",
            error_message="timeout",
        )
    )
    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.used_eur == 0.0
    assert status.within_budget is True


async def test_budget_excludes_other_patients(db: Database):
    """Spend on a different patient does not affect this patient's cap."""
    await _set_tier(db, "free")
    # Log a big spend for a DIFFERENT patient_id
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.SUMMARY_TAGS,
            patient_id="00000000-0000-4000-8000-000000000099",
            model="claude-haiku-4-5",
            input_tokens=50_000_000,
            output_tokens=5_000_000,
            status="ok",
            result_summary="other patient's spend",
        )
    )
    status = await check_patient_budget(db, ERIKA_UUID)
    assert status.used_eur == 0.0
    assert status.within_budget is True


async def test_budget_usd_to_eur_applied(db: Database):
    """used_eur = used_usd × USD_TO_EUR. Sanity check on the conversion."""
    await _set_tier(db, "free")
    # 1M input + 1M output = $0.80 + $4.00 = $4.80 USD
    await _log_ai_call(db, input_tokens=1_000_000, output_tokens=1_000_000)

    status = await check_patient_budget(db, ERIKA_UUID)
    expected_eur = 4.80 * USD_TO_EUR
    assert abs(status.used_eur - expected_eur) < 0.01
