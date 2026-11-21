"""WRITE path for LLM lab facts (#714 phase 2b for labs), OFF by default.

``curated_lab_extract`` stores the verified output of the LLM lab extractor
through ``curated.lab_store`` only with ``LLM_LAB_WRITE_ENABLED=true``; its dry
run reports the agreement with the rule facts (the write gate).

Synthetic data only: invented labels, values and dates (2027+); the local
model is always mocked; the pre-store copy is faked (the test DB is in memory,
see ``tests/test_pre_store.py`` for the real copy).
"""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config, fact_identity, pre_store
from oncofiles.curated import lab_store, llm, llm_extract, verifier
from oncofiles.database._facts import LLM_LAB_FAMILY
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID

BOB_UUID = "00000000-0000-4000-8000-000000000002"
DOB = "2027-01-02"  # invented
DAY = "2027-06-14"
DOC_NEW = 7141  # a layout no rule parser knows: no rule facts
DOC_RULE = DOC_NEW + 1  # the same page, but a rule fact for ALT is stored
DOC_BOB = DOC_NEW + 2
SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"

PAGE = "\n".join(
    [
        "Laboratórny nález",
        "Dátum odberu: 14.6.2027",
        "S-ALT 0,4537 0,10-0,60 µkat/l",
        "S-GLU 5,6194 mmol/l",
        "S-XYZ 1,5 mg/l",
    ]
)
ROWS = [
    ("S-ALT", "0,4537", "µkat/l", "0,10-0,60", "S-ALT 0,4537 0,10-0,60 µkat/l"),
    ("S-GLU", "5,6194", "mmol/l", None, "S-GLU 5,6194 mmol/l"),
    ("S-XYZ", "1,5", "mg/l", None, "S-XYZ 1,5 mg/l"),
]
# printed things that must never reach a report or an activity_log row
PRINTED = ["0,4537", "0.4537", "5,6194", "5.6194", "S-ALT", "S-GLU", "S-XYZ", "µkat", "odberu"]
TABLES = ("clinical_records", "clinical_record_audit", "provenance", "activity_log",
          "curated_runs", "devices")  # fmt: skip


def _answer_json() -> str:
    return json.dumps({"facts": [
        {"label_as_printed": label, "value": v, "comparator": None, "unit_as_printed": u,
         "ref_range_as_printed": r, "flag_as_printed": None, "date_as_printed": "14.6.2027",
         "date_role": "sample", "date_evidence": "Dátum odberu: 14.6.2027", "evidence": ev}
        for label, v, u, r, ev in ROWS
    ]}, ensure_ascii=False)  # fmt: skip


def model_chat(system, user, info=None, **kw):
    """The mocked local model: the answer for PAGE, in the content channel."""
    if info is not None:
        info.update(finish_reason="stop", raw_content=_answer_json())
    return _answer_json()


def reasoning_chat(system, user, info=None, **kw):
    """Content empty, the whole answer in LM Studio's reasoning channel."""
    if info is not None:
        info.update(finish_reason="stop", raw_content="", reasoning_content=_answer_json())
    return ""


async def _doc(db, doc_id, pid):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": f"s{doc_id}"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, 'labs', 'application/pdf', 1, ?, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", f"d{doc_id}.pdf", f"d{doc_id}.pdf", pid, f"g{doc_id}",
         json.dumps(meta), DAY),
    )  # fmt: skip
    await db.save_ocr_page(doc_id, 1, PAGE, "curated-sidecar")
    await db.db.commit()


async def _rule_fact(db, doc_id, *, code="ALT", value=0.4537, day=DAY, meta=None):
    return await db.replace_document_facts(
        patient_id=PRIMARY_TEST_UUID, document_id=doc_id, extracted_by="rule:test",
        facts=[{"code": code, "label": code, "value_num": value, "value_canonical": value,
                "occurred_at": day, "page_number": 1, "evidence": f"{code} line",
                "metadata": meta or {}}],
    )  # fmt: skip


@pytest.fixture
async def seeded(db):
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (BOB_UUID, "bob-test", "Bob Test"),
    )
    await _doc(db, DOC_NEW, PRIMARY_TEST_UUID)
    await _doc(db, DOC_RULE, PRIMARY_TEST_UUID)
    await _doc(db, DOC_BOB, BOB_UUID)
    await _rule_fact(db, DOC_RULE)
    return db


