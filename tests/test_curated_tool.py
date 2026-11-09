"""Curated pipeline operations: MCP tools, preview gate, persistence, nightly, admin API (#573)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config
from oncofiles.curated.runner import RunReport
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID


@pytest.fixture(autouse=True)
def _no_index():
    """The read-model index (#603) has its own tests; here Drive is a MagicMock."""
    with patch.object(tool, "_index_quietly", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def _reset():
    tool._runs.clear()
    tool._extract_cache.clear()
    tool._propose_cache.clear()
    yield
    tool._runs.clear()


@pytest.fixture
def llm_on(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "m")


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


def _report(status="ok", dry_run=True, docs=None, not_processed=None):
    return RunReport(
        run_id="r1",
        dry_run=dry_run,
        status=status,
        documents=docs if docs is not None else [{"source": "a.pdf", "result": "filed"}],
        not_processed=not_processed or [],
    )


def _env(report_or_fn, reason="is a curated folder"):
    gdrive = MagicMock()
    clients = (gdrive, None, None, "folder1")
    run = (
        patch("oncofiles.curated.runner.run_curated", side_effect=report_or_fn)
        if callable(report_or_fn)
        else patch("oncofiles.curated.runner.run_curated", return_value=report_or_fn)
    )
    return gdrive, (
        patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=clients)),
        patch("oncofiles.curated_folder.curated_reason", return_value=reason),
        run,
    )


async def _wait_done(db):
    for _ in range(100):
        status = await tool.run_status(db, ERIKA_UUID)
        if status["state"] != "running":
            return status
        await asyncio.sleep(0.01)
    raise AssertionError("run never finished")


# ── refusals ─────────────────────────────────────────────────────────


async def test_requires_local_llm(db, monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "")
    out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "local LLM not configured" in out["error"]


async def test_requires_google(db, llm_on):
    with patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=None)):
        out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "not connected" in out["error"]


@pytest.mark.parametrize("reason", [None, "lies inside curated folder X", "contains curated X"])
async def test_refuses_non_root_folders(db, llm_on, reason):
    _, patches = _env(_report(), reason=reason)
    with patches[0], patches[1], patches[2]:
        out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "not a curated root" in out["error"]


async def test_one_run_at_a_time(db, llm_on):
    tool._runs[ERIKA_UUID] = {"state": "running"}
    out = json.loads(await tool.curated_folder_run(_ctx(db)))
    assert "already in progress" in out["error"]


# ── background run + persistence ─────────────────────────────────────


async def test_dry_run_runs_in_background_and_is_persisted(db, llm_on):
    gdrive, patches = _env(_report())
    with patches[0], patches[1], patches[2] as run:
        out = json.loads(await tool.curated_folder_run(_ctx(db)))
        assert out == {"status": "started", "dry_run": True, "max_docs": 20}
        status = await _wait_done(db)
    assert status["state"] == "done" and status["run"]["report"]["run_id"] == "r1"
    assert status["run"]["dry_run"] == 1 and status["run"]["trigger"] == "mcp"
    assert run.call_args.args[2].dry_run is True
    gdrive.contract_writes.assert_not_called()  # a dry run never lifts the block
    gdrive.dedicated_copy.return_value.contract_writes.assert_not_called()
    gdrive.dedicated_copy.return_value.close.assert_called_once()  # own connection, closed
    # MCP status reads the persisted run after the in-memory state is gone
    mcp_status = json.loads(await tool.curated_folder_status(_ctx(db)))
    assert mcp_status["run"]["status"] == "ok"


# ── preview gate for real runs ───────────────────────────────────────


async def test_real_run_refused_without_preview(db, llm_on):
    out = json.loads(await tool.curated_folder_run(_ctx(db), dry_run=False))
    assert "real run refused" in out["error"] and "no preview" in out["error"]


