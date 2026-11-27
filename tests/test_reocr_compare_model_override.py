"""Dry re-OCR compare: per-job ``model`` override for benching an OCR model.

Infra benches a second loaded LM Studio vision model through the EXACT
production OCR path (orientation question, pass 1, pass-2 halves, the
near-empty retry, reconcile) against today's sidecars. The override is
validated and checked ``loaded`` like the scorecards' (``llm.model_override_error``
+ ``llm.model_refusal``) before any job starts, threaded explicitly per call,
and never reaches a write path. Without it every request body is unchanged.

The fake LM Studio is an ``httpx.MockTransport`` (no socket). Synthetic data
only (invented values, ids and model names, dates 2027+).
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import sys
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import llm, llm_extract_call, orientation, reocr, reocr_child
from oncofiles.curated import reocr_compare as rc
from oncofiles.curated.reocr import Reread
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID
from tests.test_reocr_compare import (  # noqa: F401
    ONE_DIGIT,
    PAGE,
    SIDECAR,
    _Drive,
    _row,
    _seed,
    env,
)

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
URL = "http://llm.test/v1"
PROD_MODEL = "mock-model"
BENCH_MODEL = "google/gemma-9-27b-vision@4bit"  # invented id
UNLOADED = "google/gemma-9-90b-vision"  # invented: downloaded, not loaded
_RealClient = httpx.Client
_THIN = "ok"  # a near-empty first answer: the #806 retry asks again


class FakeLMStudio:
    """Native ``GET /api/v0/models`` (``BENCH_MODEL`` and the production model
    loaded, ``UNLOADED`` not); ``POST /chat/completions`` records the body.
    The orientation question gets an upright answer; a transcription call
    gets a near-empty answer that spent its tokens on reasoning, unless it is
    the thinking-off retry, which gets the page."""

    def __init__(self):
        self.bodies: list[dict] = []
        self.model_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v0/models":
            self.model_requests += 1
            data = [{"id": m, "type": "vlm", "state": "loaded"} for m in (PROD_MODEL, BENCH_MODEL)]
            data.append({"id": UNLOADED, "type": "vlm", "state": "not-loaded"})
            return httpx.Response(200, json={"object": "list", "data": data})
        body = json.loads(request.content)
        self.bodies.append(body)
        system = body["messages"][0]["content"]
        retry = (body.get("chat_template_kwargs") or {}).get("enable_thinking") is False
        if system.startswith(orientation.ORIENTATION_PROMPT[:40]):
            content = '{"orientation": "upright", "confident": true}'
            usage = {"prompt_tokens": 50, "completion_tokens": 9}
        elif retry:
            content = PAGE
            usage = {"prompt_tokens": 900, "completion_tokens": 300}
        else:
            content = _THIN
            usage = {"prompt_tokens": 900, "completion_tokens": 400,
                     "completion_tokens_details": {"reasoning_tokens": 398}}  # fmt: skip
        return httpx.Response(200, json={
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": usage,
        })  # fmt: skip

    def client(self, **_kw) -> httpx.Client:
        return _RealClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def lmstudio(monkeypatch):
    fake = FakeLMStudio()
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", PROD_MODEL)
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "k-invented")
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    monkeypatch.setattr(config, "OCR_ORIENTATION", "always")  # every page asks
    monkeypatch.setattr(llm.httpx, "Client", fake.client)
    return fake


def _inked_pdf() -> bytes:
    """One page with ink (a drawn block, no text layer): not a blank back side,
    so the orientation question and the near-empty retry both happen."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    block = page.rect * 0.5  # the top-left quarter of the page, inked
    black = (0.0,) * 3
    page.draw_rect(block, color=black, fill=black)
    data = doc.tobytes()
    doc.close()
    return data


def _read(model=None) -> Reread:
    kw = {"model": model} if model is not None else {}
    return reocr.reread("20270521_Synth_501.pdf", _inked_pdf(), "application/pdf",
                        passes=2, doc_ref=501, purpose="reocr", **kw)  # fmt: skip


def _kinds(bodies: list[dict]) -> list[str]:
    out = []
    for b in bodies:
        system = b["messages"][0]["content"]
        if system.startswith(orientation.ORIENTATION_PROMPT[:40]):
            out.append("orientation")
        elif (b.get("chat_template_kwargs") or {}).get("enable_thinking") is False:
            out.append("retry")
        else:
            out.append("transcribe")
    return out


# ── the read: every call carries the override, the default is unchanged ──


def test_without_a_model_every_request_body_is_the_production_one(lmstudio):
    rr = _read()
    kinds = _kinds(lmstudio.bodies)
    # orientation, pass 1 + its retry, two pass-2 halves + their retries
    assert kinds == ["orientation", "transcribe", "retry", *["transcribe", "retry"] * 2]
    allowed = {"model", "temperature", "max_tokens", "reasoning_effort", "messages",
               "chat_template_kwargs"}  # fmt: skip
    for body in lmstudio.bodies:
        assert body["model"] == PROD_MODEL
        assert set(body) <= allowed
    assert rr.engine == PROD_MODEL
    assert rr.error is None and rr.method == "vision2"
    assert all("model" not in rec for rec in rr.llm_passes)  # records unchanged too