@pytest.fixture
def local_model(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_LAB_WRITE_ENABLED", True)
    monkeypatch.setattr(llm, "chat", model_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_readme_births", {PRIMARY_TEST_UUID: {DOB}, BOB_UUID: {DOB}})
    copies: list[str] = []

    def fake_copy(db, job):
        copies.append(job)
        return f"oncofiles-copy-{len(copies)}-{job}.db"

    monkeypatch.setattr(pre_store, "make_copy", fake_copy)
    return copies


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _all(db, table, where="1=1", params=()):
    async with db.db.execute(f"SELECT * FROM {table} WHERE {where} ORDER BY id", params) as cur:  # noqa: S608
        return [dict(r) for r in await cur.fetchall()]


async def _snapshot(db):
    out = {}
    for t in TABLES:
        out[t] = [tuple(r.values()) for r in await _all(db, t)]
    return out


async def _lab_rows(db, doc_id=None, *, active=True):
    where = "extracted_by LIKE 'llm-lab%'" + (" AND deleted_at IS NULL" if active else "")
    rows = await _all(db, "clinical_records", where)
    return [r for r in rows if doc_id is None or r["source_document_id"] == doc_id]


async def _run(db, ids, **kw):
    started = json.loads(await tool.curated_lab_extract(_ctx(db), ids, **kw))
    if "error" in started or "status" in started:
        return started
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    return tool._reocr_jobs[PRIMARY_TEST_UUID]


async def _write(db, ids):
    job = await _run(db, ids, dry_run=False)
    assert job["state"] == "done", job
    tool._reocr_jobs.clear()
    return job


_NUMBER = re.compile(r"\d+(?:[.,]\d+)+")


def _no_values(obj) -> None:
    """No printed value, label or unit in a report, a row or the log.

    Numbers are compared as WHOLE tokens (#893, #823): a substring search
    also matched a job timing or a timestamp that happened to hold the same
    digits ("speedup": 0.45, "…:10.45…Z"), which failed about one full run in
    a hundred. The fixture values have four decimals; no timing has."""
    dumped = json.dumps(obj, ensure_ascii=False, default=str)
    numbers = set(_NUMBER.findall(dumped))
    found = [p for p in PRINTED if (p in numbers if _NUMBER.fullmatch(p) else p in dumped)]
    assert not found, (found, dumped)


# ── off by default, refusals write nothing ──────────────────────────────


def test_the_write_is_off_by_default(monkeypatch):
    import importlib

    monkeypatch.delenv("LLM_LAB_WRITE_ENABLED", raising=False)
    try:
        assert importlib.reload(config).LLM_LAB_WRITE_ENABLED is False
    finally:
        importlib.reload(config)


async def test_env_off_a_real_run_writes_nothing_anywhere(seeded, local_model, monkeypatch):
    db = seeded
    monkeypatch.setattr(config, "LLM_LAB_WRITE_ENABLED", False)
    before = await _snapshot(db)
    out = json.loads(await tool.curated_lab_extract(_ctx(db), [DOC_NEW], dry_run=False))
    assert "LLM_LAB_WRITE_ENABLED" in out["error"]
    assert PRIMARY_TEST_UUID not in tool._reocr_jobs and local_model == []  # no slot, no copy
    # the job refuses on its own too (env switched off between call and job)
    tool._reocr_jobs[PRIMARY_TEST_UUID] = {"state": "running", "kind": "lab_extract"}
    docs = await db.curated_reocr_candidates(patient_id=PRIMARY_TEST_UUID)
    await tool._lab_job(db, PRIMARY_TEST_UUID, docs, write=True)
    assert tool._reocr_jobs[PRIMARY_TEST_UUID]["error"] == "write_disabled"
    assert await _snapshot(db) == before  # no fact, provenance, audit, run row
    assert local_model == []  # no pre-store copy


async def test_a_dry_run_works_with_the_env_off(seeded, local_model, monkeypatch):
    db = seeded
    monkeypatch.setattr(config, "LLM_LAB_WRITE_ENABLED", False)
    before = await _snapshot(db)
    job = await _run(db, [DOC_NEW, DOC_RULE])
    assert job["state"] == "done" and job["kind"] == "lab_preview", job
    assert not tool.run_active()  # a dry run never blocks a deploy
    assert await _snapshot(db) == before and local_model == []


async def test_a_write_is_refused_without_a_birth_date(seeded, local_model, monkeypatch):
    db = seeded
    monkeypatch.setattr(tool, "_readme_births", {})
    before = await _snapshot(db)
    with patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=None)):
        out = json.loads(await tool.curated_lab_extract(_ctx(db), [DOC_NEW], dry_run=False))
        assert "no birth date" in out["error"] and PRIMARY_TEST_UUID not in tool._reocr_jobs
        tool._reocr_jobs[PRIMARY_TEST_UUID] = {"state": "running", "kind": "lab_extract"}
        docs = await db.curated_reocr_candidates(patient_id=PRIMARY_TEST_UUID)
        await tool._lab_job(db, PRIMARY_TEST_UUID, docs, write=True)
    job = tool._reocr_jobs[PRIMARY_TEST_UUID]
    assert job["state"] == "failed" and job["error"] == "no_birth_date"
    after = await _snapshot(db)
    assert {t: v for t, v in after.items() if t not in ("curated_runs", "activity_log")} == {
        t: v for t, v in before.items() if t not in ("curated_runs", "activity_log")
    }
    with pytest.raises(verifier.NoBirthDateError):  # the store refuses on its own as well
        await lab_store.store_document(
            db, patient_id=PRIMARY_TEST_UUID, document_id=DOC_NEW, facts=[], birth_dates=set()
        )


