"""Backfill survives one bad model answer; the proposal cache is keyed on the README.

Synthetic data only (public repo): invented names, institutions and 2027+ dates.
"""

import asyncio
import dataclasses
import json
from datetime import UTC, datetime

import httpx
import pytest

from oncofiles import config
from oncofiles.curated import classify, llm, runner
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.runner import MAX_TECHNICAL_ATTEMPTS, RunOptions, probe, run_curated
from oncofiles.tools import curated as tool
from tests.test_curated_runner import LABS, README, FakeDrive, fake_extract

NOW = datetime(2028, 6, 23, 5, 17, tzinfo=UTC)
FIRST = "20270301-JanaNovakova-NOU-Labs-Alfa-Beta.pdf"
SECOND = "20270302-JanaNovakova-NOU-Labs-Gama-Delta.pdf"
PROPOSAL = {
    "doc_date": "2027-03-02",
    "doc_type": "Labs",
    "institution": "NOU",
    "doctor": "MUDr. Eva Testerova",
    "hospital": "OKB",
    "confidence": 0.9,
    "summary": "Krvny obraz.",
}
HTML = "<html><body><h1>502 Bad Gateway</h1>context unavailable</body></html>"


@pytest.fixture
def local_llm(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "test-model")
    monkeypatch.setattr(config, "LOCAL_LLM_API_KEY", "k")


def _serve(monkeypatch, handler) -> list:
    """Every ``llm.chat`` without its own client talks to ``handler``."""
    calls: list = []
    real = httpx.Client

    def counted(request):
        calls.append(json.loads(request.content))
        return handler(request)

    monkeypatch.setattr(
        llm.httpx,
        "Client",
        lambda *a, **k: real(transport=httpx.MockTransport(counted), timeout=k.get("timeout")),
    )
    return calls


def _ok(content: str, finish: str = "stop") -> httpx.Response:
    return httpx.Response(
        200, json={"choices": [{"message": {"content": content}, "finish_reason": finish}]}
    )


class _BrokenStream(httpx.SyncByteStream):
    """The connection drops in the middle of the answer body."""

    def __iter__(self):
        yield b'{"choices": [{"message": {"content": "{\\"doc'
        raise httpx.ReadError("connection reset by peer")


def _raise(exc):
    def handler(request):
        raise exc

    return handler


TRANSPORT_FAILURES = {
    "connection_refused": _raise(httpx.ConnectError("refused")),
    "read_timeout": _raise(httpx.ReadTimeout("slow")),
    "reset_before_answer": _raise(httpx.RemoteProtocolError("peer closed connection")),
    "reset_mid_answer": lambda r: httpx.Response(200, stream=_BrokenStream()),
    "proxy_502_html": lambda r: httpx.Response(502, text=HTML),
    "html_400_mentioning_context": lambda r: httpx.Response(400, text=HTML),
    "server_500_json": lambda r: httpx.Response(500, json={"error": "model not loaded"}),
    "html_200": lambda r: httpx.Response(200, text=HTML),
    "empty_body": lambda r: httpx.Response(200, content=b""),
}

ANSWER_FAILURES = {
    "not_json": (lambda r: _ok("I cannot read this document."), "model_answer_not_json"),
    "not_an_object": (lambda r: _ok('["alfa", "beta"]'), "model_answer_not_json"),
    "finish_length": (lambda r: _ok("", finish="length"), "model_answer_truncated"),
    "context_overflow": (
        lambda r: httpx.Response(400, json={"error": "prompt exceeds the context length"}),
        "model_answer_truncated",
    ),
}


# ── typed failure: transport vs answer ────────────────────────────────


@pytest.mark.parametrize("name", sorted(TRANSPORT_FAILURES))
def test_a_down_or_erroring_server_is_transport_before_any_json_parse(name, local_llm, monkeypatch):
    _serve(monkeypatch, TRANSPORT_FAILURES[name])
    monkeypatch.setattr(
        llm, "parse_json_object", lambda text: pytest.fail("parsed an answer from a dead server")
    )
    with pytest.raises(llm.LocalLLMError) as exc:
        classify.propose(parse_contract(README), "VYSLEDOK 02.03.2027")
    assert isinstance(exc.value, llm.LocalLLMTransportError)
    assert exc.value.failure == llm.TRANSPORT
    assert runner._llm_down(exc.value.failure)


@pytest.mark.parametrize("name", sorted(ANSWER_FAILURES))
def test_a_bad_answer_is_answer_kind(name, local_llm, monkeypatch):
    _serve(monkeypatch, ANSWER_FAILURES[name][0])
    with pytest.raises(llm.LocalLLMError) as exc:
        classify.propose(parse_contract(README), "VYSLEDOK 02.03.2027")
    assert isinstance(exc.value, llm.LocalLLMAnswerError)
    assert exc.value.failure == llm.ANSWER
    assert not runner._llm_down(exc.value.failure)


