"""Guard profile for re-OCR of Tesseract-era sidecars (#831, maintainer
decision 2026-11-16).

The old Tesseract text is not a trustworthy baseline (it drops decimal commas
and garbles labels), so when the OLD sidecar says ``ocr_engine: tesseract…``:

* ``lost_values``, ``missing_numbers`` and ``low_word_overlap`` are computed
  and reported as ``baseline_untrusted`` but do not stop the rewrite;
* ``text_shrunk``, ``missing_dates`` and the new ``uncertain_share_high``
  still mean ``review`` + a kept candidate;
* a near-empty page is judged by its own ink: below ``BLANK_INK_MAX`` and no
  text layer = a blank back side; otherwise ``near_empty_page_with_ink`` (or
  ``near_empty_unknown_ink``) means ``review``; ``near_empty_with_loss`` is
  only reported;
* a lost code that has an active machine / verified / corrected stored fact
  still blocks (``lost_trusted_values``; a failed lookup fails closed);
* any other engine, and ``REOCR_TESSERACT_PROFILE=false``, keep the standard
  rules;
* ``curated_promote_candidates`` re-evaluates kept candidates under the
  profile without the model and promotes the passing ones through the restore
  path (backup first, verified update, re-index). Nothing is deleted.

Synthetic data only (invented values, dates 2027+)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest

from oncofiles import config
from oncofiles.curated import llm, reocr
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.curated.indexer import parse_sidecar
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_curated_reocr import NOW
from tests.test_curated_runner import LABS, LOGS, FakeDrive
from tests.test_reocr_candidate import _wired
from tests.test_reocr_report_no_values import _LOG_COUNTS, _leaks, _sensitive, _strings
from tests.test_reocr_retry_guard_restore import _ctx, _seed_doc

TESSERACT = "tesseract-eng psm3 120dpi"
NAME = "20270614-JanaTestova-LabX-Report-Kontrola.pdf"
SOURCE = b"%PDF synthetic original"
UNTRUSTED = ["lost_values", "missing_numbers", "low_word_overlap"]


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


@pytest.fixture(autouse=True)
def _reset_jobs():
    for d in (tool._reocr_jobs, tool._runs, tool._restoring):
        d.clear()
    yield
    for d in (tool._reocr_jobs, tool._runs, tool._restoring):
        d.clear()


@pytest.fixture(autouse=True)
def _no_model(monkeypatch):
    """Nothing in this file may call the model (readers are fakes)."""
    monkeypatch.setattr(llm, "chat", lambda *a, **k: pytest.fail("the model was called"))


def _front(engine: str | None) -> str:
    lines = ["doc_date: 2027-06-14", "doc_type: Report", "extraction: ocr"]
    if engine is not None:
        lines.append(f"ocr_engine: {engine}")
    return "---\n" + "\n".join(lines) + "\n---\n\n"


# The old text as an English-only OCR of a Slovak scan would leave it: decimal
# commas dropped, labels and diacritics garbled. No page markers (Cowork era).
OLD_BODY = (
    "Kontrola zo dna 14.06.2027\n"
    "Odber 14.6.2027: WBC 637; HGB 183; PLT 287\n"
    "Riadok aIfa 7314\n"
    "Riadok bcta 8862\n"
    "Riadok garna 5907\n"
    "Riadok deIta 4413\n"
    "Poznarnka bez cisel, dIhsi opls vysetrenla na dopInenie rozsahu slrany.\n"
    "Zavcr: slav bez zrneny, kontroIa podla polreby u osetrujuceho Iekara.\n"
)
# What the local model reads from the same scan
NEW_LINES = [
    "Kontrola zo dňa 14.06.2027",
    "Odber 14.6.2027: WBC 6,37; HGB 133; PLT 287",
    "Riadok alfa 731,4",
    "Riadok beta 88,62",
    "Riadok gama 59,07",
    "Riadok delta 44,13",
    "Poznámka bez čísel, dlhší opis vyšetrenia na doplnenie rozsahu strany.",
    "Záver: stav bez zmeny, kontrola podľa potreby u ošetrujúceho lekára.",
]
NEW_PAGE = "\n".join(NEW_LINES)
# every printed value of both texts: none may reach a report or a log
TOKENS = ["14.06.2027", "14.6.2027", "2027-06-14", "637", "6,37", "6.37", "133", "183", "287",
          "7314", "731,4", "731.4", "8862", "88,62", "88.62", "5907", "59,07", "59.07",
          "4413", "44,13", "44.13"]  # fmt: skip


def _setup(
    pages, *, engine: str | None = TESSERACT, old_body: str = OLD_BODY, ink=None, trusted=()
):
    """``trusted``: codes of the document's trusted stored facts (None = unknown)."""
    drive = FakeDrive()
    src = drive.add(NAME, drive.folder[LABS], SOURCE)
    old = _front(engine) + old_body
    side = drive.add(NAME + ".md", drive.folder[LABS], old.encode())
    doc = {"document_id": 7, "filename": NAME, "gdrive_id": src, "sidecar_id": side,
           "engine": engine or "", "reasons": ["cowork_ocr"], "explicit": True,
           "trusted_codes": None if trusted is None else list(trusted)}  # fmt: skip

    def reader(*a, **k):
        return reocr.Reread(list(pages), "vision2", "qwen-local", page_ink=list(ink or []),
                            page_text_layer=[False] * len(ink or []))  # fmt: skip

    return drive, src, side, old, doc, reader


def _go(vocab, pages, *, dry_run=False, **kw):
    drive, src, side, old, doc, reader = _setup(pages, **kw)
    rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=dry_run, now=NOW,
                          reader=reader)  # fmt: skip
    return rep, rep.documents[0], drive, side, old


def _candidates(drive) -> list[str]:
    return sorted(n for n in drive.names(drive.folder[LOGS])
                  if n.startswith(reocr.CANDIDATE_PREFIX))  # fmt: skip


def _originals_untouched(drive, src):
    assert drive.items[src]["content"] == SOURCE and drive.items[src]["name"] == NAME
    assert not any(i["trashed"] for i in drive.items.values())  # nothing is deleted