async def test_a_pre_store_failure_writes_nothing(seeded, local_model, monkeypatch):
    db = seeded

    def broken(db_, job):
        raise pre_store.PreStoreError("quick_check failed on the copy")

    monkeypatch.setattr(pre_store, "make_copy", broken)
    before = await _snapshot(db)
    out = json.loads(await tool.curated_lab_extract(_ctx(db), [DOC_NEW], dry_run=False))
    assert out["error"].startswith("refused: pre-store copy failed")
    assert PRIMARY_TEST_UUID not in tool._reocr_jobs  # the slot is released
    assert await _snapshot(db) == before


async def test_guards_and_a_foreign_document(seeded, local_model, monkeypatch):
    db = seeded
    out = json.loads(await tool.curated_lab_extract(_ctx(db), list(range(11))))
    assert "at most 10" in out["error"]
    assert "required" in json.loads(await tool.curated_lab_extract(_ctx(db), []))["error"]
    for kw in ({}, {"dry_run": False}):
        out = json.loads(await tool.curated_lab_extract(_ctx(db), [DOC_NEW, DOC_BOB], **kw))
        assert out["error"] == "not_found" and out["document_ids"] == [DOC_BOB]
        assert "wrong_patient" not in json.dumps(out)
    with pytest.raises(ValueError, match="only on a dry run"):
        await tool.curated_lab_extract(_ctx(db), [DOC_NEW], dry_run=False, sampling="qwen")
    monkeypatch.setitem(tool._runs, PRIMARY_TEST_UUID, {"state": "running"})
    out = json.loads(await tool.curated_lab_extract(_ctx(db), [DOC_NEW], dry_run=False))
    assert out["error"].startswith("busy")
    tool._runs.pop(PRIMARY_TEST_UUID)
    tool._reocr_jobs.update({"x": {"state": "running", "kind": "lab_preview"}})
    assert not tool.run_active()
    tool._reocr_jobs.update({"y": {"state": "running", "kind": "lab_extract"}})
    assert tool.run_active()


# ── the dry run: the agreement gate, counts and codes only ──────────────


async def test_dry_run_reports_the_agreement_gate_and_writes_nothing(seeded, local_model):
    db = seeded
    before = await _snapshot(db)
    job = await _run(db, [DOC_NEW, DOC_RULE])
    assert await _snapshot(db) == before
    report = job["report"]
    assert report["dry_run"] is True and report["writer"] == lab_store.WRITER_VERSION
    docs = {d["document_id"]: d for d in report["documents"]}
    assert {d["result"] for d in docs.values()} == {"previewed"}
    rule = docs[DOC_RULE]
    assert rule["agree"] == rule["llm_only"] == len(["ALT"])
    assert not rule["rule_only"] and not rule["value_mismatch"]
    assert rule["agreement_by_code"] == {"ALT": {"agree": 1}, "GLU": {"llm_only": 1}}
    assert rule["would_store"] == {"machine": 1}  # GLU only: ALT has a rule fact
    assert rule["skipped"] == {"skipped_rule_exists": 1, "skipped_uncoded": 1}
    assert docs[DOC_NEW]["would_store"] == {"machine": 2}
    gate = report["totals"]["gate"]
    assert gate["documents_with_rule_facts"] == 1 and gate["rule_facts"] == 1
    assert gate["agreement_rate"] == 1.0 and gate["value_mismatch_rate"] == 0.0
    assert gate["by_code"] == {"ALT": {"agree": 1}, "GLU": {"llm_only": 1}}
    _no_values(job)


async def test_a_value_mismatch_shows_in_the_gate(seeded, local_model):
    db = seeded
    await _rule_fact(db, DOC_RULE, value=0.46)  # the rule parser read another value
    job = await _run(db, [DOC_RULE])
    gate = job["report"]["totals"]["gate"]
    assert gate["value_mismatch"] == 1 and gate["agree"] == 0 and gate["agreement_rate"] == 0.0
    one = len(["ALT"])
    # one bucket per fact (#826): a mismatch pair is not also rule_only / llm_only
    assert gate["by_code"]["ALT"] == {"value_mismatch": one}
    assert gate["rule_only"] == 0


