"""Backfill survives one bad model answer; the proposal cache is keyed on the README.

Synthetic data only (public repo): invented names, institutions and 2027+ dates.
"""

import dataclasses
from datetime import UTC, datetime

import pytest

from oncofiles.curated import classify, llm, runner
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.runner import RunOptions, run_curated
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


def _drive_with_two_unsidecared_files():
    d = FakeDrive()
    d.add(FIRST, d.folder[LABS], b"PRVY VYSLEDOK 01.03.2027")
    d.add(SECOND, d.folder[LABS], b"DRUHY VYSLEDOK 02.03.2027")
    return d


def _run(d, propose_fn):
    return run_curated(
        d,
        d.root,
        RunOptions(now=NOW, run_id="test-backfill", dry_run=False),
        extract_fn=fake_extract,
        propose_fn=propose_fn,
    )


def _failing_first(message):
    def propose(contract, text):
        if "PRVY" in text:
            raise llm.LocalLLMError(message)
        return dict(PROPOSAL)

    return propose


@pytest.mark.parametrize(
    ("message", "code"),
    [
        (
            "local LLM answer truncated: reasoning used the whole budget (900 tokens)",
            "model_answer_truncated",
        ),
        ("local LLM answer is not valid JSON", "model_answer_not_json"),
        ("local LLM answer is not a JSON object", "model_answer_not_json"),
    ],
)
def test_backfill_continues_after_one_bad_answer(message, code):
    d = _drive_with_two_unsidecared_files()
    r = _run(d, _failing_first(message))
    assert r.status == "ok", r.message
    labs = d.names(d.folder[LABS])
    assert f"{SECOND}.md" in labs  # document 2 processed
    assert f"{FIRST}.md" not in labs  # document 1 left as it is, retried next run
    by_source = {doc["source"]: doc for doc in r.documents}
    assert by_source[f"{LABS}/{FIRST}"] == {
        "source": f"{LABS}/{FIRST}",
        "result": "skipped",
        "reasons": [code],
        "stage": "backfill",
    }
    assert by_source[f"{LABS}/{SECOND}"]["result"] == "sidecar_added"
    # the error text itself never lands in the report
    assert message not in repr(r.to_dict())


def test_backfill_retries_the_skipped_document_on_the_next_run():
    d = _drive_with_two_unsidecared_files()
    _run(d, _failing_first("local LLM answer truncated: context overflow (HTTP 400)"))
    r = _run(d, lambda contract, text: dict(PROPOSAL))
    assert r.status == "ok", r.message
    assert f"{FIRST}.md" in d.names(d.folder[LABS])


@pytest.mark.parametrize("message", ["local LLM unreachable: ConnectError", "local LLM HTTP 503"])
def test_backfill_still_aborts_when_the_model_is_down(message):
    d = _drive_with_two_unsidecared_files()
    calls = []

    def down(contract, text):
        calls.append(text)
        raise llm.LocalLLMError(message)

    r = _run(d, down)
    assert r.status == "aborted" and "local model unavailable" in r.message
    assert len(calls) == 1  # no second document after the model is gone
    labs = d.names(d.folder[LABS])
    assert f"{FIRST}.md" not in labs and f"{SECOND}.md" not in labs


def test_model_down_detection_keeps_its_meaning():
    assert runner._llm_down("local LLM unreachable: ConnectError")
    assert runner._llm_down("local LLM HTTP 500")
    assert runner._llm_down("local LLM returned an unexpected payload")
    assert not runner._llm_down("local LLM answer truncated: context overflow (HTTP 400)")
    assert not runner._llm_down("local LLM answer is not valid JSON")
    assert not runner._llm_down("local LLM answer is not a JSON object")
    assert not runner._llm_down(None)


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
