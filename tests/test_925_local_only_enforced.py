"""#925: background work is local-only by construction, not by configuration.

Policy 2026-11-09: background processing uses the local model only; the
Anthropic cloud only when a user acts in Claude.ai. Before #925 nothing in the
background reached the cloud only because ``CLOUD_AI_ENABLED`` was off.

Locks:
1. Every scheduler job (``add_job`` in any module, discovered by AST) and every
   background task started in ``lifespan`` is registered through
   ``cloud_guard.local_only_task``; at runtime every job the scheduler holds
   carries the marker (all three registration modes).
2. ``local_only_task`` sets the zone for async targets, for lambdas returning
   a coroutine, and the zone follows ``asyncio.to_thread``.
3. The curated run raises ``CloudForbiddenError`` when anything inside it asks
   for the Anthropic client, also from its worker thread.
4. ``LOCAL_LLM_BASE_URL`` must be private: host check units, and ``llm.chat``
   never sends to a public host.
"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oncofiles import cloud_guard, config, scheduler_singleton
from oncofiles.cloud_guard import (
    LOCAL_ONLY_TASK_ATTR,
    CloudForbiddenError,
    cloud_ai_disabled_reason,
    local_only,
    local_only_job,
    local_only_task,
)
from oncofiles.curated import llm

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
BIG = "00000000-0000-4000-8000-00000000c925"  # invented patient


# ── 1a. AST: every registration goes through local_only_task ──────────────


def _is_local_only_task_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "local_only_task"
        and len(node.args) == 2
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.strip() != ""
    )


def _registrations() -> tuple[
    list[tuple[str, int, ast.AST | None]], list[tuple[str, int]], list[tuple[str, int]]
]:
    """(add_job targets, lifespan task calls, unwrapped ones) across src/."""
    jobs: list[tuple[str, int, ast.AST | None]] = []
    tasks: list[tuple[str, int]] = []
    unwrapped_tasks: list[tuple[str, int]] = []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job"
            ):
                target = node.args[0] if node.args else None
                for kw in node.keywords:
                    if kw.arg == "func":
                        target = kw.value
                jobs.append((rel, node.lineno, target))
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "lifespan"
            ):
                for call in ast.walk(node):
                    if (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in {"create_task", "ensure_future"}
                    ):
                        tasks.append((rel, call.lineno))
                        if not any(_is_local_only_task_call(n) for n in ast.walk(call)):
                            unwrapped_tasks.append((rel, call.lineno))
    return jobs, tasks, unwrapped_tasks


def test_every_scheduler_job_is_registered_local_only():
    jobs, _, _ = _registrations()
    assert len(jobs) >= 20, "discovery must see the real add_job calls"
    bad = [(rel, line) for rel, line, target in jobs if not _is_local_only_task_call(target)]
    assert not bad, f"add_job targets not wrapped in local_only_task (#925): {bad}"


def test_every_lifespan_background_task_is_local_only():
    _, tasks, unwrapped = _registrations()
    assert tasks, "discovery must see the curated loop task"
    assert not unwrapped, f"lifespan tasks outside local_only_task (#925): {unwrapped}"


def test_add_job_names_match_job_ids():
    """The zone is named after the job id, so a refusal names the job."""
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_job"
            ):
                ids = [kw.value.value for kw in node.keywords if kw.arg == "id"]
                zone_name = node.args[0].args[0].value
                assert ids, (path.name, node.lineno)
                assert zone_name == ids[0], (path.name, node.lineno)


# ── 1b. runtime: every job the scheduler holds carries the marker ─────────


@pytest.fixture
def _clean_registry():
    scheduler_singleton._reset_for_tests()
    yield
    sched = scheduler_singleton._scheduler
    if scheduler_singleton._scheduler_alive(sched):
        sched.shutdown(wait=False)
    scheduler_singleton._reset_for_tests()


@pytest.mark.parametrize(
    ("nightly_only", "run_on_startup", "integrity"),
    [(True, False, False), (True, True, True), (False, False, True)],
    ids=["nightly", "nightly+oneoff+integrity", "legacy"],
)
async def test_every_registered_job_runs_local_only(
    monkeypatch, gmail_calendar_on, _clean_registry, nightly_only, run_on_startup, integrity
):
    import oncofiles.server as server

    monkeypatch.setattr(server, "SYNC_ENABLED", True)
    monkeypatch.setattr(server, "AI_NIGHTLY_ONLY", nightly_only)
    monkeypatch.setattr(server, "AI_RUN_ON_STARTUP", run_on_startup)
    monkeypatch.setattr(server, "ENABLE_INTEGRITY_CHECK", integrity)
    sched, _ = server._start_sync_scheduler(
        MagicMock(), MagicMock(), None, "", MagicMock(), MagicMock(), primary_uuid=""
    )
    jobs = sched.get_jobs()
    assert jobs
    missing = [j.id for j in jobs if getattr(j.func, LOCAL_ONLY_TASK_ATTR, None) != j.id]
    assert not missing, f"jobs not in a local-only zone named after them: {missing}"


# ── 2. local_only_task semantics ──────────────────────────────────────────


async def test_local_only_task_zone_covers_async_target_and_worker_threads():
    seen: dict[str, object] = {}

    async def job(x, *, y):
        seen["loop"] = local_only_job()
        seen["thread"] = await asyncio.to_thread(local_only_job)
        seen["args"] = (x, y)
        return "done"

    wrapped = local_only_task("synthetic_job", job)
    assert asyncio.iscoroutinefunction(wrapped)
    assert getattr(wrapped, LOCAL_ONLY_TASK_ATTR) == "synthetic_job"
    assert await wrapped(1, y=2) == "done"
    assert seen == {"loop": "synthetic_job", "thread": "synthetic_job", "args": (1, 2)}
    assert local_only_job() is None, "the zone ends with the job"


async def test_local_only_task_awaits_a_lambda_returning_a_coroutine():
    seen = []

    async def job(trigger):
        seen.append((trigger, local_only_job()))

    wrapped = local_only_task("synthetic_startup", lambda: job("startup"))
    assert asyncio.iscoroutinefunction(wrapped), "APScheduler must await it, not thread it"
    await wrapped()
    assert seen == [("startup", "synthetic_startup")]


async def test_local_only_task_refuses_the_anthropic_client_even_with_cloud_on():
    from oncofiles import enhance

    assert cloud_ai_disabled_reason() is None  # conftest switches cloud AI on

    async def job():
        with pytest.raises(CloudForbiddenError):
            enhance._get_client()
        with pytest.raises(CloudForbiddenError):
            await asyncio.to_thread(enhance._get_client)
        # "skip if cloud is off" checks (Gmail/Calendar classification) skip here.
        assert cloud_guard.cloud_ai_enabled() is False
        assert "synthetic_cloud_job" in (cloud_ai_disabled_reason() or "")

    await local_only_task("synthetic_cloud_job", job)()
    assert cloud_ai_disabled_reason() is None


# ── 3. the curated run is a local-only zone ───────────────────────────────


async def test_curated_run_refuses_the_cloud_inside(db):
    from oncofiles import enhance
    from oncofiles.curated.runner import RunOptions, RunReport
    from oncofiles.tools import curated as tool

    seen: dict[str, object] = {}

    def fake_run(drive, folder_id, opts, *, extract_fn, propose_fn):
        seen["zone"] = local_only_job()
        try:
            enhance._get_client()
        except CloudForbiddenError as exc:
            seen["refused"] = type(exc).__name__
        return RunReport(run_id="r925", dry_run=True)

    with patch("oncofiles.curated.runner.run_curated", side_effect=fake_run):
        report = await tool._execute_locked(
            db, BIG, MagicMock(), "folder925", RunOptions(dry_run=True), trigger="test"
        )
    assert seen == {"zone": "curated run", "refused": "CloudForbiddenError"}
    assert report["status"] != "aborted"
    assert local_only_job() is None


async def test_curated_run_crash_from_a_cloud_call_is_reported_not_raised(db):
    from oncofiles import enhance
    from oncofiles.curated.runner import RunOptions
    from oncofiles.tools import curated as tool

    def fake_run(drive, folder_id, opts, *, extract_fn, propose_fn):
        enhance._get_client()  # raises inside the worker thread

    with patch("oncofiles.curated.runner.run_curated", side_effect=fake_run):
        report = await tool._execute_locked(
            db, BIG, MagicMock(), "folder925", RunOptions(dry_run=True), trigger="test"
        )
    assert report["status"] == "aborted"
    assert "CloudForbiddenError" in report["message"]


# ── 4. LOCAL_LLM_BASE_URL host check ──────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://host.lima.internal:1234/v1",  # production (container → host)
        "http://host.docker.internal:1234/v1",
        "http://localhost:1234/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:1234/v1",
        "http://10.20.30.40:1234/v1",
        "http://172.16.0.9/v1",
        "http://172.31.255.1/v1",
        "http://192.168.77.2:1234/v1",
        "http://[fd12:3456:789a::1]:1234/v1",
        "http://[::ffff:192.168.77.2]/v1",
        "http://studio.local:1234/v1",
        "http://model.lan/v1",
        "http://gpu.home.arpa/v1",
        "https://lmstudio.internal/v1",
        "http://llm.test/v1",
        "http://local.invalid/v1",
        "http://HOST.LIMA.INTERNAL:1234/v1",
    ],
)
def test_private_hosts_pass(monkeypatch, url):
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    assert llm.base_url_refusal(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://api.example-llm-provider.com/v1",
        "https://api.openai.com/v1",
        "http://8.8.8.8/v1",
        "http://172.32.0.1/v1",  # just outside 172.16/12
        "http://100.64.0.1/v1",  # shared address space, not RFC 1918
        "http://169.254.169.254/v1",  # link-local metadata endpoint
        "http://[2001:db8:feed:beef::42]/v1",  # documentation range, public
        "http://evil.local.example.com/v1",  # suffix must be the end of the name
        "http://internal/v1",
        "ftp://192.168.1.2/v1",
        "http:///v1",
        "not a url",
    ],
)
def test_public_or_malformed_hosts_are_refused(monkeypatch, url):
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    assert llm.base_url_refusal(url)


def test_refusal_names_the_host_never_credentials_or_path(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    reason = llm.base_url_refusal("https://user:synthetic-secret@api.example.com/v1/x?key=abc")
    assert "api.example.com" in reason
    assert "synthetic-secret" not in reason and "key=abc" not in reason and "/v1" not in reason


def test_allowlist_env_admits_an_explicit_host(monkeypatch):
    url = "https://gpu-box.example.net:8443/v1"
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    assert llm.base_url_refusal(url)
    monkeypatch.setattr(
        config, "LOCAL_LLM_ALLOWED_HOSTS", " other.example.org , GPU-BOX.example.net "
    )
    assert llm.base_url_refusal(url) is None


def test_chat_never_sends_to_a_public_host_and_logs_once(monkeypatch, caplog):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "https://api.example-llm-provider.com/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "m")
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    monkeypatch.setattr(llm, "_refusals_logged", set())
    client = MagicMock()
    with caplog.at_level(logging.ERROR, logger="oncofiles.curated.llm"):
        for _ in range(3):
            with pytest.raises(llm.LocalLLMHostRefusedError) as exc:
                llm.chat("system", "synthetic text", client=client)
            assert isinstance(exc.value, llm.LocalLLMError)
    client.post.assert_not_called()
    lines = [r for r in caplog.records if "local LLM refused" in r.getMessage()]
    assert len(lines) == 1, "logged once per process"


def test_chat_still_posts_to_the_production_host(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://host.lima.internal:1234/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "m")
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    client = MagicMock()
    client.post.return_value = resp
    assert llm.chat("system", "synthetic text", client=client) == "ok"
    assert client.post.call_args[0][0] == "http://host.lima.internal:1234/v1/chat/completions"


async def test_curated_run_refuses_a_public_model_host_before_any_run_row(db, monkeypatch):
    from oncofiles.tools import curated as tool

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "https://api.example-llm-provider.com/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "m")
    monkeypatch.setattr(config, "LOCAL_LLM_ALLOWED_HOSTS", "")
    err, ctx = await tool._prepare(db, BIG, dry_run=True, max_docs=None, max_actions=40)
    assert ctx is None and "local LLM refused" in err["error"]


def test_local_only_restores_previous_zone():
    with local_only("outer"):
        with local_only("inner"):
            assert local_only_job() == "inner"
        assert local_only_job() == "outer"
    assert local_only_job() is None