def test_with_a_model_every_call_of_the_read_carries_it_and_nothing_else_changes(lmstudio):
    base = _read()
    base_bodies, lmstudio.bodies = lmstudio.bodies, []
    rr = _read(model=BENCH_MODEL)
    assert _kinds(lmstudio.bodies) == _kinds(base_bodies)
    for body, before in zip(lmstudio.bodies, base_bodies, strict=True):
        assert body["model"] == BENCH_MODEL
        assert {k: v for k, v in body.items() if k != "model"} == {
            k: v for k, v in before.items() if k != "model"
        }
    assert config.LOCAL_LLM_MODEL == PROD_MODEL  # nothing global changed
    assert rr.engine == BENCH_MODEL and base.engine == PROD_MODEL
    assert rr.pages == base.pages
    assert {rec.get("model") for rec in rr.llm_passes} == {BENCH_MODEL}
    assert any(rec.get("kind") == "orientation" for rec in rr.llm_passes)
    assert any(rec.get("retry") for rec in rr.llm_passes)
    assert any(rec.get("tile") == 2 for rec in rr.llm_passes)


def test_a_probe_reading_of_an_unsure_page_carries_the_model_too(lmstudio, monkeypatch):
    """An unsure direction is settled by reading both ways; those probe
    readings go through the same call."""
    monkeypatch.setattr(orientation, "parse_orientation",
                        lambda answer: orientation.Guess(None, False))  # fmt: skip
    rr = _read(model=BENCH_MODEL)
    assert any(rec.get("orientation_probe") for rec in rr.llm_passes)
    assert {b["model"] for b in lmstudio.bodies} == {BENCH_MODEL}


def test_the_child_process_header_carries_the_model_only_when_given(monkeypatch):
    sent: list[bytes] = []

    class _Proc:
        returncode = 0
        stderr = b""
        stdout = json.dumps({"pages": ["x"], "method": "vision2", "engine": "e"}).encode()

    def run(cmd, *, input, **kw):
        sent.append(input)
        return _Proc()

    monkeypatch.setattr("subprocess.run", run)
    reocr.reread_isolated("a.pdf", b"%PDF-x", passes=2, doc_ref=1)
    reocr.reread_isolated("a.pdf", b"%PDF-x", passes=2, doc_ref=1, model=BENCH_MODEL)
    plain, bench = (json.loads(s.split(b"\n", 1)[0]) for s in sent)
    assert "model" not in plain
    assert bench["model"] == BENCH_MODEL
    assert {k: v for k, v in bench.items() if k != "model"} == plain


@pytest.mark.parametrize("header_model", [None, BENCH_MODEL])
def test_the_child_passes_the_header_model_to_the_read(monkeypatch, capsys, header_model):
    seen: dict = {}

    def fake_reread(name, content, mime="", **kw):
        seen.update(kw)
        return Reread(["x"], "vision2", kw.get("model") or "e")

    monkeypatch.setattr(reocr, "reread", fake_reread)
    header = {"name": "a.pdf", "mime": "", "passes": 2, "purpose": "reocr"}
    if header_model:
        header["model"] = header_model
    stdin = type("S", (), {"buffer": io.BytesIO(json.dumps(header).encode() + b"\n%PDF")})()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["reocr_child"])
    assert reocr_child.main() == 0
    assert seen["model"] == header_model
    capsys.readouterr()


def test_compare_one_passes_the_model_to_the_reader_only_when_given(vocab):
    seen: list[dict] = []

    def reader(name, content, mime="", **kw):
        seen.append(kw)
        return Reread([PAGE], "vision2", kw.get("model") or "qwen-local")

    drive = _Drive({"side": SIDECAR.encode(), "src": b"%PDF-synthetic"})
    rc.compare_one(drive, _row(), vocab, reader=reader)
    res = rc.compare_one(drive, _row(), vocab, reader=reader, model=BENCH_MODEL)
    assert "model" not in seen[0]
    assert seen[1]["model"] == BENCH_MODEL
    assert res["engine"] == BENCH_MODEL


@pytest.fixture
async def vocab(db):
    from oncofiles.curated.facts_rules import Vocabulary

    return Vocabulary.from_rows(await db.fact_code_vocabulary())


# ── the tool: refused before anything starts, echoed in the report ──────


@pytest.fixture
def bench(env, lmstudio, monkeypatch):  # noqa: F811
    """The compare job environment of ``test_reocr_compare`` with the fake LM
    Studio's model list, and a reader that records what it was given."""
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    calls: list[dict] = []

    def reader(name, content, mime="", **kw):
        calls.append(kw)
        return Reread([ONE_DIGIT], "vision2", kw.get("model") or PROD_MODEL, dpi=[150, 220])

    monkeypatch.setattr(reocr, "reread_isolated", reader)
    return calls


async def _start(db, **kw):
    return await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, [501], **kw)


