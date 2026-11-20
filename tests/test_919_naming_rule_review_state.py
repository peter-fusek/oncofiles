"""#919: one rule for the filename builder and checker; consistent review state.

* ``naming.build_filename`` normalises the subject so every name it builds
  passes ``naming.is_standard`` (property test over many synthetic subjects).
* A Google-native file (no extension through the Drive API) is standard when
  its fields are valid (``is_standard(name, mime=...)``, ``document_stage``).
* The runner never files a name the checker rejects: review, code
  ``name_not_standard``.
* The indexer keeps review reasons as codes only and an active #900 override
  as a boolean; ``document_stage`` treats a filed document under an active
  override as not needing review; ``/api/documents`` rows carry the codes.

Synthetic data only: invented names, institutions and Drive ids; every date
is invented and lies in 2027 or later.
"""

from __future__ import annotations

import hashlib
import json
import random
import string
from datetime import UTC, datetime

import pytest

from oncofiles import document_stage
from oncofiles.curated import indexer, review_codes, runner
from oncofiles.curated.naming import (
    NameFields,
    build_filename,
    is_standard,
    parse_filename,
    subject_token,
    with_collision_suffix,
)
from oncofiles.curated.runner import RunOptions, run_curated
from tests.test_curated_runner import INBOX, LABS, FakeDrive
from tests.test_curated_runner import fake_propose as _base_propose

GDOC = "application/vnd.google-apps.document"
NOW = datetime(2027, 4, 18, 9, 15, tzinfo=UTC)
TEXT = b"VYSLEDKY z 12.02.2027\nHemoglobin 130 g/l\n"


def _fields(subject):
    return NameFields(
        date="20270212",
        patient="JanaVymyslena",
        institution="SynthClinic",
        doc_type="Labs",
        doctor="Smyslena",
        hospital="SynthLab",
        subject=subject,
    )


# ── item 1: the builder only builds what the checker accepts ─────────


@pytest.mark.parametrize(
    ("subject", "token"),
    [
        ("2. cyklus", "Cyklus"),  # digit-leading → leading digits dropped
        ("12", None),  # two bare digits would be read back as nn
        ("2027", None),  # all digits
        ("1234567890123456789", None),  # an accession number
        ("Vysledok 12345678", "Vysledok"),  # an ID-like run inside
        ("Kontrola po 3 tyzdnoch", "KontrolaPo3Tyzdnoch"),  # a short number stays
        ("C2 cyklus", "C2Cyklus"),
        ("Pečeň a žlčník", "PecenAZlcnik"),
        ("Unknown", None),
        ("", None),
        ("   ", None),
        ("—–…", None),
        (None, None),
    ],
)
def test_subject_token(subject, token):
    assert subject_token(subject) == token


def test_digit_leading_and_id_subjects_build_standard_names():
    for subject in ("2. cyklus", "12", "1234567890123456789", "007"):
        name = build_filename(_fields(subject), "pdf")
        assert is_standard(name), name
        parsed = parse_filename(name)
        assert parsed.nn is None  # never read back as a collision suffix


def _random_subjects(n: int) -> list[str]:
    rng = random.Random(919)
    alphabet = (
        string.ascii_letters + string.digits + " .-_/,:;()[]#&+'\"" + "áäčďéíľňóôŕšťúýžÁČŠŽ"
        + "ßłøœ" + "Привет" + "测试" + "\t\n"
    )  # fmt: skip
    out = []
    for _ in range(n):
        size = rng.randint(0, 60)
        out.append("".join(rng.choice(alphabet) for _ in range(size)))
    # shaped cases: digit runs of every length, digits around words
    for k in range(1, 25):
        run = rng.choice(string.digits) * k
        out += [run, f"{run} Zqxwort", f"Zqxwort {run}", f"{run}q{run}"]
    return out


def test_every_built_name_passes_the_checker():
    """Property: whatever the model returns as a subject, the name is standard."""
    for subject in _random_subjects(3000):
        for nn in (None, 2, 99):
            fields = NameFields(**{**_fields(subject).__dict__, "nn": nn})
            for ext in ("pdf", "jpg", "md"):
                name = build_filename(fields, ext)
                assert is_standard(name), (subject, name)
            native = build_filename(fields, "gdoc").removesuffix(".gdoc")
            assert is_standard(native, mime=GDOC), (subject, native)


def test_long_names_stay_standard_after_truncation():
    long = NameFields(**{**_fields("Abc" * 20).__dict__, "hospital": "H" * 90})
    name = build_filename(long, "pdf")
    assert len(name) <= 120 and is_standard(name)
    assert is_standard(with_collision_suffix(name, {name}))


# ── item 1: Google-native names have no extension ────────────────────

