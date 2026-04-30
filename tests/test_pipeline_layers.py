"""Lock-in for #441 Layers 2+3+5+6.

Layer 2 — `_run_nightly_ai_pipeline` no longer applies the
``AI_REPROCESS_MAX_AGE_HOURS`` window. Backlog drains as budget permits.

Layer 3 — Tier 3 discovery (split + consolidation) lives in a separate
``_run_weekly_discovery_pipeline`` scheduled Sunday at
``AI_NIGHT_WINDOW_START_UTC + 0:30``. The daily nightly pipeline only
runs Phase 1+2+3 (sync + enhance + metadata).

Layer 5 — `prompt_log.prompt_hash` column populated by `log_ai_call`
via `compute_prompt_hash(system, user, model)` so future async callers
can dedup against the 30-day window without a separate migration.

Layer 6 — `/readiness.jobs.{job}.last_cost_usd` per job + cluster-wide
`/readiness.ai_cost_usd_24h` aggregate. Cost telemetry is what tells the
operator whether the budget gate is engaging or being bypassed.
"""

from __future__ import annotations

import inspect

from oncofiles import server
from oncofiles.prompt_dedup import compute_prompt_hash

# ── Layer 2: max_age_hours filter dropped from the nightly pipeline ───


def test_nightly_pipeline_drops_max_age_filter():
    """The pipeline must call enhance/extract with ``max_age_hours=None``,
    not the legacy ``AI_REPROCESS_MAX_AGE_HOURS``. Pre-Layer-2, any doc
    older than 36h that didn't make a window was orphaned forever."""
    src = inspect.getsource(server)
    # The OLD pattern explicitly passed AI_REPROCESS_MAX_AGE_HOURS — locking
    # the new shape means that string can no longer appear inside the
    # pipeline closure (it stays elsewhere, e.g. config import + log msg).
    pipeline_src = src[src.index("async def _run_nightly_ai_pipeline") :]
    pipeline_src = pipeline_src[: pipeline_src.index("async def _run_weekly_discovery_pipeline")]
    assert "max_age_hours=AI_REPROCESS_MAX_AGE_HOURS" not in pipeline_src, (
        "_run_nightly_ai_pipeline must not pass AI_REPROCESS_MAX_AGE_HOURS — "
        "Layer 2 dropped the age filter; only_new=True is the only filter."
    )
    assert "max_age_hours=None" in pipeline_src


# ── Layer 3: separate weekly discovery pipeline ───────────────────────


def test_weekly_discovery_pipeline_function_exists():
    """The Tier 3 (split + consolidation) work moved out of the nightly
    pipeline into its own function so it can run on a separate schedule."""
    src = inspect.getsource(server)
    assert "async def _run_weekly_discovery_pipeline" in src
    # Must call the two T3 backfills.
    weekly_src = src[src.index("async def _run_weekly_discovery_pipeline") :]
    assert "backfill_multi_document_splits" in weekly_src
    assert "backfill_consolidation" in weekly_src


def test_weekly_discovery_scheduled_on_sunday():
    """The weekly job must register with `day_of_week='sun'` so it fires
    once per week, not nightly."""
    src = inspect.getsource(server)
    # Find the scheduler.add_job block for weekly_discovery.
    job_idx = src.index('id="weekly_discovery_pipeline"')
    block = src[max(0, job_idx - 400) : job_idx + 200]
    assert 'day_of_week="sun"' in block, (
        "weekly_discovery_pipeline must schedule on Sunday, not nightly"
    )
    # Carries the same misfire-grace-time / coalesce as the nightly job
    # (#440 lesson: cron jobs without these silently miss deploys).
    assert "misfire_grace_time=1800" in block
    assert "coalesce=True" in block


def test_nightly_pipeline_no_longer_runs_phase_4():
    """Splits + consolidation must NOT run inside the nightly pipeline —
    they belong to Layer 3 weekly discovery now."""
    src = inspect.getsource(server)
    pipeline_src = src[src.index("async def _run_nightly_ai_pipeline") :]
    pipeline_src = pipeline_src[: pipeline_src.index("async def _run_weekly_discovery_pipeline")]
    assert "backfill_multi_document_splits" not in pipeline_src, (
        "splits moved to weekly discovery (#441 Layer 3)"
    )
    assert "backfill_consolidation" not in pipeline_src, (
        "consolidation moved to weekly discovery (#441 Layer 3)"
    )