async def test_real_run_refused_after_failed_preview(db, llm_on):
    rid = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="t")
    await db.finish_curated_run(
        rid, patient_id=ERIKA_UUID, status="aborted", message="x", report={}
    )
    reason = await tool.preview_gate(db, ERIKA_UUID)
    assert reason and "aborted" in reason


async def test_real_run_refused_after_stale_preview(db, llm_on):
    rid = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="t")
    await db.finish_curated_run(rid, patient_id=ERIKA_UUID, status="ok", message="", report={})
    later = datetime.now(UTC) + timedelta(hours=25)
    assert "older than 24 h" in await tool.preview_gate(db, ERIKA_UUID, now=later)


async def test_real_run_allowed_after_clean_preview(db, llm_on):
    gdrive, patches = _env(_report())
    with patches[0], patches[1], patches[2]:
        await tool.curated_folder_run(_ctx(db))
        await _wait_done(db)
        out = json.loads(await tool.curated_folder_run(_ctx(db), dry_run=False))
        assert out["status"] == "started" and out["dry_run"] is False
        await _wait_done(db)
    gdrive.contract_writes.assert_not_called()  # the shared client is never unblocked
    gdrive.dedicated_copy.return_value.contract_writes.assert_called_once()


# ── nightly ──────────────────────────────────────────────────────────


async def test_nightly_stops_when_preview_is_not_clean(db, llm_on):
    _, patches = _env(_report(status="aborted"))
    with patches[0], patches[1], patches[2] as run:
        reports = await tool.nightly_for_patient(db, ERIKA_UUID, max_real_runs=5)
    assert len(reports) == 1 and run.call_count == 1


async def test_nightly_repeats_real_runs_until_inbox_done(db, llm_on):
    seq = [
        _report(),  # preview
        _report(dry_run=False, not_processed=["x", "y"]),
        _report(dry_run=False, not_processed=["y"]),
        _report(dry_run=False, not_processed=[]),
    ]
    _, patches = _env(lambda *a, **k: seq.pop(0))
    with patches[0], patches[1], patches[2] as run:
        reports = await tool.nightly_for_patient(db, ERIKA_UUID, max_real_runs=5)
    assert len(reports) == 4 and run.call_count == 4
    assert [r["dry_run"] for r in reports] == [True, False, False, False]


async def test_nightly_respects_max_real_runs(db, llm_on):
    _, patches = _env(lambda *a, **k: _report(not_processed=["more"]))
    with patches[0], patches[1], patches[2] as run:
        await tool.nightly_for_patient(db, ERIKA_UUID, max_real_runs=2)
    assert run.call_count == 3  # 1 preview + 2 real


async def test_nightly_switch_is_persisted(db):
    assert await db.list_curated_nightly_patients() == []
    await db.set_curated_nightly(patient_id=ERIKA_UUID, enabled=True, updated_by="t")
    assert await db.list_curated_nightly_patients() == [ERIKA_UUID]
    await db.set_curated_nightly(patient_id=ERIKA_UUID, enabled=False, updated_by="t")
    assert await db.get_curated_nightly(patient_id=ERIKA_UUID) is False


# ── cache ────────────────────────────────────────────────────────────


def test_extraction_is_cached_by_content_hash():
    calls = []

    def fake(name, content, mime_type="", *, min_text_chars=100):
        from oncofiles.curated.extract import Extraction

        calls.append(name)
        return Extraction("t", "vision", 1, "m")

    with patch("oncofiles.curated.extract.extract", side_effect=fake):
        tool.cached_extract("a.pdf", b"same bytes")
        tool.cached_extract("renamed.pdf", b"same bytes")
        tool.cached_extract("b.pdf", b"other bytes")
    assert calls == ["a.pdf", "b.pdf"]


def test_failed_extraction_is_not_cached():
    from oncofiles.curated.extract import Extraction

    with patch(
        "oncofiles.curated.extract.extract",
        return_value=Extraction("", "none", 0, "m", error="local LLM unreachable"),
    ) as ex:
        tool.cached_extract("a.pdf", b"x")
        tool.cached_extract("a.pdf", b"x")
    assert ex.call_count == 2


