"""Scorecards: per-job ``model`` and ``reasoning_effort`` overrides, dry run only.

Infra evaluates a new local model without touching the production env: the
classification and the extraction scorecard take a model id (it must be
``loaded`` in LM Studio's native ``/api/v0/models``; nothing is loaded or
downloaded, never a JIT load next to the production model)
and a reasoning effort, threaded explicitly to ``llm.chat`` for that job only.
Production calls keep their request body byte for byte; write paths refuse
the overrides. A different model is a different definition: no aggregate
delta, but the per-document delta of the classification scorecard is still
given, labelled ``across_definitions``.

The fake LM Studio is an ``httpx.MockTransport`` (no socket). Synthetic data
only (the documents and answers of the two scorecard test modules).
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import classify, llm, llm_extract_call
from oncofiles.curated import classify_scorecard as csc
from oncofiles.curated import scorecard as sc
from oncofiles.tools import curated as tool
from tests import test_926_classify_scorecard as t926
from tests import test_extraction_scorecard as tex
from tests.helpers import PATIENT_A_UUID, PRIMARY_TEST_UUID

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
URL = "http://llm.test/v1"
NEW_MODEL = "qwen/qwen3.9-35b-mlx@4bit"  # invented id
UNLOADED = "qwen/qwen3.9-72b-mlx"  # invented: downloaded, not loaded (a JIT load)
LISTED = ["test-model", "mock-model", NEW_MODEL]
_RealClient = httpx.Client


class FakeLMStudio:
    """Native ``GET /api/v0/models``: ``LISTED`` loaded, ``UNLOADED`` not loaded.
    OpenAI ``GET /v1/models`` lists all of them (as with JIT loading on), so a
    check against it would wrongly pass ``UNLOADED``. ``POST
    /v1/chat/completions`` records the body."""

    def __init__(self, listed=LISTED, models_status=200):
        self.listed, self.models_status = listed, models_status
        self.bodies: list[dict] = []
        self.model_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v0/models":
            self.model_requests.append(request)
            if self.models_status != 200:
                return httpx.Response(self.models_status, json={"error": "x"})
            data = [
                {"id": m, "object": "model", "type": "llm", "state": "loaded"} for m in self.listed
            ]
            data.append({"id": UNLOADED, "object": "model", "type": "vlm",
                         "state": "not-loaded"})  # fmt: skip
            return httpx.Response(200, json={"object": "list", "data": data})
        if request.method == "GET" and request.url.path == "/v1/models":
            self.model_requests.append(request)
            ids = [*self.listed, UNLOADED]
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": m, "object": "model"} for m in ids]})  # fmt: skip
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "{\"ok\": true}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })  # fmt: skip

    def client(self, **_kw) -> httpx.Client:
        return _RealClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def lmstudio(monkeypatch):
    fake = FakeLMStudio()
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "k-invented")
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    monkeypatch.setattr(llm.httpx, "Client", fake.client)
    return fake


# ── llm.chat: the override reaches the body, the default body is unchanged ──


def test_default_request_body_is_unchanged(lmstudio):
    llm.chat("SYS", "USER")
    (body,) = lmstudio.bodies
    assert body == {
        "model": "test-model",
        "temperature": 0.0,
        "max_tokens": llm.REASONING_BUDGET,
        "reasoning_effort": "low",
        "messages": [
            {"role": "system", "content": "SYS\n" + llm.NO_THINK},
            {"role": "user", "content": "USER"},
        ],
    }
    assert llm.DEFAULT_REASONING_EFFORT == "low"


def test_model_and_reasoning_effort_override_reach_the_body(lmstudio):
    llm.chat("SYS", "USER", model=NEW_MODEL, reasoning_effort="high")
    llm.chat("SYS", "USER")  # the next call is back on the configured model
    first, second = lmstudio.bodies
    assert first["model"] == NEW_MODEL and first["reasoning_effort"] == "high"
    assert second["model"] == "test-model" and second["reasoning_effort"] == "low"
    assert config.LOCAL_LLM_MODEL == "test-model"  # nothing global changed
    assert {k: v for k, v in first.items() if k not in ("model", "reasoning_effort")} == {
        k: v for k, v in second.items() if k not in ("model", "reasoning_effort")
    }


# ── model list and validation ──────────────────────────────────────────


def test_a_loaded_model_passes(lmstudio):
    assert llm.model_refusal(NEW_MODEL) is None
    (req,) = lmstudio.model_requests
    assert str(req.url) == "http://llm.test/api/v0/models"  # native, /v1 stripped
    assert req.headers["Authorization"] == "Bearer k-invented"
    assert lmstudio.bodies == []  # checking never sends a completion (no load)


def test_a_model_that_is_not_loaded_is_refused(lmstudio):
    """Listed natively but ``not-loaded`` (and listed by /v1/models): refused,
    so the override can never trigger a JIT load."""
    assert llm.model_refusal(UNLOADED) == "model_not_loaded"
    assert llm.model_refusal("qwen/other-model") == "model_not_loaded"
    assert all(r.url.path == "/api/v0/models" for r in lmstudio.model_requests)


@pytest.mark.parametrize("status", [404, 500])
def test_native_endpoint_missing_fails_closed_without_fallback(lmstudio, status):
    lmstudio.models_status = status  # 404 = an older LM Studio without /api/v0
    assert llm.model_refusal(NEW_MODEL) == "model_state_unknown"
    assert [r.url.path for r in lmstudio.model_requests] == ["/api/v0/models"]


def test_unexpected_native_body_fails_closed(monkeypatch, lmstudio):
    def handler(request):
        return httpx.Response(200, json={"data": "nope"})

    monkeypatch.setattr(lmstudio, "handler", handler)
    assert llm.model_refusal(NEW_MODEL) == "model_state_unknown"


@pytest.mark.parametrize(
    "base,url",
    [("http://h.lan:1234/v1", "http://h.lan:1234/api/v0/models"),
     ("http://h.lan:1234/v1/", "http://h.lan:1234/api/v0/models"),
     ("http://h.lan:1234", "http://h.lan:1234/api/v0/models")],
)  # fmt: skip
def test_native_url_is_derived_from_the_base_url(base, url):
    assert llm.native_models_url(base) == url


def test_model_check_refuses_a_public_host(monkeypatch, lmstudio):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "https://api.example.com/v1")
    assert llm.model_refusal(NEW_MODEL) == "model_state_unknown"
    assert lmstudio.model_requests == []


@pytest.mark.parametrize(
    "model,effort",
    [("", None), ("a b", None), ("x" * 201, None), ("m;rm", None), ("m\n", None), (5, None),
     ("ok", "max"), ("ok", "")],
)  # fmt: skip
def test_invalid_overrides_are_refused(model, effort):
    assert llm.model_override_error(model, effort)
    with pytest.raises(ValueError):
        llm_extract_call.validate_overrides(model=model, reasoning_effort=effort)


def test_valid_overrides_pass():
    assert llm.model_override_error(None, None) is None
    assert llm.model_override_error("x" * 200, None) is None
    for effort in llm.REASONING_EFFORTS:
        assert llm.model_override_error(NEW_MODEL, effort) is None


# ── call paths ─────────────────────────────────────────────────────────


def test_classify_propose_passes_the_overrides_only_when_given(monkeypatch, lmstudio):
    seen = []

    def fake(system, user, info=None, **kw):
        seen.append(kw)
        return "{}"

    monkeypatch.setattr(llm, "chat", fake)
    classify.propose(t926.CONTRACT, "TEXT", variant="v1")
    classify.propose(t926.CONTRACT, "TEXT", variant="v1", model=NEW_MODEL,
                     reasoning_effort="none")  # fmt: skip
    assert seen[0] == {}
    assert seen[1] == {"model": NEW_MODEL, "reasoning_effort": "none"}


def test_extractor_call_settings_carry_the_overrides_only_when_given():
    calls = []

    def fake(system, user, info=None, **kw):
        calls.append(kw)
        return "{}"

    for cfg in (llm_extract_call.settings(),
                llm_extract_call.settings(model=NEW_MODEL, reasoning_effort="medium")):  # fmt: skip
        res = llm_extract_call.PageResult(page_number=1)
        llm_extract_call.call_and_parse(
            res, system="S", user="U", response_format=None, parse_content=json.loads,
            parse_reasoning=json.loads, cfg=cfg, chat=fake,
        )  # fmt: skip
    default, override = calls
    assert "model" not in default and "reasoning_effort" not in default
    assert override["model"] == NEW_MODEL and override["reasoning_effort"] == "medium"
    assert {k: v for k, v in override.items() if k not in ("model", "reasoning_effort")} == default
    assert "model" not in llm_extract_call.settings().report()


def test_write_paths_refuse_the_overrides():
    for kw in ({"model": NEW_MODEL}, {"reasoning_effort": "high"}):
        with pytest.raises(ValueError, match="only on a dry run"):
            llm_extract_call.refuse_overrides_on_write(write=True, **kw)
        llm_extract_call.refuse_overrides_on_write(write=False, **kw)  # dry run: fine


def _fn(mod: str, name: str):
    tree = ast.parse((SRC / mod).read_text())
    funcs = (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef))
    return next(n for n in funcs if n.name == name)


def test_only_the_scorecards_take_a_model_override():
    """Write jobs and production entry points have no model / effort
    parameter, and nothing in src assigns ``LOCAL_LLM_MODEL``."""
    for name in ("_clinical_job", "_lab_job", "curated_clinical_extract", "curated_lab_extract",
                 "curated_clinical_extract_preview", "curated_llm_extract_preview",
                 "curated_folder_run"):  # fmt: skip
        args = {a.arg for a in _fn("tools/curated.py", name).args.kwonlyargs} | {
            a.arg for a in _fn("tools/curated.py", name).args.args
        }
        assert not args & {"model", "reasoning_effort"}, name
    for name in ("curated_classify_scorecard", "curated_extraction_scorecard"):
        args = {a.arg for a in _fn("tools/curated.py", name).args.args}
        assert {"model", "reasoning_effort"} <= args, name
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AugAssign | ast.AnnAssign):
                targets = [node.target]
            for t in targets:
                assert not (isinstance(t, ast.Attribute) and t.attr == "LOCAL_LLM_MODEL"), path


# ── versions / compare ─────────────────────────────────────────────────


def test_classification_compare_withholds_the_delta_across_models_but_diffs_documents():
    base_v = csc.versions(sampling="legacy")
    cur_v = csc.versions(sampling="legacy", model=NEW_MODEL, reasoning_effort="high")
    assert cur_v["model"] == NEW_MODEL and cur_v["reasoning_effort"] == "high"
    assert "reasoning_effort" not in base_v
    rows_old = [t926._row(1, (False, "agree")), t926._row(2, (True, "agree"))]
    rows_new = [t926._row(1, (True, "agree")), t926._row(2, (True, "agree"))]
    out = csc.compare({"versions": base_v, "totals": {}, "per_document": rows_old}, cur_v, {},
                      rows_new)  # fmt: skip
    assert out["baseline_comparable"] is False and out["delta"] is None
    assert out["definition_changed"]["model"] == [base_v["model"], NEW_MODEL]
    assert out["definition_changed"]["reasoning_effort"] == [None, "high"]
    per_doc = out["per_document_delta"]
    assert per_doc["across_definitions"] is True and per_doc["comparable"] is True
    assert per_doc["review"]["document_ids"] == [1] and per_doc["documents_compared"] == 2
    # same model on both sides (also the pre-override baselines): comparable
    same = csc.compare({"versions": base_v, "totals": {}, "per_document": rows_old}, base_v, {},
                       rows_new)  # fmt: skip
    assert same["baseline_comparable"] is True and "across_definitions" not in (
        same["per_document_delta"])  # fmt: skip
    effort_only = csc.versions(sampling="legacy", reasoning_effort="none")
    assert csc.compare({"versions": base_v, "totals": {}}, effort_only, {})[
        "baseline_comparable"] is False  # fmt: skip


def test_extraction_compare_treats_another_model_as_another_definition():
    kw = dict(extractor="ext-v", verifier="ver-v", clinical_extractor="cx-v",
              clinical_verifier="cv-v")  # fmt: skip
    base = sc.versions(**kw)
    assert set(base) == set(sc.VERSION_FIELDS)  # no override: the old shape
    over = sc.versions(**kw, model=NEW_MODEL, reasoning_effort="low")
    out = sc.compare({"versions": base, "totals": {}}, over, {})
    assert out["baseline_comparable"] is False and out["delta"] is None
    assert out["definition_changed"] == {"model": [None, NEW_MODEL],
                                         "reasoning_effort": [None, "low"]}  # fmt: skip
    # and the other way round
    assert sc.compare({"versions": over, "totals": {}}, base, {})["baseline_comparable"] is False
    assert sc.compare({"versions": over, "totals": {}}, over, {})["baseline_comparable"] is True
    assert sc.compare({"versions": base, "totals": {}}, base, {})["baseline_comparable"] is True
    assert sc.versions_of({"versions": over}) == over


# ── the tools ──────────────────────────────────────────────────────────


async def _rows(db):
    async with db.db.execute("SELECT * FROM curated_runs ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


@pytest.fixture
def classify_env(monkeypatch, t926_scorecard_env, lmstudio):
    return lmstudio


@pytest.fixture
def t926_scorecard_env(monkeypatch):
    t926.SEEN_KWARGS.clear()
    monkeypatch.setattr(llm, "chat", t926.fake_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(csc, "_today", lambda: t926.TODAY)
    monkeypatch.setattr(config, "CURATED_CLASSIFY_PROMPT", "v1")
    monkeypatch.setenv("CURATED_AUTO_VOCAB", "true")

    async def contract(_db, _pid):
        return t926.CONTRACT

    monkeypatch.setattr(tool, "_patient_contract", contract)


async def _classify(db, **kw):
    return json.loads(await tool.curated_classify_scorecard(
        t926._ctx(db), [t926.DOC_A, t926.DOC_B, t926.DOC_C], **kw))  # fmt: skip


async def test_classify_scorecard_refuses_bad_or_unlisted_models_before_anything(
    seeded_926, classify_env
):
    db = seeded_926
    for kw in ({"model": "a b"}, {"model": ""}, {"reasoning_effort": "max"}):
        out = await _classify(db, **kw)
        assert "error" in out, kw
    out = await _classify(db, model="qwen/not-there")
    assert out["error"] == "model_not_loaded" and out["model"] == "qwen/not-there"
    out = await _classify(db, model=UNLOADED)  # downloaded, not loaded: no JIT load
    assert out["error"] == "model_not_loaded"
    classify_env.models_status = 404
    out = await _classify(db, model=NEW_MODEL)
    assert out["error"] == "model_state_unknown"
    assert tool._reocr_jobs == {} and await _rows(db) == []
    assert classify_env.bodies == []


@pytest.fixture
async def seeded_926(db):
    for doc_id, text in t926.TEXT_OF.items():
        await db.db.execute(
            "INSERT INTO documents (id, file_id, filename, original_filename, category, "
            "mime_type, size_bytes, patient_id, gdrive_id, structured_metadata) "
            "VALUES (?, ?, ?, ?, 'report', 'application/pdf', 1, ?, ?, '{}')",
            (doc_id, f"gdrive:g{doc_id}", t926.FILED[doc_id], t926.FILED[doc_id],
             PATIENT_A_UUID, f"g{doc_id}"),
        )  # fmt: skip
        await db.save_ocr_page(doc_id, 1, text, "curated-sidecar")
    await db.db.commit()
    return db


@pytest.fixture
async def seeded_ex(db, monkeypatch, lmstudio):
    """The extraction scorecard module's documents and mocked model, on the fake LM Studio."""
    await tex._doc(db, tex.DOC_A, tex.PAGES_A, "report")
    await tex._doc(db, tex.DOC_B, tex.PAGES_B, "discharge")
    await tex._doc(db, tex.DOC_LAB, [tex.LAB_PAGE], "labs")
    await db.db.commit()
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(llm, "chat", tex.chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(tool, "_readme_births", {PRIMARY_TEST_UUID: {tex.DOB}})
    return db


async def test_classify_scorecard_with_another_model(seeded_926, classify_env):
    db = seeded_926
    started = await _classify(db, label="base")
    await asyncio.wait_for(tool._reocr_jobs[PATIENT_A_UUID]["task"], 10)
    assert all(kw == {} for kw in t926.SEEN_KWARGS)  # the production request
    t926.SEEN_KWARGS.clear()
    started = await _classify(db, label="new-model", model=NEW_MODEL, reasoning_effort="high")
    assert started["kind"] == "classify_scorecard", started
    await asyncio.wait_for(tool._reocr_jobs[PATIENT_A_UUID]["task"], 10)
    assert t926.SEEN_KWARGS and all(
        kw == {"model": NEW_MODEL, "reasoning_effort": "high"} for kw in t926.SEEN_KWARGS
    )
    report = json.loads(await tool.curated_reocr_status(t926._ctx(db)))["report"]
    assert report["settings"]["model"] == NEW_MODEL
    assert report["settings"]["reasoning_effort"] == "high"
    assert report["settings"]["overrides"] == {"model": NEW_MODEL, "reasoning_effort": "high"}
    assert report["versions"]["model"] == NEW_MODEL
    assert report["versions"]["reasoning_effort"] == "high"
    assert report["compared_to"]["label"] == "base"
    assert report["baseline_comparable"] is False and report["delta"] is None
    assert report["definition_changed"]["model"] == ["test-model", NEW_MODEL]
    per_doc = report["per_document_delta"]
    assert per_doc["across_definitions"] is True and per_doc["documents_compared"] == 3
    stored = json.loads((await _rows(db))[-1]["report_json"])
    assert stored["versions"]["model"] == NEW_MODEL and stored["per_document"]
    assert config.LOCAL_LLM_MODEL == "test-model"


async def test_extraction_scorecard_refuses_an_unlisted_model_before_anything(seeded_ex):
    seeded = seeded_ex
    out = json.loads(await tool.curated_extraction_scorecard(
        tex._ctx(seeded), [tex.DOC_A], model=UNLOADED))  # fmt: skip
    assert out["error"] == "model_not_loaded"
    # invalid overrides raise, like thinking / sampling (the tool error of FastMCP)
    for kw in ({"reasoning_effort": "extreme"}, {"model": "a b"}):
        with pytest.raises(ValueError):
            await tool.curated_extraction_scorecard(tex._ctx(seeded), [tex.DOC_A], **kw)
    assert tool._reocr_jobs == {} and await _rows(seeded) == []


async def test_extraction_scorecard_with_another_model(monkeypatch, seeded_ex):
    seen = []

    def chat(system, user, info=None, **kw):
        seen.append({k: kw.get(k) for k in ("model", "reasoning_effort")})
        return tex.chat(system, user, info=info, **kw)

    monkeypatch.setattr(llm, "chat", chat)
    seeded = seeded_ex
    await tex._run(seeded, label="base")
    assert seen and all(s == {"model": None, "reasoning_effort": None} for s in seen)
    seen.clear()
    status = await tex._run(seeded, label="new", model=NEW_MODEL, reasoning_effort="none")
    assert seen and all(s == {"model": NEW_MODEL, "reasoning_effort": "none"} for s in seen)
    report = status["report"]
    assert report["settings"]["model"] == NEW_MODEL
    assert report["settings"]["reasoning_effort"] == "none"
    assert report["versions"]["model"] == NEW_MODEL
    assert report["baseline_comparable"] is False and report["delta"] is None
    assert set(report["definition_changed"]) == {"model", "reasoning_effort"}
    assert PRIMARY_TEST_UUID in tool._reocr_jobs