def test_model_down_is_decided_by_kind_only():
    assert runner._llm_down(llm.TRANSPORT)
    assert not runner._llm_down(llm.ANSWER)
    assert runner._llm_down(llm.UNKNOWN)  # a plain LocalLLMError: fail closed
    assert runner._llm_down("some-future-kind")  # unknown kind: fail closed
    assert not runner._llm_down(None)  # not a model error at all (corrupt file, …)
    assert llm.LocalLLMError("local LLM answer truncated").failure == llm.UNKNOWN


# ── backfill through the real model client ────────────────────────────


def _drive_with_two_unsidecared_files():
    d = FakeDrive()
    d.add(FIRST, d.folder[LABS], b"PRVY VYSLEDOK 01.03.2027")
    d.add(SECOND, d.folder[LABS], b"DRUHY VYSLEDOK 02.03.2027")
    return d


def _run(d, propose_fn=classify.propose, **opts):
    return run_curated(
        d,
        d.root,
        RunOptions(now=NOW, run_id="test-backfill", dry_run=False, **opts),
        extract_fn=fake_extract,
        propose_fn=propose_fn,
    )


def _first_fails(bad):
    """``bad`` for the request about document 1, a good proposal otherwise."""

    def handler(request):
        if "PRVY" in request.content.decode():
            return bad(request)
        return _ok(json.dumps(PROPOSAL))

    return handler


@pytest.mark.parametrize("name", sorted(ANSWER_FAILURES))
def test_backfill_skips_one_bad_answer_and_processes_the_next_document(
    name, local_llm, monkeypatch
):
    bad, code = ANSWER_FAILURES[name]
    _serve(monkeypatch, _first_fails(bad))
    d = _drive_with_two_unsidecared_files()
    r = _run(d)
    assert r.status == "ok", r.message
    labs = d.names(d.folder[LABS])
    assert f"{SECOND}.md" in labs  # document 2 processed
    assert f"{FIRST}.md" not in labs  # document 1 left as it is, retried next run
    by_source = {doc["source"]: doc for doc in r.documents}
    first = by_source[f"{LABS}/{FIRST}"]
    assert first["result"] == "skipped" and first["reasons"] == [code]
    assert first["attempts"] == 1 and first["stage"] == "backfill"
    assert by_source[f"{LABS}/{SECOND}"]["result"] == "sidecar_added"
    assert r.backfill_skipped == 1
    assert "local LLM" not in json.dumps(r.to_dict())  # codes only, never the message


@pytest.mark.parametrize("name", sorted(TRANSPORT_FAILURES))
def test_backfill_aborts_after_one_call_when_the_model_is_down(name, local_llm, monkeypatch):
    calls = _serve(monkeypatch, TRANSPORT_FAILURES[name])
    d = _drive_with_two_unsidecared_files()
    r = _run(d)
    assert r.status == "aborted" and "local model unavailable" in r.message
    assert len(calls) == 1  # no second document after the model is gone
    labs = d.names(d.folder[LABS])
    assert f"{FIRST}.md" not in labs and f"{SECOND}.md" not in labs


def test_backfill_aborts_on_an_untyped_model_error():
    """A LocalLLMError of unknown kind counts as down (fail closed)."""
    d = _drive_with_two_unsidecared_files()
    calls = []

    def untyped(contract, text):
        calls.append(text)
        raise llm.LocalLLMError("local LLM answer truncated")

    r = _run(d, untyped)
    assert r.status == "aborted" and len(calls) == 1


def test_backfill_retries_the_skipped_document_on_the_next_run():
    d = _drive_with_two_unsidecared_files()

    def first_truncated(contract, text):
        if "PRVY" in text:
            raise llm.LocalLLMAnswerError("local LLM answer truncated: context overflow")
        return dict(PROPOSAL)

    _run(d, first_truncated)
    r = _run(d, lambda contract, text: dict(PROPOSAL))
    assert r.status == "ok", r.message
    assert f"{FIRST}.md" in d.names(d.folder[LABS])


# ── retry cap ─────────────────────────────────────────────────────────


def _always_truncated(calls):
    def propose(contract, text):
        calls.append(text)
        if "PRVY" in text:
            raise llm.LocalLLMAnswerError("local LLM answer truncated: reasoning budget")
        return dict(PROPOSAL)

    return propose


def test_third_bad_answer_turns_into_review_with_attempts():
    d = _drive_with_two_unsidecared_files()
    first_id = d.find(d.folder[LABS], FIRST)
    prior = {first_id: {"attempts": MAX_TECHNICAL_ATTEMPTS - 1, "code": "model_answer_truncated"}}
    r = _run(d, _always_truncated([]), backfill_attempts=prior)
    entry = next(x for x in r.documents if x["source"] == f"{LABS}/{FIRST}")
    assert entry["result"] == "review"
    assert entry["reasons"] == ["model_answer_truncated"]
    assert entry["attempts"] == MAX_TECHNICAL_ATTEMPTS
    assert f"{LABS}/{FIRST}" in r.awaiting_review
    assert r.backfill_skipped == 1


