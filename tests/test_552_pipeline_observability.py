"""Regression locks for #552 — a dead pipeline must never read as healthy.

The GCP project behind all Google access was deleted on ~2026-07-06. Every
Drive call returned `403 "Project #417160101925 has been deleted."` for a
month while `/health` reported `status: ok` and the nightly job recorded a
fresh `last_ok`. Two defects made that possible:

  D1  `_run_nightly_ai_pipeline` swallowed every per-patient exception with
      `continue`, so the job never raised and `_job_executed` refreshed
      `last_ok` — a night where every patient failed looked exactly like a
      clean night.
  D2  `_handle_sync_exception` only promoted `invalid_grant` to a health
      surface; a hard 403 was logged and surfaced nowhere.

These tests lock the three fixes: classify structural auth failures (C2),
fail the nightly job when nothing succeeded (C1), and detect staleness
independently of any known failure mode (C3).
"""

from __future__ import annotations

import inspect

from oncofiles.database import Database

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"

# The verbatim error prod returned, for the whole month.
PROJECT_DELETED = (
    "<HttpError 403 when requesting https://www.googleapis.com/drive/v3/files?q=... "
    'returned "Project #417160101925 has been deleted.". Details: '
    "\"[{'message': 'Project #417160101925 has been deleted.', "
    "'domain': 'global', 'reason': 'forbidden'}]\">"
)


# ── C2: classifying structural credential failures ────────────────────────


def test_project_deleted_is_classified_as_hard_auth():
    """The exact #552 error must classify, or the whole fix is theatre."""
    from oncofiles.server import _hard_auth_reason

    assert _hard_auth_reason(Exception(PROJECT_DELETED)) == "project_deleted"


def test_api_not_enabled_and_dead_oauth_client_classify():
    from oncofiles.server import _hard_auth_reason

    assert (
        _hard_auth_reason(Exception("Drive API has not been used in project 123 before"))
        == "api_not_enabled"
    )
    assert _hard_auth_reason(Exception("accessNotConfigured")) == "api_not_enabled"
    assert _hard_auth_reason(Exception('{"error": "invalid_client"}')) == "oauth_client_invalid"


def test_insufficient_file_permissions_is_not_hard_auth():
    """#445 guard. `insufficientFilePermissions` means a single file is owned
    by someone else and shared read-only — sync survives it file-by-file.
    Classifying it here would permanently light up the health endpoint for
    Mattias-class patients whose Drive files are merely shared."""
    from oncofiles.server import _hard_auth_reason

    exc = Exception(
        '<HttpError 403 ... returned "The user has not granted the app write access". '
        "Details: \"[{'reason': 'insufficientFilePermissions'}]\">"
    )
    assert _hard_auth_reason(exc) is None


def test_ordinary_errors_are_not_hard_auth():
    from oncofiles.server import _hard_auth_reason

    assert _hard_auth_reason(TimeoutError("timed out")) is None
    assert _hard_auth_reason(ValueError("something else went wrong")) is None


def test_hard_auth_reason_recurses_into_cause():
    """asyncio.to_thread and googleapiclient re-raise with the real error
    nested, so top-level-only matching would miss it."""
    from oncofiles.server import _hard_auth_reason

    inner = Exception(PROJECT_DELETED)
    outer = RuntimeError("sync failed")
    outer.__cause__ = inner
    assert _hard_auth_reason(outer) == "project_deleted"


def test_hard_auth_reason_terminates_on_cause_cycle():
    """A self-referential __cause__ must not hang the health path."""
    from oncofiles.server import _hard_auth_reason

    a = Exception("boring")
    b = Exception("also boring")
    a.__cause__ = b
    b.__cause__ = a
    assert _hard_auth_reason(a) is None


# ── C2: tracking + clearing ───────────────────────────────────────────────


def test_handle_sync_exception_records_hard_auth_failure():
    from oncofiles import server

    server._hard_auth_failures.clear()
    try:
        server._handle_sync_exception("pid-1", "gdrive", Exception(PROJECT_DELETED))
        assert ("pid-1", "gdrive") in server._hard_auth_failures
        entry = server._hard_auth_failures[("pid-1", "gdrive")]
        assert entry["reason"] == "project_deleted"
        assert entry["first_seen"] and entry["last_seen"]
    finally:
        server._hard_auth_failures.clear()


