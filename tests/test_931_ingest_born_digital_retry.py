"""Ingest OCR shares the born-digital decision and the #806 retry with re-OCR (#931).

* a PDF whose text layer is a scanner's OCR layer (invisible text over a page
  image) is vision-read at ingest, like a scan; the engine is the local model,
  so re-OCR's local/non-local classification sees a local reading;
* a born-digital text layer is still taken as is (no model call);
* a near-empty, reasoning-heavy page answer is asked once more with thinking
  off (the per-call marker), same image and prompt; the longer text stands;
* both rules are ONE implementation (``extract``), used by re-OCR too.

Synthetic data only (invented values, dates 2027+)."""

from __future__ import annotations

import logging

import pytest

from oncofiles.curated import extract, llm, reocr, runner
from oncofiles.tools import curated as curated_tools

LAYER_TEXT = "Leukocyty 7,21 G/l 4,0 - 11 " * 6
LONG = "Leukocyty 7,93 G/l 4,0 - 11\nOdber 14.03.2027 07:40\nHemoglobin 128 g/l"
SENTINEL = "SYNTHETIC REASONING TRANSCRIPT Leukocyty 9,99 G/l " * 4


def _born_digital_pdf(text: str = LAYER_TEXT) -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), text)
    data = doc.tobytes()
    doc.close()
    return data


def _scanner_layer_pdf(text: str = LAYER_TEXT) -> bytes:
    """A 'scan': a full-page image with an invisible OCR text layer on top."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 80), False)
    pix.clear_with(230)
    page.insert_image(page.rect, pixmap=pix)
    page.insert_text((72, 72), text, render_mode=3)
    data = doc.tobytes()
    doc.close()
    return data


def _ink_pdf() -> bytes:
    """A scan without any text layer (a dark block)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    black = pymupdf.pdfcolor["black"]
    page.draw_rect(pymupdf.Rect(61, 83, 413, 297), color=black, fill=black)
    data = doc.tobytes()
    doc.close()
    return data


def _fake_chat(first: str, retry: str, *, reasoning=950, completion=1000, calls=None):
    """``llm.chat`` stand-in: a normal call answers ``first`` with the given
    counts, a thinking-off retry answers ``retry``. Both put a long sentinel
    into the raw reasoning channel, which must never become text."""

    def chat(system, content, **kw):
        is_retry = kw.get("reasoning_marker") is True
        if calls is not None:
            calls.append({"system": system, "content": content, **kw})
        info = kw.get("info")
        if info is not None:
            info.update(finish_reason="stop", reasoning_content=SENTINEL, prompt_tokens=900,
                        completion_tokens=completion if not is_retry else 300,
                        reasoning_tokens=reasoning if not is_retry else 0)  # fmt: skip
        return retry if is_retry else first

    return chat


@pytest.fixture
def model(monkeypatch):
    monkeypatch.setattr(llm, "model_id", lambda: "qwen-local")


# --- 1. the born-digital decision ---------------------------------------------------


def test_scanner_ocr_layer_is_vision_read_at_ingest(monkeypatch, model):
    calls: list[dict] = []
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10, calls=calls))
    ex = extract.extract("scan.pdf", _scanner_layer_pdf())
    assert ex.ok and ex.method == "vision" and ex.text_layer_ignored
    assert ex.engine == "qwen-local" and ex.ocr_version == extract.OCR_VERSION
    assert "Leukocyty 7,93" in ex.text and "7,21" not in ex.text  # never the OCR layer
    assert [c for c in calls if c["system"] == extract.OCR_SYSTEM_PROMPT]


def test_born_digital_layer_needs_no_model(monkeypatch):
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("LLM must not be called"))
    ex = extract.extract("lab.pdf", _born_digital_pdf())
    assert ex.ok and ex.method == "text_layer" and ex.engine == "pymupdf"
    assert not ex.text_layer_ignored and ex.retried_pages == []