# --- 1. engine detection ------------------------------------------------------------


@pytest.mark.parametrize(
    "engine",
    [
        "tesseract-eng",
        "tesseract-eng psm3 120dpi",
        "tesseract-eng psm3",
        "tesseract-eng (synthetic free text, second try)",
        '"tesseract-eng psm3"',
        "'Tesseract-ENG'",
        "TESSERACT 5",
    ],
)
def test_a_tesseract_engine_selects_the_profile(engine):
    old = _front(engine) + OLD_BODY
    assert reocr.is_tesseract_engine(reocr.sidecar_engine(old))
    assert reocr.guard_profile(old) == reocr.TESSERACT_PROFILE == "tesseract_baseline"


@pytest.mark.parametrize(
    "engine", ["pdftotext", "qwen3-vl-local", "none", "", "pymupdf", "cloud tesseract", None]
)
def test_any_other_engine_keeps_the_standard_rules(engine):
    old = _front(engine) + OLD_BODY
    assert not reocr.is_tesseract_engine(reocr.sidecar_engine(old))
    assert reocr.guard_profile(old) is None


def test_no_front_matter_or_another_key_is_not_the_profile():
    assert reocr.sidecar_engine(OLD_BODY) == "" and reocr.guard_profile(OLD_BODY) is None
    # ``extraction`` (what older fixtures carry) is not the engine key
    old = "---\nextraction: tesseract eng-only\n---\n\n" + OLD_BODY
    assert reocr.guard_profile(old) is None
    # an ``ocr_engine`` line in the BODY is text, not front matter
    assert reocr.guard_profile(_front("pdftotext") + "ocr_engine: tesseract\n") is None


def test_the_constants_of_the_profile():
    assert list(reocr.BASELINE_UNTRUSTED_REASONS) == [*UNTRUSTED, "near_empty_with_loss"]
    assert reocr.BLANK_INK_MAX == 0.005 and reocr.NEAR_EMPTY_CHARS == 50
    assert reocr.UNCERTAIN_SHARE_MAX == 0.15 and reocr.PROMOTE_MAX_DOCS == 20
    assert config.REOCR_TESSERACT_PROFILE is True  # on by default


# --- 2. same texts, two engines --------------------------------------------------------


async def test_another_engine_reviews_with_the_current_reasons(vocab):
    _, d, drive, side, old = _go(vocab, [NEW_PAGE], engine="pdftotext")
    assert d.result == "review" and d.decision == "review"
    assert d.review_reasons == UNTRUSTED  # the standard rules, unchanged
    assert d.profile is None and d.baseline_untrusted == [] and d.uncertain_share is None
    assert drive.items[side]["content"] == old.encode()
    [name] = _candidates(drive)
    head, _ = parse_sidecar(drive.items[drive.find(drive.folder[LOGS], name)]["content"].decode())
    assert "profile" not in head and "baseline_untrusted" not in head
    inner = reocr.candidate_sidecar_text(
        drive.items[drive.find(drive.folder[LOGS], name)]["content"].decode()
    )
    assert "replaced_engine" not in inner


async def test_a_tesseract_baseline_is_rewritten_and_says_what_it_muted(vocab):
    rep, d, drive, side, old = _go(vocab, [NEW_PAGE])
    assert d.result == "rewritten" and d.decision == "rewrite" and d.review_reasons == []
    assert d.profile == "tesseract_baseline" and d.baseline_untrusted == UNTRUSTED
    assert d.lost_codes == {"HGB": 1}  # still computed and reported
    assert d.missing_numbers > reocr.REVIEW_MISSING_NUMBERS and d.missing_dates == 0
    assert d.word_overlap is not None and d.word_overlap < reocr.MIN_WORD_OVERLAP
    assert d.uncertain_share == 0.0
    # the usual backup of the old sidecar first, then the update
    backup = f"_reocr-backup-{NOW:%Y%m%d-%H%M%S}-{NAME}.md"
    assert drive.writes == [("create", backup), ("update", NAME + ".md")]
    assert drive.items[d.backup]["content"] == old.encode()
    assert _candidates(drive) == []
    front, pages = parse_sidecar(drive.items[side]["content"].decode())
    assert front["ocr_engine"] == "qwen-local" and front["extraction"] == "vision2"
    assert front["replaced_engine"] == TESSERACT and pages == [NEW_PAGE]
    entry = rep.to_dict()["documents"][0]
    assert entry["profile"] == "tesseract_baseline" and entry["baseline_untrusted"] == UNTRUSTED


async def test_a_dry_run_reports_the_profile_and_writes_nothing(vocab):
    _, d, drive, side, old = _go(vocab, [NEW_PAGE], dry_run=True)
    assert d.result == "previewed" and d.decision == "rewrite"
    assert d.profile == "tesseract_baseline" and d.baseline_untrusted == UNTRUSTED
    assert drive.writes == [] and drive.items[side]["content"] == old.encode()


def test_replaced_engine_is_short_and_single_line():
    long = "tesseract-eng (synthetic: free text\twith a colon, a # and many more words after it)"
    rr = reocr.Reread(["Strana"], "vision2", "qwen-local")
    new = reocr.rewrite_sidecar(_front(long) + "x\n", rr, previous_engine="", now=NOW,
                                replaced_engine=long)  # fmt: skip
    value = parse_sidecar(new)[0]["replaced_engine"]
    assert value.startswith("tesseract-eng (synthetic") and len(value) <= 40
    assert ":" not in value and "#" not in value and "\t" not in value
    # without the profile the key is not written
    plain = reocr.rewrite_sidecar(_front(long) + "x\n", rr, previous_engine="", now=NOW)
    assert "replaced_engine" not in plain


# --- 3. what still blocks under the profile ---------------------------------------------