def test_successful_sync_clears_hard_auth_failure():
    """Otherwise a repaired patient reports broken forever."""
    from oncofiles import server

    server._hard_auth_failures.clear()
    try:
        server._handle_sync_exception("pid-2", "gdrive", Exception(PROJECT_DELETED))
        assert ("pid-2", "gdrive") in server._hard_auth_failures
        server._record_service_sync("pid-2", "gdrive")
        assert ("pid-2", "gdrive") not in server._hard_auth_failures
    finally:
        server._hard_auth_failures.clear()


def test_invalid_grant_still_takes_its_own_path():
    """#415 behavior must be untouched: a stale refresh token is caregiver-
    fixable and must NOT be reported as a structural failure."""
    from oncofiles import server

    class RefreshError(Exception):
        pass

    RefreshError.__name__ = "RefreshError"
    server._hard_auth_failures.clear()
    server._invalid_grant_tokens.clear()
    try:
        server._handle_sync_exception("pid-3", "gdrive", RefreshError("invalid_grant"))
        assert ("pid-3", "gdrive") in server._invalid_grant_tokens
        assert ("pid-3", "gdrive") not in server._hard_auth_failures
    finally:
        server._hard_auth_failures.clear()
        server._invalid_grant_tokens.clear()


# ── C2/C3: endpoint contracts ─────────────────────────────────────────────


def test_health_reports_hard_auth_and_staleness_as_counts_only():
    """#488 precedent: /health is unauthenticated, so no patient IDs."""
    from oncofiles.server import health

    source = inspect.getsource(health)
    assert "hard_auth_failed_count" in source
    assert "stale_sync_count" in source
    # Detail shapes belong on operator-only /readiness, never here.
    assert 'hard_auth_failures"]' not in source
    assert '"patients"' not in source


def test_health_makes_no_db_call():
    """/health is the liveness probe — a blocking DB call gets the process
    killed at 120s. Staleness must be read from the watchdog's cache."""
    from oncofiles.server import health

    source = inspect.getsource(health)
    for forbidden in ("await db.", "db.execute", "list_patients", "get_last_successful_sync"):
        assert forbidden not in source, f"/health must not do DB work: found {forbidden!r}"


def test_readiness_exposes_hard_auth_detail_and_staleness():
    from oncofiles.server import readiness

    source = inspect.getsource(readiness)
    assert "hard_auth_failures" in source
    assert "sync_staleness" in source


# ── C1: a run that did nothing must not report success ────────────────────


def test_nightly_pipeline_raises_when_every_patient_failed():
    """The D1 lock. `_run_nightly_ai_pipeline` is a closure inside
    `_start_sync_scheduler`, so assert on its source: it must count attempts
    and failures and raise when they are equal and non-zero."""
    from oncofiles.server import _start_sync_scheduler

    source = inspect.getsource(_start_sync_scheduler)
    assert "sync_attempted" in source
    assert "sync_failed" in source
    assert "sync_attempted > 0 and sync_failed == sync_attempted" in source
    assert "last_run_summary" in source


def test_nightly_pipeline_counts_failure_on_every_continue_path():
    """Both bail-out branches (timeout and generic exception) must bump the
    failure counter — otherwise a timeout-only outage still reads clean."""
    from oncofiles.server import _start_sync_scheduler

    source = inspect.getsource(_start_sync_scheduler)
    # Anchor on the `async def`, not the bare name — the name also appears in
    # an unrelated comment earlier in the function.
    nightly = source.split("async def _run_nightly_ai_pipeline")[1].split(
        "async def _run_weekly_discovery_pipeline"
    )[0]
    assert nightly.count("sync_failed += 1") >= 2


# ── C3: staleness helper ──────────────────────────────────────────────────