def test_a_given_up_document_costs_no_model_call():
    d = _drive_with_two_unsidecared_files()
    first_id = d.find(d.folder[LABS], FIRST)
    prior = {first_id: {"attempts": MAX_TECHNICAL_ATTEMPTS, "code": "model_answer_not_json"}}
    calls = []
    r = _run(d, _always_truncated(calls), backfill_attempts=prior)
    assert not any("PRVY" in c for c in calls)  # never asked again
    entry = next(x for x in r.documents if x["source"] == f"{LABS}/{FIRST}")
    assert entry["result"] == "review" and entry["reasons"] == ["model_answer_not_json"]
    assert entry["attempts"] == MAX_TECHNICAL_ATTEMPTS
    assert r.backfill_skipped == 0
    assert f"{SECOND}.md" in d.names(d.folder[LABS])
    # nor does the loop treat it as pending work
    found = probe(d, d.root, now=NOW, backfill_given_up=frozenset({first_id}))
    assert first_id not in found.pending
    assert d.find(d.folder[LABS], SECOND) not in found.pending  # has its sidecar now


def test_attempts_accumulate_across_real_runs_through_the_reports(monkeypatch):
    """The tool keeps the counter in the run report history (no migration)."""
    d = _drive_with_two_unsidecared_files()
    first_id = d.find(d.folder[LABS], FIRST)
    pid = "patient-under-test"
    tool._backfill_failures.pop(pid, None)

    class FakeDB:
        reports: list[dict] = []

        async def list_curated_runs(self, *, patient_id, limit=20):
            return [{"id": i, "dry_run": False} for i in range(len(self.reports))]

        async def get_curated_run(self, row_id, *, patient_id):
            return {"report": self.reports[row_id]}

    db = FakeDB()
    calls: list = []
    results = []
    for _ in range(MAX_TECHNICAL_ATTEMPTS + 1):
        attempts = asyncio.run(tool.backfill_failures(db, pid))
        report = _run(d, _always_truncated(calls), backfill_attempts=attempts).to_dict()
        tool._remember_backfill_failures(pid, report)
        db.reports.append(report)
        results.append(next(x for x in report["documents"] if x["source"] == f"{LABS}/{FIRST}"))
    assert [x["attempts"] for x in results] == [
        *range(1, MAX_TECHNICAL_ATTEMPTS + 1),
        MAX_TECHNICAL_ATTEMPTS,
    ]
    assert [x["result"] for x in results] == ["skipped", "skipped", "review", "review"]
    assert sum("PRVY" in c for c in calls) == MAX_TECHNICAL_ATTEMPTS
    # a restart re-seeds the counter from the persisted reports
    tool._backfill_failures.pop(pid, None)
    seeded = asyncio.run(tool.backfill_failures(db, pid))
    assert seeded[first_id]["attempts"] == MAX_TECHNICAL_ATTEMPTS
    assert tool._backfill_given_up(seeded) == frozenset({first_id})
    tool._backfill_failures.pop(pid, None)


# ── proposal cache key ────────────────────────────────────────────────


@pytest.fixture
def counted_propose(monkeypatch):
    tool._propose_cache.clear()
    calls = []

    def propose(contract, text):
        calls.append(contract.readme_text)
        return dict(PROPOSAL)

    monkeypatch.setattr(classify, "propose", propose)
    yield calls
    tool._propose_cache.clear()


def test_cache_hits_when_the_readme_is_unchanged(counted_propose):
    contract = parse_contract(README)
    tool.cached_propose(contract, "VYSLEDOK 02.03.2027")
    tool.cached_propose(parse_contract(README), "VYSLEDOK 02.03.2027")
    assert len(counted_propose) == 1


def test_cache_misses_after_a_readme_edit_under_the_same_version(counted_propose):
    before = parse_contract(README)
    edited = parse_contract(README + "\n<!-- edited by the maintainer -->\n")
    assert edited.version == before.version
    tool.cached_propose(before, "VYSLEDOK 02.03.2027")
    tool.cached_propose(edited, "VYSLEDOK 02.03.2027")
    assert len(counted_propose) == 2


def test_cache_misses_after_the_run_adds_a_vocabulary_token(counted_propose):
    """The runner classifies later documents with the re-parsed contract after its
    own ``readme_vocabulary_add``; that contract carries the new README text."""
    before = parse_contract(README)
    after = dataclasses.replace(
        before, readme_text=before.readme_text + "\n- `Testovo` (printed: Testovo)\n"
    )
    tool.cached_propose(before, "VYSLEDOK 02.03.2027")
    tool.cached_propose(after, "VYSLEDOK 02.03.2027")
    assert len(counted_propose) == 2


def test_cache_misses_after_a_classify_prompt_change(counted_propose, monkeypatch):
    contract = parse_contract(README)
    tool.cached_propose(contract, "VYSLEDOK 02.03.2027")
    monkeypatch.setattr(classify, "PROMPT_VERSION", "0000000000000000")
    tool.cached_propose(contract, "VYSLEDOK 02.03.2027")
    assert len(counted_propose) == 2