# ── admin API ────────────────────────────────────────────────────────


def _admin_request(db, *, email, body=None, query=None):
    req = MagicMock()
    req.app.state.fastmcp_server._lifespan_result = {"db": db}
    req.json = AsyncMock(return_value=body or {})
    req.query_params = query or {}
    return req, email


async def _call(endpoint, req, email, *, admin):
    from oncofiles import server

    with (
        patch.object(server, "_check_bearer", return_value=JSONResponse_401()),
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        return await endpoint(req)


def JSONResponse_401():  # noqa: N802 — a non-None value means "bearer check failed"
    return object()


async def test_admin_api_refuses_non_admin(db):
    from oncofiles import server

    req, email = _admin_request(db, email="caregiver@example.com")
    resp = await _call(server.api_admin_curated_overview, req, email, admin=False)
    assert resp.status_code == 403


async def test_admin_overview_and_nightly_toggle(db):
    from oncofiles import server

    req, email = _admin_request(
        db, email="admin@example.com", body={"patient_id": ERIKA_UUID, "enabled": True}
    )
    resp = await _call(server.api_admin_curated_nightly, req, email, admin=True)
    assert resp.status_code == 200
    req, email = _admin_request(db, email="admin@example.com")
    resp = await _call(server.api_admin_curated_overview, req, email, admin=True)
    data = json.loads(resp.body)
    card = next(c for c in data["patients"] if c["patient_id"] == ERIKA_UUID)
    assert card["nightly_enabled"] is True
    assert "no preview" in card["real_run_blocked"]


async def test_admin_real_run_without_preview_is_409(db, llm_on):
    from oncofiles import server

    req, email = _admin_request(
        db, email="admin@example.com", body={"patient_id": ERIKA_UUID, "dry_run": False}
    )
    resp = await _call(server.api_admin_curated_run, req, email, admin=True)
    assert resp.status_code == 409 and "real run refused" in json.loads(resp.body)["error"]


# ── hourly mode ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected", [("1", 1), ("23", 23), ("every", None), ("Hourly", None), ("*", None)]
)
def test_nightly_hour_setting(monkeypatch, raw, expected):
    monkeypatch.setenv("CURATED_NIGHTLY_HOUR", raw)
    assert tool.nightly_settings()[1] == expected


async def test_hourly_mode_reuses_a_fresh_clean_preview(db, llm_on):
    rid = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="t")
    await db.finish_curated_run(rid, patient_id=ERIKA_UUID, status="ok", message="", report={})
    _, patches = _env(lambda *a, **k: _report(dry_run=False, not_processed=[]))
    with patches[0], patches[1], patches[2] as run:
        reports = await tool.nightly_for_patient(
            db, ERIKA_UUID, max_real_runs=5, reuse_preview=True
        )
    assert run.call_count == 1 and [r["dry_run"] for r in reports] == [False]
    assert run.call_args.args[2].dry_run is False


async def test_hourly_mode_previews_when_no_fresh_preview(db, llm_on):
    seq = [_report(), _report(dry_run=False, not_processed=[])]
    _, patches = _env(lambda *a, **k: seq.pop(0))
    with patches[0], patches[1], patches[2] as run:
        reports = await tool.nightly_for_patient(
            db, ERIKA_UUID, max_real_runs=5, reuse_preview=True
        )
    assert run.call_count == 2 and [r["dry_run"] for r in reports] == [True, False]


async def test_startup_marks_runs_left_running_as_interrupted(db):
    rid = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=False, trigger="nightly")
    done = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="t")
    await db.finish_curated_run(done, patient_id=ERIKA_UUID, status="ok", message="", report={})
    assert await db.mark_interrupted_curated_runs() == 1
    row = await db.get_curated_run(rid, patient_id=ERIKA_UUID)
    assert row["status"] == "interrupted"
    assert (await db.get_curated_run(done, patient_id=ERIKA_UUID))["status"] == "ok"


