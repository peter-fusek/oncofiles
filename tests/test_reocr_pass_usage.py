"""Per-pass LLM usage of the re-OCR and the verify-only re-read (#800).

Counts only: finish, token counts, dpi, seconds per page and pass; a pass cut
off at its budget is a report warning, never a silent pass; no page text and
no raw answer channel reaches the report or the log. Synthetic data only
(invented values, dates 2027+)."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from unittest.mock import patch

import pytest

from oncofiles.curated import llm, reocr
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_admin_reread_console import _seed
from tests.test_curated_reocr import NOW, _scan_pdf, _setup
from tests.test_pre_store import _clients, _reset_jobs, file_db  # noqa: F401 (fixtures)

ANSWER = "Leukocyty 6,37 G/l 4,0 - 11"
REASONING = "SYNTHETIC-REASONING-SENTINEL 8,41"


def _usage_chat(per_call: list[dict], answers: list[str] | None = None, *, delay=False):
    """A fake ``llm.chat`` that fills ``info`` like the real one (finish,
    counts and the raw channels) from ``per_call`` in call order."""
    lock = threading.Lock()
    calls = iter(range(10_000))

    def fake_chat(system, content, **kw):
        with lock:
            n = next(calls)
        spec = per_call[n % len(per_call)]
        if delay:
            time.sleep(random.uniform(0, 0.02))
        info = kw.get("info")
        assert info is not None, "re-OCR must ask for usage info"
        answer = (answers or [ANSWER])[n % len(answers or [ANSWER])]
        info.update(finish_reason=spec.get("finish", "stop"), raw_content=answer,
                    reasoning_content=REASONING, prompt_tokens=spec.get("prompt", 900),
                    completion_tokens=spec.get("completion", 300),
                    reasoning_tokens=spec.get("reasoning", 120))  # fmt: skip
        if spec.get("raise"):
            raise llm.LocalLLMError("local LLM answer truncated: reasoning used the whole budget")
        return answer

    return fake_chat


def test_reread_records_every_pass_with_the_info_values(monkeypatch):
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")
    monkeypatch.setattr(llm, "chat", _usage_chat([
        {"prompt": 1001, "completion": 501, "reasoning": 201},
        {"prompt": 1002, "completion": 502, "reasoning": 202},
    ]))  # fmt: skip
    rr = reocr.reread("scan.pdf", _scan_pdf(1))
    assert rr.method == "vision2" and rr.error is None
    first, second = rr.llm_passes
    assert first["page"] == second["page"] == 1
    assert (first["pass"], second["pass"]) == (1, 2)
    assert [p["dpi"] for p in rr.llm_passes] == reocr.reocr_dpis(2)
    assert (first["finish"], first["prompt_tokens"], first["completion_tokens"],
            first["reasoning_tokens"]) == ("stop", 1001, 501, 201)  # fmt: skip
    assert (second["prompt_tokens"], second["completion_tokens"],
            second["reasoning_tokens"]) == (1002, 502, 202)  # fmt: skip
    assert all(isinstance(p["seconds"], float) for p in rr.llm_passes)
    keys = {"page", "pass", "dpi", "finish", "prompt_tokens", "completion_tokens",
            "reasoning_tokens", "seconds"}  # fmt: skip
    assert all(set(p) == keys for p in rr.llm_passes)  # never a raw channel


async def test_report_carries_usage_maxima_and_budget(vocab, monkeypatch):
    drive, doc, _ = _setup()
    monkeypatch.setattr(llm, "chat", _usage_chat([
        {"completion": 700, "reasoning": 400}, {"completion": 650, "reasoning": 480},
    ]))  # fmt: skip
    doc = {**doc, "gdrive_id": drive.add("scan.pdf", drive.root, _scan_pdf(1))}
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=True, now=NOW,
                          reader=reocr.reread)  # fmt: skip
    out = rep.to_dict()
    d = out["documents"][0]
    assert d["result"] == "previewed" and len(d["llm_passes"]) == 2
    assert d["reasoning_tokens_max"] == 480 and d["completion_tokens_max"] == 700
    assert d["budget"] == reocr.REOCR_BUDGET == 16_000
    assert d["truncated_passes"] == [] and out["warnings"] == []


async def test_a_truncated_pass_is_a_warning_and_marked(vocab, monkeypatch):
    drive, doc, _ = _setup()
    monkeypatch.setattr(llm, "chat", _usage_chat([
        {"finish": "stop"}, {"finish": "length", "completion": 16_000, "reasoning": 15_990},
    ]))  # fmt: skip
    doc = {**doc, "gdrive_id": drive.add("scan.pdf", drive.root, _scan_pdf(2))}
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=True, now=NOW,
                          reader=reocr.reread)  # fmt: skip
    out = rep.to_dict()
    d = out["documents"][0]
    assert d["result"] == "previewed"  # report only: the decision is unchanged
    pages = [1, 2]
    second = 2  # the cut-off pass of every page
    assert d["truncated_passes"] == [{"page": p, "pass": second} for p in pages]
    assert out["warnings"] == [
        {"warning": "reocr_pass_truncated", "document_id": doc["document_id"], "page": p,
         "pass": second}
        for p in pages
    ]  # fmt: skip


async def test_a_pass_that_raises_on_length_is_still_reported(vocab, monkeypatch):
    """``llm.chat`` raises when the whole budget went to reasoning; the pass
    is still recorded and warned about, next to the failed result."""
    drive, doc, _ = _setup()
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")
    monkeypatch.setattr(llm, "chat", _usage_chat([
        {"finish": "length", "completion": 16_000, "reasoning": 16_000, "raise": True},
    ]))  # fmt: skip
    doc = {**doc, "gdrive_id": drive.add("scan.pdf", drive.root, _scan_pdf(1))}
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=False, now=NOW,
                          reader=reocr.reread)  # fmt: skip
    d = rep.to_dict()["documents"][0]
    assert d["result"] == "failed" and drive.writes == []
    [rec] = d["llm_passes"]
    assert rec["finish"] == "length" and rec["error"] == "LocalLLMError"
    assert rep.warnings == [{"warning": "reocr_pass_truncated", "document_id": 7,
                             "page": 1, "pass": 1}]  # fmt: skip


async def test_no_text_reaches_the_report_or_the_log(vocab, monkeypatch, caplog):
    drive, doc, _ = _setup()
    monkeypatch.setattr(llm, "chat", _usage_chat([{"finish": "length"}]))
    doc = {**doc, "gdrive_id": drive.add("scan.pdf", drive.root, _scan_pdf(1))}
    with caplog.at_level(logging.INFO, logger="oncofiles.curated.reocr"):
        rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=True, now=NOW,
                              reader=reocr.reread)  # fmt: skip
    lines = [r.getMessage() for r in caplog.records if "re-OCR llm pass" in r.getMessage()]
    assert len(lines) == 2
    first = lines[0]
    assert f"doc={doc['document_id']} page=1 pass=1" in first
    assert "finish=length" in first
    assert "budget=16000" in lines[0]
    logged = "\n".join(r.getMessage() for r in caplog.records)
    usage = json.dumps(
        {k: rep.to_dict()["documents"][0][k] for k in ("llm_passes", "truncated_passes")}
        | {"w": rep.to_dict()["warnings"]}
    )
    for text in (ANSWER, "Leukocyty", "6,37", REASONING, "SENTINEL"):
        assert text not in logged and text not in usage
    assert REASONING not in json.dumps(rep.to_dict())


def test_parallel_passes_keep_every_record(monkeypatch):
    monkeypatch.setattr(llm, "chat", _usage_chat([{"prompt": 11}, {"prompt": 22}], delay=True))
    pages, dpis = 4, [100, 120]
    usage: list[dict] = []
    passes = reocr._vision_passes(_scan_pdf(pages), "pdf", dpis, client=object(), parallel=2,
                                  usage=usage)  # fmt: skip
    assert len(passes) == 2 and len(passes[0]) == pages
    assert [(r["page"], r["pass"]) for r in usage] == [
        (p + 1, k + 1) for p in range(pages) for k in range(len(dpis))
    ]
    assert all(r["dpi"] == dpis[r["pass"] - 1] for r in usage)
    assert all(r["prompt_tokens"] in (11, 22) for r in usage)


def test_adaptive_deferred_second_pass_is_recorded(monkeypatch):
    """A page whose second pass is deferred and then taken keeps pass 2."""
    monkeypatch.setattr(llm, "chat", _usage_chat([{}]))
    usage: list[dict] = []
    reocr._vision_passes(_scan_pdf(2), "pdf", [100, 120], client=object(),
                         second_pass_pages={1}, adaptive=True, usage=usage)  # fmt: skip
    # page 2 shares the value with page 1 (read twice), so it is read twice too
    both = [1, 2]
    assert [(r["page"], r["pass"]) for r in usage] == [(p, k) for p in both for k in both]


# ── the verify-only re-read carries the same fields ─────────────────


class _Drive:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def download(self, file_id: str) -> bytes:
        return self.files[file_id]


def test_verify_only_reread_reports_the_same_usage(monkeypatch, vocab, caplog):
    side = b"---\ndoc_type: Labs\n---\n\n<!-- page 1 -->\nLeukocyty 6,3? G/l\n"
    drive = _Drive({"side_a": side, "src_a": _scan_pdf(1)})
    sha = hashlib.sha256(side).hexdigest()
    d = {"id": 41, "filename": "20270412_Synth_Labs.pdf", "gdrive_id": "src_a",
         "mime_type": "application/pdf", "category": "labs", "document_date": "2027-04-12",
         "structured_metadata": {"curated": {"sidecar_id": "side_a",
                                             "sidecar_sha": sha}}}  # fmt: skip
    monkeypatch.setattr(llm, "chat", _usage_chat([{"reasoning": 77}, {"finish": "length"}]))
    monkeypatch.setattr(
        reocr, "reread_isolated", lambda *a, **kw: reocr.reread(*a, **kw)
    )  # in-process: the test has no LM Studio child
    with caplog.at_level(logging.INFO, logger="oncofiles.curated.reocr"):
        res = tool._reread_one(drive, d, [], vocab)
    assert res["result"] == "previewed" and len(res["llm_passes"]) == 2
    assert res["budget"] == reocr.REOCR_BUDGET and res["reasoning_tokens_max"] == 120
    assert res["truncated_passes"] == [{"page": 1, "pass": 2}]
    assert sum("re-OCR llm pass: doc=41" in r.getMessage() for r in caplog.records) == 2


async def test_verify_only_job_report_lists_truncation_warnings(file_db):  # noqa: F811
    await _seed(file_db)
    result = {"document_id": 501, "result": "previewed", "error": None,
              "truncated_passes": [{"page": 3, "pass": 1}]}  # fmt: skip
    with (
        patch("oncofiles.server._create_patient_clients", _clients()),
        patch.object(tool, "_reread_one", return_value=result),
    ):
        await tool.start_reread(file_db, ERIKA_UUID, [501], dry_run=True)
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    report = tool._reocr_jobs[ERIKA_UUID]["report"]
    assert report["warnings"] == [{"warning": "reocr_pass_truncated", "document_id": 501,
                                   "page": 3, "pass": 1}]  # fmt: skip


@pytest.fixture
async def vocab(db):
    from oncofiles.curated.facts_rules import Vocabulary

    return Vocabulary.from_rows(await db.fact_code_vocabulary())
