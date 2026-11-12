"""#708: the nightly AI phases must not read as healthy when they did nothing.

With cloud AI off (``CLOUD_AI_ENABLED`` off or no key) every Anthropic call
raises ``CloudAIDisabledError``. ``sync.enhance_documents`` swallows that per
document into ``{"errors": N}``, so before #708 a night where every document
failed still refreshed ``last_ok`` — the #552 bug class again.

Now: cloud AI off → the AI phases are skipped once per run (one INFO line, a
``enhance_skipped`` marker, no per-document attempt) and ``/health`` says
``cloud_ai: "disabled"``; cloud AI on → per-document failures are counted and
the job raises when every attempted document failed. All ids are synthetic.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

PIDS = ("00000000-0000-4000-8000-0000000007a1", "00000000-0000-4000-8000-0000000007a2")


def _stats(**counts: int) -> dict:
    """The dict shape enhance_documents / extract_all_metadata return."""
    return {
        "processed": counts.get("ok", 0),
        "skipped": counts.get("no_text", 0),
        "errors": counts.get("failed", 0),
    }


def _nightly(monkeypatch, *, enhance=None, metadata=None):
    """Build the scheduler and return (nightly job function, job_tracker, mocks).

    Phase 1 (Drive sync) succeeds for every patient; the AI phases are mocks
    so a test can see whether they were reached and what they reported.
    """
    import oncofiles.budget as budget
    import oncofiles.server as server
    import oncofiles.sync as sync_mod

    sync_mock = AsyncMock(return_value={"new": 0})
    enhance_mock = AsyncMock(return_value=enhance or _stats())
    metadata_mock = AsyncMock(return_value=metadata or _stats())
    monkeypatch.setattr(sync_mod, "sync", sync_mock)
    monkeypatch.setattr(sync_mod, "enhance_documents", enhance_mock)
    monkeypatch.setattr(sync_mod, "extract_all_metadata", metadata_mock)
    monkeypatch.setattr(
        budget, "check_patient_budget", AsyncMock(return_value=SimpleNamespace(within_budget=True))
    )
    monkeypatch.setattr(
        server,
        "_create_patient_clients",
        AsyncMock(return_value=(MagicMock(), None, None, "synthetic-folder")),
    )
    monkeypatch.setattr(server, "is_memory_pressure", lambda *_a, **_k: False)
    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_NIGHTLY_ONLY", True)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", False)
    for pid in PIDS:
        server._curated_patients.discard(pid)
        server._folder_404_counts.pop(pid, None)

    db = MagicMock()
    db.list_patients = AsyncMock(return_value=[SimpleNamespace(patient_id=p) for p in PIDS])
    scheduler, tracker = server._start_sync_scheduler(db, MagicMock(), None, "", primary_uuid="")
    try:
        func = scheduler.get_job("nightly_ai_pipeline").func
    finally:
        scheduler.shutdown(wait=False)
    return func, tracker, SimpleNamespace(sync=sync_mock, enhance=enhance_mock, md=metadata_mock)


def _summary(tracker) -> dict:
    return tracker["nightly_ai_pipeline"]["last_run_summary"]


# ── cloud AI off: a deliberate skip, once per run ──────────────────────────


@pytest.mark.parametrize(
    ("flag", "key"),
    [(False, "test-key-not-real"), (True, ""), (False, "")],
    ids=["flag-off", "no-key", "both-off"],
)
async def test_cloud_ai_off_skips_ai_phases_once_without_per_doc_calls(
    monkeypatch, caplog, flag, key
):
    monkeypatch.setattr("oncofiles.config.CLOUD_AI_ENABLED", flag)
    monkeypatch.setattr("oncofiles.config.ANTHROPIC_API_KEY", key)
    # Documents are pending: had the phases run, every one would have failed.
    func, tracker, m = _nightly(monkeypatch, enhance=_stats(failed=4))

    with caplog.at_level(logging.INFO, logger="oncofiles.server"):
        await func()  # must not raise: a skip is not a failure

    m.enhance.assert_not_awaited()
    m.md.assert_not_awaited()
    assert m.sync.await_count == len(PIDS)  # Phase 1 still runs (no AI cost)
    skip_lines = [r for r in caplog.records if "AI phases skipped" in r.getMessage()]
    assert len(skip_lines) == 1, "logged once per run, not once per patient/document"
    assert skip_lines[0].levelno == logging.INFO
    summary = _summary(tracker)
    assert summary["enhance_skipped"] == "cloud_ai_disabled"
    assert summary["enhance_attempted"] == 0
    assert summary["enhance_failed"] == 0


async def test_health_reports_cloud_ai_disabled(monkeypatch):
    from oncofiles import server

    monkeypatch.setattr(server, "get_rss_mb", lambda: 100.0)
    monkeypatch.setattr("oncofiles.config.CLOUD_AI_ENABLED", False)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    body = json.loads((await server.health(request)).body)
    assert body["cloud_ai"] == "disabled"

    monkeypatch.setattr("oncofiles.config.CLOUD_AI_ENABLED", True)
    monkeypatch.setattr("oncofiles.config.ANTHROPIC_API_KEY", "")
    body = json.loads((await server.health(request)).body)
    assert body["cloud_ai"] == "disabled"


async def test_health_reports_cloud_ai_enabled(monkeypatch):
    from oncofiles import server

    monkeypatch.setattr(server, "get_rss_mb", lambda: 100.0)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))
    body = json.loads((await server.health(request)).body)
    assert body["cloud_ai"] == "enabled"


# ── cloud AI on: per-document failures count (#552 accounting) ─────────────


async def test_every_document_failing_fails_the_job(monkeypatch):
    func, tracker, m = _nightly(monkeypatch, enhance=_stats(no_text=1, failed=3))

    with pytest.raises(RuntimeError, match="document enhancement"):
        await func()

    assert m.enhance.await_count == len(PIDS)
    summary = _summary(tracker)
    assert summary["enhance_attempted"] == 3 * len(PIDS)
    assert summary["enhance_failed"] == 3 * len(PIDS)
    assert summary["enhance_skipped"] is None


async def test_ai_phase_raising_for_every_patient_fails_the_job(monkeypatch):
    func, _tracker, m = _nightly(monkeypatch)
    m.enhance.side_effect = TimeoutError()
    m.md.side_effect = RuntimeError("synthetic metadata failure")

    with pytest.raises(RuntimeError, match="document enhancement"):
        await func()


async def test_partial_failure_does_not_raise_and_logs_counts(monkeypatch, caplog):
    func, tracker, _m = _nightly(
        monkeypatch,
        enhance=_stats(ok=2, failed=1),
        metadata=_stats(ok=1),
    )

    with caplog.at_level(logging.INFO, logger="oncofiles.server"):
        await func()

    summary = _summary(tracker)
    assert summary["enhance_attempted"] == 4 * len(PIDS)
    assert summary["enhance_failed"] == 1 * len(PIDS)
    text = caplog.text
    assert f"ai_docs_failed={len(PIDS)}/{4 * len(PIDS)}" in text
    assert f"{len(PIDS)} of {4 * len(PIDS)} document enhancement(s) failed" in text


async def test_nothing_pending_stays_a_success(monkeypatch):
    """0 attempted is a quiet night, not a failure."""
    func, tracker, m = _nightly(monkeypatch)

    await func()

    assert m.enhance.await_count == len(PIDS)
    summary = _summary(tracker)
    assert summary["enhance_attempted"] == 0
    assert summary["enhance_skipped"] is None