# ── the write ───────────────────────────────────────────────────────────


async def test_write_stores_machine_facts_with_provenance_and_stable_ids(seeded, local_model):
    db = seeded
    job = await _write(db, [DOC_NEW])
    doc = job["report"]["documents"][0]
    assert doc["result"] == "stored"
    assert doc["stored"]["added"] == 2 and doc["stored"]["machine"] == 2
    assert doc["skipped"] == {"skipped_uncoded": 1}  # S-XYZ: no fact_codes alias
    rows = await _lab_rows(db, DOC_NEW)
    assert sorted(r["code"] for r in rows) == ["ALT", "GLU"]
    for r in rows:
        assert r["extracted_by"] == f"{lab_store.WRITER_VERSION}/mock-model"
        assert r["extracted_by"].startswith(LLM_LAB_FAMILY + "-v")
        assert r["verification"] in lab_store.WRITE_LABELS
        assert r["page_number"] == 1 and r["evidence"] and r["fhir_id"]
        assert r["source"] == "curated-llm-lab" and r["occurred_at"] == DAY
        prov = await _all(db, "provenance", "target_type = 'fact' AND target_id = ?", (r["id"],))
        assert len(prov) == 1 and prov[0]["activity"] == "extract"
        assert prov[0]["source_document_id"] == DOC_NEW
        assert prov[0]["page_number"] == r["page_number"]
    assert local_model == ["lab"]
    # not a curated run
    runs = await _all(db, "curated_runs")
    assert [(r["trigger"], r["status"]) for r in runs] == [("lab", "ok")]
    assert await db.list_curated_runs(patient_id=PRIMARY_TEST_UUID) == []
    # the same facts extracted again keep their ids, as the next version
    ids = {r["code"]: r["fhir_id"] for r in rows}
    await _write(db, [DOC_NEW])
    again = await _lab_rows(db, DOC_NEW)
    assert {r["code"]: r["fhir_id"] for r in again} == ids
    assert {r["version_id"] for r in again} == {2}
    assert len(await _lab_rows(db, DOC_NEW, active=False)) == 2 * 2  # old ones soft-deleted


async def test_llm_ids_never_equal_rule_ids():
    key = dict(code="ALT", label="ALT", occurred_at=DAY, page=1, meta={})
    rule = fact_identity.natural_key(PRIMARY_TEST_UUID, DOC_NEW, **key)
    lab = fact_identity.natural_key(PRIMARY_TEST_UUID, DOC_NEW, family=LLM_LAB_FAMILY, **key)
    assert rule != lab and fact_identity.fact_uuid(rule, 0) != fact_identity.fact_uuid(lab, 0)
    # the rule ids themselves are unchanged by the new parameter
    assert fact_identity.natural_key(PRIMARY_TEST_UUID, DOC_NEW, family=None, **key) == rule


async def test_skipped_rule_exists_never_two_values_for_one_printed_result(seeded, local_model):
    db = seeded
    job = await _write(db, [DOC_RULE])
    doc = job["report"]["documents"][0]
    assert doc["skipped"] == {"skipped_rule_exists": 1, "skipped_uncoded": 1}
    assert [r["code"] for r in await _lab_rows(db, DOC_RULE)] == ["GLU"]
    alt = await _all(db, "clinical_records", "code = 'ALT' AND source_document_id = ? "
                     "AND deleted_at IS NULL", (DOC_RULE,))  # fmt: skip
    assert len(alt) == 1 and alt[0]["extracted_by"] == "rule:test"


def test_a_rule_fact_under_a_printed_range_blocks_its_whole_window():
    meta = json.dumps({"date_range_start": "2027-06-10", "date_range_end": "2027-06-20"})
    others = [{"code": "ALT", "occurred_at": "2027-06-20", "metadata_json": meta}]
    assert lab_store._blocked({"code": "ALT", "occurred_at": DAY}, others)
    assert not lab_store._blocked({"code": "ALT", "occurred_at": "2027-06-21"}, others)
    assert not lab_store._blocked({"code": "GLU", "occurred_at": DAY}, others)
    assert lab_store._blocked({"code": "ALT", "occurred_at": None}, others)  # undated


def test_labels_are_machine_or_unverified_only():
    base = {"code": "ALT", "verification": "machine", "metadata": {"answer_source": "content"}}
    assert lab_store._label(base)["verification"] == "machine"
    assert lab_store._label({**base, "verification": "verified"})["verification"] == "unverified"
    with_reason = {**base, "metadata": {"unverified_reasons": ["cowork_ocr"]}}
    out = lab_store._label(with_reason)
    assert out["verification"] == "unverified"
    assert out["metadata"]["unverified_reasons"] == ["cowork_ocr"]  # never removed
    reasoning = {**base, "metadata": {"answer_source": "reasoning"}}
    out = lab_store._label(reasoning)
    assert out["verification"] == "unverified"
    assert verifier.REASONING_CHANNEL_REASON in out["metadata"]["unverified_reasons"]


