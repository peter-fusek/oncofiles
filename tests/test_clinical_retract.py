# ruff: noqa: F811  (pytest fixtures imported from test_clinical_extract)
"""User-invoked rollback of the clinical LLM extractor's rows (#773 / #726).

``curated_clinical_retract`` soft-deletes exactly what a re-extraction of a
document would remove (the same ``clinical_store._plan_removal``) and adds
nothing. Synthetic data only: the documents are the worked examples of the
extractor prompt (invented drugs, labels, values, dates 2027+); the local
model is always mocked.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from oncofiles import config, pre_store
from oncofiles.curated import clinical_store
from oncofiles.persistent_oauth import _verified_caller_email, _verified_caller_is_admin
from oncofiles.secrets_keys import caller_email_hash
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_clinical_extract import (  # noqa: F401  (fixtures)
    BOB_UUID,
    DAY,
    DOC_A,
    DOC_B,
    DOC_BOB,
    PAGES_A,
    PAGES_B,
    _active,
    _ctx,
    _rows,
    _snapshot,
    _store,
    local_model,
    seeded,
)

LLM_TABLES = (
    "conditions", "cancer_stages", "treatment_courses", "treatment_cycles", "medication_events",
    "procedures", "clinical_records", "encounters",
)  # fmt: skip


async def _llm_active(db, table, doc_id=None, pid=ERIKA_UUID):
    rows = await _active(db, table, pid)
    return [
        r
        for r in rows
        if str(r.get("extracted_by") or "").startswith("llm-clinical")
        and (doc_id is None or r.get("source_document_id") == doc_id)
    ]


async def _retract(db, ids, **kw):
    return json.loads(await tool.curated_clinical_retract(_ctx(db), ids, **kw))


async def _activity(db) -> list[dict]:
    async with db.db.execute(
        "SELECT * FROM activity_log WHERE tool_name = 'curated_clinical_retract' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _set(db, table, row_id, verification):
    await db.db.execute(
        f"UPDATE {table} SET verification = ? WHERE id = ?",  # noqa: S608
        (verification, row_id),
    )
    await db.db.commit()


async def _protect_some(db):
    """Verify a medication dose (its cycle and course must stay), correct the
    primary diagnosis (its stage points to it), verify one lesion size (its
    imaging procedure must stay) and one planned encounter."""
    med = next(m for m in await _llm_active(db, "medication_events", DOC_A) if m["cycle_id"])
    await _set(db, "medication_events", med["id"], "verified")
    primary = next(c for c in await _llm_active(db, "conditions", DOC_A) if c["is_primary_cancer"])
    await _set(db, "conditions", primary["id"], "corrected")
    lesion = next(
        r
        for r in await _llm_active(db, "clinical_records", DOC_A)
        if r["code"] == "LESION_SIZE" and json.loads(r["metadata_json"]).get("imaging_procedure_id")
    )
    await _set(db, "clinical_records", lesion["id"], "verified")
    enc = (await _llm_active(db, "encounters", DOC_A))[0]
    await _set(db, "encounters", enc["id"], "verified")
    return {
        "medication_events": med["id"],
        "treatment_cycles": med["cycle_id"],
        "treatment_courses": med["course_id"],
        "conditions": primary["id"],
        "clinical_records": lesion["id"],
        "procedures": json.loads(lesion["metadata_json"])["imaging_procedure_id"],
        "encounters": enc["id"],
    }


# ── dry run == real run; protected rows and their parents stay ──────────


async def test_dry_run_counts_equal_what_the_real_run_removes(seeded, local_model):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_B, PAGES_B)
    await _protect_some(db)
    before = await _snapshot(db)

    dry = await _retract(db, [DOC_A, DOC_B])
    assert dry["dry_run"] is True
    assert await _snapshot(db) == before  # a dry run writes nothing
    assert await _activity(db) == [] and local_model == []  # no copy, no audit row

    real = await _retract(db, [DOC_A, DOC_B], dry_run=False, reason="first write rollback")
    assert real["dry_run"] is False and real["pre_store"].endswith("-clinical.db")
    assert local_model == ["clinical"]
    for d, r in zip(dry["documents"], real["documents"], strict=True):
        assert d["document_id"] == r["document_id"]
        assert d["result"] == "would_retract" and r["result"] == "retracted"
        assert d["stored"] == r["stored"]
    assert dry["totals"] == real["totals"]
    assert sum(c.get("removed", 0) for c in real["totals"].values()) > 0

    # everything still active from the extractor is exactly the "kept" count
    kept = sum(c.get("kept", 0) for c in real["totals"].values())
    left = 0
    for t in LLM_TABLES:
        left += len(await _llm_active(db, t))
    assert left == kept
    # a second call finds nothing more to remove
    again = await _retract(db, [DOC_A, DOC_B], dry_run=False, reason="again")
    assert all(c.get("removed", 0) == 0 for c in again["totals"].values())


async def test_verified_or_corrected_rows_and_their_parents_survive(seeded, local_model):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    protected = await _protect_some(db)
    out = await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    assert out["documents"][0]["result"] == "retracted"
    for table, row_id in protected.items():
        active = {r["id"] for r in await _active(db, table)}
        assert row_id in active, table
    # nothing else of the extractor stays
    for table in LLM_TABLES:
        ids = {r["id"] for r in await _llm_active(db, table, DOC_A)}
        assert ids <= {protected.get(table)}, table
    # stages of the corrected primary point to a kept row; other stages are gone
    stages = await _llm_active(db, "cancer_stages", DOC_A)
    assert all(s["verification"] in ("verified", "corrected") for s in stages)
    removed = await _rows(db, "conditions", where="deleted_at IS NOT NULL")
    assert removed and all(r["verification"] == "machine" for r in removed)
    assert all(r["deleted_by"].startswith("retract by ") for r in removed)
    # the fact audit carries the same reason, and so do the soft-deleted codings
    async with db.db.execute(
        "SELECT reason FROM clinical_record_audit WHERE action = 'delete'"
    ) as cur:
        reasons = [r[0] for r in await cur.fetchall()]
    assert reasons and all(r.startswith("retract by ") and r.endswith(": rollback")
                           for r in reasons)  # fmt: skip
    codings = await _rows(db, "codings", where="deleted_at IS NOT NULL")
    assert codings and all(c["deleted_by"].startswith("retract by ") for c in codings)


async def test_lab_rule_facts_and_other_documents_survive(seeded, local_model):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_B, PAGES_B)
    await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=DOC_A, extracted_by="rule:test",
        facts=[{"code": "GLU", "label": "Glukóza", "value_num": 5.5, "occurred_at": DAY}],
    )  # fmt: skip
    rule = [r for r in await _active(db, "clinical_records") if r["extracted_by"] == "rule:test"]
    doc_b = {t: len(await _llm_active(db, t, DOC_B)) for t in LLM_TABLES}
    await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    after = [r for r in await _active(db, "clinical_records") if r["extracted_by"] == "rule:test"]
    assert after == rule and len(rule) == 1
    assert {t: len(await _llm_active(db, t, DOC_B)) for t in LLM_TABLES} == doc_b
    for t in LLM_TABLES:
        assert await _llm_active(db, t, DOC_A) == [], t


# ── isolation and guards ────────────────────────────────────────────────


async def test_a_foreign_document_is_not_found_and_nothing_changes(seeded, local_model):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_BOB, PAGES_B, pid=BOB_UUID)
    before = await _snapshot(db)
    for kw in ({}, {"dry_run": False, "reason": "rollback"}):
        out = await _retract(db, [DOC_A, DOC_BOB], **kw)
        assert out["error"] == "not_found" and out["document_ids"] == [DOC_BOB]
        assert "wrong_patient" not in json.dumps(out)
        unknown = await _retract(db, [DOC_BOB + 1000], **kw)
        assert unknown["error"] == "not_found"  # same answer as for an unknown id
    assert await _snapshot(db) == before
    assert await _activity(db) == [] and local_model == []
    assert ERIKA_UUID not in tool._reocr_jobs


@pytest.mark.parametrize("busy", ["run", "job", "lock"])
async def test_refused_while_a_run_or_job_is_active(seeded, local_model, monkeypatch, busy):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    before = await _snapshot(db)
    lock = tool.patient_lock(ERIKA_UUID)
    if busy == "run":
        monkeypatch.setitem(tool._runs, ERIKA_UUID, {"state": "running"})
    elif busy == "job":
        tool._reocr_jobs[ERIKA_UUID] = {"state": "running", "kind": "reread"}
    else:
        await lock.acquire()
    try:
        out = await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    finally:
        if lock.locked():
            lock.release()
    assert out["error"].startswith("busy")
    assert await _snapshot(db) == before
    assert await _activity(db) == [] and local_model == []
    # a dry run is read-only and still answers
    assert (await _retract(db, [DOC_A]))["dry_run"] is True


async def test_no_pre_store_copy_no_change(seeded, local_model, monkeypatch):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    before = await _snapshot(db)

    def failing_copy(db_, job):
        raise pre_store.PreStoreError("quick_check failed on the copy")

    monkeypatch.setattr(pre_store, "make_copy", failing_copy)
    out = await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    assert out["error"].startswith("refused: pre-store copy failed")
    assert "nothing was written" in out["error"]
    assert await _snapshot(db) == before
    assert await _activity(db) == []
    assert ERIKA_UUID not in tool._reocr_jobs  # the reserved slot was released
    assert not tool.patient_lock(ERIKA_UUID).locked()


async def test_the_in_memory_db_has_no_file_to_copy(seeded, monkeypatch):
    """Without the fake copy the real make_copy fails closed on ``:memory:``."""
    db = seeded
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    await _store(db, DOC_A, PAGES_A)
    before = await _snapshot(db)
    out = await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    assert "no local database file" in out["error"]
    assert await _snapshot(db) == before


async def test_argument_guards(seeded, local_model, monkeypatch):
    db = seeded
    assert "required" in (await _retract(db, []))["error"]
    too_many = list(range(DOC_A, DOC_A + tool.RETRACT_MAX_DOCS + 1))
    assert "at most 50" in (await _retract(db, too_many))["error"]
    assert "reason is required" in (await _retract(db, [DOC_A], dry_run=False))["error"]
    assert (
        "reason is required" in (await _retract(db, [DOC_A], dry_run=False, reason="   "))["error"]
    )
    long = "x" * (tool.RETRACT_REASON_MAX + 1)
    assert "too long" in (await _retract(db, [DOC_A], dry_run=False, reason=long))["error"]
    assert local_model == []
    token = _verified_caller_is_admin.set(True)
    try:
        assert "patient_slug" in (await _retract(db, [DOC_A]))["error"]
    finally:
        _verified_caller_is_admin.reset(token)
    # the rollback works while the extractor is switched off (no model needed)
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", True)
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "")
    await _store(db, DOC_A, PAGES_A)
    out = await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    assert out["documents"][0]["result"] == "retracted"


# ── audit row ───────────────────────────────────────────────────────────


async def test_activity_log_row_has_ids_and_counts_only(seeded, local_model):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    token = _verified_caller_email.set("caregiver@example.com")
    try:
        out = await _retract(db, [DOC_A], dry_run=False, reason="wrong regimen mFOLFOX6 line")
    finally:
        _verified_caller_email.reset(token)
    (row,) = await _activity(db)
    assert row["patient_id"] == ERIKA_UUID and row["agent_id"] == "mcp"
    assert row["status"] == "ok"
    given = json.loads(row["input_summary"])
    assert given["caller_email_hash"] == caller_email_hash("caregiver@example.com")
    assert given["document_ids"] == [DOC_A] and given["pre_store"] == out["pre_store"]
    assert given["reason_chars"] == len("wrong regimen mFOLFOX6 line")
    result = json.loads(row["output_summary"])
    assert result["results"] == {"retracted": 1}
    assert result["stored"] == out["totals"]
    dumped = json.dumps(row, default=str)
    assert "caregiver@example.com" not in dumped  # never the email itself
    assert "wrong regimen" not in dumped  # never the free-text reason
    for value in ("mFOLFOX6", "Oxaliplatina", "sigmoidea", DAY):
        assert value not in dumped  # counts, never values
    # the tool's own answer carries counts only as well
    assert "Oxaliplatina" not in json.dumps(out) and "sigmoidea" not in json.dumps(out)


# ── re-extraction after a retract ───────────────────────────────────────


async def test_re_extraction_after_a_retract_stores_the_document_anew(seeded, local_model):
    """The hard tables and LLM clinical_records get a fresh random ``fhir_id``
    per insert (no stable-id path, unlike the lab facts of #726): the contract
    is that the retracted rows stay soft-deleted with their own ids and the
    re-extraction inserts the same content again as new rows."""
    db = seeded
    first = await _store(db, DOC_A, PAGES_A)
    old = {t: await _llm_active(db, t, DOC_A) for t in LLM_TABLES}
    await _retract(db, [DOC_A], dry_run=False, reason="rollback")
    second = await _store(db, DOC_A, PAGES_A)
    assert {k: v.get("added", 0) for k, v in second["stored"].items()} == {
        k: v.get("added", 0) for k, v in first["stored"].items()
    }
    assert all(v.get("removed", 0) == 0 for v in second["stored"].values())
    for table in LLM_TABLES:
        new = await _llm_active(db, table, DOC_A)
        assert len(new) == len(old[table]), table
        assert not {r["id"] for r in new} & {r["id"] for r in old[table]}
        assert not {r.get("fhir_id") for r in new if r.get("fhir_id")} & {
            r.get("fhir_id") for r in old[table]
        }
        deleted = {r["id"]: r for r in await _rows(db, table, where="deleted_at IS NOT NULL")}
        for r in old[table]:  # the retracted rows keep their ids and fhir_ids
            assert deleted[r["id"]].get("fhir_id") == r.get("fhir_id")


# ── registration ────────────────────────────────────────────────────────


def test_registered_as_destructive_and_never_scheduled():
    from oncofiles.server import mcp

    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    ann = tools["curated_clinical_retract"].annotations
    assert ann.readOnlyHint is False and ann.destructiveHint is True
    # store_document and retract share one removal plan (no duplicated keep logic)
    import inspect

    src = inspect.getsource(clinical_store.store_document)
    assert "_plan_removal(" in src and "_soft_delete_planned(" in src
    assert "PROTECTED_VERIFICATION" not in src
