"""#919 item 4: month-only dates as ``YYYYMM00`` + ``doc_date_precision: month``.

Maintainer decision 2026-11-21: a document whose printed date has only a month
and a year gets the filename date ``YYYYMM00`` and the front-matter key
``doc_date_precision: month``. Synthetic data only (invented names, 2027+).
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from oncofiles import document_stage
from oncofiles.curated import classify, indexer, normalise
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.naming import (
    NameFields,
    build_filename,
    date_precision,
    is_standard,
    iso_of,
    month_token,
    parse_filename,
)
from oncofiles.curated.runner import CANARY, RunOptions, run_curated
from oncofiles.tools.hygiene import _build_document_matrix
from tests.helpers import PRIMARY_TEST_UUID, make_doc
from tests.test_919_normalise_filed import run, sidecar
from tests.test_curated_runner import (
    INBOX,
    LABS,
    README,
    REPORT,
    FakeDrive,
    JournalCheckingDrive,
    fake_extract,
)

CONTRACT = parse_contract(README)  # README birth date: 1970-02-03
TODAY = date(2027, 6, 1)
BASE = "JanaNovakova-NOU-Labs-Kovacova-OKB"

# ── naming: one date rule for builder and checker ────────────────────


@pytest.mark.parametrize(
    "token, precision",
    [
        ("20270312", "day"),
        ("20270300", "month"),
        ("20271200", "month"),
        ("20270100", "month"),
        ("20270000", None),  # never a 00 month
        ("20270012", None),  # a day without a month
        ("20271300", None),
        ("20271301", None),
        ("20270230", None),  # not a calendar day
        ("00000000", None),
        ("2027030", None),
        ("202703", None),
        ("Unknown", None),
        ("", None),
    ],
)
def test_date_precision_accepts_only_a_day_or_yyyymm00(token, precision):
    assert date_precision(token) == precision


@pytest.mark.parametrize(
    "date_token, standard",
    [
        ("20270300", True),
        ("20270312", True),
        ("20270000", False),
        ("20270012", False),
        ("20271300", False),
        ("20270231", False),
    ],
)
def test_checker_accepts_yyyymm00_and_rejects_other_partial_dates(date_token, standard):
    assert is_standard(f"{date_token}-{BASE}.pdf") is standard
    assert (parse_filename(f"{date_token}-{BASE}.pdf") is not None) is standard


@pytest.mark.parametrize(
    "date_token", ["20270300", "20270312", "20271100", "20270000", "20271300", "20270230", "2027"]
)
def test_builder_and_checker_agree_on_every_date(date_token):
    fields = NameFields(date_token, "JanaNovakova", "NOU", "Labs", "Kovacova", "OKB")
    try:
        built = build_filename(fields, "pdf")
    except ValueError:
        assert date_precision(date_token) is None
        assert not is_standard(f"{date_token}-{BASE}.pdf")
        return
    assert is_standard(built)
    assert parse_filename(built).date == date_token


def test_month_token_and_iso():
    assert month_token("2027-03") == "20270300"
    assert iso_of("20270300") == "2027-03"
    assert iso_of("20270312") == "2027-03-12"
    assert iso_of("20270000") is None
    for bad in ("2027-00", "2027-13", "2027-3", "2027-03-01"):
        with pytest.raises(ValueError):
            month_token(bad)


# ── printed month-year ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "text, printed",
    [
        ("Poznamky 03/2027", True),
        ("Poznamky 3/2027", True),
        ("Poznamky 3.2027", True),
        ("Obdobie: 2027-03", True),
        ("Marec 2027: poznamky", True),
        ("brezen 2027", True),  # Czech
        ("Kontrola 15.03.2027", False),  # only as part of a full day
        ("Kontrola 15. 03. 2027", False),
        ("Kontrola 2027-03-15", False),
        ("Kontrola 5. marca 2027", False),
        ("Poznamky 04/2027", False),
        ("Poznamky 13/2027", False),
        ("narodena 03/2027", False),  # birth context
    ],
)
def test_month_in_text(text, printed):
    assert classify.month_in_text("2027-03", text) is printed


def _raw(doc_date):
    return {
        "doc_date": doc_date,
        "doc_type": "Labs",
        "institution": "NOU",
        "doctor": "MUDr. Jana Kováčová",
        "hospital": "OKB",
        "confidence": 0.95,
    }


@pytest.mark.parametrize("doc_date", ["2027-03", "2027-03-00"])
def test_validate_accepts_a_printed_month_year(doc_date):
    cls = classify.validate(CONTRACT, _raw(doc_date), "Poznamky marec 2027", today=TODAY)
    assert not cls.review_needed, cls.reasons
    assert cls.fields.date == "20270300"
    assert build_filename(cls.fields, "pdf") == f"20270300-{BASE}.pdf"


def test_validate_month_must_be_printed_on_its_own():
    cls = classify.validate(CONTRACT, _raw("2027-03"), "Kontrola 15.03.2027", today=TODAY)
    assert cls.review_needed and cls.fields is None
    assert any("not printed on its own" in r for r in cls.reasons)


@pytest.mark.parametrize(
    "doc_date, text, why",
    [
        ("1970-02", "Narodena 03.02.1970, poznamky 02/1970", "birth month"),
        ("1970-02", "poznamky 02/1970", "birth month"),  # even printed on its own
        ("2027-09", "plan 09/2027", "in the future"),
        ("1899-05", "05/1899", "implausible"),
        ("2027-00", "00/2027", "no valid clinical date"),
        ("2027-13", "13/2027", "no valid clinical date"),
    ],
)
def test_validate_month_guards(doc_date, text, why):
    cls = classify.validate(CONTRACT, _raw(doc_date), text, today=TODAY)
    assert cls.review_needed and cls.fields is None
    assert any(why in r for r in cls.reasons), cls.reasons


def test_birth_date_guard_still_holds_for_a_full_day():
    cls = classify.validate(CONTRACT, _raw("1970-02-03"), "Narodena 03.02.1970", today=TODAY)
    assert cls.fields is None
    assert any("birth date" in r for r in cls.reasons)


def test_month_in_birth_context_is_not_printed():
    cls = classify.validate(CONTRACT, _raw("2027-03"), "narodeny 03/2027", today=TODAY)
    assert cls.fields is None


def test_verify_metadata_accepts_month_evidence():
    raw = {"date_evidence": "marec 2027"}
    out, codes = classify.verify_metadata(raw, "Poznamky marec 2027", "2027-03-00")
    assert out["date_evidence"] == "marec 2027" and codes == []


# ── inbox: sidecar front matter ──────────────────────────────────────


def test_inbox_month_document_is_filed_as_yyyymm00_with_precision_key():
    d = FakeDrive()
    d.add("scan_poznamky.pdf", d.folder[INBOX], b"Poznamky marec 2027", modified="2027-05-01T0")

    def propose(contract, text):
        return _raw("2027-03")

    o = RunOptions(now=datetime.fromisoformat("2027-06-01T06:30:00+00:00"), dry_run=False)
    r = run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=propose)
    assert r.status == "ok", r.message
    name = f"20270300-{BASE}.pdf"
    assert name in d.names(d.folder[LABS])
    side = d.items[d.find(d.folder[LABS], name + ".md")]["content"].decode()
    front, _ = indexer.parse_sidecar(side)
    assert front["doc_date"] == "2027-03"
    assert front["doc_date_precision"] == "month"


def test_day_sidecar_has_no_precision_key():
    d = FakeDrive()
    d.add("scan_den.pdf", d.folder[INBOX], b"Vysledky 12.03.2027", modified="2027-05-01T0")
    o = RunOptions(now=datetime.fromisoformat("2027-06-01T06:30:00+00:00"), dry_run=False)
    r = run_curated(
        d, d.root, o, extract_fn=fake_extract, propose_fn=lambda c, t: _raw("2027-03-12")
    )
    assert r.status == "ok", r.message
    side = d.items[d.find(d.folder[LABS], f"20270312-{BASE}.pdf.md")]["content"].decode()
    assert "doc_date_precision" not in side


# ── normalise: filed month-only notes ────────────────────────────────

NOTE = "2027-03-Advocate-Poznamky.md"
NOTE_BODY = b"Marec 2027\n\n- otazky na kontrolu\n- lieky bez zmeny\n"
NOTE_TARGET = "20270300-JanaNovakova-Unknown-Report-Unknown-Unknown.md"


def _note_drive(body=NOTE_BODY, name=NOTE):
    d = FakeDrive()
    note = d.add(name, d.folder[REPORT], body)
    d.items[note]["mime"] = "text/markdown"
    return d, note


class KeyJournalDrive(JournalCheckingDrive):
    """Moves (parent class) and front-matter writes must be journaled first."""

    key_write_journaled = False

    def update_text(self, fid, text, mime="text/markdown"):
        if self.items[fid]["name"] != CANARY:
            journal = self.download(self.find(self.root, CANARY)).decode()
            assert '"set_date_precision"' in journal, "key write not journaled first"
            self.key_write_journaled = True
        super().update_text(fid, text, mime)


def test_normalise_renames_a_month_only_note_journaled_with_precision_keys():
    d, note = _note_drive()
    d.__class__ = KeyJournalDrive  # every move and key write journaled first
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert d.items[note]["name"] == NOTE_TARGET
    assert d.items[note]["parent"] == d.folder[REPORT]
    (rename,) = [a for a in r.actions if a["action"] == "normalise_rename"]
    assert rename["status"] == "done" and rename["to"] == [f"{REPORT}/{NOTE_TARGET}"]
    (keys,) = [a for a in r.actions if a["action"] == "set_date_precision"]
    assert keys["status"] == "done" and keys["path"] == f"{REPORT}/{NOTE_TARGET}"
    (entry,) = r.normalise["renamed"]
    assert entry["date_precision"] == "month" and entry["precision_keys"] == "written"
    assert entry["date_source"] == "filename"
    content = d.items[note]["content"]
    assert content.startswith(b'---\ndoc_date: "2027-03"\ndoc_date_precision: "month"\n---\n')
    assert content.endswith(NOTE_BODY)  # body byte for byte
    assert content[len(content) - len(NOTE_BODY) :] == NOTE_BODY
    front, pages = indexer.parse_sidecar(content.decode())
    assert front == {"doc_date": "2027-03", "doc_date_precision": "month"}
    assert pages == [NOTE_BODY.decode().strip()]
    assert d.key_write_journaled  # the canary held the action before the write


def test_normalise_keeps_existing_front_matter_and_body_bytes():
    body = b'---\nauthor: "opatrovatel"\ndoc_date: null\n---\n\nMarec 2027: poznamky\n'
    d, note = _note_drive(body)
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    content = d.items[note]["content"].decode()
    assert content == (
        '---\nauthor: "opatrovatel"\ndoc_date: "2027-03"\ndoc_date_precision: "month"\n---'
        "\n\nMarec 2027: poznamky\n"
    )


def test_normalise_writes_precision_into_a_sidecar_pair():
    d = FakeDrive()
    d.add("poznamky_marec.pdf", d.folder[LABS], b"PDF")
    side = d.add(
        "poznamky_marec.pdf.md",
        d.folder[LABS],
        sidecar("2027-03", "NOU", None, "Kontroly marec 2027"),
    )
    d.items[side]["mime"] = "text/markdown"
    before = d.items[side]["content"].decode()
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    target = "20270300-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf"
    assert d.items[side]["name"] == target + ".md"
    after = d.items[side]["content"].decode()
    assert indexer.parse_sidecar(after)[1] == indexer.parse_sidecar(before)[1]  # same body
    front, _ = indexer.parse_sidecar(after)
    assert front["doc_date"] == "2027-03" and front["doc_date_precision"] == "month"
    assert r.normalise["renamed"][0]["date_source"] == "front_matter"


def test_normalise_dry_run_plans_the_month_rename_and_writes_nothing():
    d, note = _note_drive()
    r = run(d)
    assert d.writes == []
    assert d.items[note]["content"] == NOTE_BODY
    (entry,) = r.normalise["planned"]
    assert entry["to"] == NOTE_TARGET and entry["date_precision"] == "month"
    assert r.normalise["counts"]["month_precision"] == 1


def test_month_only_note_without_a_printed_month_waits_for_review():
    d, note = _note_drive(b"- otazky na kontrolu 12.03.2027\n")
    r = run(d, dry_run=False)
    assert d.items[note]["name"] == NOTE
    assert r.normalise["review"] == [
        {"source": f"{REPORT}/{NOTE}", "code": "name_not_standard", "why": "no_printed_date"}
    ]


def test_a_printed_day_wins_over_the_month():
    p = normalise.plan_name(
        name=NOTE,
        mime="text/markdown",
        doc_type="Report",
        front={"doc_date": "2027-03-12"},
        text="Marec 2027, kontrola 12.03.2027",
        contract=CONTRACT,
        today=TODAY,
        taken=set(),
    )
    assert p.target.startswith("20270312-") and p.precision == "day"


def test_with_month_keys_refuses_an_unclosed_front_matter_block():
    assert normalise.with_month_keys("---\nbroken: yes\nno end", "2027-03") is None
    done = '---\ndoc_date: "2027-03"\ndoc_date_precision: "month"\n---\nx'
    assert normalise.with_month_keys(done, "2027-03") is None  # already present


# ── read model: index + document_stage + dashboard ───────────────────


def test_index_stores_month_precision_and_no_fake_day():
    d, _ = _note_drive(name=NOTE_TARGET)
    run(d, dry_run=False)  # already standard: nothing to do, the name carries it
    s = indexer.scan(d, d.root)
    (rec,) = [x for x in s.records if x.filename == NOTE_TARGET]
    assert rec.document_date is None
    assert rec.metadata["doc_date_precision"] == "month"
    assert rec.metadata["doc_month"] == "2027-03"


def test_index_reads_month_from_front_matter():
    d = FakeDrive()
    name = "20270300-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf"
    d.add(name, d.folder[LABS], b"PDF")
    d.add(name + ".md", d.folder[LABS], sidecar("2027-03", "NOU", doc_date_precision="month"))
    (rec,) = indexer.scan(d, d.root).records
    assert rec.document_date is None and rec.metadata["doc_month"] == "2027-03"


def test_index_day_documents_are_unchanged():
    d = FakeDrive()
    name = f"20270312-{BASE}.pdf"
    d.add(name, d.folder[LABS], b"PDF")
    d.add(name + ".md", d.folder[LABS], sidecar("2027-03-12", "NOU"))
    (rec,) = indexer.scan(d, d.root).records
    assert rec.document_date == date(2027, 3, 12)
    assert "doc_date_precision" not in rec.metadata and "doc_month" not in rec.metadata


def test_index_version_bumped_for_month_precision():
    assert indexer.INDEX_VERSION >= 25


def _month_meta() -> str:
    return json.dumps(
        {
            "source": "curated",
            "folder": "report",
            "curated": {"sidecar_id": "synth-sidecar", "index_version": 25},
            "doc_date_precision": "month",
            "doc_month": "2027-03",
        }
    )


def test_document_stage_month_row_is_not_missing_date():
    meta = _month_meta()
    assert document_stage.document_month("gdrive:synth-1", meta) == "2027-03"
    assert document_stage.document_month("file_synth_1", meta) is None  # curated only
    out = document_stage.describe(
        filename=NOTE_TARGET,
        file_id="gdrive:synth-1",
        structured_metadata=meta,
        patient_id=PRIMARY_TEST_UUID,
        has_text=True,
        extractable=False,
        gdrive_id="synth-1",
        has_date=False,
        mime_type="text/markdown",
    )
    assert out["attention_reason"] is None and out["ready"]
    # without the month the same row is missing_date
    no_month = json.dumps({**json.loads(meta), "doc_date_precision": None})
    out = document_stage.describe(
        filename=NOTE_TARGET,
        file_id="gdrive:synth-1",
        structured_metadata=no_month,
        patient_id=PRIMARY_TEST_UUID,
        has_text=True,
        extractable=False,
        gdrive_id="synth-1",
        has_date=False,
        mime_type="text/markdown",
    )
    assert out["attention_reason"] == "missing_date"


async def test_dashboard_row_shows_month_precision(db):
    doc = await db.insert_document(
        make_doc(
            file_id="gdrive:synth-month-1",
            filename=NOTE_TARGET,
            original_filename=NOTE_TARGET,
            document_date=None,
            gdrive_id="synth-month-1",
            mime_type="text/markdown",
        ),
        patient_id=PRIMARY_TEST_UUID,
    )
    await db.update_structured_metadata(doc.id, _month_meta())
    await db.save_ocr_page(doc.id, 1, "synthetic note text", "curated-sidecar")
    result = await _build_document_matrix(db, patient_id=PRIMARY_TEST_UUID)
    row = next(r for r in result["documents"] if r["id"] == doc.id)
    assert row["date"] == "2027-03"  # the "YYYY-MM" shape is the month precision
    assert row["has_date"] is True
    assert row["attention_reason"] is None


def test_dashboard_js_formats_a_month_without_a_day():
    from tests.test_dashboard_redesign import _function

    src = _function("fmtDate")
    assert "-(\\d{2})$/" in src  # a YYYY-MM value is matched first
    assert "month[2] + '/' + month[1]" in src  # and shown as MM/YYYY, never with a day


def test_reocr_fact_comparison_ignores_a_month_doc_date():
    from oncofiles.curated import reocr
    from oncofiles.curated.facts_rules import Vocabulary

    text = '---\ndoc_date: "2027-03"\ndoc_date_precision: "month"\ndoc_type: "Labs"\n---\n\n15.3. x'
    assert reocr._facts_of(text, Vocabulary.from_rows([])) == set()