async def test_reasoning_channel_facts_are_stored_unverified(seeded, local_model, monkeypatch):
    db = seeded
    monkeypatch.setattr(llm, "chat", reasoning_chat)
    job = await _write(db, [DOC_NEW])
    stored = job["report"]["documents"][0]["stored"]
    assert stored["added"] == stored["unverified"] == len(["ALT", "GLU"])
    assert "machine" not in stored and not stored["removed"]
    for r in await _lab_rows(db, DOC_NEW):
        assert r["verification"] == "unverified"
        reasons = json.loads(r["metadata_json"])["unverified_reasons"]
        assert verifier.REASONING_CHANNEL_REASON in reasons


async def test_re_extraction_replaces_only_llm_lab_rows_and_keeps_verified(seeded, local_model):
    db = seeded
    await _write(db, [DOC_NEW, DOC_RULE])
    alt = next(r for r in await _lab_rows(db, DOC_NEW) if r["code"] == "ALT")
    await db.db.execute("UPDATE clinical_records SET verification = 'verified' WHERE id = ?",
                        (alt["id"],))  # fmt: skip
    await db.db.commit()
    rule_before = await _all(db, "clinical_records", "extracted_by = 'rule:test'")
    job = await _write(db, [DOC_NEW, DOC_RULE])
    stored = {d["document_id"]: d["stored"] for d in job["report"]["documents"]}
    assert stored[DOC_NEW]["kept_verified"] == 1 and stored[DOC_NEW]["added"] == 1
    assert stored[DOC_NEW]["removed"] == 1  # GLU replaced, verified ALT kept
    active = await _lab_rows(db, DOC_NEW)
    assert sorted(r["code"] for r in active) == ["ALT", "GLU"]
    assert next(r for r in active if r["code"] == "ALT")["id"] == alt["id"]
    assert await _all(db, "clinical_records", "extracted_by = 'rule:test'") == rule_before


async def test_rule_re_extraction_never_touches_llm_lab_rows(seeded, local_model):
    db = seeded
    await _write(db, [DOC_NEW])
    lab_before = await _lab_rows(db, DOC_NEW)
    res = await _rule_fact(db, DOC_NEW, code="CEA", value=2.2)  # a rule parser re-indexes
    assert res["removed"] == 0
    assert await _lab_rows(db, DOC_NEW) == lab_before
    # the rule side never sees them in corroboration / re-reads / the shadow either
    assert {r["code"] for r in await db.fact_verification_rows(patient_id=PRIMARY_TEST_UUID)} == {
        "ALT", "CEA",
    }  # fmt: skip
    # each family is checked, not trusted
    with pytest.raises(ValueError, match="family"):
        await db.replace_document_facts(patient_id=PRIMARY_TEST_UUID, document_id=DOC_NEW, facts=[],
                                        extracted_by="llm-lab-v9/x")  # fmt: skip
    with pytest.raises(ValueError, match="llm-lab"):
        await db.replace_document_facts(patient_id=PRIMARY_TEST_UUID, document_id=DOC_NEW, facts=[],
                                        extracted_by="rule:x", family=LLM_LAB_FAMILY)  # fmt: skip
    assert await _lab_rows(db, DOC_NEW) == lab_before


# ── retract ─────────────────────────────────────────────────────────────


async def _retract(db, ids, **kw):
    return json.loads(await tool.curated_lab_retract(_ctx(db), ids, **kw))


async def test_retract_counts_equal_the_written_counts(seeded, local_model):
    db = seeded
    job = await _write(db, [DOC_NEW, DOC_RULE])
    written = sum(d["stored"]["added"] for d in job["report"]["documents"])
    before = await _snapshot(db)
    dry = await _retract(db, [DOC_NEW, DOC_RULE])
    assert await _snapshot(db) == before
    assert dry["totals"] == {"lab_facts": {"removed": written}}
    real = await _retract(db, [DOC_NEW, DOC_RULE], dry_run=False, reason="first write rollback")
    assert real["totals"] == dry["totals"] and real["pre_store"].endswith("-lab.db")
    assert await _lab_rows(db) == []
    rule = await _all(db, "clinical_records", "extracted_by = 'rule:test' AND deleted_at IS NULL")
    assert len(rule) == 1  # rule facts stay
    audit = await _all(db, "activity_log", "tool_name = 'curated_lab_retract'")
    assert len(audit) == 1 and "first write rollback" not in json.dumps(audit)
    _no_values(audit)
    again = await _retract(db, [DOC_NEW], dry_run=False, reason="again")
    assert again["totals"] == {}