SHORT = "Kontrola zo dňa 14.06.2027\nRiadok alfa 731,4"  # far below 0.6 of the old body
MARKED = "\n".join(
    line + " [?]" if line.startswith(("Riadok alfa", "Riadok beta")) else line for line in NEW_LINES
)  # 2 of 9 numeric tokens sit on a [?] line
OLD_WITH_TIME = OLD_BODY + "Odber: 21.06.2027 07:40\n"  # a sampling timestamp


@pytest.mark.parametrize(
    "pages,old_body,reason",
    [
        ([SHORT], OLD_BODY, "text_shrunk"),
        ([NEW_PAGE, "  Strana  "], OLD_BODY, "near_empty_unknown_ink"),  # ink not measured
        ([NEW_PAGE], OLD_WITH_TIME, "missing_dates"),
        ([MARKED], OLD_BODY, "uncertain_share_high"),
    ],
)
async def test_these_still_review_and_keep_a_candidate(vocab, pages, old_body, reason):
    _, d, drive, side, old = _go(vocab, pages, old_body=old_body)
    assert d.result == "review" and d.decision == "review" and d.profile == "tesseract_baseline"
    assert reason in d.review_reasons
    assert not set(d.review_reasons) & set(reocr.BASELINE_UNTRUSTED_REASONS)  # never block
    assert set(d.baseline_untrusted) <= set(reocr.BASELINE_UNTRUSTED_REASONS)
    assert d.baseline_untrusted
    assert d.backup is None and drive.items[side]["content"] == old.encode()
    [name] = _candidates(drive)  # the new text is kept as a candidate, as today
    assert drive.writes == [("create", name)]
    text = drive.items[d.candidate]["content"].decode()
    head, _ = parse_sidecar(text)
    assert head["profile"] == "tesseract_baseline" and reason in str(head["review_reasons"])
    assert reocr.candidate_sidecar_text(text) is not None


async def test_the_only_blocking_reason_can_be_the_uncertain_share(vocab):
    _, d, *_ = _go(vocab, [MARKED])
    assert d.review_reasons == ["uncertain_share_high"] and d.uncertain_share == 0.222
    # the same marked text against another engine: the share is not a reason there
    _, other, *_ = _go(vocab, [MARKED], engine="pdftotext")
    assert "uncertain_share_high" not in other.review_reasons and other.uncertain_share is None


def test_uncertain_share_counts_numbers_on_marked_lines():
    assert reocr.uncertain_share("") == 0.0 and reocr.uncertain_share("bez cisel [?]") == 0.0
    text = "---\ndoc_date: 2027-06-14\n---\n\n<!-- page 1 -->\na 917 b 4862\nc 7305 [?]\nd 609,5\n"
    assert reocr.uncertain_share(text) == 0.25  # front matter and page markers ignored
    # a date is one token, wherever the marker stands on its line
    assert reocr.uncertain_share("Odber 21.06.2027 [?] 07:40\nx 55") == 0.75
    # at the limit is still fine, above it is not
    at = "\n".join(["r 71 [?]", "r 72 [?]", "r 73 [?]"] + [f"r {80 + i}" for i in range(17)])
    assert reocr.uncertain_share(at) == 0.15
    assert reocr.profile_reasons(reocr.TESSERACT_PROFILE, [], at)[0] == []
    above = at + " [?]"
    assert reocr.profile_reasons(reocr.TESSERACT_PROFILE, [], above)[0] == ["uncertain_share_high"]
    assert reocr.profile_reasons(None, ["missing_numbers"], above) == (
        ["missing_numbers"],
        [],
        None,
    )


def test_missing_dates_counts_a_sampling_timestamp_by_its_date():
    old = "Odber: 21.06.2027 07:40\nRiadok 55\n"
    assert reocr.token_loss(old, "Riadok 55")["missing_dates"] == 1
    # the same day printed another way, or with another time, is not lost
    for new in ("Odber 21.6.2027 07:45\nRiadok 55", "Odber: 2027-06-21\nRiadok 55"):
        assert reocr.token_loss(old, new)["missing_dates"] == 0
    # a date the old text mangled does not parse, so it cannot count as missing
    assert reocr.token_loss("Odber: 2I.06.2O27 07:40\nRiadok 55", "Riadok 55")["missing_dates"] == 0


# --- 3b. near-empty pages are judged by their own ink ------------------------------------

BACK = "  Strana  "  # a near-empty second page
TWO = [NEW_PAGE, BACK]
LOW, HIGH = [0.2113, 0.0041], [0.2113, 0.0068]  # page 2 below / above BLANK_INK_MAX
UNTRUSTED_NEAR = ["lost_values", "missing_numbers", "near_empty_with_loss", "low_word_overlap"]


def test_near_empty_by_ink_rules():
    f = reocr.near_empty_by_ink
    assert f([], LOW, [False, False]) == ([], [])
    assert f([2], LOW, [False, False]) == ([2], [])  # blank back side
    assert f([2], LOW, None) == ([2], [])  # an image has no text layer at all
    assert f([2], HIGH, [False, False]) == ([], ["near_empty_page_with_ink"])
    assert f([2], [0.2113, reocr.BLANK_INK_MAX], [False, False])[1] == ["near_empty_page_with_ink"]
    assert f([2], LOW, [False, True]) == ([], ["near_empty_page_with_ink"])  # a text layer
    for unknown in (None, [], [0.2113], [0.2113, None], [0.2113, "x"], [0.2113, True]):
        assert f([2], unknown, None) == ([], ["near_empty_unknown_ink"])
    mixed = HIGH[-1:] + LOW[-1:]  # first page with ink, second blank, third unknown
    three = [n + len(mixed) - len(TWO) + 1 for n in range(len(mixed) + 1)]
    assert f(three, mixed, None) == (
        [three[1]], ["near_empty_page_with_ink", "near_empty_unknown_ink"]
    )  # fmt: skip