NATIVE_NAME = "20270212-JanaVymyslena-SynthClinic-Advocate-Unknown-Unknown-Poznamky"


def test_google_native_name_without_extension_is_standard_only_with_its_mime():
    assert not is_standard(NATIVE_NAME)  # default unchanged: extension required
    assert not is_standard(NATIVE_NAME, mime="application/pdf")
    assert is_standard(NATIVE_NAME, mime=GDOC)
    assert is_standard(NATIVE_NAME, mime="application/vnd.google-apps.spreadsheet")
    assert is_standard(NATIVE_NAME + ".pdf", mime=GDOC)  # a title with an extension too
    assert not is_standard("20270212_JanaVymyslena_Notes", mime=GDOC)  # still 7 fields
    assert not is_standard(NATIVE_NAME + ".", mime=GDOC)


def test_document_stage_is_named_passes_the_row_mime():
    kw = {"file_id": "gdrive:synth-919-1", "structured_metadata": None, "patient_id": "p"}
    assert not document_stage.is_named(NATIVE_NAME, **kw)
    assert document_stage.is_named(NATIVE_NAME, mime_type=GDOC, **kw)
    row = document_stage.describe(
        filename=NATIVE_NAME,
        mime_type=GDOC,
        has_text=True,
        extractable=True,
        gdrive_id="synth-919-1",
        **kw,
    )
    assert row["steps"]["named"] is True and row["ready"] is True


# ── item 1: the runner never files a name the checker rejects ────────


def _propose(subject):
    def propose(contract, text):
        return {**_base_propose(contract, text), "doc_date": "2027-02-12", "subject": subject}

    return propose


def _extract(name, content, mime="", *, min_text_chars=100):
    return runner.extract_mod.Extraction(
        text=content.decode("utf-8", errors="replace"), method="text_layer", pages=1, engine="t"
    )


def _run(d, propose):
    o = RunOptions(now=NOW, run_id="t919", dry_run=False)
    return run_curated(d, d.root, o, extract_fn=_extract, propose_fn=propose)


def test_runner_files_digit_leading_subject_under_a_standard_name():
    d = FakeDrive()
    d.add("scan_919.pdf", d.folder[INBOX], TEXT)
    r = _run(d, _propose("2. cyklus"))
    assert r.status == "ok", r.message
    doc = r.documents[0]
    assert doc["result"] == "filed"
    filed = doc["to"].split("/", 1)[1]
    assert filed.endswith("-Cyklus.pdf") and is_standard(filed)


def test_runner_files_a_google_doc_under_a_name_the_checker_accepts():
    d = FakeDrive()
    fid = d.add("Poznamky z kontroly", d.folder[INBOX], TEXT)
    d.items[fid]["mime"] = GDOC
    r = _run(d, _propose(None))
    assert r.status == "ok", r.message
    doc = r.documents[0]
    assert doc["result"] == "filed"
    filed = doc["to"].split("/", 1)[1]
    assert "." not in filed  # no extension through the Drive API
    assert is_standard(filed, mime=GDOC)


def test_runner_sends_a_name_the_checker_rejects_to_review(monkeypatch):
    monkeypatch.setattr(runner, "build_filename", lambda fields, ext: "not standard.pdf")
    d = FakeDrive()
    d.add("scan_919.pdf", d.folder[INBOX], TEXT)
    r = _run(d, _propose(None))
    assert r.status == "ok", r.message
    doc = r.documents[0]
    assert doc["result"] == "review_needed"
    assert doc["reasons"] == [runner.NAME_NOT_STANDARD_NOTE]
    assert review_codes.codes_of(doc["reasons"]) == ["name_not_standard"]
    assert d.find(d.folder[INBOX], "scan_919.pdf")  # nothing filed
    assert not d.names(d.folder[LABS])
    side = d.items[d.find(d.folder[INBOX], "scan_919.pdf.md")]["content"].decode()
    assert "review_needed: true" in side and "name_not_standard" in side


def test_runner_sends_an_unusable_extension_to_review_instead_of_crashing():
    d = FakeDrive()
    d.add("scan_919.pdf~", d.folder[INBOX], TEXT)  # extension the builder refuses
    r = _run(d, _propose(None))
    assert r.status == "ok", r.message
    assert r.documents[0]["result"] == "review_needed"
    assert review_codes.codes_of(r.documents[0]["reasons"]) == ["name_not_standard"]


# ── item 3: review reasons as codes, override as a boolean ──────────

OVERRIDE_REASON = "rotated stamp ignored, maintainer 2027-03-01"


