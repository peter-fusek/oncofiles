"""#854: automatic README vocabulary for curated folders.

A new institution / doctor printed in the document joins README §3 and the
document is filed; real ambiguity still goes to review. Synthetic data only:
invented names, institutions and dates (2027+).
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from oncofiles.curated import classify, vocabulary
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.drive_ops import WriteVerificationError
from oncofiles.curated.runner import CANARY, JOURNAL_MARK, RunOptions, probe, run_curated
from oncofiles.curated.vocabulary import (
    BLOCK_BEGIN,
    BLOCK_END,
    AutoVocabUnsupportedError,
    VocabAddition,
)
from tests.test_curated_runner import INBOX, LABS, REPORT, FakeDrive, fake_extract

README = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
CONTRACT = parse_contract(README)
TODAY = date(2027, 3, 1)
NOW = datetime.fromisoformat("2027-03-01T06:30:00").replace(tzinfo=UTC)

# Invented: a polyclinic and a doctor that are not in the fixture README §3.
TEXT = (
    "POLIKLINIKA JAVOROVÁ s.r.o.\n"
    "Ambulancia vnútorného lekárstva\n"
    "Pacientka: Jana Nováková\n"
    "Lekárska správa zo dňa 12.02.2027\n"
    "MUDr. Tomáš Brezina\n"
)
GOOD = {
    "doc_date": "2027-02-12",
    "doc_type": "Report",
    "institution": "PoliklinikaJavorova",
    "institution_printed": "Poliklinika Javorová",
    "doctor": "MUDr. Tomáš Brezina",
    "hospital": "Interna",
    "multiple_documents": False,
    "confidence": 0.9,
}


def validate(raw=None, text=TEXT, contract=CONTRACT, auto=True):
    return classify.validate(contract, raw or GOOD, text, today=TODAY, auto_vocab=auto)


# ── classify.validate ───────────────────────────────────────────────


def test_new_institution_and_doctor_printed_are_added_and_filed():
    c = validate()
    assert not c.review_needed, c.reasons
    assert c.fields.institution == "PoliklinikaJavorova"
    assert c.fields.doctor == "Brezina"
    assert [(a.label, a.token) for a in c.additions] == [
        ("Institution", "PoliklinikaJavorova"),
        ("Doctor", "Brezina"),
    ]
    assert c.additions[0].printed == "Poliklinika Javorová"
    assert any("added to README vocabulary" in f for f in c.flags)


def test_flag_off_keeps_the_754_review():
    c = validate(auto=False)
    assert c.review_needed and not c.additions
    assert any(
        "new institution 'PoliklinikaJavorova' not in README vocabulary" in r for r in c.reasons
    )
    assert any("new doctor 'Brezina' not in README vocabulary" in r for r in c.reasons)


def test_value_not_printed_in_the_text_is_never_added():
    # e.g. read off the filename or invented by the model
    raw = {**GOOD, "institution": "KlinikaDubova", "institution_printed": "Klinika Dubová"}
    c = validate(raw)
    assert c.fields.institution == "Unknown"
    assert not c.review_needed, c.reasons  # Unknown alone is no review reason any more
    assert [a.token for a in c.additions] == ["Brezina"]
    assert any("not printed in the text" in f for f in c.flags)


def test_camelcase_token_alone_is_not_a_printed_spelling():
    c = validate({**GOOD, "institution_printed": None})
    assert c.fields.institution == "Unknown"
    assert all(a.label != "Institution" for a in c.additions)


@pytest.mark.parametrize(
    "printed,text_line",
    [
        ("Sample Hospitál", "SAMPLE HOSPITÁL a.s."),  # same token after diacritic folding
        ("sample hospital", "Sample hospital a.s."),  # case only
    ],
)
def test_near_duplicate_uses_the_existing_token(printed, text_line):
    raw = {**GOOD, "institution": "SampleHospitalAS", "institution_printed": printed}
    c = validate(raw, TEXT + text_line)
    assert c.fields.institution == "SampleHospital"
    assert all(a.label != "Institution" for a in c.additions)
    assert not c.review_needed, c.reasons


def test_ocr_typo_of_a_printed_readme_token_snaps():
    raw = {**GOOD, "institution": "SampleHospitel", "institution_printed": "Sample Hospitel"}
    c = validate(raw, TEXT + "SampleHospital a.s.")
    assert c.fields.institution == "SampleHospital"
    assert all(a.label != "Institution" for a in c.additions)


def test_printed_near_neighbour_of_a_different_entity_goes_to_review():
    # #754: Hruskova is in §3, the text really prints Hruskovic — never merge,
    # never add a variant: a human decides.
    raw = {**GOOD, "doctor": "MUDr. Pavol Hruskovic"}
    c = validate(raw, TEXT + "MUDr. Pavol Hruskovic")
    assert c.review_needed and not c.additions
    assert any("close to README token 'Hruskova'" in r for r in c.reasons)


def test_patient_own_surname_as_doctor_is_not_added():
    text = TEXT + "Ošetrujúci lekár: MUDr. Nováková"
    c = validate({**GOOD, "doctor": "MUDr. Nováková"}, text)
    assert c.fields.doctor == "Unknown"
    assert not c.review_needed, c.reasons
    assert all(a.label != "Doctor" for a in c.additions)
    assert any("names the patient" in f for f in c.flags)


def test_word_built_on_the_patient_surname_is_not_added():
    text = TEXT + "Ambulancia Novák s.r.o."
    raw = {**GOOD, "institution": "AmbulanciaNovak", "institution_printed": "Ambulancia Novák"}
    c = validate(raw, text)
    assert c.fields.institution == "Unknown"
    assert all(a.label != "Institution" for a in c.additions)


def test_uncertain_reading_becomes_unknown_and_is_filed():
    text = TEXT.replace("Brezina", "Bre[?]ina")
    c = validate({**GOOD, "doctor": "MUDr. Tomáš Bre[?]ina"}, text)
    assert c.fields.doctor == "Unknown"
    assert not c.review_needed, c.reasons
    assert all(a.label != "Doctor" for a in c.additions)
    assert any("uncertain" in f for f in c.flags)


def test_name_printed_only_in_a_birth_context_is_not_added():
    text = (
        "Pacientka: Jana Nováková, narodená 03.02.1970, Nemocnica Lipová\n"
        "Lekárska správa zo dňa 12.02.2027\nMUDr. Tomáš Brezina"
    )
    raw = {**GOOD, "institution": "NemocnicaLipova", "institution_printed": "Nemocnica Lipová"}
    c = validate(raw, text)
    assert c.fields.institution == "Unknown"
    assert all(a.label != "Institution" for a in c.additions)


@pytest.mark.parametrize(
    "patch,text,reason",
    [
        ({"doc_date": None}, TEXT, "no valid clinical date"),
        ({"doc_date": "1970-02-03"}, TEXT + "03.02.1970", "birth date"),
        ({"doc_type": "Consultation"}, TEXT, "not in contract"),
        ({}, TEXT.replace("Pacientka: Jana", "Pacient: Marek"), "names another person"),
        ({"multiple_documents": True}, TEXT, "several documents"),
    ],
)
def test_real_ambiguity_still_goes_to_review_and_adds_nothing(patch, text, reason):
    c = validate({**GOOD, **patch}, text)
    assert c.review_needed
    assert any(reason in r for r in c.reasons), c.reasons
    assert c.additions == []


def test_prompt_asks_for_the_printed_institution():
    assert '"institution_printed"' in classify.SYSTEM_TEMPLATE


# ── vocabulary.add_to_readme ─────────────────────────────────────────

ADD = [
    VocabAddition("Institution", "PoliklinikaJavorova", 'Poliklinika `Javorová` <b>"x"'),
    VocabAddition("Doctor", "Brezina", "Brezina"),
]


def test_add_inserts_a_managed_block_at_the_end_of_section_3_only():
    text, after = vocabulary.add_to_readme(README, CONTRACT, ADD, run_id="r1", day="2027-03-01")
    assert after.known_tokens == CONTRACT.known_tokens | {"PoliklinikaJavorova", "Brezina"}
    assert BLOCK_BEGIN in text and BLOCK_END in text
    # pure insertion: everything outside the block is unchanged
    start = text.index("\n\n**Auto-added vocabulary**")
    end = text.index(BLOCK_END) + len(BLOCK_END)
    assert text[:start] + text[end:] == README
    assert text.index(BLOCK_END) < text.index("## 4.")
    line = next(ln for ln in text.splitlines() if "`PoliklinikaJavorova`" in ln)
    assert line.startswith("- 2027-03-01 auto-added by run r1: Institution")
    assert "`Javorová`" not in line and "<b>" not in line  # printed form sanitised


def test_second_add_appends_inside_the_existing_block():
    first, mid = vocabulary.add_to_readme(README, CONTRACT, ADD[:-1], run_id="ra", day="2027-03-01")
    second, after = vocabulary.add_to_readme(first, mid, ADD, run_id="rb", day="2027-03-02")
    assert second.count(BLOCK_BEGIN) == 1
    assert "Brezina" in after.known_tokens
    assert second.count("`PoliklinikaJavorova`") == 1  # already known: not repeated
    assert second.index("run rb") < second.index(BLOCK_END)


@pytest.mark.parametrize(
    "mutate,why",
    [
        (lambda t: t.replace("- **Standard version:** 1.0 — 2026-11-07 (test)\n", ""), "version"),
        (
            lambda t: (
                t + f"\n{BLOCK_BEGIN.replace('v1', 'v9')}\n<!-- /oncofiles:auto-vocabulary -->"
            ),
            "v9",
        ),
        (lambda t: t + f"\n{BLOCK_BEGIN}\n", "malformed"),
        (lambda t: t + f"\n{BLOCK_BEGIN}\n{BLOCK_END}\n", "outside"),
    ],
)
def test_unknown_readme_layout_is_refused(mutate, why):
    text = mutate(README)
    contract = parse_contract(text)
    reason = vocabulary.unsupported_reason(text, contract)
    assert reason and why in reason
    with pytest.raises(AutoVocabUnsupportedError):
        vocabulary.add_to_readme(text, contract, ADD, run_id="r", day="2027-03-01")


def test_flag_reads_the_environment(monkeypatch):
    monkeypatch.delenv("CURATED_AUTO_VOCAB", raising=False)
    assert vocabulary.enabled()
    monkeypatch.setenv("CURATED_AUTO_VOCAB", "false")
    assert not vocabulary.enabled()
    assert RunOptions().auto_vocab is False


def test_token_digest_hides_the_token():
    d = vocabulary.token_digest("PoliklinikaJavorova")
    assert d.startswith("len19:") and "Poliklinika" not in d


# ── runner ───────────────────────────────────────────────────────────

DOC = TEXT.encode()


def propose(contract, text):
    if "NO DATE" in text:
        return {**GOOD, "doc_date": None}
    if "LABS" in text:
        return {**GOOD, "doc_type": "Labs", "doc_date": "2027-02-14"}
    return dict(GOOD)


def _drive(*docs):
    d = FakeDrive()
    # same modified time for all: the inbox keeps insertion order
    for i, body in enumerate(docs):
        d.add(f"scan_{chr(97 + i)}.pdf", d.folder[INBOX], body)
    return d


def _run(d, **opts):
    o = RunOptions(now=NOW, run_id="run-854", **opts)
    return run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=propose)


def _readme(d):
    return d.download(d.find(d.root, "README.md")).decode()


def test_real_run_adds_vocabulary_then_files():
    second = (TEXT + "LABS 14.02.2027").encode()
    d = _drive(DOC, second)
    r = _run(d, dry_run=False)
    assert r.status == "ok", r.message
    name = "20270212-JanaNovakova-PoliklinikaJavorova-Report-Brezina-Interna.pdf"
    assert name in d.names(d.folder[REPORT])
    assert "20270214-JanaNovakova-PoliklinikaJavorova-Labs-Brezina-Interna.pdf" in d.names(
        d.folder[LABS]
    )
    readme = _readme(d)
    contract = parse_contract(readme)
    assert {"PoliklinikaJavorova", "Brezina"} <= contract.known_tokens
    assert 'printed as "Poliklinika Javorová"' in readme and "run run-854" in readme
    # the second document reused the new tokens: ONE README write for the run
    assert [w for w in d.writes if w[:2] == ("update", "README.md")] == [("update", "README.md")]
    assert [(v["token"], v["status"]) for v in r.vocabulary] == [
        ("PoliklinikaJavorova", "added"),
        ("Brezina", "added"),
    ]
    actions = [
        json.loads(x) for x in d.download(d.find(d.root, "_actions.jsonl")).decode().splitlines()
    ]
    vocab = [a for a in actions if a["action"] == "readme_vocabulary_add"]
    assert len(vocab) == 1 and vocab[0]["undo"]["op"] == "readme_vocabulary_remove"
    assert "Javorová" not in json.dumps(vocab, ensure_ascii=False)  # printed form: README only
    assert (
        "README vocabulary added: Institution `PoliklinikaJavorova`"
        in d.download(d.find(d.root, "_changelog.md")).decode()
    )


def test_journal_entry_precedes_the_readme_write():
    d = _drive(DOC)
    seen = {}
    original = d.update_text

    def spy(fid, text, mime="text/markdown"):
        if d.items[fid]["name"] == "README.md":
            canary = d.find(d.root, CANARY)
            seen["journal"] = d.download(canary).decode()
        original(fid, text, mime)

    d.update_text = spy
    r = _run(d, dry_run=False)
    assert r.status == "ok", r.message
    lines = seen["journal"].splitlines()
    assert lines[0] == f"run-854 {JOURNAL_MARK}"
    last = json.loads(lines[-1])
    assert last["action"] == "readme_vocabulary_add" and "seq" in last
    assert "PoliklinikaJavorova" in json.dumps(last["tokens"])


def test_dry_run_plans_the_vocabulary_and_writes_nothing():
    d = _drive(DOC)
    before = json.dumps(d.items, default=str, sort_keys=True)
    r = _run(d)
    assert r.status == "ok", r.message
    assert d.writes == [] and json.dumps(d.items, default=str, sort_keys=True) == before
    assert [(v["token"], v["status"]) for v in r.vocabulary] == [
        ("PoliklinikaJavorova", "planned"),
        ("Brezina", "planned"),
    ]
    assert r.documents[0]["result"] == "filed"
    assert "PoliklinikaJavorova" in r.documents[0]["to"]


def test_readme_write_failure_aborts_without_retry():
    d = _drive(DOC)
    attempts = []
    original = d.update_text

    def failing(fid, text, mime="text/markdown"):
        if d.items[fid]["name"] == "README.md":
            attempts.append(fid)
            raise WriteVerificationError("simulated read-back mismatch")
        original(fid, text, mime)

    d.update_text = failing
    r = _run(d, dry_run=False)
    assert r.status == "aborted" and "readme_vocabulary_add" in r.message
    assert len(attempts) == 1  # never retried
    assert _readme(d) == README
    # nothing filed, no sidecar written for the document
    assert d.names(d.folder[INBOX]) == {"scan_a.pdf"}


def test_readme_edited_during_the_run_is_never_overwritten():
    d = _drive(DOC)
    original = d.download
    readme_id = d.find(d.root, "README.md")
    calls = []

    def download(fid):
        if fid == readme_id:
            calls.append(fid)
            if len(calls) > 1:  # the run's own re-read right before the write
                return (README + "\nhand edit\n").encode()
        return original(fid)

    d.download = download
    r = _run(d, dry_run=False)
    assert r.status == "aborted"
    assert original(readme_id).decode() == README


def test_flag_off_keeps_documents_in_review():
    d = _drive(DOC)
    r = _run(d, dry_run=False, auto_vocab=False)
    assert r.status == "ok", r.message
    assert r.documents[0]["result"] == "review_needed"
    assert _readme(d) == README and r.vocabulary == []


def test_other_ambiguity_still_goes_to_review():
    d = _drive((TEXT + "NO DATE").encode())
    r = _run(d, dry_run=False)
    assert r.documents[0]["result"] == "review_needed"
    assert _readme(d) == README and r.vocabulary == []


def test_unsupported_readme_falls_back_to_review():
    d = _drive(DOC)
    no_version = README.replace("- **Standard version:** 1.0 — 2026-11-07 (test)\n", "")
    d.items[d.find(d.root, "README.md")]["content"] = no_version.encode()
    r = _run(d, dry_run=False)
    assert r.documents[0]["result"] == "review_needed"
    assert any("automatic vocabulary off" in f for f in r.flags)
    assert _readme(d) == no_version


def test_document_held_only_for_vocabulary_is_requeued():
    d = _drive(DOC)
    first = _run(d, dry_run=False, auto_vocab=False)  # the #754 behaviour
    assert first.documents[0]["result"] == "review_needed"
    assert probe(d, d.root, now=NOW).pending  # auto vocabulary on: pending again
    r = _run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert r.documents[0]["result"] == "filed"


def test_document_in_review_for_a_real_reason_stays():
    d = _drive((TEXT + "NO DATE").encode())
    _run(d, dry_run=False)
    assert not probe(d, d.root, now=NOW).pending
    r = _run(d, dry_run=False)
    assert r.documents == [] and r.awaiting_review


def test_text_source_gets_the_vocabulary_before_it_is_filed():
    d = FakeDrive()
    d.add("note.txt", d.folder[INBOX], DOC)
    r = _run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert r.documents[0]["result"] == "filed"
    assert "PoliklinikaJavorova" in parse_contract(_readme(d)).known_tokens
    readme_write = d.writes.index(("update", "README.md"))
    move = next(i for i, w in enumerate(d.writes) if w[0] == "move")
    assert readme_write < move
