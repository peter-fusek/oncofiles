"""Re-OCR job reports carry codes and counts only, never values (#805).

``lost_values`` compares the parsed facts of the old and the new sidecar and
returns "CODE value missing" strings. Those drive the review decision, but the
report, the job dict ``curated_reocr_status`` returns, ``curated_runs.report_json``
and the log only ever see ``lost_codes`` ({code: count}). Rows persisted before
the fix are redacted on read. Synthetic data only (invented values, dates 2027+).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter

import pytest

from oncofiles.curated import reocr
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.tools import curated as tool
from oncofiles.tools.db_query import (
    ALLOWED_TABLES,
    ALLOWED_TABLES_ADMIN_ONLY,
    ALLOWED_TABLES_PATIENT_SCOPED,
)
from tests.helpers import ERIKA_UUID
from tests.test_curated_reocr import NOW
from tests.test_curated_runner import LABS, FakeDrive


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


def _sidecar(body: str, doc_date: str = "2027-06-14") -> str:
    return (
        "---\n"
        f"doc_date: {doc_date}\n"
        "doc_type: Report\n"
        "extraction: tesseract eng-only\n"
        "handwritten: false\n"
        "---\n\n" + body
    )


FILLER = "Poznamka bez cisel, dlhsi opis vysetrenia na doplnenie rozsahu strany."

# name -> (old sidecar body, new pages); each case trips other review reasons
CASES = {
    # PLT and HGB dropped: lost_values (+ the generic counts)
    "SynthLabsAlfa.pdf": (
        "<!-- page 1 -->\nOdber 14.6.2027: WBC 6,37; HGB 133; PLT 287; CEA 2,83\n" + FILLER + "\n",
        ["Odber 14.6.2027: WBC 6,37; CEA 2,83\n" + FILLER],
    ),
    # a printed date lost and more than two numbers lost
    "SynthKontrolaBeta.pdf": (
        "Kontrola zo dna 19.08.2027\nRiadok alfa 731,4\nRiadok beta 8862\n"
        "Riadok gama 59,07\nRiadok delta 4413\n" + FILLER + "\n",
        ["Kontrola\nRiadok alfa 731,4\n" + FILLER],
    ),
    # a near-empty page together with a lost number
    "SynthKontrolaGama.pdf": (
        "Riadok sigma 6618\nRiadok tau 947,2\n" + FILLER + "\n",
        ["Riadok sigma 6618\n" + FILLER, " "],
    ),
}

# keys whose numbers are counts, ids, page numbers, dpi, token counts or seconds
COUNT_KEYS = {
    "document_id", "pages", "uncertain_before", "uncertain_after", "facts_machine",
    "facts_unverified", "missing_numbers", "missing_dates", "near_empty_pages",
    "old_chars", "new_chars", "dpi", "passes_per_page", "skipped_second_pass_pages",
    "reasoning_tokens_max", "completion_tokens_max", "budget", "seconds", "page", "pass",
    "prompt_tokens", "completion_tokens", "reasoning_tokens", "candidates",
    "unverified_facts", "backup", "lost_codes", "lost_count",
}  # fmt: skip

_DMY = r"\b(?P<d>\d\d?)\.\s?(?P<m>\d\d?)\.\s?(?P<y>\d{4})\b"
_ISO = r"\b(?P<iy>\d{4})-(?P<im>\d\d)-(?P<iday>\d\d)\b"
_DATE = re.compile(_DMY + "|" + _ISO)
_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _sensitive(texts: list[str]) -> set[str]:
    """Every rendering of every date and number printed in the texts."""
    out: set[str] = set()
    for text in texts:
        for m in _DATE.finditer(text):
            if m["y"]:
                d, mo, y = int(m["d"]), int(m["m"]), m["y"]
            else:
                y, mo, d = m["iy"], int(m["im"]), int(m["iday"])
            out |= {f"{y}-{mo:02d}-{d:02d}", f"{d}.{mo}.{y}", f"{d:02d}.{mo:02d}.{y}"}
        for tok in _NUM.findall(_DATE.sub(" ", text)):
            out |= {tok, tok.replace(",", "."), str(float(tok.replace(",", ".")))}
    return out


def _strings(obj, key=None):
    """Everything in a JSON value outside the count keys, as strings."""
    if key in COUNT_KEYS:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _strings(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v, key)
    elif obj is not None:
        yield str(obj)


def _leaks(haystack: str, sensitive: set[str]) -> list[str]:
    return sorted(
        tok for tok in sensitive if re.search(rf"(?<![\d.,]){re.escape(tok)}(?![\d])", haystack)
    )


_LOG_COUNTS = re.compile(
    r"\b(?:doc|missing_numbers|missing_dates|old_chars|new_chars|decision|reasons|page|pass|"
    r"dpi|finish|prompt_tokens|completion_tokens|reasoning_tokens|budget|seconds|error)=\S+"
    r"|near_empty_pages=\[[^\]]*\]"
)


def _drive_and_docs():
    drive = FakeDrive()
    docs, pages_of = [], {}
    for i, (name, (old_body, new_pages)) in enumerate(CASES.items(), start=1):
        src = drive.add(name, drive.folder[LABS], b"%PDF synthetic")
        side = drive.add(name + ".md", drive.folder[LABS], _sidecar(old_body).encode())
        docs.append({"document_id": i, "filename": name, "gdrive_id": src, "sidecar_id": side,
                     "engine": "tesseract eng-only", "reasons": ["cowork_ocr"],
                     "explicit": True})  # fmt: skip
        pages_of[name] = new_pages

    def reader(name, *a, **k):
        return reocr.Reread(list(pages_of[name]), "vision2", "qwen-local")

    return drive, docs, reader


def _all_texts() -> list[str]:
    return [_sidecar(old) for old, _ in CASES.values()] + [
        p for _, pages in CASES.values() for p in pages
    ]


@pytest.mark.parametrize("dry_run", [True, False])
async def test_report_job_and_log_carry_no_values(db, vocab, caplog, dry_run):
    caplog.set_level(logging.DEBUG)
    drive, docs, reader = _drive_and_docs()
    rep = reocr.run_reocr(drive, drive.root, docs, vocab, dry_run=dry_run, now=NOW,
                          reader=reader)  # fmt: skip
    by_name = {d.filename: d for d in rep.documents}

    # the decision logic is unchanged: every case goes to review
    alfa = by_name["SynthLabsAlfa.pdf"]
    assert "lost_values" in alfa.review_reasons
    assert alfa.lost_codes == {"HGB": 1, "PLT": 1}
    assert {"missing_dates", "missing_numbers"} <= set(
        by_name["SynthKontrolaBeta.pdf"].review_reasons
    )
    assert "near_empty_with_loss" in by_name["SynthKontrolaGama.pdf"].review_reasons
    assert all(d.decision == "review" for d in rep.documents)
    assert drive.writes == []
    if not dry_run:
        assert {d.result for d in rep.documents} == {"review"}

    report = rep.to_dict()
    for d in report["documents"]:
        assert "lost" not in d

    # the job dict curated_reocr_status returns while the job is in memory
    tool._reocr_jobs[ERIKA_UUID] = {
        "state": "done", "kind": "reocr", "dry_run": dry_run, "passes": 2,
        "started_at": "2027-01-02T03:04:05+00:00", "candidates": len(docs), "task": object(),
        "documents": [{k: d[k] for k in ("document_id", "filename", "reasons")} for d in docs],
        "report": report,
    }  # fmt: skip
    try:
        status = await tool.reocr_status(db, ERIKA_UUID)
        view = tool.admin_job_view(status)
    finally:
        tool._reocr_jobs.pop(ERIKA_UUID, None)

    sensitive = _sensitive(_all_texts())
    assert {"287", "133", "19.08.2027", "731,4"} <= sensitive  # the guard sees them
    for label, obj in (("report", report), ("status", status), ("view", view)):
        haystack = "\n".join(_strings(json.loads(json.dumps(obj, default=str))))
        assert _leaks(haystack, sensitive) == [], label
    assert [r["lost_count"] for r in view["results"]][0] == 2

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "re-OCR loss check" in logged
    assert _leaks(_LOG_COUNTS.sub(" ", logged), sensitive) == []


async def test_persisted_report_carries_codes_only(db, vocab):
    drive, docs, reader = _drive_and_docs()
    rep = reocr.run_reocr(drive, drive.root, docs, vocab, dry_run=True, now=NOW, reader=reader)
    row = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="reocr")
    await db.finish_curated_run(row, patient_id=ERIKA_UUID, status="ok", message="",
                                report=rep.to_dict())  # fmt: skip
    async with db.db.execute("SELECT report_json FROM curated_runs WHERE id = ?", (row,)) as cursor:
        raw = (await cursor.fetchone())[0]
    stored = json.loads(raw)
    assert all("lost" not in d for d in stored["documents"])
    haystack = "\n".join(_strings(stored))
    assert _leaks(haystack, _sensitive(_all_texts())) == []


# --- redact on read: rows written before #805 ------------------------------------

LEGACY_LOST = ["WBC 6.37 missing", "PLT 287.0 re-dated 2027-06-14->2027-06-15",
               "PLT 291.0 missing", "12.5 broken"]  # fmt: skip


# the leading code of each entry, counted; the malformed one is "unknown"
LEGACY_CODES = dict(
    sorted(
        Counter(
            head if head.isalpha() else "unknown"
            for head in (entry.split(" ")[0] for entry in LEGACY_LOST)
        ).items()
    )
)


async def _legacy_row(db) -> int:
    row = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=False, trigger="reocr")
    report = {"dry_run": False, "warnings": [], "documents": [
        {"document_id": 11, "filename": "SynthLabsAlfa.pdf", "result": "review",
         "lost": LEGACY_LOST, "facts_machine": 2, "facts_unverified": 1},
        {"document_id": 12, "filename": "SynthKontrolaBeta.pdf", "result": "rewritten"},
    ]}  # fmt: skip
    await db.finish_curated_run(row, patient_id=ERIKA_UUID, status="ok", message="",
                                report=report)  # fmt: skip
    return row


def test_lost_codes_keeps_only_the_code():
    assert reocr.lost_codes(LEGACY_LOST) == LEGACY_CODES
    assert reocr.lost_codes([]) == {}


async def test_legacy_rows_are_redacted_on_read(db):
    row = await _legacy_row(db)
    sensitive = _sensitive(LEGACY_LOST)
    assert {"287.0", "6.37", "2027-06-15"} <= sensitive

    run = await db.get_curated_run(row, patient_id=ERIKA_UUID)
    last = await db.get_last_reocr_job(patient_id=ERIKA_UUID)
    tool._reocr_jobs.pop(ERIKA_UUID, None)
    status = await tool.reocr_status(db, ERIKA_UUID)  # the MCP status after a restart
    view = tool.admin_job_view(status)
    for obj in (run, last, status, view):
        text = json.dumps(obj, default=str)
        assert '"lost"' not in text
        assert _leaks(text.replace(f'"id": {row}', ""), sensitive) == []
    doc = run["report"]["documents"][0]
    assert doc["lost_codes"] == LEGACY_CODES
    assert doc["facts_machine"] == 2  # the rest of the report is untouched
    assert run["report"]["documents"][1] == {
        "document_id": 12, "filename": "SynthKontrolaBeta.pdf", "result": "rewritten"
    }  # fmt: skip
    assert view["results"][0]["lost_count"] == 4

    # nothing was migrated: the stored row is as it was written
    async with db.db.execute("SELECT report_json FROM curated_runs WHERE id = ?", (row,)) as cursor:
        raw = (await cursor.fetchone())[0]
    assert json.loads(raw)["documents"][0]["lost"] == LEGACY_LOST


def test_redact_report_tolerates_other_shapes():
    assert reocr.redact_report(None) is None
    assert reocr.redact_report({"run_id": "x"}) == {"run_id": "x"}
    assert reocr.redact_report({"documents": [{"lost": None}, "x"]}) == {
        "documents": [{"lost_codes": {}}, "x"]
    }


def test_query_db_cannot_read_curated_runs():
    """report_json is a job report; ad-hoc SQL must never reach it (#805)."""
    for allowed in (ALLOWED_TABLES, ALLOWED_TABLES_PATIENT_SCOPED, ALLOWED_TABLES_ADMIN_ONLY):
        assert "curated_runs" not in allowed