def test_a_short_born_digital_layer_is_not_reported_as_ignored(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    ex = extract.extract("short.pdf", _born_digital_pdf("Kontrola 2027"))
    assert ex.method == "vision" and not ex.text_layer_ignored


def test_min_text_chars_still_applies_to_a_born_digital_layer(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    pdf = _born_digital_pdf("Lekarska sprava " * 8)  # ~128 characters
    assert extract.extract("a.pdf", pdf, min_text_chars=100).method == "text_layer"
    assert extract.extract("a.pdf", pdf, min_text_chars=200).method == "vision"


def test_ingest_and_reocr_take_the_same_decision(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    for pdf, own in ((_born_digital_pdf(), True), (_scanner_layer_pdf(), False)):
        assert extract.pdf_text_layer(pdf).is_own_text() is own
        assert (extract.extract("d.pdf", pdf).method == "text_layer") is own
        assert (reocr.reread("d.pdf", pdf).method == "text_layer") is own


def test_one_implementation():
    """re-OCR uses the shared helpers; it keeps no copy of its own."""
    import inspect

    assert reocr.born_digital is extract.born_digital
    assert reocr.needs_retry is extract.needs_retry
    assert reocr.NEAR_EMPTY_CHARS == extract.NEAR_EMPTY_CHARS == 50
    assert reocr.RETRY_REASONING_RATIO == extract.RETRY_REASONING_RATIO == 0.8
    src = inspect.getsource(reocr)
    assert "get_texttrace" not in src and "def born_digital" not in src
    assert "def needs_retry" not in src
    assert "retry_near_empty" in inspect.getsource(reocr._vision_passes)
    assert "pdf_text_layer" in inspect.getsource(reocr.reread)
    assert "retry_near_empty" in inspect.getsource(extract._vision_pages)


def test_a_vision_sidecar_engine_is_local_and_not_reselected(monkeypatch, model):
    """A sidecar written by the new path records the local model as engine, so
    re-OCR does not pick it as a Cowork-era reading (only ``[?]`` lines would)."""
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    ex = extract.extract("scan.pdf", _scanner_layer_pdf())
    meta = {"ocr_engine": ex.engine, "extraction": ex.method}
    assert reocr.needs_reocr(meta, [ex.text]) == []


def test_existing_pymupdf_sidecars_keep_their_reocr_classification():
    """Explicit decision (#931): an existing ``pymupdf`` sidecar stays local for
    re-OCR selection (its source is not re-checked from the sidecar alone)."""
    assert reocr.needs_reocr({"ocr_engine": "pymupdf", "extraction": "text_layer"}, ["x"]) == []


# --- 2. the #806 retry at ingest -----------------------------------------------------


def test_near_empty_reasoning_heavy_page_is_retried_with_thinking_off(monkeypatch, model):
    calls: list[dict] = []
    monkeypatch.setattr(llm, "chat", _fake_chat("x", LONG, calls=calls))
    ex = extract.extract("scan.pdf", _ink_pdf())
    assert ex.ok and ex.retried_pages == [1]
    assert "Leukocyty 7,93" in ex.text and SENTINEL not in ex.text
    ocr = [c for c in calls if c["system"] == extract.OCR_SYSTEM_PROMPT]
    normal = [c for c in ocr if "reasoning_marker" not in c]
    retries = [c for c in ocr if c.get("reasoning_marker") is True]
    assert len(normal) == len(retries) == 1
    assert retries[0]["thinking"] is False  # per call, nothing global
    assert retries[0]["content"] == normal[0]["content"]  # same image, same prompt


@pytest.mark.parametrize(
    "first,reasoning,retried",
    [
        ("x", 801, True),  # 0.801 > 0.8
        ("x", 800, False),  # exactly 0.8 is not above the ratio
        ("x" * extract.NEAR_EMPTY_CHARS, 1000, False),  # long enough
        ("x" * (extract.NEAR_EMPTY_CHARS - 1), 1000, True),
    ],
)
def test_retry_depends_on_ratio_and_length(monkeypatch, model, first, reasoning, retried):
    monkeypatch.setattr(llm, "chat", _fake_chat(first, LONG, reasoning=reasoning))
    ex = extract.extract("scan.pdf", _ink_pdf())
    assert ex.retried_pages == ([1] if retried else [])


def test_unknown_token_counts_never_retry(monkeypatch, model):
    calls: list = []

    def chat(system, content, **kw):  # fills no counts
        calls.append(kw)
        return ""

    monkeypatch.setattr(llm, "chat", chat)
    ex = extract.extract("scan.pdf", _ink_pdf())
    assert ex.retried_pages == []
    assert not [k for k in calls if k.get("reasoning_marker")]


@pytest.mark.parametrize("retry_text", ["", "ab"])
def test_retry_text_is_kept_only_when_longer(monkeypatch, model, retry_text):
    monkeypatch.setattr(llm, "chat", _fake_chat("abc", retry_text))
    ex = extract.extract("scan.pdf", _ink_pdf())
    assert ex.retried_pages == [1] and ex.text.endswith("abc")


def test_a_failed_retry_keeps_the_first_answer(monkeypatch, model, caplog):
    def chat(system, content, **kw):
        if kw.get("reasoning_marker"):
            raise llm.LocalLLMError("synthetic timeout")
        kw["info"].update(reasoning_tokens=990, completion_tokens=1000)
        return "abc"

    monkeypatch.setattr(llm, "chat", chat)
    with caplog.at_level(logging.WARNING):
        ex = extract.extract("scan.pdf", _ink_pdf())
    assert ex.ok and ex.retried_pages == [1] and ex.text.endswith("abc")
    assert "synthetic timeout" not in caplog.text  # class name only


def test_the_reasoning_channel_is_never_a_transcript(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat("", ""))
    ex = extract.extract("scan.pdf", _ink_pdf())
    assert ex.retried_pages == [1] and SENTINEL not in ex.text
    import inspect

    assert "reasoning_content" not in inspect.getsource(extract)


def test_retry_is_reported_in_the_run_report_and_not_in_the_sidecar(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat("x", LONG))
    curated_tools._extract_cache.clear()
    content = _ink_pdf()
    report = runner.RunReport(run_id="t", dry_run=True)
    ex = curated_tools.cached_extract("s.pdf", content, "application/pdf")
    report.count_orientation(ex)
    assert report.ocr == {"retried_pages": 1, "text_layer_ignored": 0}
    assert "retried_pages" not in ex.front_matter()
    hit = curated_tools.cached_extract("s.pdf", content, "application/pdf")
    report.count_orientation(hit)  # a cache hit retried nothing in this run
    assert report.ocr["retried_pages"] == 1
    curated_tools._extract_cache.clear()


def test_ignored_layer_is_counted_in_the_run_report(monkeypatch, model):
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    report = runner.RunReport(run_id="t", dry_run=True)
    report.count_orientation(extract.extract("scan.pdf", _scanner_layer_pdf()))
    assert report.ocr == {"retried_pages": 0, "text_layer_ignored": 1}


def test_ocr_version_bumped():
    assert extract.OCR_VERSION == "ocr-v4"


# --- 4. infra review: the OCR_VERSION bump changes nothing over existing sidecars ----

PRIOR_FILED = "20270805-JanaTestova-LabX-Labs-Novak-Kontrola.pdf"
PRIOR_SIDECAR = """---
source_file: "20270805-JanaTestova-LabX-Labs-Novak-Kontrola.pdf"
patient: "Jana Testova"
doc_date: "2027-08-05"
institution: "LabX"
doc_type: "Labs"
extraction: "vision"
ocr_engine: "qwen-local"
ocr_version: "ocr-v3"
rotation: [0]
rotation_source: ["prefilter"]
has_chart: false
rotated_region: false
review_needed: false
---

<!-- page 1 -->
Hemoglobin 131 g/l
"""


PRIOR_SOURCE_MODIFIED = "2027-08-06" + "T07:15Z"
PRIOR_SIDECAR_MODIFIED = "2027-08-06" + "T07:16Z"


def _prior_drive():
    from tests.test_curated_runner import LABS, FakeDrive

    d = FakeDrive()
    src = d.add(PRIOR_FILED, d.folder[LABS], b"%PDF synthetic", modified=PRIOR_SOURCE_MODIFIED)
    side = PRIOR_SIDECAR.encode()
    d.add(PRIOR_FILED + ".md", d.folder[LABS], side, modified=PRIOR_SIDECAR_MODIFIED)
    return d, src


def test_ocr_version_is_written_never_compared():
    """Every reader of ``OCR_VERSION`` / ``ocr_version`` in src only writes or
    copies it (sidecar front matter, index metadata); none compares it, so a
    bump cannot re-select, re-index or re-queue anything."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
    users = set()
    for path in src.rglob("*.py"):
        for n, line in enumerate(path.read_text().splitlines(), start=1):
            if "OCR_VERSION" in line or "ocr_version" in line:
                users.add(path.relative_to(src).as_posix())
                code = line.split("#", 1)[0].replace("->", "")
                assert not re.search(r"==|!=|\bin\b|startswith|<|>", code), (
                    f"{path.name}:{n} compares the OCR version: {line.strip()}"
                )
    assert users == {"curated/extract.py", "curated/reocr.py", "curated/runner.py",
                     "curated/indexer.py"}  # fmt: skip


def test_probe_stays_idle_over_an_ocr_v3_sidecar():
    from oncofiles.curated.runner import probe
    from tests.test_curated_runner import NOW

    d, _ = _prior_drive()
    first = probe(d, d.root, now=NOW)
    assert first.pending == {} and d.writes == []
    again = probe(d, d.root, idle_signature=first.signature, now=NOW)
    assert again.pending == {} and again.signature == first.signature


def test_reocr_never_selects_an_ocr_v3_sidecar():
    from oncofiles.curated import indexer

    front, pages = indexer.parse_sidecar(PRIOR_SIDECAR)
    assert front["ocr_version"] == "ocr-v3" != extract.OCR_VERSION
    assert reocr.needs_reocr(front, pages) == []


async def test_index_stays_unchanged_over_an_ocr_v3_sidecar(db):
    from unittest.mock import AsyncMock, MagicMock, patch

    from tests.helpers import ERIKA_UUID

    d, src = _prior_drive()
    gdrive = MagicMock()
    gdrive.dedicated_copy.return_value = MagicMock()
    with (
        patch("oncofiles.server._create_patient_clients",
              AsyncMock(return_value=(gdrive, None, None, d.root))),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: d),
    ):  # fmt: skip
        first = await curated_tools.index_patient(db, ERIKA_UUID)
        again = await curated_tools.index_patient(db, ERIKA_UUID)
        assert first["created"] == 1
        assert not again["created"] and not again.get("updated")
        assert again["unchanged"] == 1
        doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
        assert "ocr-v3" in str(doc.structured_metadata)  # the old reading is kept as it is
        targets = await curated_tools._reocr_targets(db, ERIKA_UUID, None, only_with_facts=False)
        assert targets == []
    assert d.writes == []


# --- 5. infra review: raw genotype data is excluded BEFORE any model call ----------

RSIDS = "rs1000001 AA\nrs1000002 AG\nrs1000003 GG\nrs1000004 CT\n" * 3


@pytest.fixture
def no_model(monkeypatch):
    def fail(*a, **k):
        pytest.fail("the local model must not be called")

    monkeypatch.setattr(llm, "chat", fail)
    monkeypatch.setattr(extract.orientation, "ask_rotation", fail)


@pytest.mark.parametrize("make", [_scanner_layer_pdf, _born_digital_pdf])
def test_genotype_pdf_with_a_text_layer_never_reaches_the_model(no_model, make):
    ex = extract.extract("export_2027.pdf", make(RSIDS), "application/pdf")
    assert not ex.ok and ex.error.startswith("raw_genotype_excluded")
    assert ex.text == "" and ex.method == "none"


@pytest.mark.parametrize("name", ["23andMe_export.pdf", "MyHeritage raw data.pdf",
                                  "sample.vcf.pdf"])  # fmt: skip
def test_raw_export_pdf_name_is_excluded_before_rendering(no_model, monkeypatch, name):
    monkeypatch.setattr(extract, "_render_pdf_pages", lambda c: pytest.fail("must not render"))
    ex = extract.extract(name, _ink_pdf(), "application/pdf")
    assert ex.error.startswith("raw_genotype_excluded")


def test_raw_export_image_name_is_excluded_before_rendering(no_model, monkeypatch):
    monkeypatch.setattr(extract, "_render_image", lambda c, e: pytest.fail("must not render"))
    ex = extract.extract("ancestry_raw_data.jpg", b"\xff\xd8 synthetic", "image/jpeg")
    assert ex.error.startswith("raw_genotype_excluded")


def test_a_scanned_clinical_genetics_report_is_still_read(monkeypatch, model):
    """Only raw exports are excluded: a genetics report scan named with DNA /
    genotype words and no rsIDs is read as before."""
    monkeypatch.setattr(llm, "chat", _fake_chat(LONG, LONG, reasoning=10))
    ex = extract.extract("Genetika_DNA_genotypizacia_KRAS.pdf", _ink_pdf(), "application/pdf")
    assert ex.ok and ex.method == "vision"


def test_runner_excludes_a_genotype_pdf_before_extraction_reaches_the_model(no_model):
    """Order lock: in a curated run the exclusion runs inside ``extract`` before
    the vision read, so neither the OCR nor the classification is called."""
    from oncofiles.curated.runner import RunOptions, run_curated
    from tests.test_curated_runner import INBOX, NOW, FakeDrive

    def propose(contract, text):
        pytest.fail("classification must not be called")

    d = FakeDrive()
    d.add("export_2027.pdf", d.folder[INBOX], _scanner_layer_pdf(RSIDS))
    r = run_curated(d, d.root, RunOptions(now=NOW, run_id="t"), extract_fn=extract.extract,
                    propose_fn=propose)  # fmt: skip
    doc = next(x for x in r.documents if x["source"] == "export_2027.pdf")
    assert doc["result"] == "review_needed"
    assert any("raw_genotype_excluded" in r for r in doc["reasons"])