async def test_a_blank_back_side_is_rewritten_under_the_profile_only(vocab):
    _, d, drive, side, old = _go(vocab, TWO, ink=LOW)
    assert d.result == "rewritten" and d.decision == "rewrite" and d.review_reasons == []
    assert d.near_empty_pages == [2] and d.blank_pages == [2]  # no old page markers needed
    assert d.baseline_untrusted == UNTRUSTED_NEAR  # what it would have been, still visible
    assert [w[0] for w in drive.writes] == ["create", "update"] and d.backup
    assert parse_sidecar(drive.items[side]["content"].decode())[1] == [NEW_PAGE, BACK.strip()]
    # the same read against another engine: the standard rules, as today
    _, other, drive_b, side_b, old_b = _go(vocab, TWO, ink=LOW, engine="pdftotext")
    assert other.result == "review" and other.review_reasons == UNTRUSTED_NEAR
    assert other.blank_pages == [] and other.profile is None
    assert drive_b.items[side_b]["content"] == old_b.encode()


async def test_a_near_empty_page_with_ink_is_a_stop_on_its_own(vocab):
    _, d, drive, side, old = _go(vocab, TWO, ink=HIGH)
    assert d.result == "review" and d.review_reasons == ["near_empty_page_with_ink"]
    assert d.blank_pages == [] and d.baseline_untrusted == UNTRUSTED_NEAR
    assert drive.items[side]["content"] == old.encode()
    # regardless of missing numbers: an old text that loses nothing still stops
    drive_x, _, side_x, old_x, doc_x, reader_x = _setup(TWO, ink=HIGH, old_body=NEW_PAGE + "\n")
    [dx] = reocr.run_reocr(drive_x, drive_x.root, [doc_x], vocab, dry_run=True, now=NOW,
                           reader=reader_x).documents  # fmt: skip
    assert dx.missing_numbers == 0 and dx.baseline_untrusted == []
    assert dx.review_reasons == ["near_empty_page_with_ink"]
    # the candidate header records the ink of every page
    text = drive.items[d.candidate]["content"].decode()
    assert f"page_ink: {json.dumps(HIGH)}" in text and "page_text_layer: [false, false]" in text
    assert reocr.candidate_page_ink(text) == (HIGH, [False, False])
    assert parse_sidecar(reocr.candidate_sidecar_text(text))[1] == [NEW_PAGE, BACK.strip()]


async def test_a_single_page_document_is_unaffected_by_the_ink_rule(vocab):
    _, d, *_ = _go(vocab, [NEW_PAGE], ink=[0.2113])
    assert d.result == "rewritten" and d.near_empty_pages == [] and d.blank_pages == []
    assert d.baseline_untrusted == UNTRUSTED


def test_candidate_page_ink_of_other_headers():
    old_style = "---\nkind: reocr_candidate\ncreated: 2027-05-03\n---\n---\nx: 1\n---\n\nText\n"
    assert reocr.candidate_page_ink(old_style) == (None, None)
    assert reocr.candidate_page_ink("no header") == (None, None)
    broken = old_style.replace("created:", "page_ink: [0.1,\ncreated:")
    assert reocr.candidate_page_ink(broken) == (None, None)
    # a ``page_ink`` line in the sidecar text below the header is not the header
    below = old_style.replace("x: 1", "page_ink: [0.001]")
    assert reocr.candidate_page_ink(below) == (None, None)


@pytest.mark.parametrize(
    "ink,result,reasons,blank",
    [
        (LOW, "promoted", [], [2]),
        (HIGH, "review", ["near_empty_page_with_ink"], []),
        (None, "review", ["near_empty_unknown_ink"], []),  # a candidate kept without ink
    ],
)
async def test_promotion_applies_the_same_ink_rule(vocab, ink, result, reasons, blank):
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab, TWO, ink=ink)
    raw = drive.items[d.candidate]["content"].decode()
    assert ("page_ink:" in raw) is (ink is not None)
    out = _promote(drive, [doc], vocab, dry_run=False)
    [e] = out["documents"]
    assert e["result"] == result and e["review_reasons"] == reasons
    assert e["blank_pages"] == blank and e["near_empty_pages"] == [2]
    assert e["baseline_untrusted"] == UNTRUSTED_NEAR
    if result == "promoted":
        assert [w[0] for w in drive.writes] == ["create", "update"]
        assert parse_sidecar(drive.items[side]["content"].decode())[1] == [NEW_PAGE, BACK.strip()]
    else:
        assert drive.writes == [] and drive.items[side]["content"] == old.encode()
    _originals_untouched(drive, src)


# --- 4. the env switch -------------------------------------------------------------------


async def test_env_switch_off_restores_the_standard_rules(vocab, monkeypatch):
    monkeypatch.setattr(config, "REOCR_TESSERACT_PROFILE", False)
    assert reocr.guard_profile(_front(TESSERACT) + OLD_BODY) is None
    _, d, drive, side, old = _go(vocab, [NEW_PAGE])
    assert d.result == "review" and d.review_reasons == UNTRUSTED
    assert d.profile is None and d.baseline_untrusted == []
    assert drive.items[side]["content"] == old.encode()
    inner = reocr.candidate_sidecar_text(drive.items[d.candidate]["content"].decode())
    assert "replaced_engine" not in inner


def test_env_switch_is_read_from_the_environment(monkeypatch):
    import importlib

    try:
        for value, expected in (("false", False), ("0", False), ("OFF", False), ("true", True)):
            monkeypatch.setenv("REOCR_TESSERACT_PROFILE", value)
            assert importlib.reload(config).REOCR_TESSERACT_PROFILE is expected
        monkeypatch.delenv("REOCR_TESSERACT_PROFILE")
        assert importlib.reload(config).REOCR_TESSERACT_PROFILE is True
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# --- 5. promotion of candidates kept before the profile (core) ---------------------------


def _reviewed_before_the_profile(
    vocab, pages=(NEW_PAGE,), *, engine=TESSERACT, old_body=OLD_BODY, ink=None
):
    """A candidate written by a run that still used the standard rules."""
    drive, src, side, old, doc, reader = _setup(
        list(pages), engine=engine, old_body=old_body, ink=ink
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config, "REOCR_TESSERACT_PROFILE", False)
        rep = reocr.run_reocr(drive, drive.root, [doc], vocab, dry_run=False, now=NOW,
                              reader=reader)  # fmt: skip
    [d] = rep.documents
    assert d.result == "review" and d.candidate
    drive.writes.clear()
    keep = ("document_id", "filename", "sidecar_id", "trusted_codes")
    return drive, src, side, old, {k: doc[k] for k in keep}, d