# ── Layer 5: prompt_hash recorded on every log_ai_call ────────────────


def test_compute_prompt_hash_is_stable():
    """Same inputs → same hash. SHA-256 so collisions are astronomically
    unlikely."""
    h1 = compute_prompt_hash("sys", "user", "model")
    h2 = compute_prompt_hash("sys", "user", "model")
    assert h1 == h2
    # Length is 64 hex chars (SHA-256).
    assert len(h1) == 64


def test_compute_prompt_hash_distinguishes_inputs():
    """Different system, user, or model → different hash. The boundary
    between fields is null-byte-separated so 'a' + 'b' ≠ 'ab' + ''."""
    h_base = compute_prompt_hash("sys", "user", "m")
    assert compute_prompt_hash("sys2", "user", "m") != h_base
    assert compute_prompt_hash("sys", "user2", "m") != h_base
    assert compute_prompt_hash("sys", "user", "m2") != h_base
    # Boundary check: shifting bytes between fields must not collide.
    assert compute_prompt_hash("sysu", "ser", "m") != compute_prompt_hash("sys", "user", "m")


def test_log_ai_call_auto_computes_prompt_hash():
    """`log_ai_call` defaults `prompt_hash=None` and falls back to computing
    it from system+user+model. Locks the contract so future logger callers
    don't need to compute the hash themselves."""
    from oncofiles import prompt_logger

    logger_src = inspect.getsource(prompt_logger.log_ai_call)
    assert "compute_prompt_hash" in logger_src
    # The fallback only fires when the caller didn't supply one.
    assert "prompt_hash is None" in logger_src


# ── Layer 6: cost telemetry surfaces in /readiness ────────────────────


def test_readiness_exposes_per_job_last_cost_usd():
    """When a pipeline run finishes, it stores `last_cost_usd` in
    `job_tracker[job_id]`. /readiness must surface that field."""
    from oncofiles.server import readiness

    src = inspect.getsource(readiness)
    assert "last_cost_usd" in src, "/readiness must expose per-job last_cost_usd (#441 Layer 6)"


def test_readiness_exposes_cluster_ai_cost_24h():
    """Cluster-wide AI cost in the last 24h is the operator's primary
    spending dial. /readiness must surface it as `ai_cost_usd_24h`."""
    from oncofiles.server import readiness

    src = inspect.getsource(readiness)
    assert "ai_cost_usd_24h" in src
    # The query must filter by status='ok' so failed calls don't inflate cost.
    assert "status = 'ok'" in src


def test_pipeline_records_last_cost_usd_in_job_tracker():
    """The closure that wraps each pipeline must record cost_delta in
    job_tracker so /readiness can surface it. Source-level lock against
    a future refactor that drops the recording."""
    src = inspect.getsource(server)
    nightly_src = src[src.index("async def _run_nightly_ai_pipeline") :]
    nightly_src = nightly_src[: nightly_src.index("async def _run_weekly_discovery_pipeline")]
    assert 'job_tracker.get("nightly_ai_pipeline"' in nightly_src
    assert '"last_cost_usd"' in nightly_src

    weekly_src = src[src.index("async def _run_weekly_discovery_pipeline") :]
    weekly_src = weekly_src[: weekly_src.index("from apscheduler.triggers.cron")]
    assert 'job_tracker.get("weekly_discovery_pipeline"' in weekly_src
    assert '"last_cost_usd"' in weekly_src


# ── Layer 1 sanity: budget gate still wired ───────────────────────────


def test_nightly_pipeline_calls_check_patient_budget():
    """Every Layer-1 budget gate must remain — Layer 2's drop of
    max_age_hours puts MORE docs in the queue, so the budget check is
    the only thing keeping a heavy onboarding from blowing the cap."""
    src = inspect.getsource(server)
    nightly_src = src[src.index("async def _run_nightly_ai_pipeline") :]
    nightly_src = nightly_src[: nightly_src.index("async def _run_weekly_discovery_pipeline")]
    assert "check_patient_budget" in nightly_src


def test_weekly_discovery_pipeline_calls_check_patient_budget():
    """Same gate on the weekly job — T3 cost is the highest per-call,
    skipping for over-budget patients matters even more here."""
    src = inspect.getsource(server)
    weekly_src = src[src.index("async def _run_weekly_discovery_pipeline") :]
    weekly_src = weekly_src[: weekly_src.index("from apscheduler.triggers.cron")]
    assert "check_patient_budget" in weekly_src