async def test_retract_keeps_verified_rows_and_refuses_foreign_ids(seeded, local_model):
    db = seeded
    await _write(db, [DOC_NEW])
    alt = next(r for r in await _lab_rows(db, DOC_NEW) if r["code"] == "ALT")
    await db.db.execute("UPDATE clinical_records SET verification = 'corrected' WHERE id = ?",
                        (alt["id"],))  # fmt: skip
    await db.db.commit()
    out = await _retract(db, [DOC_NEW], dry_run=False, reason="rollback")
    assert out["totals"] == {"lab_facts": {"removed": 1, "kept": 1}}
    assert [r["id"] for r in await _lab_rows(db)] == [alt["id"]]
    before = await _snapshot(db)
    for kw in ({}, {"dry_run": False, "reason": "rollback"}):
        bad = await _retract(db, [DOC_NEW, DOC_BOB], **kw)
        assert bad["error"] == "not_found" and bad["document_ids"] == [DOC_BOB]
    assert await _snapshot(db) == before


# ── activity_log and report: counts only ────────────────────────────────


async def test_activity_log_row_and_report_hold_no_values(seeded, local_model, caplog):
    db = seeded
    caplog.set_level("INFO")  # app logs (aiosqlite DEBUG echoes SQL params)
    job = await _write(db, [DOC_NEW, DOC_RULE])
    _no_values({k: v for k, v in job.items() if k != "task"})
    _no_values(await _all(db, "curated_runs"))  # the persisted job report
    _no_values(caplog.text)
    rows = await _all(db, "activity_log", "tool_name = 'curated_lab_extract'")
    assert len(rows) == 1
    row = rows[0]
    _no_values(row)
    summary = json.loads(row["input_summary"])
    assert summary["dry_run"] is False and summary["pre_store"].endswith("-lab.db")
    assert summary["document_ids"] == [DOC_NEW, DOC_RULE]
    out = json.loads(row["output_summary"])
    assert out["stored"]["added"] == 3 and out["skipped"]["skipped_rule_exists"] == 1


# ── read side: method / verification_label, rule value preferred ───────


async def test_read_side_labels_llm_lab_rows(seeded, local_model):
    db = seeded
    await _write(db, [DOC_NEW])
    series = json.loads(await cf.fact_series(_ctx(db), ["GLU"]))
    point = series["series"]["GLU"]["values"][0]
    assert point["method"] == "llm-lab"
    assert point["verification_label"]["en"] == "text verified on page, read by the model"
    assert series["series"]["GLU"]["latest_any"]["method"] == "llm-lab"
    feed = json.loads(await cf.dashboard_feed(_ctx(db)))
    by_doc = {(f["document_id"], f.get("method")) for f in feed["facts"]}
    assert by_doc == {(DOC_NEW, "llm-lab"), (DOC_RULE, None)}
    detail = json.loads(await cf.document_detail(_ctx(db), DOC_NEW))
    assert {f["method"] for f in detail["facts"]} == {"llm-lab"}
    overview = json.loads(await cf.patient_overview(_ctx(db)))
    marks = {(r["source_document_id"], r["method"]) for r in overview["latest_key_values"]}
    assert marks == {(DOC_NEW, "llm-lab"), (DOC_RULE, "rule")}  # ALT is a key code
    from oncofiles.tools import lab_trends

    trends = json.loads(await lab_trends.get_lab_trends(_ctx(db), parameter="GLU"))
    assert [(v["document_id"], v["method"]) for v in trends["values"]] == [(DOC_NEW, "llm-lab")]
    # a rule fact of the same document shows as rule, without the llm label
    rule = json.loads(await cf.document_detail(_ctx(db), DOC_RULE))
    assert all("method" not in f for f in rule["facts"])


def test_fact_labels_know_the_llm_lab_family():
    from oncofiles import fact_labels

    row = fact_labels.label_row({"verification": "machine", "extracted_by": "llm-lab-v9/m"})
    assert row["method"] == "llm-lab"
    assert row["verification_label"] == fact_labels.verification_label("machine", "llm-clinical")
    assert row["verification_label"] != fact_labels.verification_label("machine", "rule:x")
    unverified = fact_labels.verification_label("unverified", "llm-lab-v9/m")
    assert unverified["en"] == "read by the model, text not verified"
    assert fact_labels.method_of("llm-clinical-v1/m") == "llm-clinical"
    assert fact_labels.method_of("rule:x") == "rule" and fact_labels.method_of(None) is None