def _promote(drive, docs, vocab, **kw):
    kw.setdefault("now", NOW + timedelta(hours=1))
    return reocr.promote_candidates(drive, drive.root, docs, vocab, **kw)


async def test_promote_dry_run_reports_the_decision_and_writes_nothing(vocab):
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab)
    out = _promote(drive, [doc], vocab)
    [e] = out["documents"]
    assert out["dry_run"] is True and out["profile"] == "tesseract_baseline"
    assert e["result"] == "previewed" and e["decision"] == "rewrite" and e["review_reasons"] == []
    assert e["baseline_untrusted"] == UNTRUSTED and e["profile"] == "tesseract_baseline"
    assert e["candidate"] == d.candidate and e["candidates"] == 1 and e["lost_codes"] == {"HGB": 1}
    assert drive.writes == [] and drive.items[side]["content"] == old.encode()
    _originals_untouched(drive, src)


async def test_promote_backs_up_then_writes_the_candidate_and_keeps_it(vocab):
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab)
    later = NOW + timedelta(hours=1)
    out = _promote(drive, [doc], vocab, dry_run=False, now=later)
    [e] = out["documents"]
    assert e["result"] == "promoted" and e["review_reasons"] == []
    name = f"_restore-backup-{later:%Y%m%d-%H%M%S}-{NAME}.md"
    assert drive.writes == [("create", name), ("update", NAME + ".md")]  # backup first
    assert drive.items[e["restore_backup_id"]]["content"] == old.encode()
    front, pages = parse_sidecar(drive.items[side]["content"].decode())
    assert front["ocr_engine"] == "qwen-local" and front["replaced_engine"] == TESSERACT
    assert pages == [NEW_PAGE] and "reocr_candidate" not in drive.items[side]["content"].decode()
    assert _candidates(drive) and not drive.items[d.candidate]["trashed"]  # stays in logs
    _originals_untouched(drive, src)
    # the sidecar is the local model's now: a second call finds nothing to do
    again = _promote(drive, [doc], vocab, dry_run=False)
    assert again["documents"] == [] and again["not_tesseract"] == 1
    assert len(drive.writes) == 2


async def test_a_candidate_that_still_fails_is_not_promoted(vocab):
    drive, src, side, old, doc, _ = _reviewed_before_the_profile(vocab, old_body=OLD_WITH_TIME)
    out = _promote(drive, [doc], vocab, dry_run=False)
    [e] = out["documents"]
    assert e["result"] == "review" and e["decision"] == "review"
    assert e["review_reasons"] == ["missing_dates"] and e["baseline_untrusted"] == UNTRUSTED
    assert "restore_backup_id" not in e
    assert drive.writes == [] and drive.items[side]["content"] == old.encode()
    _originals_untouched(drive, src)


async def test_a_non_tesseract_sidecar_is_refused(vocab):
    drive, src, side, old, doc, _ = _reviewed_before_the_profile(vocab, engine="pdftotext")
    out = _promote(drive, [doc], vocab, dry_run=False, explicit=True)
    [e] = out["documents"]
    assert e["result"] == "refused" and e["error"] == "not_tesseract_sidecar"
    assert "review_reasons" not in e  # refused before any evaluation
    auto = _promote(drive, [doc], vocab, dry_run=False)  # automatic selection: only counted
    assert auto["documents"] == [] and auto["not_tesseract"] == 1
    assert drive.writes == [] and drive.items[side]["content"] == old.encode()


async def test_documents_without_a_candidate(vocab):
    drive, src, side, old, doc, reader = _setup([NEW_PAGE])
    doc = {k: doc[k] for k in ("document_id", "filename", "sidecar_id", "trusted_codes")}
    out = _promote(drive, [doc], vocab, dry_run=False, explicit=True)
    assert out["documents"] == [
        {"document_id": 7, "candidates": 0, "result": "refused", "error": "no_candidate"}
    ]
    auto = _promote(drive, [doc], vocab, dry_run=False)
    assert auto["documents"] == [] and auto["without_candidate"] == 1
    assert drive.writes == []


async def test_the_newest_candidate_is_the_one_evaluated(vocab):
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab)
    older = f"{reocr.CANDIDATE_PREFIX}20270101-080000-{NAME}.md"
    stale = drive.items[d.candidate]["content"].decode().replace("731,4", "731,4\nRiadok navyse 99")
    drive.add(older, drive.folder[LOGS], stale.encode())
    out = _promote(drive, [doc], vocab, dry_run=False)
    [e] = out["documents"]
    assert e["candidates"] == 2 and e["candidate"] == d.candidate and e["result"] == "promoted"
    assert "navyse" not in drive.items[side]["content"].decode()
    assert len(_candidates(drive)) == 2  # both stay


async def test_a_malformed_candidate_and_a_failed_write(vocab):
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab)
    drive.fail_on.add("update_text")
    out = _promote(drive, [doc], vocab, dry_run=False)
    [e] = out["documents"]
    assert e["result"] == "failed" and e["error"] == "WriteVerificationError"
    drive.fail_on.clear()
    newer = f"{reocr.CANDIDATE_PREFIX}20271231-080000-{NAME}.md"
    drive.add(newer, drive.folder[LOGS], b"Riadok bez hlavicky\n")
    out = _promote(drive, [doc], vocab, dry_run=False)
    assert out["documents"][0]["error"] == "candidate_malformed"
    assert drive.items[side]["content"] == old.encode()