def test_reason_sentences_map_to_codes_and_never_leak_text():
    reasons = [
        "date 2027-02-12 does not appear in the document text",
        "document names another person (Tomas Vymysleny), not Jana Vymyslena",
        "new doctor 'Smyslena' not in README vocabulary",
        "rotated region on page 1: not read",
        "model confidence 0.20 < 0.6",
        "something nobody planned for",
        "no valid clinical date",
        "no valid clinical date",
    ]
    codes = review_codes.codes_of(reasons)
    assert codes == [
        "date_not_printed", "other_person", "new_vocabulary", "rotated_region",
        "low_confidence", "other", "no_date",
    ]  # fmt: skip
    assert all(review_codes.CODE_RE.match(c) for c in codes)
    assert "Tomas" not in json.dumps(codes)
    assert review_codes.codes_of("not a list") == ["other"]
    assert review_codes.codes_of({"a": 1}) == []
    assert len(review_codes.codes_of([f"x{i}" for i in range(50)])) == 1  # deduped
    many = ["no valid clinical date (x)", "model confidence 0.13 < 0.6", "chart on page 7: x"] * 5
    assert review_codes.codes_of(many) == ["no_date", "low_confidence", "chart_page"]
    assert review_codes.safe_codes(["no_date", "Tomas Vymysleny", "x" * 80, 3]) == ["no_date"]


def _sidecar(*, override: bool, status: bool = False) -> str:
    lines = [
        'source_file: "20270212-JanaVymyslena-SynthClinic-Labs-Smyslena-SynthLab.pdf"',
        'doc_date: "2027-02-12"',
        f'sha256: "{hashlib.sha256(b"synthetic").hexdigest()}"',
        "review_needed: true",
        'review_reasons: ["rotated region on page 1: not read", '
        '"document names another person (Tomas Vymysleny), not Jana Vymyslena"]',
    ]
    if override:
        lines.append(f"review_override: {json.dumps(OVERRIDE_REASON)}")
    if status:
        lines.append('review_override_status: "hard_checks_failed"')
    return "---\n" + "\n".join(lines) + "\n---\n\nVYSLEDKY z 12.02.2027\n"


@pytest.mark.parametrize(
    ("override", "status", "active"),
    [(True, False, True), (True, True, False), (False, False, False)],
)
def test_indexer_keeps_codes_and_override_presence_only(override, status, active):
    text = _sidecar(override=override, status=status)
    rec = indexer.IndexRecord(
        gdrive_id="synth-919", filename="x.pdf", folder=LABS,
        category=indexer.category_for("Labs"), mime_type="application/pdf",
        size_bytes=1, modified="2028-05-17T06:41:00Z",
    )  # fmt: skip
    front, pages = indexer.parse_sidecar(text)
    indexer._apply_sidecar(rec, front, pages, text)
    assert rec.review_reasons == ["rotated_region", "other_person"]
    assert rec.review_override is active
    # neither the reason sentences nor the override text are kept anywhere
    kept = json.dumps([rec.metadata, rec.review_reasons, rec.review_override])
    assert "Tomas" not in kept and OVERRIDE_REASON not in kept


def _meta(*, override, reasons=("rotated_region",)):
    return json.dumps(
        {
            "source": "curated",
            "review_needed": True,
            "curated": {
                "sidecar_id": "synth-sidecar",
                "index_version": indexer.INDEX_VERSION,
                "review_reasons": list(reasons),
                "review_override": override,
            },
        }
    )


def _describe(meta):
    return document_stage.describe(
        filename="20270212-JanaVymyslena-SynthClinic-Labs-Smyslena-SynthLab.pdf",
        file_id="gdrive:synth-919-2",
        structured_metadata=meta,
        patient_id="p",
        has_text=True,
        extractable=True,
        gdrive_id="synth-919-2",
    )


def test_a_filed_document_under_an_active_override_needs_no_review():
    row = _describe(_meta(override=True))
    assert row["attention_reason"] is None and row["ready"] is True
    assert row["review_reasons"] == []


def test_review_without_override_carries_the_codes():
    row = _describe(_meta(override=False, reasons=("date_not_printed", "Tomas Vymysleny")))
    assert row["attention_reason"] == "review_needed"
    assert row["review_reasons"] == ["date_not_printed"]  # text never passes the read check


def test_rows_indexed_before_919_keep_todays_behaviour():
    meta = json.dumps({"source": "curated", "review_needed": True, "curated": {}})
    row = _describe(meta)
    assert row["attention_reason"] == "review_needed" and row["review_reasons"] == []


def test_index_version_bumped_so_existing_rows_get_the_new_keys():
    assert indexer.INDEX_VERSION >= 24


def test_dashboard_has_help_text_for_every_code():
    from pathlib import Path

    js = (Path(indexer.__file__).parents[1] / "dashboard.js").read_text()
    for code in review_codes.KNOWN_CODES - {review_codes.OTHER}:
        assert f"      {code}: [" in js, code
    assert "review_reasons" in js