async def test_a_flagged_llm_lab_value_stays_on_the_timeline_behind_a_rule_twin(seeded):
    """#817 rule with #714 rows: ``rule_preferred`` keeps the (unflagged,
    normal) rule value in fact_series, but the timeline never hides the
    flagged llm-lab value of the same document, code and day behind it."""
    db = seeded
    rule = {"code": "GLU", "label": "GLU", "value_num": 5.2, "value_canonical": 5.2,
            "unit_canonical": "mmol/L", "ref_low": 3.9, "ref_high": 5.6, "flag": None,
            "occurred_at": DAY, "page_number": 1, "evidence": "GLU rule line",
            "metadata": {}}  # fmt: skip
    await db.replace_document_facts(
        patient_id=PRIMARY_TEST_UUID, document_id=DOC_NEW, facts=[rule], extracted_by="rule:test"
    )
    lab = {**rule, "value_num": 8.4, "value_canonical": 8.4, "flag": "H",
           "evidence": "GLU model line", "verification": "machine"}  # fmt: skip
    await db.replace_document_facts(
        patient_id=PRIMARY_TEST_UUID, document_id=DOC_NEW, facts=[lab],
        extracted_by=lab_store.extracted_by_for("mock-model"), family=LLM_LAB_FAMILY,
    )  # fmt: skip
    lab_id = (await _lab_rows(db, DOC_NEW))[0]["id"]

    rows = await db.fact_series_rows(patient_id=PRIMARY_TEST_UUID, codes=["GLU"])
    kept, removed = cf._dedupe(rows)
    assert [r["extracted_by"] for r in kept] == ["rule:test"]
    assert [(d["reason"], d["removed"]["fact_id"]) for d in removed] == [("rule_preferred", lab_id)]

    series = json.loads(await cf.fact_series(_ctx(db), ["GLU"]))["series"]["GLU"]
    assert [(p["flag"], "method" in p) for p in series["values"]] == [(None, False)]

    timeline = json.loads(await cf.timeline(_ctx(db)))
    shown = [
        r for r in timeline["rows"] if r.get("entity") == "fact" and r.get("entity_id") == lab_id
    ]
    assert len(shown) == 1 and shown[0]["flag"] == "H"
    assert shown[0]["method"] == "llm-lab"
    assert lab_id not in {d["removed"]["fact_id"] for d in timeline["duplicates"]}


async def test_dedupe_prefers_the_rule_value_of_the_same_document_and_day(seeded, local_model):
    db = seeded
    await _write(db, [DOC_NEW])
    await _rule_fact(db, DOC_NEW, code="GLU", value=5.62)  # a parser learned the layout later
    series = json.loads(await cf.fact_series(_ctx(db), ["GLU"]))
    values = series["series"]["GLU"]["values"]
    assert len(values) == 1 and "method" not in values[0]
    assert [d["reason"] for d in series["duplicates"]] == ["rule_preferred"]
    # an exact duplicate across documents keeps the rule value too
    rows = [
        {"id": 1, "code": "ALT", "effective_at": DAY, "value_canonical": 1.0, "value_text": None,
         "verification": "machine", "source_document_id": 1, "extracted_by": "llm-lab-v9/m",
         "metadata_json": "{}"},
        {"id": 2, "code": "ALT", "effective_at": DAY, "value_canonical": 1.0, "value_text": None,
         "verification": "machine", "source_document_id": 2, "extracted_by": "rule:x",
         "metadata_json": "{}"},
    ]  # fmt: skip
    kept, removed = cf._dedupe(rows)
    assert [r["id"] for r in kept] == [2] and removed[0]["reason"] == "exact"


# ── lock tests: one entry point, one family ─────────────────────────────


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        yield path.relative_to(SRC).as_posix(), ast.parse(path.read_text(encoding="utf-8"))


def test_only_the_tool_calls_the_lab_store_write():
    callers = set()
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "oncofiles.curated.lab_store":
                names = {a.name for a in node.names}
                assert "store_document" not in names, rel
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "store_document"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "lab_store"
            ):
                callers.add(rel)
    assert callers == {"tools/curated.py"}
    # and within the tool module, only the write job calls it
    tree = dict(_modules())["tools/curated.py"]
    fns = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "store_document"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "lab_store"
    }
    assert fns == {"_lab_job"}