async def test_automatic_selection_is_capped(vocab):
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab)
    second = "20270615-JanaTestova-LabX-Report-Druha.pdf"
    side_b = drive.add(second + ".md", drive.folder[LABS], old.encode())
    cand = drive.items[d.candidate]["content"]
    drive.add(f"{reocr.CANDIDATE_PREFIX}20270503-210000-{second}.md", drive.folder[LOGS], cand)
    docs = [doc, {"document_id": 8, "filename": second, "sidecar_id": side_b}]
    out = _promote(drive, docs, vocab, limit=1)
    assert [e["document_id"] for e in out["documents"]] == [7] and out["remaining"] == 1
    assert len(_promote(drive, docs, vocab)["documents"]) == 2


async def test_a_candidate_still_hides_the_document_from_automatic_selection(vocab):
    """#827 stays: a batch run does not read the scan again; promotion is the way."""
    drive, src, side, old, doc, d = _reviewed_before_the_profile(vocab)
    name = drive.items[d.candidate]["name"]
    assert reocr.handled_before([name], NAME)
    full = {**doc, "gdrive_id": src, "engine": TESSERACT, "reasons": ["cowork_ocr"]}
    rep = reocr.run_reocr(drive, drive.root, [full], vocab, dry_run=False, now=NOW,
                          reader=lambda *a, **k: pytest.fail("read again"))  # fmt: skip
    assert rep.documents[0].result == "skipped" and drive.writes == []


# --- 6. the MCP tool ---------------------------------------------------------------------


async def _call(db, **kw):
    return json.loads(await tool.curated_promote_candidates(_ctx(db), **kw))


async def _activity(db) -> list[dict]:
    async with db.db.execute(
        "SELECT patient_id, input_summary, output_summary, status FROM activity_log "
        "WHERE tool_name = 'curated_promote_candidates' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_tool_requires_admin(db, vocab):
    out = await _call(db, dry_run=False)
    assert "requires admin scope" in out["error"]


@pytest.mark.usefixtures("admin_scope")
async def test_tool_dry_run_then_real_promotion(db, vocab):
    drive, src, side, old, _, d = _reviewed_before_the_profile(vocab)
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with _wired(drive) as (index, entered):
        preview = await _call(db)
        assert preview["dry_run"] is True and preview["results"] == {"previewed": 1}
        assert drive.writes == [] and entered == [] and index.await_count == 0
        assert "pre_store" not in preview and await _activity(db) == []
        assert tool._restoring[ERIKA_UUID]["state"] == "done" and not tool.run_active()

        out = await _call(db, document_ids=[801], dry_run=False)
    assert out["results"] == {"promoted": 1} and out["pre_store"] == "synthetic-copy.db"
    [e] = out["documents"]
    assert e["document_id"] == 801 and e["baseline_untrusted"] == UNTRUSTED
    assert entered == [True]  # written inside contract_writes()
    assert [w[0] for w in drive.writes] == ["create", "update"]
    assert drive.writes[0][1].startswith("_restore-backup-")
    assert drive.items[e["restore_backup_id"]]["content"] == old.encode()
    assert parse_sidecar(drive.items[side]["content"].decode())[1] == [NEW_PAGE]
    index.assert_awaited_once()
    assert index.await_args.kwargs["force_ids"] == {801}
    _originals_untouched(drive, src)
    assert tool._restoring[ERIKA_UUID]["kind"] == "promote"
    # report and activity_log: ids, counts and reason codes only
    [row] = await _activity(db)
    assert row["patient_id"] == ERIKA_UUID and row["status"] == "ok"
    inp, outp = json.loads(row["input_summary"]), json.loads(row["output_summary"])
    assert inp["document_ids"] == [801] and inp["profile"] == "tesseract_baseline"
    assert outp["promoted"] == [801] and outp["results"] == {"promoted": 1}
    assert outp["baseline_untrusted"] == dict.fromkeys(sorted(UNTRUSTED), 1)
    blob = json.dumps([out, preview, row])
    assert not [t for t in TOKENS if t in blob] and NAME not in blob


@pytest.mark.usefixtures("admin_scope")
async def test_tool_keeps_a_failing_candidate_and_never_reindexes(db, vocab):
    drive, src, side, old, _, _ = _reviewed_before_the_profile(vocab, old_body=OLD_WITH_TIME)
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with _wired(drive) as (index, _):
        out = await _call(db, dry_run=False)
    assert out["results"] == {"review": 1}
    assert out["documents"][0]["review_reasons"] == ["missing_dates"]
    assert drive.writes == [] and index.await_count == 0
    assert drive.items[side]["content"] == old.encode()


@pytest.mark.usefixtures("admin_scope")
async def test_tool_refuses_other_documents_and_engines(db, vocab):
    drive, src, side, old, _, _ = _reviewed_before_the_profile(vocab, engine="pdftotext")
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with _wired(drive) as (index, _):
        out = await _call(db, document_ids=[801, 999], dry_run=False)
    by_id = {e["document_id"]: e for e in out["documents"]}
    assert by_id[999] == {"document_id": 999, "result": "refused", "error": "not_found"}
    assert by_id[801]["error"] == "not_tesseract_sidecar"
    assert drive.writes == [] and index.await_count == 0


@pytest.mark.usefixtures("admin_scope")
async def test_tool_refuses_while_the_patient_is_busy(db, vocab):
    drive, src, side, old, _, _ = _reviewed_before_the_profile(vocab)
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with _wired(drive) as (index, _):
        tool._reocr_jobs[ERIKA_UUID] = {"kind": "reocr", "state": "running"}
        assert (await _call(db, dry_run=False))["error"].startswith("busy:")
        tool._reocr_jobs.clear()
        tool._runs[ERIKA_UUID] = {"state": "running"}
        assert (await _call(db, dry_run=False))["error"].startswith("busy:")
        tool._runs.clear()
        tool._restoring[ERIKA_UUID] = {"kind": "restore", "state": "running"}
        assert (await _call(db, dry_run=False))["error"].startswith("busy:")
        tool._restoring.clear()
        async with tool.patient_lock(ERIKA_UUID):
            assert (await _call(db, dry_run=False))["error"].startswith("busy:")
    assert drive.writes == [] and index.await_count == 0
    assert drive.items[side]["content"] == old.encode()


@pytest.mark.usefixtures("admin_scope")
async def test_tool_limits_and_the_env_switch(db, vocab, monkeypatch):
    drive, src, side, old, _, _ = _reviewed_before_the_profile(vocab)
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with _wired(drive) as (index, _):
        many = await _call(db, document_ids=list(range(1, 22)), dry_run=False)
        assert many == {"error": "refused: at most 20 documents per call"}
        monkeypatch.setattr(config, "REOCR_TESSERACT_PROFILE", False)
        off = await _call(db, dry_run=False)
        assert off == {"error": "refused: tesseract_profile_disabled"}
    assert drive.writes == [] and ERIKA_UUID not in tool._restoring


@pytest.mark.usefixtures("admin_scope")
async def test_tool_without_a_pre_store_copy_writes_nothing(db, vocab):
    from unittest.mock import patch

    from oncofiles.pre_store import PreStoreError

    drive, src, side, old, _, _ = _reviewed_before_the_profile(vocab)
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    with (
        _wired(drive) as (index, entered),
        patch("oncofiles.pre_store.make_copy", side_effect=PreStoreError("synthetic")),
    ):
        out = await _call(db, dry_run=False)
    assert out["error"].startswith("refused: pre-store copy failed")
    assert drive.writes == [] and entered == [] and index.await_count == 0
    assert tool._restoring[ERIKA_UUID]["state"] == "failed" and not tool.run_active()


def test_a_running_promotion_counts_as_an_active_run():
    tool._restoring["pid-831"] = {"kind": "promote", "state": "running", "dry_run": False}
    assert tool.run_active() is True
    tool._restoring["pid-831"]["state"] = "done"
    assert tool.run_active() is False


def test_the_tool_is_registered_like_the_restore_tool():
    from unittest.mock import MagicMock

    mcp = MagicMock()
    tool.register(mcp)
    registered = [c.args[0].__name__ for c in mcp.tool.return_value.call_args_list]
    assert "curated_promote_candidates" in registered and "curated_restore_sidecar" in registered


# --- 7. reports and logs carry no values ---------------------------------------------------

_PROFILE_LOG = r"\b(?:profile|baseline_untrusted|uncertain_share|dry_run|patient|documents)=\S+"


@pytest.mark.parametrize("dry_run", [True, False])
async def test_a_profile_report_and_log_carry_no_values(db, vocab, caplog, dry_run):
    import re

    caplog.set_level(logging.DEBUG)
    cases = [  # rewrite; review with a kept candidate
        ([NEW_PAGE], OLD_BODY, "rewrite"),
        ([MARKED], OLD_WITH_TIME, "review"),
    ]
    reports, texts = [], []
    for pages, old_body, decision in cases:
        rep, d, drive, side, old = _go(vocab, pages, dry_run=dry_run, old_body=old_body)
        assert d.decision == decision and d.profile == "tesseract_baseline"
        reports.append(rep.to_dict())
        texts += [old, *pages]
    # a real job's activity_log row: counts of the muted reason codes
    job = {"kind": "reocr", "dry_run": False, "state": "done", "documents": [],
           "report": reports[0]}  # fmt: skip
    await tool._audit_real_job(db, ERIKA_UUID, job)
    async with db.db.execute(
        "SELECT output_summary FROM activity_log WHERE tool_name = 'curated_reocr'"
    ) as cur:
        [row] = await cur.fetchall()
    summary = json.loads(row["output_summary"])
    assert summary["baseline_untrusted"] == dict.fromkeys(sorted(UNTRUSTED), 1)
    assert summary["profile_documents"] == 1
    # the promotion report of a kept candidate
    drive, src, side, old, doc, _ = _reviewed_before_the_profile(vocab)
    promoted = _promote(drive, [doc], vocab, dry_run=dry_run)

    sensitive = _sensitive(texts)
    assert {"637", "6,37", "731,4", "7314", "21.06.2027"} <= sensitive  # the guard sees them
    for label, obj in (("reocr", reports), ("promote", promoted), ("activity", summary)):
        haystack = "\n".join(_strings(json.loads(json.dumps(obj, default=str))))
        assert _leaks(haystack, sensitive) == [], label
    logged = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("oncofiles"))
    assert "re-OCR loss check" in logged and "re-OCR candidate check" in logged
    assert "profile=tesseract_baseline" in logged
    assert "baseline_untrusted=lost_values,missing_numbers,low_word_overlap" in logged
    cleaned = re.sub(_PROFILE_LOG, " ", _LOG_COUNTS.sub(" ", logged))
    assert _leaks(cleaned, sensitive) == []


