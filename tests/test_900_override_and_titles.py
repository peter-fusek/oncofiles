"""#900: maintainer override of a vision soft review + titles in doctor tokens.

Synthetic data only: invented names (Jana Vymyslená, Eva Smyšlená-Nová),
invented institutions and dates.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, date, datetime

import pytest

from oncofiles.curated import classify, extract, ocr_markers
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.naming import doctor_surname, has_title, surname_text, surname_token
from oncofiles.curated.runner import RunOptions, probe, review_override, run_curated
from tests.test_curated_runner import INBOX, LABS, README, FakeDrive
from tests.test_curated_runner import fake_propose as _base_propose

# ── 2. titles never reach a doctor token ────────────────────────────


@pytest.mark.parametrize(
    ("printed", "surname"),
    [
        ("MUDr. Mgr. Jana Vymyslená", "Vymyslená"),
        ("MUDr.Mgr.Jana Vymyslená", "Vymyslená"),  # glued with dots
        ("MUDrMgrJanaVymyslená", "Vymyslená"),  # glued without dots
        ("mudr. MGR. Jana Vymyslená", "Vymyslená"),  # case-insensitive
        ("Jana Vymyslená, PhD.", "Vymyslená"),  # title after the name (the #900 bug)
        ("Jana Vymyslená MUDr. Ph.D.", "Vymyslená"),
        ("Jana VymyslenáPhD", "Vymyslená"),
        ("prim. doc. MUDr. Jana Vymyslená, CSc., MPH", "Vymyslená"),
        ("Vymyslená, Jana (MUDr. Mgr., PhD.)", "Vymyslená"),  # sidecar form
        ("MUDr. Eva Smyšlená-Nová, MBA", "Smyšlená-Nová"),  # hyphenated surname
        ("PharmDr. RNDr. Eva Smyšlená", "Smyšlená"),
        ("MDDr. Bc. J. Vymyslená", "Vymyslená"),  # an initial is no surname
        ("MUDr. Mgr.", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_doctor_surname_strips_titles(printed, surname):
    assert doctor_surname(printed) == surname
    assert surname_text(printed) == surname  # one shared function


@pytest.mark.parametrize(
    ("printed", "token"),
    [
        ("MUDr. Mgr. Jana Vymyslená", "Vymyslena"),
        ("Jana Vymyslená, PhD.", "Vymyslena"),
        ("MUDr. Eva Smyšlená-Nová", "SmyslenaNova"),
        ("MUDR. JANA VYMYSLENÁ", "Vymyslena"),  # all caps → README spelling
        ("DRAHOŠ", "Drahos"),  # an all-caps surname is never cut as a glued "Dr"
    ],
)
def test_surname_token_has_no_title(printed, token):
    assert surname_token(printed) == token
    assert not has_title(surname_token(printed))


def test_has_title_detects_glued_titles():
    assert has_title("MUDrMgrJanaVymyslena")
    assert has_title("VymyslenaPhD")
    assert has_title("Mgr")
    assert not has_title("Vymyslena")
    assert not has_title("Drahos")
    assert not has_title("Kolumba")


VOCAB_README = README.replace(
    "**Known doctors:** `Kovacova`, `Hruskova`.",
    "**Known doctors:** `Kovacova`, `Hruskova`, `Vymyslena`.",
)
TODAY = date(2027, 3, 1)


def _text(doctor_line: str) -> str:
    return (
        "NOU Bratislava\nPacientka: Jana Nováková\n"
        f"Lekárska správa zo dňa 12.02.2027\n{doctor_line}\n"
    )


def _raw(doctor: str) -> dict:
    return {
        "doc_date": "2027-02-12",
        "doc_type": "Report",
        "institution": "NOU",
        "doctor": doctor,
        "hospital": "OKB",
        "multiple_documents": False,
        "confidence": 0.9,
    }


@pytest.mark.parametrize(
    "printed",
    [
        "MUDr. Mgr. Jana Vymyslená",
        "Jana Vymyslená, PhD.",
        "MUDr.Mgr.Jana Vymyslená, MPH",
        "MUDR. MGR. JANA VYMYSLENÁ",
    ],
)
@pytest.mark.parametrize("auto", [True, False])
def test_known_surname_matches_readme_and_adds_nothing(printed, auto):
    contract = parse_contract(VOCAB_README)
    c = classify.validate(contract, _raw(printed), _text(printed), today=TODAY, auto_vocab=auto)
    assert not c.review_needed, c.reasons
    assert c.fields.doctor == "Vymyslena"
    assert c.additions == []


def test_new_doctor_adds_the_surname_only():
    contract = parse_contract(README)  # Vymyslena not in §3
    printed = "MUDr. Mgr. Jana Vymyslená, PhD."
    c = classify.validate(contract, _raw(printed), _text(printed), today=TODAY, auto_vocab=True)
    assert not c.review_needed, c.reasons
    assert c.fields.doctor == "Vymyslena"
    assert [(a.label, a.token, a.printed) for a in c.additions] == [
        ("Doctor", "Vymyslena", "Vymyslená")
    ]


def test_new_hyphenated_doctor_keeps_both_parts():
    contract = parse_contract(README)
    printed = "MUDr. Eva Smyšlená-Nová, MBA"
    c = classify.validate(contract, _raw(printed), _text(printed), today=TODAY, auto_vocab=True)
    assert not c.review_needed, c.reasons
    assert c.fields.doctor == "SmyslenaNova"
    assert [a.token for a in c.additions if a.label == "Doctor"] == ["SmyslenaNova"]


def test_a_token_with_a_title_is_never_filed_or_added(monkeypatch):
    contract = parse_contract(README)
    monkeypatch.setattr(classify, "surname_token", lambda v: "MUDrMgrJanaVymyslena")
    monkeypatch.setattr(classify, "surname_text", lambda v: "MUDrMgrJanaVymyslena")
    printed = "MUDrMgrJanaVymyslena"
    c = classify.validate(contract, _raw(printed), _text(printed), today=TODAY, auto_vocab=True)
    assert c.fields.doctor == "Unknown"
    assert not [a for a in c.additions if a.label == "Doctor"]
    assert any("still carries a title" in f for f in c.flags)


# ── 1. maintainer override of a rotated region / vision soft review ──

SCAN = b"%PDF synthetic scan bytes"
BODY = (
    "VYSLEDKY z 12.02.2027\n"
    "Hemoglobin 130 g/l\n"
    "[rotated region]\n"
    "[chart: Trend hemoglobinu; os 100-160]\n"
)
REASON = "rotated stamp on p. 1 ignored, maintainer 2027-01-05"
NOW = datetime(2027, 4, 18, 9, 15, tzinfo=UTC)


def fake_propose(contract, text):
    return {**_base_propose(contract, text), "doc_date": "2027-02-12"}


def _sidecar(
    body=BODY, *, override=True, review_needed=True, note=None, reasons=None, sha=None
) -> bytes:
    sha = sha or hashlib.sha256(SCAN).hexdigest()
    reasons = reasons if reasons is not None else ["rotated region on page 1: not read"]
    lines = [
        'source_file: "scan_001.pdf"',
        'patient: "Jana Nováková"',
        "pages: 1",
        'extraction: "vision"',
        'ocr_engine: "qwen-test"',
        "rotated_region: true",
        f'sha256: "{sha}"',
        f"review_needed: {json.dumps(review_needed)}",
        f"review_reasons: {json.dumps(reasons)}",
    ]
    if note is not None:
        lines.append(f"review_note: {json.dumps(note)}")
    if override:
        lines.append(f"review_override: {json.dumps(REASON)}")
    return ("---\n" + "\n".join(lines) + "\n---\n\n" + body).encode()


def _no_ocr(*a, **k):
    raise AssertionError("an overridden document must never be re-OCR'd")


def _drive(sidecar: bytes) -> FakeDrive:
    d = FakeDrive()
    d.add("scan_001.pdf", d.folder[INBOX], SCAN)
    d.add("scan_001.pdf.md", d.folder[INBOX], sidecar)
    return d


def _run(d, *, dry_run=False, extract_fn=_no_ocr, propose_fn=fake_propose):
    o = RunOptions(now=NOW, run_id="t900", dry_run=dry_run)
    return run_curated(d, d.root, o, extract_fn=extract_fn, propose_fn=propose_fn)


FILED = "20270212-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"


def test_review_override_rule():
    assert review_override(_sidecar().decode()) == REASON
    assert review_override(_sidecar(override=False).decode()) is None
    # backward compatible: review_needed false + a non-empty review_note
    legacy = _sidecar(override=False, review_needed=False, note="stamp only").decode()
    assert review_override(legacy) == "stamp only"
    assert review_override(_sidecar(override=False, review_needed=False).decode()) is None
    empty = _sidecar(override=False, review_needed=False, note="").decode()
    assert review_override(empty) is None
    plain = _sidecar(override=False).decode() + ""
    plain = plain.replace("---\n\n", f"review_override: {REASON}\n---\n\n", 1)
    assert review_override(plain) == REASON  # an unquoted value works too


def test_override_files_the_document_and_keeps_the_sidecar(caplog):
    original = _sidecar()
    d = _drive(original)
    with caplog.at_level(logging.DEBUG):
        r = _run(d)
    assert r.status == "ok", r.message
    doc = next(x for x in r.documents if x["source"] == "scan_001.pdf")
    assert doc["result"] == "filed" and doc["to"] == f"{LABS}/{FILED}"
    assert doc["review_override"] is True and doc["override_dropped"] == ["rotated_region"]
    # the sidecar moved with the source, byte for byte (override + reason kept)
    side = d.items[d.find(d.folder[LABS], FILED + ".md")]["content"]
    assert side == original
    assert f"review_override: {json.dumps(REASON)}".encode() in side
    # no sidecar rewrite was planned or done
    assert not [a for a in r.actions if a["action"] in ("update_sidecar", "write_sidecar")]
    assert not [w for w in d.writes if w[0] == "update" and w[1].endswith(".pdf.md")]
    ov = [a for a in r.actions if a["action"] == "review_override"]
    assert [a["reason"] for a in ov] == [REASON]
    assert [a["outcome"] for a in ov] == ["file"]
    # the journal keeps the reason; the server log only says an override was used
    assert REASON not in caplog.text
    assert "review override used" in caplog.text
    # a chart stays fenced in the kept text, so the parsers still skip it
    assert "[chart:" in side.decode()
    assert "os 100-160" not in ocr_markers.strip_charts(side.decode())


def test_override_dry_run_shows_the_plan_and_writes_nothing():
    d = _drive(_sidecar())
    before = json.dumps(d.items, default=str, sort_keys=True)
    r = _run(d, dry_run=True)
    assert r.status == "ok", r.message
    assert d.writes == [] and json.dumps(d.items, default=str, sort_keys=True) == before
    assert [a["action"] for a in r.actions] == ["review_override", "move_pair"]
    assert r.actions[1]["to"] == [f"{LABS}/{FILED}", f"{LABS}/{FILED}.md"]
    doc = r.documents[0]
    assert doc["result"] == "filed" and doc["review_override"] is True


def test_override_still_applies_hard_checks_date_not_printed():
    body = "VYSLEDKY bez datumu\n[rotated region]\n"
    original = _sidecar(body)
    d = _drive(original)
    r = _run(d)
    assert r.status == "ok", r.message
    doc = r.documents[0]
    assert doc["result"] == "review_needed" and doc["review_override"] is True
    assert any("does not appear in the document text" in x for x in doc["reasons"])
    assert not any(x.startswith("rotated region") for x in doc["reasons"])
    sid = d.find(d.folder[INBOX], "scan_001.pdf.md")
    assert sid and d.find(d.folder[INBOX], "scan_001.pdf")
    side = d.items[sid]["content"].decode()
    assert side.endswith("\n---\n\n" + body)  # body byte for byte
    assert "review_needed: true" in side and "review_override_status" in side
    assert f"review_override: {json.dumps(REASON)}" in side
    assert not [a for a in r.actions if a["action"] == "update_sidecar"]
    # parked for a person: the next run and the probe leave it alone
    r2 = _run(d)
    assert r2.awaiting_review == [f"{INBOX}/scan_001.pdf"] and r2.documents == []
    assert not probe(d, d.root, now=NOW).pending


def test_override_still_applies_hard_checks_other_person():
    d = _drive(_sidecar("VYSLEDKY z 12.02.2027\nPacient: Tomáš Novák\n"))
    r = _run(d)
    doc = r.documents[0]
    assert doc["result"] == "review_needed"
    assert any("names another person" in x for x in doc["reasons"])


def test_override_still_applies_hard_checks_low_confidence_and_split():
    def propose(contract, text):
        return {**fake_propose(contract, text), "confidence": 0.2, "multiple_documents": True}

    d = _drive(_sidecar())
    doc = _run(d, propose_fn=propose).documents[0]
    assert doc["result"] == "review_needed"
    assert any("several documents" in x for x in doc["reasons"])
    assert any("confidence" in x for x in doc["reasons"])


def test_legacy_review_needed_false_with_note_is_an_override():
    original = _sidecar(override=False, review_needed=False, note="stamp, ignore it")
    d = _drive(original)
    r = _run(d)
    assert r.documents[0]["result"] == "filed"
    assert d.items[d.find(d.folder[LABS], FILED + ".md")]["content"] == original


def test_override_is_pending_for_the_probe():
    d = _drive(_sidecar())
    assert probe(d, d.root, now=NOW).pending


def test_without_override_todays_behaviour_reocr_and_review():
    calls = []

    def vision(name, content, mime="", *, min_text_chars=100):
        calls.append(name)
        return extract.Extraction(
            BODY, "vision", 1, "qwen-test", rotation=[0], rotation_source=["model"],
            rotated_region_pages=[1], ocr_version=extract.OCR_VERSION,
        )  # fmt: skip

    # review_needed: false without a note is NOT an override: the file is read again
    d = _drive(_sidecar(override=False, review_needed=False))
    r = _run(d, extract_fn=vision)
    assert calls == ["scan_001.pdf"]
    doc = r.documents[0]
    assert doc["result"] == "review_needed" and "review_override" not in doc
    assert any(x.startswith("rotated region on page 1") for x in doc["reasons"])
    assert [a["action"] for a in r.actions] == ["update_sidecar"]


def test_override_is_void_when_the_source_changed():
    calls = []

    def text_layer(name, content, mime="", *, min_text_chars=100):
        calls.append(name)
        return extract.Extraction(BODY, "text_layer", 1, "test")

    d = _drive(_sidecar(sha="0" * 64))
    r = _run(d, extract_fn=text_layer)
    assert calls == ["scan_001.pdf"]  # read again: the override was for other bytes
    assert "review_override" not in r.documents[0]