def test_the_llm_lab_insert_path_is_reached_only_with_the_llm_lab_family():
    """Only lab_store passes ``family=`` to replace_document_facts, always
    ``LLM_LAB_FAMILY``, with an ``extracted_by`` built by ``extracted_by_for``
    (the ``llm-lab-v<N>`` writer version); the DB checks it again at runtime."""
    passing = {}
    for rel, tree in _modules():
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "replace_document_facts"
            ):
                continue
            kws = {k.arg: k.value for k in node.keywords}
            if "family" in kws:
                passing.setdefault(rel, []).append(kws)
    assert set(passing) == {"curated/lab_store.py"}
    for kws in passing["curated/lab_store.py"]:
        assert isinstance(kws["family"], ast.Name) and kws["family"].id == "LLM_LAB_FAMILY"
        assert isinstance(kws["extracted_by"], ast.Name)
    assert lab_store.WRITER_VERSION.startswith(LLM_LAB_FAMILY + "-v")
    assert lab_store.extracted_by_for("m").startswith(LLM_LAB_FAMILY + "-v")
    assert lab_store.WRITER_VERSION.endswith(llm_extract.EXTRACTOR_VERSION.rsplit("-v", 1)[1])


# ── golden ids: the rule ids are exactly what origin/main computed ──────

# UUIDs computed ONCE with fact_identity.py of origin/main (fdb657f, before the
# ``family`` parameter), on these synthetic inputs. They must never change.
GOLDEN_CASES = {
    "coded": (DOC_NEW, {"code": "ALT", "label": "S-ALT", "occurred_at": "2027-06-14",
                        "page_number": 1, "metadata": {}}),
    "uncoded": (DOC_NEW, {"code": None, "label": "W-Index Q", "occurred_at": "2027-06-14",
                          "page_number": 2, "metadata": {}}),
    "range": (DOC_NEW, {"code": "CRP", "label": "CRP", "occurred_at": "2027-09-09",
                        "page_number": 1, "metadata": {"date_range_start": "2027-09-01",
                                                       "date_range_end": "2027-09-09"}}),
    "no_doc": (None, {"code": "GLU", "label": "GLU", "occurred_at": "2027-03-05T07:40",
                      "page_number": None, "metadata": {}}),
}  # fmt: skip
GOLDEN_IDS = {  # (ordinal 0, ordinal 1)
    "coded": ("ec59eb89-0e22-5cef-ab21-00ba3db2c788", "f3475c82-158e-5511-b1dd-627fbe4fdab1"),
    "uncoded": ("fc67b488-4dc2-5016-87dd-44ae007efeae", "1a4f3448-82f0-53bb-a741-d8dc93573d06"),
    "range": ("ddf5a185-2e43-57a1-b02d-37d5cb765550", "8a5794cc-3b4e-54f1-a5d7-57571ba62fcd"),
    "no_doc": ("e3934979-ea8c-56b5-b9ad-47e85cfe0ef6", "41ff2395-855b-58e6-9361-c5666547a349"),
}


@pytest.mark.parametrize("case", sorted(GOLDEN_CASES))
def test_rule_fact_ids_equal_the_golden_ids_of_main(case):
    doc, fact = GOLDEN_CASES[case]
    want = GOLDEN_IDS[case]
    # the insert path: two facts with the same natural key get ordinals 0 and 1
    twins = [{**fact, "evidence": "a"}, {**fact, "evidence": "b"}]
    assert tuple(fact_identity.ids_for_new_facts(PRIMARY_TEST_UUID, doc, twins, set())) == want
    key = fact_identity.fact_key(PRIMARY_TEST_UUID, doc, fact)
    assert fact_identity.fact_uuid(key, 0) == want[0]
    # the read path for stored rows without fhir_id (dashboard_feed)
    row = {"id": 1, "code": fact["code"], "param": fact["label"],
           "occurred_at": fact["occurred_at"], "page_number": fact["page_number"],
           "metadata_json": json.dumps(fact["metadata"]), "evidence": "a",
           "verification": "machine", "fhir_id": None}  # fmt: skip
    assert fact_identity.ids_for_rows(PRIMARY_TEST_UUID, doc, [row]) == {1: want[0]}
    # the llm-lab family never yields a rule id
    lab = fact_identity.ids_for_new_facts(
        PRIMARY_TEST_UUID, doc, twins, set(), family=LLM_LAB_FAMILY
    )
    assert not set(lab) & set(want)
    lab_row = fact_identity.ids_for_rows(PRIMARY_TEST_UUID, doc, [row], family=LLM_LAB_FAMILY)
    assert lab_row[1] != want[0]


def test_no_values_ignores_timings_and_timestamps_but_catches_a_value():
    """#893 / #823: the check must not depend on what a clock happens to print."""
    value = next(p for p in PRINTED if "." in p)
    _no_values({"timing": {"speedup": 0.45, "wall_seconds": 5.6}, "at": f"2027-03-09T10:1{value}Z"})
    for leaked in ({"value": float(value)}, {"note": f"ALT {value} ok"}, {"unit": "µkat/l"}):
        with pytest.raises(AssertionError):
            _no_values(leaked)