# --- 8. a lost code with a trusted stored fact still blocks (infra condition) ---------------


def test_trusted_loss_rules():
    f = reocr.trusted_loss
    profile = reocr.TESSERACT_PROFILE
    assert f(profile, {"HGB": 1}, ["HGB", "PLT"]) == ({"HGB": 1}, "lost_trusted_values")
    several = dict.fromkeys(["PLT", "unknown"], 4)  # a code that is not a code never matches
    assert f(profile, several, {"PLT"}) == ({"PLT": 4}, "lost_trusted_values")
    assert f(profile, {"HGB": 1}, ["PLT"]) == ({}, None)  # another code is trusted
    assert f(profile, {"HGB": 1}, []) == ({}, None)  # no trusted stored fact
    assert f(profile, {"HGB": 1}, None) == ({}, "lost_trusted_values_unknown")  # fails closed
    assert f(profile, {}, None) == ({}, None)  # nothing lost: nothing to protect
    assert f(None, {"HGB": 1}, ["HGB"]) == ({}, None)  # the standard rules have lost_values


async def test_a_lost_trusted_code_still_reviews_and_keeps_a_candidate(vocab):
    _, d, drive, side, old = _go(vocab, [NEW_PAGE], trusted=["HGB"])
    assert d.result == "review" and d.review_reasons == ["lost_trusted_values"]
    assert d.lost_trusted_codes == {"HGB": 1} and d.baseline_untrusted == UNTRUSTED
    assert drive.items[side]["content"] == old.encode() and d.backup is None
    [name] = _candidates(drive)
    head, _ = parse_sidecar(drive.items[d.candidate]["content"].decode())
    assert "lost_trusted_values" in str(head["review_reasons"])
    # a trusted fact of a code that is not lost does not block
    _, other, *_ = _go(vocab, [NEW_PAGE], trusted=["PLT"])
    assert other.result == "rewritten" and other.lost_trusted_codes == {}
    # unknown stored facts fail closed
    _, unknown, drive_u, side_u, old_u = _go(vocab, [NEW_PAGE], trusted=None)
    assert unknown.result == "review"
    assert unknown.review_reasons == ["lost_trusted_values_unknown"]
    assert drive_u.items[side_u]["content"] == old_u.encode()


