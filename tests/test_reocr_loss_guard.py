"""Generic loss guard of the re-OCR rewrite (#660 follow-up, #773).

Before a sidecar is overwritten, every printed number and date of the old text
is looked for in the new one (not only lab-vocabulary values): a lost date,
more than ``REVIEW_MISSING_NUMBERS`` lost numbers, or a near-empty page with
any loss means ``review`` and nothing is written. Counts only in the report and
the log. Synthetic data only (invented values, dates 2027+)."""

from __future__ import annotations

import json
import logging

import pytest

from oncofiles.curated import reocr
from oncofiles.curated.facts_rules import Vocabulary
from tests.test_curated_reocr import NOW, OLD_SIDECAR
from tests.test_curated_runner import LABS, FakeDrive


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


FRONT = OLD_SIDECAR.split("<!-- page 1 -->")[0]
FILLER = "Poznamka bez cisel, dlhsi opis vysetrenia na doplnenie rozsahu strany."
# Cowork-style body: no page markers, no lab-vocabulary labels
OLD_LINES = [
    "Kontrola zo dna 17.04.2027",
    "Riadok alfa 731,4",
    "Riadok beta 8862",
    "Riadok gama 59,07",
    "Riadok delta 4413",
    "Riadok epsilon 286",
    FILLER,
]
OLD_BODY = "\n".join(OLD_LINES) + "\n"
TOKENS = ["17.04.2027", "2027-04-17", "731,4", "731.4", "8862", "59,07", "59.07", "4413", "286"]


def _drop(*parts: str) -> str:
    """The old body as one new page, without the given substrings."""
    text = OLD_BODY
    for p in parts:
        text = text.replace(p, "")
    return text.strip()


def _run(pages: list[str], *, dry_run=False, old_body=OLD_BODY):
    drive = FakeDrive()
    name = "20270412-JanaTestova-LabX-Labs-Kontrola.pdf"
    src = drive.add(name, drive.folder[LABS], b"%PDF synthetic")
    old = FRONT + old_body
    side = drive.add(name + ".md", drive.folder[LABS], old.encode())
    doc = {"document_id": 7, "filename": name, "gdrive_id": src, "sidecar_id": side,
           "engine": "tesseract eng-only", "reasons": ["cowork_ocr"]}  # fmt: skip

    def reader(*a, **k):
        return reocr.Reread(list(pages), "vision2", "qwen-local")

    return drive, side, old, doc, reader


def _go(vocab, pages, *, dry_run=False, old_body=OLD_BODY):
    drive, side, old, doc, reader = _run(pages, dry_run=dry_run, old_body=old_body)
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=dry_run, now=NOW,
                          reader=reader)  # fmt: skip
    return rep, rep.documents[0], drive, side, old


def _unchanged(drive, side, old):
    assert drive.writes == []  # no backup, no update
    assert drive.items[side]["content"] == old.encode()


# --- token normalisation ------------------------------------------------------


def test_numbers_and_dates_are_normalised():
    assert reocr.token_loss("a 12,5 b", "a 12.5 b")["missing_numbers"] == 0
    same_day = ["5.3.2027", "05.03.2027", "5. 3. 2027", "2027-03-05"]
    for a in same_day:
        for b in same_day:
            assert reocr.token_loss(f"odber {a}", f"odber {b}")["missing_dates"] == 0
    # a 2-digit year stays as printed
    assert reocr.token_loss("odber 5.3.27", "odber 5.3.2027")["missing_dates"] == 1


def test_a_date_never_also_counts_as_numbers():
    loss = reocr.token_loss("odber 17.04.2027 a 2027-04-18", "odber")
    assert loss["missing_dates"] == 2 and loss["missing_numbers"] == 0


def test_front_matter_and_page_markers_are_ignored():
    old = "---\ndoc_date: 2027-04-12\n---\n\nRiadok 731,4\n"
    new = "---\ndoc_date: 2027-09-30\n---\n\n<!-- page 1 -->\nRiadok 731,4\n"
    loss = reocr.token_loss(old, new)
    assert loss["missing_numbers"] == 0 and loss["missing_dates"] == 0
    # a page marker of the old text is not a lost number either
    assert reocr.token_loss("<!-- page 3 -->\nx", "x")["missing_numbers"] == 0


def test_near_empty_pages_are_one_based():
    assert reocr.near_empty_pages([FILLER, "  Strana  ", "", FILLER]) == [2, 3]


def test_cowork_text_without_markers_versus_new_paged_text():
    half = len(OLD_LINES) // 2
    first, second = "\n".join(OLD_LINES[:half]), "\n".join(OLD_LINES[half:])
    rr = reocr.Reread([first, second], "v", "e")
    paged = reocr.rewrite_sidecar("", rr, previous_engine="", now=NOW)
    new = FRONT + paged.split("---\n\n", 1)[1]
    loss = reocr.token_loss(FRONT + OLD_BODY, new)
    assert loss["missing_numbers"] == 0 and loss["missing_dates"] == 0
    assert loss["old_chars"] == loss["new_chars"] > 0


# --- thresholds -----------------------------------------------------------------