async def test_nightly_loop_marks_interrupted_then_waits_before_first_run(db, monkeypatch):
    monkeypatch.setenv("CURATED_NIGHTLY_ENABLED", "true")
    monkeypatch.setenv("CURATED_NIGHTLY_HOUR", "every")
    await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=False, trigger="nightly")
    calls = []

    async def fake_nightly(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(tool, "nightly_for_patient", fake_nightly)
    task = asyncio.create_task(tool.curated_nightly_loop(db, poll_s=3600, startup_grace_s=3600))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == []  # still in the startup grace period
    rows = await db.list_curated_runs(patient_id=ERIKA_UUID)
    assert rows[0]["status"] == "interrupted"


# ── continuous mode ──────────────────────────────────────────────────


def _probe(pending=None, age=600.0, sig="s1", canary=False):
    from oncofiles.curated.runner import Probe

    return Probe(signature=sig, pending=pending or {}, newest_age_s=age, canary=canary)


@pytest.fixture
def cont(monkeypatch):
    """Fake probe + sequence; returns (probes queue, sequence calls, reports queue)."""
    tool._watch.clear()
    probes, calls, reports = [], [], []

    async def fake_probe(db, pid, idle):
        calls.append(("probe", idle))
        return probes.pop(0)

    async def fake_seq(db, pid, **kw):
        calls.append(("run", kw))
        return reports.pop(0)

    monkeypatch.setattr(tool, "probe_patient", fake_probe)
    monkeypatch.setattr(tool, "nightly_for_patient", fake_seq)
    yield probes, calls, reports
    tool._watch.clear()


def _ok(result="filed"):
    return {"dry_run": False, "status": "ok", "documents": [{"result": result}], "actions": []}


async def _tick(db):
    return await tool.continuous_tick(db, ERIKA_UUID, settle_s=120, chunk=5, max_runs=5)


async def test_continuous_idle_remembers_the_signature(db, cont):
    probes, calls, _ = cont
    probes += [_probe(sig="idle"), _probe(sig="idle")]
    assert await _tick(db) is None
    assert await _tick(db) is None
    assert calls == [("probe", None), ("probe", "idle")]  # second probe skips sidecar reads


async def test_continuous_waits_for_uploads_to_settle(db, cont):
    probes, calls, reports = cont
    probes += [_probe({"f1": "inbox/a.pdf"}, age=30), _probe({"f1": "inbox/a.pdf"}, age=200)]
    reports.append([_ok()])
    assert await _tick(db) is None
    assert [c[0] for c in calls] == ["probe"]
    assert await _tick(db) == [_ok()]
    run_kw = calls[-1][1]
    assert run_kw["trigger"] == "auto" and run_kw["max_docs"] == 5 and run_kw["reuse_preview"]


async def test_continuous_parks_work_a_sequence_could_not_move(db, cont):
    probes, calls, reports = cont
    stuck = {"f1": "(root)/a.pdf"}
    probes += [_probe(stuck), _probe(stuck, sig="s2"), _probe({**stuck, "f2": "inbox/b.pdf"})]
    reports += [[_ok("skipped")], [_ok()]]
    await _tick(db)  # runs, nothing moves → f1 parked
    assert await _tick(db) is None  # only parked work → no run
    assert await _tick(db) is not None  # a new file arrives → runs again
    assert [c[0] for c in calls].count("run") == 2


async def test_continuous_backs_off_after_a_failed_sequence(db, cont):
    probes, calls, reports = cont
    probes.append(_probe({"f1": "inbox/a.pdf"}))
    reports.append([{"dry_run": False, "status": "aborted", "documents": []}])
    await _tick(db)
    assert tool._watch[ERIKA_UUID].failures == 1
    assert await _tick(db) is None  # inside the backoff: no probe at all
    assert [c[0] for c in calls] == ["probe", "run"]


async def test_continuous_runs_to_recover_a_leftover_canary(db, cont):
    probes, calls, reports = cont
    probes.append(_probe({}, canary=True))
    reports.append([_ok()])
    assert await _tick(db) is not None


def test_continuous_settings_defaults(monkeypatch):
    for k in ("CURATED_CONTINUOUS", "CURATED_POLL_S", "CURATED_SETTLE_S", "CURATED_CHUNK_DOCS"):
        monkeypatch.delenv(k, raising=False)
    assert tool.continuous_settings() == (True, 120, 120, 5)
    monkeypatch.setenv("CURATED_CONTINUOUS", "false")
    monkeypatch.setenv("CURATED_POLL_S", "5")
    assert tool.continuous_settings()[:2] == (False, 30)  # poll floor 30 s


def test_run_active_reflects_running_runs():
    tool._runs["x"] = {"state": "running"}
    try:
        assert tool.run_active()
    finally:
        tool._runs.pop("x")
    assert not tool.run_active()


async def test_text_review_memory_is_seeded_from_earlier_real_runs(db):
    tool._text_reviews.clear()
    report = {
        "documents": [
            {
                "source": "n.txt",
                "result": "review_needed",
                "text_source": {"id": "f9", "modified": "m"},
            },
            {"source": "x.pdf", "result": "filed"},
        ]
    }
    rid = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=False, trigger="auto")
    await db.finish_curated_run(rid, patient_id=ERIKA_UUID, status="ok", message="", report=report)
    try:
        assert await tool.reviewed_text_sources(db, ERIKA_UUID) == frozenset({("f9", "m")})
    finally:
        tool._text_reviews.clear()