async def test_the_standard_rules_ignore_trusted_codes(vocab):
    for trusted in (["HGB"], [], None):
        _, d, *_ = _go(vocab, [NEW_PAGE], engine="pdftotext", trusted=trusted)
        assert d.review_reasons == UNTRUSTED and d.lost_trusted_codes == {}


async def _fact(db, doc_id: int, code: str, verification: str, *, deleted: bool = False):
    await db.db.execute(
        "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
        "code, verification, source, deleted_at) VALUES (?, 'lab', ?, ?, ?, ?, 't', ?)",
        (ERIKA_UUID, doc_id, code, code, verification, "2027-05-01" if deleted else None),
    )
    await db.db.commit()


async def test_trusted_fact_codes_is_scoped_and_skips_unverified_and_deleted(db):
    from tests.helpers import TEST_PATIENT_UUID

    await _seed_doc(db, 801, ERIKA_UUID, "s801", filename=NAME)
    await _fact(db, 801, "HGB", "machine")
    await _fact(db, 801, "PLT", "verified")
    await _fact(db, 801, "CEA", "corrected")
    await _fact(db, 801, "WBC", "unverified")
    await _fact(db, 801, "CRP", "machine", deleted=True)
    got = await db.trusted_fact_codes(patient_id=ERIKA_UUID, document_ids=[801, 802])
    assert got == {801: {"HGB", "PLT", "CEA"}, 802: set()}
    other = await db.trusted_fact_codes(patient_id=TEST_PATIENT_UUID, document_ids=[801])
    assert other == {801: set()}


@pytest.mark.usefixtures("admin_scope")
@pytest.mark.parametrize(
    "verification,blocked",
    [("machine", True), ("verified", True), ("corrected", True), ("unverified", False),
     (None, False)],
)  # fmt: skip
async def test_reocr_job_and_promotion_respect_stored_trusted_facts(
    db, vocab, verification, blocked
):
    # the re-OCR job
    drive, src, side, old, doc, reader = _setup([NEW_PAGE])
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    if verification:
        await _fact(db, 801, "HGB", verification)
    doc = {k: v for k, v in doc.items() if k != "trusted_codes"} | {"document_id": 801}
    tool._reocr_jobs[ERIKA_UUID] = job = {"kind": "reocr", "state": "running",
                                          "dry_run": False, "documents": [doc]}  # fmt: skip
    with _wired(drive, lambda *a, **k: reader()) as (index, _):
        await tool._reocr_job(db, ERIKA_UUID, [doc], dry_run=False, passes=2)
    [d] = job["report"]["documents"]
    if blocked:
        assert d["result"] == "review" and d["review_reasons"] == ["lost_trusted_values"]
        assert d["lost_trusted_codes"] == {"HGB": 1} and d["candidate"]
        assert drive.items[side]["content"] == old.encode() and index.await_count == 0
    else:
        assert d["result"] == "rewritten" and d["lost_trusted_codes"] == {}

    # the promotion tool, on a candidate kept before the profile
    tool._reocr_jobs.clear()
    drive, src, side, old, _, _ = _reviewed_before_the_profile(vocab)
    await db.db.execute(
        "UPDATE documents SET structured_metadata = ? WHERE id = 801",
        (json.dumps({"curated": {"sidecar_id": side}}),),
    )
    await db.db.commit()
    with _wired(drive) as (index, _):
        out = await _call(db, document_ids=[801], dry_run=False)
    [e] = out["documents"]
    if blocked:
        assert e["result"] == "review" and e["review_reasons"] == ["lost_trusted_values"]
        assert e["lost_trusted_codes"] == {"HGB": 1}
        assert drive.writes == [] and index.await_count == 0
        assert drive.items[side]["content"] == old.encode()
    else:
        assert e["result"] == "promoted" and e["lost_trusted_codes"] == {}
    blob = json.dumps([d, out])
    assert not [t for t in TOKENS if t in blob]


@pytest.mark.usefixtures("admin_scope")
async def test_a_failed_fact_lookup_fails_closed_in_the_job_and_the_tool(db, vocab, monkeypatch):
    async def boom(**kw):
        raise RuntimeError("synthetic lookup failure")

    monkeypatch.setattr(db, "trusted_fact_codes", boom)
    drive, src, side, old, doc, reader = _setup([NEW_PAGE])
    await _seed_doc(db, 801, ERIKA_UUID, side, filename=NAME)
    doc = {k: v for k, v in doc.items() if k != "trusted_codes"} | {"document_id": 801}
    tool._reocr_jobs[ERIKA_UUID] = job = {"kind": "reocr", "state": "running",
                                          "dry_run": False, "documents": [doc]}  # fmt: skip
    with _wired(drive, lambda *a, **k: reader()) as (index, _):
        await tool._reocr_job(db, ERIKA_UUID, [doc], dry_run=False, passes=2)
        [d] = job["report"]["documents"]
        assert d["result"] == "review"
        assert d["review_reasons"] == ["lost_trusted_values_unknown"]
        assert drive.items[side]["content"] == old.encode()
        tool._reocr_jobs.clear()
        drive.writes.clear()
        out = await _call(db, document_ids=[801], dry_run=False)
    [e] = out["documents"]
    assert e["result"] == "review" and e["review_reasons"] == ["lost_trusted_values_unknown"]
    assert drive.writes == [] and index.await_count == 0
