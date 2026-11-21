"""#919 item 4: filed documents held in review with no recognised reason.

A filed sidecar that says ``review_needed: true`` with empty or unrecognised
``review_reasons`` used to stay in "needs attention" forever: the runner never
revisits filed documents. The run now re-checks a few of them per run on their
kept sidecar text (``runner._recheck_stale_reviews``): every hard check passes
→ the flag is cleared; a check fails → its reason codes are written. Front-matter
keys only, body byte for byte, never a move or rename.

Synthetic data only (invented names, institutions and dates in 2027+).
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from oncofiles import document_stage
from oncofiles.curated import llm
from oncofiles.curated import runner as runner_mod
from oncofiles.curated.indexer import parse_sidecar
from oncofiles.curated.review_codes import codes_of
from oncofiles.curated.runner import RunOptions, run_curated
from tests.test_curated_runner import LABS, REPORT, FakeDrive, fake_extract

NOW = datetime.fromisoformat("2027-06-01T06:30:00+00:00")
GDOC = "application/vnd.google-apps.document"
NAME = "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"
BODY = "\n<!-- page 1 -->\nVYSLEDKY 12.03.2027\nKrvny obraz v norme.\n\n"

RAW = {
    "doc_date": "2027-03-12",
    "doc_type": "Labs",
    "institution": "NOU",
    "doctor": "MUDr. Jana Kováčová",
    "hospital": "OKB",
    "confidence": 0.95,
}


def propose(contract, text):
    """The model's reading of the kept text (deterministic, no LM Studio)."""
    if "UNPRINTED" in text:
        return {**RAW, "doc_date": "2027-02-11"}  # not printed in the text
    if "SPRAVA" in text:
        return {**RAW, "doc_type": "Report", "doc_date": "2027-04-02"}
    return dict(RAW)


def sidecar(*, body=BODY, review_needed=True, reasons=None, raw_front=None, **extra) -> bytes:
    """A Cowork-era sidecar: the flag, no (or unknown) reasons, a kept body."""
    front = {"source_file": "x", "patient": "Jana Nováková", "review_needed": review_needed}
    if reasons is not None:
        front["review_reasons"] = reasons
    front.update(extra)
    lines = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items())
    if raw_front:
        lines += "\n" + raw_front
    return f"---\n{lines}\n---{body}".encode()


def _filed(d, name=NAME, folder=LABS, content=b"PDF BYTES", **side):
    src = d.add(name, d.folder[folder], content)
    side_id = d.add(name + ".md", d.folder[folder], sidecar(**side))
    return src, side_id


def run(d, ids, **opts):
    o = RunOptions(now=NOW, run_id="stale-run", stale_review_ids=frozenset(ids), **opts)
    return run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=propose)


def _split(raw: bytes) -> tuple[str, str]:
    text = raw.decode()
    end = text.index("\n---", 4)
    return text[: end + 4], text[end + 4 :]


def _front(d, fid) -> dict:
    return parse_sidecar(d.items[fid]["content"].decode())[0]


# ── stale flag, every hard check passes → cleared ─────────────────────


def test_stale_flag_with_passing_checks_is_cleared_and_body_is_byte_identical():
    d = FakeDrive()
    src, side = _filed(d)
    before_src = dict(d.items[src])
    before_front, before_body = _split(d.items[side]["content"])
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    after_front, after_body = _split(d.items[side]["content"])
    assert after_body == before_body  # body byte for byte
    front = _front(d, side)
    assert front["review_needed"] is False
    assert front["review_cleared_by"] == "auto (#919)"
    assert front["review_cleared_at"] == "2027-06-01"
    # only those three keys changed; every other front-matter line is kept
    old_lines = before_front.splitlines()
    new_lines = after_front.splitlines()
    assert [ln for ln in new_lines if not ln.startswith("review_")] == [
        ln for ln in old_lines if not ln.startswith("review_")
    ]
    # the original source is untouched: same name, folder, bytes; never moved
    assert d.items[src] == before_src
    assert not [w for w in d.writes if w[0] == "move"]
    assert d.writes.count(("update", NAME + ".md")) == 1
    (act,) = [a for a in r.actions if a.get("kind") == "stale_review"]
    assert act["action"] == "update_sidecar_review_keys"
    assert act["outcome"] == "clear" and act["status"] == "done"
    assert r.stale_review["done"] == r.stale_review["planned"]
    assert r.stale_review["counts"]["cleared"] == 1
    changelog = d.items[d.find(d.root, "_changelog.md")]["content"].decode()
    assert "stale review flag cleared" in changelog
    # the read model now shows no review
    meta = {"source": "curated", "curated": {"review_reasons": []}, **front}
    assert document_stage.attention_reason(file_id="gdrive:x", structured_metadata=meta) is None


def test_the_action_is_journaled_in_the_canary_before_the_write():
    d = FakeDrive()
    src, side = _filed(d)
    seen = {}
    orig = d.update_text

    def spy(fid, text, mime="text/markdown"):
        if fid == side:
            canary = d.find(d.root, runner_mod.CANARY)
            seen["journal"] = d.items[canary]["content"].decode()
        orig(fid, text, mime)

    d.update_text = spy
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    assert '"action": "update_sidecar_review_keys"' in seen["journal"]
    assert '"kind": "stale_review"' in seen["journal"]