async def test_nothing_lost_is_rewritten(vocab):
    _, d, drive, _, _ = _go(vocab, [_drop()])
    assert d.result == "rewritten" and d.decision == "rewrite" and d.review_reasons == []
    assert (d.missing_numbers, d.missing_dates, d.near_empty_pages) == (0, 0, [])
    assert [w[0] for w in drive.writes] == ["create", "update"]


async def test_two_missing_numbers_are_still_rewritten(vocab):
    _, d, drive, _, _ = _go(vocab, [_drop("8862", "4413")])
    assert d.lost_codes == {} and d.missing_numbers == 2
    assert d.result == "rewritten" and d.review_reasons == []


async def test_three_missing_numbers_go_to_review(vocab):
    _, d, drive, side, old = _go(vocab, [_drop("8862", "4413", "286")])
    assert d.lost_codes == {} and d.missing_numbers == 3
    assert d.result == "review" and d.review_reasons == ["missing_numbers"]
    assert d.backup is None
    _unchanged(drive, side, old)


async def test_one_missing_date_goes_to_review(vocab):
    _, d, drive, side, old = _go(vocab, [_drop("17.04.2027")])
    assert d.missing_dates == 1 and d.missing_numbers == 0
    assert d.result == "review" and d.review_reasons == ["missing_dates"]
    _unchanged(drive, side, old)


async def test_a_near_empty_page_alone_is_not_a_stop(vocab):
    _, d, drive, _, _ = _go(vocab, [_drop(), "Strana"])
    assert d.near_empty_pages == [2] and d.missing_numbers == 0
    assert d.new_chars >= reocr.NEAR_EMPTY_CHARS_RATIO * d.old_chars
    assert d.result == "rewritten" and d.review_reasons == []


async def test_near_empty_page_and_one_missing_number_go_to_review(vocab):
    _, d, drive, side, old = _go(vocab, [_drop("286"), ""])
    assert d.near_empty_pages == [2] and d.missing_numbers == 1
    assert d.result == "review" and d.review_reasons == ["near_empty_with_loss"]
    _unchanged(drive, side, old)


async def test_near_empty_page_and_a_shrunk_body_go_to_review(vocab):
    # shrunk below NEAR_EMPTY_CHARS_RATIO but not below TEXT_SHRUNK_RATIO (#806)
    old_body = OLD_BODY + FILLER + "\n"
    _, d, drive, side, old = _go(vocab, [_drop(), "Strana"], old_body=old_body)
    assert d.missing_numbers == 0 and d.missing_dates == 0
    assert d.new_chars < reocr.NEAR_EMPTY_CHARS_RATIO * d.old_chars
    assert d.new_chars >= reocr.TEXT_SHRUNK_RATIO * d.old_chars
    assert d.result == "review" and d.review_reasons == ["near_empty_with_loss"]
    _unchanged(drive, side, old)


async def test_a_dry_run_reports_the_decision_and_writes_nothing(vocab):
    _, d, drive, side, old = _go(vocab, [_drop("8862", "4413", "286"), ""], dry_run=True)
    assert d.result == "previewed" and d.decision == "review"
    assert d.review_reasons == ["missing_numbers", "near_empty_with_loss"]
    _unchanged(drive, side, old)
    _, ok, drive, side, old = _go(vocab, [_drop()], dry_run=True)
    assert ok.result == "previewed" and ok.decision == "rewrite"
    _unchanged(drive, side, old)


@pytest.mark.parametrize("value", [2, 3])
def test_thresholds_are_module_constants(value):
    assert reocr.REVIEW_MISSING_NUMBERS == 2 and reocr.REVIEW_MISSING_DATES == 0
    assert reocr.NEAR_EMPTY_CHARS == 50 and reocr.NEAR_EMPTY_CHARS_RATIO == 0.8
    loss = {"missing_numbers": value, "missing_dates": 0, "old_chars": 10, "new_chars": 10}
    expected = ["missing_numbers"] if value > reocr.REVIEW_MISSING_NUMBERS else []
    assert reocr.review_reasons([], loss, []) == expected


# --- no values in the report or the log ----------------------------------------


async def test_report_and_log_carry_counts_only(vocab, caplog):
    with caplog.at_level(logging.INFO, logger="oncofiles.curated.reocr"):
        dates, nums = ["17.04.2027"], ["8862", "4413", "286"]
        pages = [_drop(*dates, *nums), ""]
        rep, d, _, _, _ = _go(vocab, pages)
    assert d.result == "review" and d.lost_codes == {}
    out = rep.to_dict()["documents"][0]
    for key in ("missing_numbers", "missing_dates", "near_empty_pages", "review_reasons",
                "old_chars", "new_chars"):  # fmt: skip
        assert key in out
    assert out["review_reasons"] == ["missing_dates", "missing_numbers", "near_empty_with_loss"]
    lines = [r.getMessage() for r in caplog.records if "loss check" in r.getMessage()]
    assert len(lines) == 1
    expected = (f"missing_numbers={d.missing_numbers} missing_dates={d.missing_dates} "
                f"near_empty_pages={d.near_empty_pages}")  # fmt: skip
    assert expected in lines[0]
    assert d.missing_numbers == len(nums) and d.missing_dates == len(dates)
    assert d.near_empty_pages == [len(pages)]
    report = json.dumps(rep.to_dict(), ensure_ascii=False)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for token in TOKENS:
        assert token not in report, token
        assert token not in logged, token
