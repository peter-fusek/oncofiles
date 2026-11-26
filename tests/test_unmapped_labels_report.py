"""Label review list of the lab shadow preview (#814). Synthetic data only:
invented labels, values and dates (2027+); the local model is always mocked.

The report lists the labels that map to no ``fact_codes`` alias (or only
fuzzily) so aliases can be added without reading a sidecar — never a value,
a date or evidence text, digits masked, in the in-memory report only."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from unittest.mock import MagicMock

from oncofiles.cloud_guard import local_only
from oncofiles.curated import llm, llm_extract
from oncofiles.curated import scorecard as sc
from oncofiles.curated.facts_rules import Vocabulary
from tests.helpers import PRIMARY_TEST_UUID

VOCAB = Vocabulary.from_rows(
    [
        {"code": "QALT", "category": "lab", "unit_canonical": "µkat/L",
         "aliases": ["QALT"], "unit_conversions": {}},
        {"code": "ZORBITAN", "category": "lab", "unit_canonical": "mmol/L",
         "aliases": ["Zorbitanol"], "unit_conversions": {}},
    ]
)  # fmt: skip

LONG = "Velmi dlhy nazov vysetrenia ktory sa nikdy nezmesti do zoznamu poloziek"
# (label, value, unit, evidence) — invented; the values must never reach a report
ROWS = [
    ("QALT", "0,37", "µkat/l", "QALT 0,37 µkat/l"),
    ("Plumbizin", "41,93", "mg/l", "Plumbizin 41,93 mg/l"),
    ("Plumbizin", "38,17", "mg/l", "Plumbizin 38,17 mg/l"),
    ("Q7 marker", "8,26", "U/ml", "Q7 marker 8,26 U/ml"),
    ("Zorbitanox", "6,04", "mmol/l", "Zorbitanox 6,04 mmol/l"),
    (LONG, "2,71", "g/l", f"{LONG} 2,71 g/l"),
]
VALUES = [v for _, v, _, _ in ROWS] + [v.replace(",", ".") for _, v, _, _ in ROWS]
DATE = "17.3.2027"
PAGE = "\n".join([f"Dátum odberu: {DATE}", *(ev for *_, ev in ROWS)])


def answer(*_a, **_k):
    """What the mocked local model answers for PAGE."""
    return json.dumps({"facts": [
        {"label_as_printed": label, "value": v, "comparator": None, "unit_as_printed": u,
         "ref_range_as_printed": None, "flag_as_printed": None, "date_as_printed": DATE,
         "date_role": "sample", "evidence": ev}
        for label, v, u, ev in ROWS
    ]})  # fmt: skip


def _facts():
    with local_only("test"):
        return llm_extract.extract_document([(1, PAGE)], vocab=VOCAB, chat=answer)["facts"]


def _assert_no_values(obj):
    dumped = json.dumps(obj, ensure_ascii=False)
    for v in VALUES:
        # whole number only: a timestamp such as "…:46.040409" or a timing
        # float must not count as the invented value 6.04 (flaky before)
        assert not re.search(rf"(?<![\d.,]){re.escape(v)}(?![\d])", dumped), v
    for token in (DATE, "2027-03-17", "Dátum", "Q7"):
        assert token not in dumped, token


def test_unmapped_and_fuzzy_labels_are_listed_masked_and_deduplicated():
    labels, omitted = llm_extract.unmapped_labels(_facts())
    assert labels == [
        {"reason": "label_unmapped", "label": "Plumbizin", "label_masked": False, "count": 2,
         "unit": "mg/l"},
        {"reason": "label_fuzzy", "label": "Zorbitanox", "label_masked": False, "count": 1,
         "near_code": "ZORBITAN", "unit": "mmol/l"},
        {"reason": "label_unmapped", "label": "Q9 marker", "label_masked": True, "count": 1,
         "unit": "U/ml"},
    ]  # fmt: skip
    assert omitted == 1  # the label longer than 60 characters
    _assert_no_values(labels)
    assert LONG not in json.dumps(labels)
    assert sc.review_labels(labels) == labels  # the scorecard keeps it as is


def test_whitespace_is_normalised_and_units_masked_and_capped():
    facts = [
        {"label": "  Plumbizin \n  forte ", "unit": "10^9/l", "code": None,
         "metadata": {"unverified_reasons": ["label_unmapped"]}},
        {"label": "Plumbizin forte", "unit": "x" * 30, "code": None,
         "metadata": {"unverified_reasons": ["label_unmapped", "date_missing"]}},
        {"label": "QALT", "unit": None, "code": "QALT", "metadata": {}},  # mapped: not listed
    ]  # fmt: skip
    labels, omitted = llm_extract.unmapped_labels(facts)
    assert omitted == 0
    assert labels == [
        {"reason": "label_unmapped", "label": "Plumbizin forte", "label_masked": False,
         "count": 1},
        {"reason": "label_unmapped", "label": "Plumbizin forte", "label_masked": False,
         "count": 1, "unit": "99^9/l"},
    ]  # fmt: skip
    many = [{"label": f"Plumbizin {'abcdefghijklmnopqrstuvwxyz'[i % 26]}{i // 26 * 'z'}",
             "metadata": {"unverified_reasons": ["label_unmapped"]}}
            for i in range(llm_extract.LABEL_REVIEW_TOP + 7)]  # fmt: skip
    labels, omitted = llm_extract.unmapped_labels(many)
    assert len(labels) == llm_extract.LABEL_REVIEW_TOP and omitted == len(range(7))


def test_the_scorecard_recheck_drops_anything_with_an_unmasked_digit():
    bad = [
        {"reason": "label_unmapped", "label": "Q7 marker", "label_masked": True, "count": 1},
        {"reason": "label_unmapped", "label": "Q9 marker", "label_masked": False, "count": 1},
        {"reason": "evidence_not_found", "label": "Plumbizin", "count": 1},
        {"reason": "label_unmapped", "label": "x" * 61, "count": 1},
        "Plumbizin",
    ]  # fmt: skip
    assert sc.review_labels(bad) == []
    unit = [{"reason": "label_unmapped", "label": "Plumbizin", "count": 2, "unit": "41,93"}]
    assert sc.review_labels(unit) == [
        {"reason": "label_unmapped", "label": "Plumbizin", "label_masked": False, "count": 2}
    ]
    card = sc.lab_card({"result": "compared", "unmapped_labels": unit,
                        "unmapped_labels_omitted": 3})  # fmt: skip
    assert card["unmapped_labels_omitted"] == 3
    persisted = sc.persisted({"scorecard": "x", "totals": {}, "documents": [card]})
    assert "documents" not in persisted and "Plumbizin" not in json.dumps(persisted)


# ── the preview job: in memory only, nothing stored, nothing logged ─────

DOC = 8141


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _seed(db):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": "side"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, 'labs', 'application/pdf', 1, ?, ?, ?, '2027-03-17')",
        (DOC, f"gdrive:g{DOC}", f"d{DOC}.pdf", f"d{DOC}.pdf", PRIMARY_TEST_UUID, f"g{DOC}",
         json.dumps(meta)),
    )  # fmt: skip
    await db.save_ocr_page(DOC, 1, PAGE, "curated-sidecar")
    await db.db.commit()


async def _rows_containing(db, needle: str) -> list[str]:
    """Every table whose rows mention ``needle`` (ocr page text excluded: the
    seeded synthetic sidecar itself is the source of the labels)."""
    hits = []
    async with db.db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") as cur:
        tables = [r[0] for r in await cur.fetchall()]
    for table in tables:
        if table in ("document_pages",) or table.startswith("sqlite_"):
            continue
        async with db.db.execute(f'SELECT * FROM "{table}"') as cur:  # noqa: S608
            for row in await cur.fetchall():
                if needle in json.dumps([str(v) for v in tuple(row)], ensure_ascii=False):
                    hits.append(table)
                    break
    return hits


async def test_the_preview_reports_labels_in_memory_only(db, monkeypatch, caplog):
    from oncofiles import config
    from oncofiles.tools import curated as tool

    await _seed(db)
    monkeypatch.setattr(config, "LLM_EXTRACT_SHADOW_DISABLED", False)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(llm, "chat", answer)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_stopping", threading.Event())  # an earlier test may leave it set
    monkeypatch.setattr(tool, "_readme_births", {PRIMARY_TEST_UUID: {"2027-01-02"}})
    rows = [{"code": "QALT", "category": "lab", "unit_canonical": "µkat/L",
             "aliases": ["QALT"], "unit_conversions": {}},
            {"code": "ZORBITAN", "category": "lab", "unit_canonical": "mmol/L",
             "aliases": ["Zorbitanol"], "unit_conversions": {}}]  # fmt: skip
    monkeypatch.setattr(db, "fact_code_vocabulary", lambda: _async(rows))
    caplog.set_level(logging.DEBUG)
    await tool.curated_llm_extract_preview(_ctx(db), document_ids=[DOC])
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    job = json.loads(await tool.curated_reocr_status(_ctx(db)))
    assert job["state"] == "done", job
    doc = job["report"]["documents"][0]
    assert doc["result"] == "compared", doc
    by_label = {item["label"]: item for item in doc["unmapped_labels"]}
    assert by_label["Plumbizin"]["count"] == 2 and by_label["Plumbizin"]["label_masked"] is False
    assert by_label["Q9 marker"]["label_masked"] is True
    assert by_label["Zorbitanox"]["near_code"] == "ZORBITAN"
    assert doc["unmapped_labels_omitted"] == 1
    # no value, date, evidence or unmasked label anywhere in the report
    _assert_no_values(job)
    assert LONG not in json.dumps(job, ensure_ascii=False)
    # never stored: no DB row anywhere holds a listed label
    for label in ("Plumbizin", "Zorbitanox", "Q9 marker"):
        assert await _rows_containing(db, label) == [], label
    # never logged
    logged = "\n".join(r.getMessage() for r in caplog.records)
    for label in ("Plumbizin", "Zorbitanox", "Q7 marker", "Q9 marker"):
        assert label not in logged, label


async def _async(value):
    return value


def test_only_the_in_memory_reports_carry_the_list():
    """The lab write job persists its report (``curated_runs``): it must never
    attach the label review list; only the shadow preview and the scorecard
    (whose ``persisted`` drops the cards) do."""
    import ast
    import inspect

    from oncofiles.tools import curated as tool

    tree = ast.parse(inspect.getsource(tool))
    callers = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_add_label_review"
    }
    assert callers == {"_llm_shadow_job", "_scorecard_job"}
    assert "unmapped_labels" not in tool._SHADOW_DOC_KEYS