def test_unrecognised_reason_sentences_count_as_no_reason():
    d = FakeDrive()
    src, side = _filed(d, reasons=["checked by hand in an older tool"])
    assert codes_of(["checked by hand in an older tool"]) == ["other"]
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    assert _front(d, side)["review_needed"] is False


# ── a hard check fails → recognised codes written ─────────────────────


def test_failing_check_writes_recognised_reason_codes_and_keeps_the_flag():
    d = FakeDrive()
    src, side = _filed(d, body="\nUNPRINTED VYSLEDKY bez datumu\n")
    _, before_body = _split(d.items[side]["content"])
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    front = _front(d, side)
    assert front["review_needed"] is True
    assert front["review_reasons"] == ["date_not_printed"]
    assert "review_cleared_by" not in front
    assert _split(d.items[side]["content"])[1] == before_body
    (entry,) = r.stale_review["done"]
    assert entry["outcome"] == "reasons" and entry["codes"] == ["date_not_printed"]
    # the dashboard can now name the reason
    meta = {"source": "curated", "curated": {"review_reasons": codes_of(front["review_reasons"])}}
    assert document_stage.review_reason_codes(meta) == ["date_not_printed"]
    assert not document_stage.stale_review("gdrive:x", {**meta, "review_needed": True})


def test_empty_text_gets_the_no_text_code_without_a_model_call():
    d = FakeDrive()
    src, side = _filed(d, body="\n\n(no text extracted)\n")

    def never(contract, text):
        raise AssertionError("no model call without text")

    o = RunOptions(now=NOW, dry_run=False, stale_review_ids=frozenset([src]))
    r = run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=never)
    assert r.status == "ok", r.message
    assert _front(d, side)["review_reasons"] == ["no_text"]


def test_yaml_list_reasons_are_replaced_whole():
    d = FakeDrive()
    src, side = _filed(
        d, body="\nUNPRINTED text\n", raw_front="review_reasons:\n  - stary dovod\n  - iny"
    )
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    text = d.items[side]["content"].decode()
    assert "stary dovod" not in text and "  - iny" not in text
    assert _front(d, side)["review_reasons"] == ["date_not_printed"]


# ── bounded ───────────────────────────────────────────────────────────


def test_bounded_per_run_and_the_rest_waits():
    d = FakeDrive()
    ids = [
        _filed(d, name=f"2027031{i}-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf")[0] for i in range(7)
    ]
    r = run(d, ids, dry_run=False)
    assert r.status == "ok", r.message
    cap = runner_mod.STALE_REVIEW_MAX_PER_RUN
    assert cap == 5
    assert len(r.stale_review["done"]) == cap
    assert len(r.stale_review["deferred"]) == 7 - cap
    sidecar_updates = [w for w in d.writes if w[0] == "update" and w[1].endswith(".pdf.md")]
    assert len(sidecar_updates) == cap
    assert "stale review flags left for a later run (cap): 2" in (
        d.items[d.find(d.root, "_changelog.md")]["content"].decode()
    )


def test_cap_and_action_budget_both_bound_the_pass():
    d = FakeDrive()
    ids = [
        _filed(d, name=f"2027031{i}-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf")[0] for i in range(3)
    ]
    r = run(d, ids, dry_run=False, stale_review_max=1)
    assert len(r.stale_review["done"]) == 1 and len(r.stale_review["deferred"]) == 2
    r = run(d, ids[1:], dry_run=False, max_actions=1)  # one file action left
    assert len(r.stale_review["done"]) == 1 and len(r.stale_review["deferred"]) == 1


# ── dry run ───────────────────────────────────────────────────────────


def test_dry_run_plans_and_changes_nothing():
    d = FakeDrive()
    ok, _ = _filed(d)
    bad, _ = _filed(d, name="20270313-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf", body="\nUNPRINTED\n")
    before = json.dumps(d.items, default=str, sort_keys=True)
    r = run(d, [ok, bad])
    assert r.status == "ok", r.message
    assert json.dumps(d.items, default=str, sort_keys=True) == before
    assert d.writes == []
    outcomes = {p["source"].rsplit("/", 1)[1]: p["outcome"] for p in r.stale_review["planned"]}
    assert outcomes == {
        NAME: "clear",
        "20270313-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf": "reasons",
    }
    assert r.stale_review["done"] == []


# ── maintainer override and other things the pass leaves alone ────────


@pytest.mark.parametrize(
    "extra",
    [
        {"review_override": "checked: the chart is a printed table"},
        {"review_override": "ok", "review_override_status": "hard_checks_failed"},
    ],
)
def test_maintainer_override_is_respected(extra):
    d = FakeDrive()
    src, side = _filed(d, **extra)
    before = d.items[side]["content"]
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    assert d.items[side]["content"] == before
    assert r.stale_review["skipped"] == [{"source": f"{LABS}/{NAME}", "code": "override"}]