# ── heartbeat and loop detection ──


def test_loop_flag_after_repeated_empty_runs_over_the_same_documents():
    tool._empty_streak.clear()
    empty = {"actions": [], "documents": [{"source": "a.txt"}, {"source": "b.md"}]}
    for _ in range(tool.LOOP_ALERT_RUNS - 1):
        tool._note_run_outcome("p1", empty)
    assert not tool.heartbeat()["curated_loop_suspected"]
    tool._note_run_outcome("p1", empty)
    assert tool.heartbeat()["curated_loop_suspected"]
    assert tool.loop_suspected_patients() == ["p1"]
    # a run that moves something clears it
    tool._note_run_outcome("p1", {"actions": [{"type": "move"}], "documents": []})
    assert not tool.heartbeat()["curated_loop_suspected"]
    tool._empty_streak.clear()


def test_empty_runs_over_different_documents_are_not_a_loop():
    tool._empty_streak.clear()
    for i in range(tool.LOOP_ALERT_RUNS + 2):
        tool._note_run_outcome("p1", {"actions": [], "documents": [{"source": f"new{i}.pdf"}]})
    assert not tool.loop_suspected_patients()
    tool._empty_streak.clear()


def test_public_heartbeat_has_only_an_age_and_a_boolean(monkeypatch):
    tool._empty_streak.clear()
    monkeypatch.setattr(tool, "_last_probe_at", None)
    assert tool.heartbeat() == {"curated_last_probe_age_s": None, "curated_loop_suspected": False}
    monkeypatch.setattr(tool, "_last_probe_at", tool.time.monotonic() - 42)
    hb = tool.heartbeat()
    assert set(hb) == {"curated_last_probe_age_s", "curated_loop_suspected"}
    assert 41 <= hb["curated_last_probe_age_s"] <= 44


async def test_index_runs_after_a_real_run_once_it_is_no_longer_active(db, llm_on):
    seen = []

    async def spy(_db, pid):
        seen.append(tool.run_active())

    gdrive, patches = _env(_report(dry_run=False))
    await db.finish_curated_run(
        await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="mcp"),
        patient_id=ERIKA_UUID,
        status="ok",
        message="",
        report={"status": "ok"},
    )
    with patches[0], patches[1], patches[2], patch.object(tool, "_index_quietly", spy):
        await tool.execute_run(db, ERIKA_UUID, dry_run=False, trigger="mcp")
    assert seen == [False]  # indexed exactly once, and not counted as an active run