@pytest.mark.parametrize("bad", ["", "has space", "x" * 201, 7, "a;b"])
async def test_an_invalid_model_id_is_refused_before_anything(db, bench, lmstudio, bad):
    await _seed(db)
    out = await _start(db, model=bad)
    assert out["error"].startswith("model must be")
    assert tool._reocr_jobs == {} and bench == []
    assert lmstudio.model_requests == 0


@pytest.mark.parametrize("model", [UNLOADED, "google/not-there"])
async def test_a_model_that_is_not_loaded_is_refused_before_any_job(
    db, bench, lmstudio, monkeypatch, model
):
    await _seed(db)

    async def no_targets(*a, **k):
        raise AssertionError("no document is looked up before the model check")

    monkeypatch.setattr(tool, "_compare_targets", no_targets)
    out = await _start(db, model=model)
    assert out["error"] == "model_not_loaded" and out["model"] == model
    assert tool._reocr_jobs == {} and bench == []
    assert lmstudio.bodies == []  # no chat call either


async def test_an_unreadable_model_list_refuses_fail_closed(db, bench, monkeypatch):
    await _seed(db)

    def unavailable(**kw):
        raise llm.LocalLLMTransportError("local LLM HTTP 404")

    monkeypatch.setattr(llm, "model_states", unavailable)
    out = await _start(db, model=BENCH_MODEL)
    assert out["error"] == "model_state_unknown"
    assert tool._reocr_jobs == {} and bench == []


async def test_with_a_loaded_model_the_job_reads_with_it_and_echoes_it(db, bench):
    await _seed(db)
    out = await _start(db, model=BENCH_MODEL)
    assert out.get("kind") == "reocr_compare", out
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    job = tool._reocr_jobs[PRIMARY_TEST_UUID]
    assert job["state"] == "done", job
    assert job["model"] == BENCH_MODEL and job["overrides"] == {"model": BENCH_MODEL}
    assert job["report"]["settings"] == {"model": BENCH_MODEL,
                                         "overrides": {"model": BENCH_MODEL}}  # fmt: skip
    assert [c["model"] for c in bench] == [BENCH_MODEL]
    assert job["report"]["documents"][0]["engine"] == BENCH_MODEL
    assert config.LOCAL_LLM_MODEL == PROD_MODEL


async def test_without_a_model_the_job_is_unchanged_and_says_so(db, bench, lmstudio):
    await _seed(db)
    await _start(db)
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    job = tool._reocr_jobs[PRIMARY_TEST_UUID]
    assert job["report"]["settings"] == {"model": PROD_MODEL, "overrides": {}}
    assert ["model" in c for c in bench] == [False]
    assert lmstudio.model_requests == 0  # no model check without an override


async def test_the_mcp_tool_takes_and_threads_the_model(db, bench):
    from tests.test_reocr_compare import _as_caller, _Ctx, _primary_slug

    await _seed(db)
    slug = await _primary_slug(db)
    with _as_caller(admin=True, pid=""):
        refused = json.loads(await tool.curated_reocr_compare(
            _Ctx(db), [501], patient_slug=slug, model=UNLOADED))  # fmt: skip
        assert refused["error"] == "model_not_loaded"
        out = json.loads(await tool.curated_reocr_compare(
            _Ctx(db), [501], patient_slug=slug, model=BENCH_MODEL))  # fmt: skip
    assert out["kind"] == "reocr_compare"
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    assert [c["model"] for c in bench] == [BENCH_MODEL]


# ── locks: write paths and production callers never take it ─────────────


def _fn(mod: str, name: str):
    tree = ast.parse((SRC / mod).read_text())
    funcs = (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef))
    return next(n for n in funcs if n.name == name)


def _params(fn) -> set[str]:
    return {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs]}


def test_write_and_production_entry_points_take_no_model():
    for name in ("curated_reocr", "start_reocr", "_reocr_job", "curated_reread_verify",
                 "start_reread", "_reread_job", "curated_folder_run"):  # fmt: skip
        assert "model" not in _params(_fn("tools/curated.py", name)), name
    compare = _params(_fn("tools/curated.py", "curated_reocr_compare"))
    assert "model" in compare and "reasoning_effort" not in compare
    # the compare stays a dry, read-only job: never a write kind
    assert tool.COMPARE_KIND in tool._NO_WRITE_KINDS


def test_production_callers_never_pass_a_model():
    """Ingest's orientation question and every re-OCR / re-read call of
    ``reread_isolated`` outside the compare pass no ``model``."""
    targets = {"ask_rotation", "reread_isolated", "reread", "_vision_passes", "compare_one"}
    for path in (SRC / "curated" / "extract.py", SRC / "tools" / "curated.py",
                 SRC / "curated" / "runner.py"):  # fmt: skip
        if not path.exists():
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in targets:
                continue
            kws = {k.arg for k in node.keywords}
            if name == "compare_one":
                continue  # the compare job itself threads the validated override
            assert "model" not in kws, (path.name, name, node.lineno)


def test_write_paths_still_refuse_a_model_override():
    with pytest.raises(ValueError, match="only on a dry run"):
        llm_extract_call.refuse_overrides_on_write(write=True, model=BENCH_MODEL)