@pytest.mark.parametrize(
    "side, code",
    [
        ({"review_needed": False}, "resolved"),
        ({"reasons": ["no valid clinical date"]}, "resolved"),  # a recognised reason
        ({"sha256": "0" * 64}, "source_changed"),
    ],
)
def test_sidecars_that_are_not_stale_are_left_alone(side, code):
    d = FakeDrive()
    src, side_id = _filed(d, **side)
    before = d.items[side_id]["content"]
    r = run(d, [src], dry_run=False)
    assert r.status == "ok", r.message
    assert d.items[side_id]["content"] == before
    assert r.stale_review["skipped"] == [{"source": f"{LABS}/{NAME}", "code": code}]


def test_only_documents_the_read_model_names_are_rechecked():
    d = FakeDrive()
    _src, side = _filed(d)
    before = d.items[side]["content"]
    r = run(d, [], dry_run=False)
    assert d.items[side]["content"] == before
    assert r.stale_review["planned"] == [] and r.stale_review["skipped"] == []
    r = run(d, ["gone-id"], dry_run=False)
    assert r.stale_review["skipped"] == [{"file_id": "gone-id", "code": "not_found"}]


def test_text_note_and_missing_sidecar_are_skipped():
    d = FakeDrive()
    note = d.add("20270401-JanaNovakova-NOU-Report-Unknown-Unknown.md", d.folder[REPORT],
                 sidecar())  # fmt: skip
    d.items[note]["mime"] = "text/markdown"
    bare = d.add("20270402-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf", d.folder[LABS], b"X")
    r = run(d, [note, bare], dry_run=True)
    codes = {s["code"] for s in r.stale_review["skipped"]}
    assert codes == {"text_source", "no_sidecar"}


def test_model_answer_failure_writes_nothing_and_retries_later():
    d = FakeDrive()
    src, side = _filed(d)
    before = d.items[side]["content"]

    def bad(contract, text):
        raise llm.LocalLLMAnswerError("local LLM answer truncated")

    o = RunOptions(now=NOW, dry_run=False, stale_review_ids=frozenset([src]))
    r = run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=bad)
    assert r.status == "ok", r.message
    assert d.items[side]["content"] == before
    assert r.stale_review["skipped"][0]["code"] == "model_answer_truncated"


def test_google_doc_with_an_empty_sidecar_is_read_through_its_text_export():
    d = FakeDrive()
    title = "Sprava_kontrola_jar"
    doc = d.add(title, d.folder[REPORT], b"SPRAVA z kontroly 02.04.2027")
    d.items[doc]["mime"] = GDOC
    side = d.add(title + ".md", d.folder[REPORT], sidecar(body="\n\n(no text extracted)\n"))
    r = run(d, [doc], dry_run=False)
    assert r.status == "ok", r.message
    assert (doc, "text/plain") in d.exports
    assert _front(d, side)["review_needed"] is False
    assert d.items[doc]["name"] == title  # never renamed by this pass


# ── read model selection ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "meta, stale",
    [
        ({"review_needed": True, "curated": {"review_reasons": []}}, True),
        ({"review_needed": True, "curated": {"review_reasons": ["other"]}}, True),
        ({"review_needed": True, "curated": {}}, True),
        ({"review_needed": True, "curated": {"review_reasons": ["no_date"]}}, False),
        ({"review_needed": False, "curated": {}}, False),
        ({"review_needed": True, "curated": {"review_override": True}}, False),
        ({"source": "drive_sync", "review_needed": True}, False),  # not a curated row
    ],
)
def test_document_stage_stale_review(meta, stale):
    assert document_stage.stale_review("gdrive:synth-1", meta) is stale


async def test_db_lists_only_stale_review_ids_of_the_patient(db):
    from tests.helpers import PRIMARY_TEST_UUID, make_doc

    async def add(n, pid=PRIMARY_TEST_UUID, **meta):
        doc = await db.insert_document(
            make_doc(
                file_id=f"gdrive:synth-stale-{n}",
                filename=f"20270{n}01-JanaTestova-NOU-Labs-Unknown-Unknown.pdf",
                gdrive_id=f"synth-stale-{n}",
                document_date=date(2027, 4, 1),
            ),
            patient_id=pid,
        )
        await db.update_structured_metadata(
            doc.id, json.dumps({"source": "curated", "curated": {"index_version": 24}, **meta})
        )
        return doc

    await add(1, review_needed=True)
    await add(2, review_needed=True, curated={"review_reasons": ["no_date"]})
    await add(3, review_needed=False)
    deleted = await add(4, review_needed=True)
    await db.db.execute(
        "UPDATE documents SET deleted_at = '2027-05-01' WHERE id = ?", (deleted.id,)
    )
    await db.db.commit()
    assert await db.curated_stale_review_ids(patient_id=PRIMARY_TEST_UUID) == frozenset(
        {"synth-stale-1"}
    )
    assert await db.curated_stale_review_ids(patient_id="00000000-0000-4000-8000-0000000009b9") == (
        frozenset()
    )
