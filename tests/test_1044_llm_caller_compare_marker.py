"""#1044: a caller code on the local-model usage log line, and a per-job
reasoning-marker override on the dry re-OCR compare.

1. ``llm.chat(caller=…)`` adds ``caller=<code> marker=on|off`` to the
   ``local llm call:`` line. Codes come from a fixed allowlist (unknown →
   ``other``); ``None`` falls back to the context (chat scorecard, then the
   ``local_only`` zone). The request body never carries it.
2. ``curated_reocr_compare(reasoning_marker="on"|"off"|null)``: threaded
   explicitly to every vision call of the compare read, echoed in ``settings``
   and the usage records; null keeps every request body unchanged, and no
   write path takes it.

Fake LM Studio = ``httpx.MockTransport`` (no socket). Synthetic data only.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import logging
import sys
from pathlib import Path

import httpx
import pytest

from oncofiles import cloud_guard, config
from oncofiles.curated import extract, llm, llm_extract_call, orientation, reocr, reocr_child
from oncofiles.curated import reocr_compare as rc
from oncofiles.curated.reocr import Reread
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID
from tests.test_reocr_compare import SIDECAR, _Drive, _row, _seed, env  # noqa: F401
from tests.test_reocr_compare_model_override import (  # noqa: F401
    PROD_MODEL,
    URL,
    _inked_pdf,
    _kinds,
    bench,
    lmstudio,
    vocab,
)

SRC = Path(__file__).parent.parent / "src" / "oncofiles"
_RealClient = httpx.Client


# ── 1. the caller code on the log line ──────────────────────────────────


class _Server:
    """A minimal ``/chat/completions`` that records each request body."""

    def __init__(self):
        self.bodies: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        })  # fmt: skip

    def client(self, **_kw) -> httpx.Client:
        return _RealClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def server(monkeypatch):
    fake = _Server()
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", URL)
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", PROD_MODEL)
    monkeypatch.setattr(config, "LLM_REASONING_MARKER", False)
    monkeypatch.setattr(llm.httpx, "Client", fake.client)
    return fake


def _log_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "local llm call:" in r.getMessage()]


def test_the_log_line_has_caller_and_marker_and_the_body_is_unchanged(server, caplog):
    caplog.set_level(logging.INFO, logger="oncofiles.curated.llm")
    llm.chat("Synthetic system.", "Synthetic question 2027.")
    llm.chat("Synthetic system.", "Synthetic question 2027.", caller="classify")
    plain, coded = server.bodies
    assert plain == coded  # byte-identical request: the caller is never sent
    assert "caller" not in json.dumps(coded)
    assert set(coded) == {"model", "temperature", "max_tokens", "reasoning_effort", "messages"}
    first, second = _log_lines(caplog)
    assert first.endswith("caller=other marker=off")
    assert second.endswith("caller=classify marker=off")
    assert "Synthetic" not in first + second  # no content, ever


def test_the_log_line_says_marker_on_when_the_marker_is_sent(server, caplog):
    caplog.set_level(logging.INFO, logger="oncofiles.curated.llm")
    llm.chat("S.", "U.", thinking=False, reasoning_marker=True, caller="reocr_pass")
    (line,) = _log_lines(caplog)
    assert line.endswith("caller=reocr_pass marker=on")
    assert server.bodies[0]["messages"][0]["content"].endswith(llm.REASONING_OFF_MARKER)


@pytest.mark.parametrize("bad", ["nonsense", "", "OCR_INGEST", "chat; rm", "x" * 300])
def test_an_unknown_code_is_logged_as_other(server, caplog, bad):
    caplog.set_level(logging.INFO, logger="oncofiles.curated.llm")
    assert llm.resolve_caller(bad) == "other"
    llm.chat("S.", "U.", caller=bad)
    (line,) = _log_lines(caplog)
    assert line.endswith("caller=other marker=off")
    assert bad not in line.split("caller=")[1] or bad == ""


def test_every_allowlisted_code_is_kept():
    for code in llm.CALLERS:
        assert llm.resolve_caller(code) == code


@pytest.mark.parametrize(
    ("zone", "expected"),
    [
        ("chat", "chat"),  # the chat core's zone (chat/core.py CHAT_JOB)
        ("holding_extract", "holding_extract"),
        ("curated chat scorecard", "chat_scorecard"),
        ("gdrive_sync", "other"),
        ("curated run", "other"),
    ],
)
def test_without_a_caller_the_zone_decides(zone, expected):
    with cloud_guard.local_only(zone):
        assert llm.resolve_caller(None) == expected
        assert llm.resolve_caller("classify") == "classify"  # explicit wins


def test_outside_any_zone_it_is_other_and_the_chat_scorecard_context_wins():
    assert llm.resolve_caller(None) == "other"
    with cloud_guard.local_only("chat"), llm.scorecard_call_overrides():
        assert llm.resolve_caller(None) == "chat_scorecard"


def test_the_zone_fallback_reaches_the_log_line(server, caplog):
    caplog.set_level(logging.INFO, logger="oncofiles.curated.llm")
    with cloud_guard.local_only("chat"):
        llm.chat("S.", "U.")
    assert _log_lines(caplog)[0].endswith("caller=chat marker=off")


def _png() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=200, height=280)
    black = (0.0,) * 3
    page.draw_rect(page.rect * 0.5, color=black, fill=black)
    png = page.get_pixmap(dpi=36).tobytes("png")
    doc.close()
    return png


@pytest.mark.parametrize(("zone", "ingest"), [(None, "ocr_ingest"),
                                              ("holding_extract", "holding_extract")])  # fmt: skip
def test_ingest_passes_its_codes(monkeypatch, zone, ingest):
    monkeypatch.setattr(config, "OCR_ORIENTATION", "off")
    seen: list[str] = []

    def fake_chat(system, content, *, info=None, caller=None, **kw):
        seen.append(caller)
        if caller != "ocr_retry":  # near-empty and nearly all reasoning: retried
            info.update(reasoning_tokens=390, completion_tokens=400)
            return "ok"
        return "Hemoglobin 140 g/l 01.02.2027 and more synthetic text here"

    monkeypatch.setattr(llm, "chat", fake_chat)
    if zone:
        with cloud_guard.local_only(zone):
            extract._vision_pages([_png()])
    else:
        extract._vision_pages([_png()])
    assert seen == [ingest, "ocr_retry"]


def test_every_llm_chat_call_site_outside_chat_names_its_caller():
    """The chat core relies on its zone ("chat"); every other direct
    ``llm.chat(`` call passes an explicit ``caller=`` code."""
    found = 0
    for path in SRC.rglob("*.py"):
        if "chat" in path.relative_to(SRC).parts[:1]:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "chat"
                    and getattr(node.func.value, "id", None) == "llm"):  # fmt: skip
                found += 1
                kws = {k.arg for k in node.keywords}
                assert "caller" in kws, (path.name, node.lineno)
    assert found >= 5
    # the extractors' shared call path names theirs too
    text = (SRC / "curated" / "llm_extract_call.py").read_text()
    assert '"caller": caller' in text and '"extract_clinical"' in text


# ── 2. the compare's reasoning-marker override ──────────────────────────


def _read(marker=None) -> Reread:
    kw = {"reasoning_marker": marker} if marker is not None else {}
    return reocr.reread("20270521_Synth_501.pdf", _inked_pdf(), "application/pdf",
                        passes=2, doc_ref=501, purpose="reocr", **kw)  # fmt: skip


def _system(body: dict) -> str:
    return body["messages"][0]["content"]


def test_null_keeps_every_request_body_unchanged(lmstudio):  # noqa: F811
    base = _read()
    base_bodies, lmstudio.bodies = lmstudio.bodies, []
    off = _read(marker=False)
    # "off" with the env off is today's request byte for byte
    assert lmstudio.bodies == base_bodies
    assert all("reasoning_marker" not in rec for rec in base.llm_passes)
    passes = [r for r in off.llm_passes if r.get("kind") != "orientation"]
    assert {r["reasoning_marker"] for r in passes if not r.get("retry")} == {"off"}
    # the near-empty retry always sends its own marker (as in production)
    assert {r["reasoning_marker"] for r in passes if r.get("retry")} == {"on"}
    assert any(r.get("kind") == "orientation" and r["reasoning_marker"] == "off"
               for r in off.llm_passes)  # fmt: skip


def test_on_threads_the_marker_to_every_call_kind(lmstudio):  # noqa: F811
    rr = _read(marker=True)
    kinds = _kinds(lmstudio.bodies)
    # orientation, pass 1 and the two pass-2 halves; thinking off, so the
    # fake's answer is the page and no near-empty retry is needed
    assert kinds == ["orientation", "retry", "retry", "retry"]
    for body in lmstudio.bodies:
        assert _system(body).endswith(llm.REASONING_OFF_MARKER)
        assert llm.NO_THINK not in json.dumps(body["messages"])
        assert body["chat_template_kwargs"] == {"enable_thinking": False}
        assert body["model"] == PROD_MODEL
    assert {r["reasoning_marker"] for r in rr.llm_passes} == {"on"}
    assert any(r.get("tile") == 2 for r in rr.llm_passes)
    assert rr.error is None and rr.method == "vision2"
    assert config.LLM_REASONING_MARKER is False  # nothing global changed


def test_on_reaches_the_probe_readings_too(lmstudio, monkeypatch):  # noqa: F811
    monkeypatch.setattr(orientation, "parse_orientation",
                        lambda answer: orientation.Guess(None, False))  # fmt: skip
    rr = _read(marker=True)
    probes = [r for r in rr.llm_passes if r.get("orientation_probe")]
    assert probes and all(r["reasoning_marker"] == "on" for r in probes)
    assert all(_system(b).endswith(llm.REASONING_OFF_MARKER) for b in lmstudio.bodies)


def test_the_child_header_carries_the_marker_only_when_given(monkeypatch):
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
    reocr.reread_isolated("a.pdf", b"%PDF-x", passes=2, doc_ref=1, reasoning_marker=False)
    plain, off = (json.loads(s.split(b"\n", 1)[0]) for s in sent)
    assert "reasoning_marker" not in plain
    assert off["reasoning_marker"] is False
    assert {k: v for k, v in off.items() if k != "reasoning_marker"} == plain


@pytest.mark.parametrize(("value", "expected"), [(None, None), (True, True), (False, False),
                                                 ("on", None)])  # fmt: skip
def test_the_child_passes_the_header_marker_to_the_read(monkeypatch, capsys, value, expected):
    seen: dict = {}

    def fake_reread(name, content, mime="", **kw):
        seen.update(kw)
        return Reread(["x"], "vision2", "e")

    monkeypatch.setattr(reocr, "reread", fake_reread)
    header = {"name": "a.pdf", "mime": "", "passes": 2, "purpose": "reocr"}
    if value is not None:
        header["reasoning_marker"] = value
    stdin = type("S", (), {"buffer": io.BytesIO(json.dumps(header).encode() + b"\n%PDF")})()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "argv", ["reocr_child"])
    assert reocr_child.main() == 0
    assert seen["reasoning_marker"] is expected
    capsys.readouterr()


def test_compare_one_passes_the_marker_to_the_reader_only_when_given(vocab):  # noqa: F811
    seen: list[dict] = []

    def reader(name, content, mime="", **kw):
        seen.append(kw)
        return Reread(["Hemoglobin 140"], "vision2", "qwen-local")

    drive = _Drive({"side": SIDECAR.encode(), "src": b"%PDF-synthetic"})
    rc.compare_one(drive, _row(), vocab, reader=reader)
    rc.compare_one(drive, _row(), vocab, reader=reader, reasoning_marker=True)
    assert "reasoning_marker" not in seen[0]
    assert seen[1]["reasoning_marker"] is True


async def _start(db, **kw):
    return await tool.start_reocr_compare(db, PRIMARY_TEST_UUID, [501], **kw)


@pytest.mark.parametrize("bad", ["yes", "ON", True, 1, ""])
async def test_an_invalid_marker_is_refused_before_anything(db, bench, bad):  # noqa: F811
    await _seed(db)
    out = await _start(db, reasoning_marker=bad)
    assert "reasoning_marker must be one of" in out["error"]
    assert tool._reocr_jobs == {} and bench == []


@pytest.mark.parametrize(("marker", "flag"), [("on", True), ("off", False)])
async def test_the_job_threads_and_echoes_the_marker(db, bench, marker, flag):  # noqa: F811
    await _seed(db)
    out = await _start(db, reasoning_marker=marker)
    assert out.get("kind") == "reocr_compare", out
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    job = tool._reocr_jobs[PRIMARY_TEST_UUID]
    assert job["state"] == "done", job
    assert job["report"]["settings"] == {"model": PROD_MODEL, "reasoning_marker": marker,
                                         "overrides": {"reasoning_marker": marker}}  # fmt: skip
    assert job["reasoning_marker"] == marker
    assert [c["reasoning_marker"] for c in bench] == [flag]
    assert "model" not in bench[0]


async def test_without_a_marker_the_job_report_and_reader_are_unchanged(db, bench):  # noqa: F811
    await _seed(db)
    await _start(db)
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    job = tool._reocr_jobs[PRIMARY_TEST_UUID]
    assert job["report"]["settings"] == {"model": PROD_MODEL, "overrides": {}}
    assert ["reasoning_marker" in c for c in bench] == [False]


async def test_the_mcp_tool_takes_and_threads_the_marker(db, bench):  # noqa: F811
    from tests.test_reocr_compare import _as_caller, _Ctx, _primary_slug

    await _seed(db)
    slug = await _primary_slug(db)
    with _as_caller(admin=True, pid=""):
        refused = json.loads(await tool.curated_reocr_compare(
            _Ctx(db), [501], patient_slug=slug, reasoning_marker="maybe"))  # fmt: skip
        assert "reasoning_marker must be one of" in refused["error"]
        out = json.loads(await tool.curated_reocr_compare(
            _Ctx(db), [501], patient_slug=slug, reasoning_marker="on"))  # fmt: skip
    assert out["kind"] == "reocr_compare"
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    assert [c["reasoning_marker"] for c in bench] == [True]


# ── locks: write paths and production callers never take it ─────────────


def _fn(mod: str, name: str):
    tree = ast.parse((SRC / mod).read_text())
    funcs = (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef))
    return next(n for n in funcs if n.name == name)


def _params(fn) -> set[str]:
    return {a.arg for a in [*fn.args.args, *fn.args.kwonlyargs]}


def test_write_and_production_entry_points_take_no_marker():
    for name in ("curated_reocr", "start_reocr", "_reocr_job", "curated_reread_verify",
                 "start_reread", "_reread_job", "curated_folder_run",
                 "curated_restore_sidecar", "curated_promote_candidates"):  # fmt: skip
        assert "reasoning_marker" not in _params(_fn("tools/curated.py", name)), name
    assert "reasoning_marker" in _params(_fn("tools/curated.py", "curated_reocr_compare"))
    assert tool.COMPARE_KIND in tool._NO_WRITE_KINDS


def test_production_callers_never_pass_a_marker_to_the_read():
    targets = {"ask_rotation", "reread_isolated", "reread", "_vision_passes"}
    for path in (SRC / "curated" / "extract.py", SRC / "tools" / "curated.py",
                 SRC / "curated" / "runner.py", SRC / "holding_extract.py"):  # fmt: skip
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name in targets:
                kws = {k.arg for k in node.keywords}
                assert "reasoning_marker" not in kws, (path.name, name, node.lineno)


def test_write_paths_refuse_a_marker_override():
    with pytest.raises(ValueError, match="only on a dry run"):
        llm_extract_call.refuse_overrides_on_write(write=True, reasoning_marker="on")