async def test_get_last_successful_sync_ignores_failed_runs(db: Database):
    """The whole point: `get_sync_stats_summary.last_sync_at` is
    MAX(started_at) over ALL rows, so it stays fresh while every run fails.
    This helper must only see completed runs."""
    sid = await db.insert_sync_history(trigger="nightly", patient_id=ERIKA_UUID)
    await db.complete_sync_history(sid, status="failed", error_message="403 project deleted")

    assert await db.get_last_successful_sync(patient_id=ERIKA_UUID) is None

    ok = await db.insert_sync_history(trigger="nightly", patient_id=ERIKA_UUID)
    await db.complete_sync_history(ok, status="completed", duration_s=1.0)
    assert await db.get_last_successful_sync(patient_id=ERIKA_UUID) is not None


async def test_get_last_successful_sync_is_patient_scoped(db: Database):
    """#499 convention — another patient's success must not count as ours."""
    sid = await db.insert_sync_history(trigger="nightly", patient_id="someone-else")
    await db.complete_sync_history(sid, status="completed", duration_s=1.0)

    assert await db.get_last_successful_sync(patient_id=ERIKA_UUID) is None
    assert await db.get_last_successful_sync(patient_id="someone-else") is not None


async def test_get_last_successful_sync_requires_patient_id_keyword(db: Database):
    """Positional call must be a TypeError, per the #499 helper convention."""
    import pytest

    with pytest.raises(TypeError):
        await db.get_last_successful_sync(ERIKA_UUID)  # type: ignore[misc]


# ── C4: alerting ──────────────────────────────────────────────────────────


def test_pipeline_alert_is_deduped_and_reuses_existing_resend_env():
    """No new credentials, and a persistent outage must not mail hourly."""
    from oncofiles.server import _notify_admin_pipeline_alert

    source = inspect.getsource(_notify_admin_pipeline_alert)
    assert "RESEND_API_KEY" in source
    assert "NOTIFY_TO_EMAIL" in source
    assert "_pipeline_alert_sent" in source


# ── Google kill switch ────────────────────────────────────────────────────


async def test_create_patient_clients_returns_none_when_google_disabled(monkeypatch):
    """The single chokepoint for per-patient Google clients. Returning None
    here is what stops every Drive/Gmail/Calendar call downstream."""
    from oncofiles import server

    monkeypatch.setattr(server, "GOOGLE_INTEGRATION_ENABLED", False)
    assert await server._create_patient_clients(None, "any-pid") is None


def test_kill_switch_short_circuits_before_any_token_load():
    """Must bail before the OAuth token load / refresh — attempting a refresh
    against a dead client is exactly the 401 storm #552 produced."""
    from oncofiles.server import _create_patient_clients

    source = inspect.getsource(_create_patient_clients)
    guard = source.index("if not GOOGLE_INTEGRATION_ENABLED")
    assert guard < source.index("get_oauth_token")
    assert guard < source.index("refresh_access_token")


def test_service_account_fallback_is_gated_by_kill_switch():
    """The SA path is what threw 'Project #... has been deleted' nightly."""
    from oncofiles.server import lifespan

    source = inspect.getsource(lifespan)
    assert "if not gdrive and GOOGLE_INTEGRATION_ENABLED:" in source


def test_watchdog_does_not_page_during_an_intentional_pause():
    """Staleness is expected while the switch is off. Paging anyway is how the
    next real outage gets ignored."""
    from oncofiles.server import _start_sync_scheduler

    source = inspect.getsource(_start_sync_scheduler)
    assert "if stale and not GOOGLE_INTEGRATION_ENABLED:" in source


def test_health_marks_google_disabled():
    """A quiet pipeline must be distinguishable from a healthy one."""
    from oncofiles.server import health

    assert '"disabled"' in inspect.getsource(health)


def test_watchdog_is_registered_and_costs_nothing():
    from oncofiles.server import _start_sync_scheduler

    source = inspect.getsource(_start_sync_scheduler)
    assert 'id="sync_watchdog"' in source
    # Must run regardless of AI_NIGHTLY_ONLY — it is the always-on guard.
    housekeeping = source.split("Zero-cost housekeeping")[1]
    assert "sync_watchdog" in housekeeping
