# ruff: noqa: F811  (pytest fixtures imported from test_clinical_extract)
"""Infra design review of PR #729 (issue #726, item four).

Covers: the pre-store safety copy and the activity_log row of a real write;
model-read rows labelled everywhere; identical rows grouped across documents
(until #730); the rebuilt timeline view keeps the pre-existing rows.
Synthetic data only (invented values and dates, 2027+); the model is mocked.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from oncofiles import config, pre_store
from oncofiles.curated import llm
from oncofiles.database import Database
from oncofiles.fact_labels import LLM_METHOD
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.persistent_oauth import _verified_caller_email
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from oncofiles.tools import treatment as tt
from tests.helpers import ERIKA_UUID
from tests.test_clinical_extract import (  # noqa: F401  (fixtures)
    DAY,
    DOB,
    DOC_A,
    DOC_B,
    PAGES_A,
    PAGES_B,
    _active,
    _ctx,
    _doc,
    _snapshot,
    _store,
    local_model,
    model_chat,
    seeded,
)

DOC_C = DOC_A + 10  # a second document with the same pages as DOC_A
LLM_LABEL = {"en": "text verified on page, read by the model",
             "sk": "prečítané modelom, text overený"}  # fmt: skip
RULE_LABEL = {"en": "machine-verified", "sk": "overené strojom"}
MIGRATIONS = Path(__file__).parent.parent / "migrations"


# ── 1. the write path: pre-store copy, max ids, activity_log ────────────


@pytest.fixture
async def file_db(tmp_path, monkeypatch):
    path = tmp_path / "data" / "oncofiles.db"
    path.parent.mkdir()
    monkeypatch.setattr(config, "DATABASE_PATH", path)
    database = Database(path)
    await database.connect()
    await database.migrate()
    token = _current_patient_id.set(ERIKA_UUID)
    yield database
    _current_patient_id.reset(token)
    await database.close()


def _real_model(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_CLINICAL_EXTRACT_DISABLED", False)
    monkeypatch.setattr(llm, "chat", model_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_readme_births", {ERIKA_UUID: {DOB}})


async def _activity(db) -> list[dict]:
    async with db.db.execute(
        "SELECT patient_id, agent_id, tool_name, input_summary, output_summary, status "
        "FROM activity_log WHERE tool_name = 'curated_clinical_extract' ORDER BY id"
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_a_real_write_makes_a_verified_copy_first_and_logs_it(file_db, monkeypatch):
    db = file_db
    _real_model(monkeypatch)
    await _doc(db, DOC_A, ERIKA_UUID, PAGES_A)
    token = _verified_caller_email.set("caregiver@example.com")
    try:
        out = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False))
    finally:
        _verified_caller_email.reset(token)
    assert out["pre_store"].endswith("-clinical.db") and out["actor"] == "caregiver@example.com"
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)
    job = tool._reocr_jobs[ERIKA_UUID]
    assert job["state"] == "done" and job["report"]["pre_store"] == out["pre_store"]
    copy = pre_store.pre_store_dir() / out["pre_store"]
    assert pre_store.NAME_RE.fullmatch(copy.name) and copy.is_file()
    assert stat.S_IMODE(os.stat(copy).st_mode) == stat.S_IRUSR | stat.S_IWUSR
    # the copy is the state BEFORE the write: no conditions in it
    copy_db = Database(copy)
    await copy_db.connect()
    try:
        async with copy_db.db.execute("SELECT COUNT(*) FROM conditions") as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await copy_db.close()
    assert len(await _active(db, "conditions")) == len(["primary", "metastasis"])

    (row,) = await _activity(db)
    assert row["patient_id"] == ERIKA_UUID and row["agent_id"] == "caregiver@example.com"
    assert row["status"] == "ok"
    given = json.loads(row["input_summary"])
    assert given["document_ids"] == [DOC_A] and given["pre_store"] == out["pre_store"]
    result = json.loads(row["output_summary"])
    assert result["results"] == {"stored": 1}
    assert result["counts"]["diagnoses"]["machine"] == len(["primary", "metastasis"])
    assert result["stored"]["conditions"]["added"] == len(["primary", "metastasis"])
    dumped = row["input_summary"] + row["output_summary"]
    assert "Oxaliplatina" not in dumped and "mFOLFOX6" not in dumped  # counts, never values


async def test_no_copy_no_write(seeded, monkeypatch):
    """The in-memory DB has no file to copy: the write is refused, the slot freed."""
    db = seeded
    _real_model(monkeypatch)
    before = await _snapshot(db)
    out = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False))
    assert out["error"].startswith("refused: pre-store copy failed")
    assert "nothing was written" in out["error"]
    assert ERIKA_UUID not in tool._reocr_jobs  # the reserved slot was released
    assert await _snapshot(db) == before
    assert await _activity(db) == []
    # a dry run needs no copy
    started = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_A]))
    assert started["kind"] == "clinical_preview" and "pre_store" not in started
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)


async def test_more_than_ten_explicit_ids_is_a_clear_error(seeded, local_model):
    ids = [DOC_A + i for i in range(tool.CLINICAL_MAX_DOCS + 1)]
    out = json.loads(await tool.curated_clinical_extract(_ctx(seeded), ids, dry_run=False))
    assert out["error"] == "too many documents: at most 10 explicit document_ids per call, got 11"
    assert local_model == []  # refused before any copy


async def test_a_second_call_cannot_start_while_the_first_is_starting(
    seeded, local_model, monkeypatch
):
    """The slot is reserved before the DOB lookup and the copy (both await)."""
    db = seeded
    gate = asyncio.Event()

    async def slow_dob(db_, pid):
        await gate.wait()
        return {DOB}

    monkeypatch.setattr(tool, "_all_birth_dates", slow_dob)
    first = asyncio.create_task(tool.curated_clinical_extract(_ctx(db), [DOC_A], dry_run=False))
    await asyncio.sleep(0.01)
    second = json.loads(await tool.curated_clinical_extract(_ctx(db), [DOC_B], dry_run=False))
    assert second["error"].startswith("busy")
    gate.set()
    assert json.loads(await first)["kind"] == "clinical_extract"
    await asyncio.wait_for(tool._reocr_jobs[ERIKA_UUID]["task"], 10)


# ── 2. model-read rows are labelled everywhere ──────────────────────────


async def test_model_read_rows_say_so_and_rule_rows_keep_their_label(seeded):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=DOC_B, extracted_by="rule:test",
        facts=[{"code": "CRP", "label": "CRP", "value_num": 9.5, "flag": "H",
                "occurred_at": DAY, "verification": "machine"}],
    )  # fmt: skip
    ctx = _ctx(db)
    rows = json.loads(await cf.timeline(ctx))["rows"]
    llm_rows = [r for r in rows if r["method"] == LLM_METHOD]
    assert {r["entity"] for r in llm_rows} >= {
        "condition", "treatment_course", "treatment_cycle", "medication_event", "procedure",
        "fact", "encounter",
    }  # fmt: skip
    assert all(r["verification"] == "machine" for r in llm_rows)  # the DB keeps machine
    assert all(r["verification_label"] == LLM_LABEL for r in llm_rows)
    crp = next(r for r in rows if r["entity"] == "fact" and r["kind"] == "lab")
    assert crp["method"] == "rule" and crp["verification_label"] == RULE_LABEL
    document = next(r for r in rows if r["entity"] == "document")
    assert document["method"] is None and document["verification_label"] is None

    overview = json.loads(await cf.patient_overview(ctx))
    assert all(d["method"] == LLM_METHOD and d["verification_label"] == LLM_LABEL
               for d in overview["diagnoses_and_markers"])  # fmt: skip
    assert overview["llm_note"].startswith("Rows with method 'llm-clinical'")
    listed = json.loads(await tt.list_treatment_events(ctx))
    for name in ("courses", "cycles", "medication_events"):
        assert listed[name] and all(r["method"] == LLM_METHOD for r in listed[name]), name
        assert all(r["verification_label"] == LLM_LABEL for r in listed[name]), name
    for fn in (cf.timeline, cf.patient_overview, tt.list_treatment_events,
               tool.curated_clinical_extract, tool.curated_clinical_extract_preview):  # fmt: skip
        assert "is not verified" in " ".join((fn.__doc__ or "").split()).replace("NOT", "not")


# ── 3. identical rows across documents: one item with sources ───────────


async def test_identical_rows_from_two_documents_are_one_item(seeded):
    db = seeded
    await _doc(db, DOC_C, ERIKA_UUID, PAGES_A)
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_C, PAGES_A)
    first = next(c for c in await _active(db, "conditions") if c["source_document_id"] == DOC_A)
    await db.db.execute("UPDATE conditions SET verification = 'verified' WHERE id = ?",
                        (first["id"],))  # fmt: skip
    await db.db.commit()
    ctx = _ctx(db)

    rows = json.loads(await cf.timeline(ctx, kinds=["condition", "treatment", "procedure"]))
    for r in rows["rows"]:
        docs = {s["document_id"] for s in r["sources"]}
        assert docs == {DOC_A, DOC_C} and r["per_document"] is False, r["title"]
    titles = [r["title"] for r in rows["rows"]]
    assert len(titles) == len(set(titles))  # no duplicate row per document
    primary = next(
        r for r in rows["rows"] if r["entity"] == "condition" and r["entity_id"] == first["id"]
    )
    assert primary["verification"] == "verified"  # the strongest of its sources
    assert {s["verification"] for s in primary["sources"]} == {"verified", "machine"}
    assert all(s["page"] for s in primary["sources"])

    overview = json.loads(await cf.patient_overview(ctx))
    names = [d["name"] for d in overview["diagnoses_and_markers"]]
    assert len(names) == len(set(names))
    kras = next(d for d in overview["diagnoses_and_markers"] if d["code"] == "KRAS")
    assert {s["document_id"] for s in kras["sources"]} == {DOC_A, DOC_C}

    listed = json.loads(await tt.list_treatment_events(ctx))
    (course,) = listed["courses"]
    assert {s["document_id"] for s in course["sources"]} == {DOC_A, DOC_C}
    assert len(listed["medication_events"]) == len(["Oxaliplatina", "5-FU"])
    assert len(listed["cycles"]) == len(["administered", "planned"])


async def test_different_rows_are_never_grouped(seeded):
    db = seeded
    await _store(db, DOC_A, PAGES_A)
    await _store(db, DOC_B, PAGES_B)
    listed = json.loads(await tt.list_treatment_events(_ctx(db)))
    assert {c["regimen_printed"] for c in listed["courses"]} == {"mFOLFOX6", "FOLFIRI"}
    assert all(len(c["sources"]) == 1 for c in listed["courses"])


# ── 4. the rebuilt timeline view keeps the pre-existing rows ────────────

OLD_COLUMNS = (
    "patient_id", "at", "kind", "title", "value", "unit", "flag", "verification",
    "source_document_id", "entity", "entity_id", "organization_id", "practitioner_id",
)  # fmt: skip
NEW_ENTITIES = ("condition", "treatment_course", "treatment_cycle", "medication_event",
                "procedure")  # fmt: skip


async def _old_view(db) -> None:
    """timeline_v exactly as migration 075 defined it, as timeline_old_v."""
    text = (MIGRATIONS / "075_codes_iron_vitd_views.sql").read_text()
    sql = text[text.index("CREATE VIEW timeline_v AS") :].split(";")[0]
    await db.db.execute(sql.replace("CREATE VIEW timeline_v", "CREATE VIEW timeline_old_v"))
    await db.db.commit()


async def _rows_of(db, view: str, where: str = "1=1") -> list[tuple]:
    cols = ", ".join(OLD_COLUMNS)
    async with db.db.execute(f"SELECT {cols} FROM {view} WHERE {where}") as cur:  # noqa: S608
        return sorted((tuple(r) for r in await cur.fetchall()), key=repr)


async def _seed_pre_existing(db) -> None:
    org = await db.upsert_organization("TestNemocnica")
    enc = await db.upsert_encounter(
        patient_id=ERIKA_UUID, natural_key=f"{DAY}|TestNemocnica|-|visit", start_at=DAY,
        kind="visit", title="Návšteva", organization_id=org, department_id=None,
        practitioner_id=None,
    )  # fmt: skip
    await db.db.execute("UPDATE documents SET encounter_id = ?, title = 'Správa' WHERE id = ?",
                        (enc, DOC_A))  # fmt: skip
    await db.db.execute(
        "INSERT INTO treatment_events (event_date, event_type, title, patient_id, "
        "source_document_id) VALUES (?, 'chemo', 'Legacy cycle', ?, ?)",
        ("2027-05-03", ERIKA_UUID, DOC_A),
    )
    facts = [
        {"category": "lab", "code": "CRP", "label": "CRP", "value_num": 9.5, "flag": "H"},
        {"category": "lab", "code": "GLU", "label": "Glukóza", "value_num": 5.1},
        {"category": "condition", "label": "Hypertenzia", "value_text": "áno"},
        {"category": "procedure", "label": "Biopsia", "value_text": "vykonaná"},
        {"category": "genetic_variant", "code": "KRAS", "label": "KRAS", "value_text": "wt"},
        {"category": "biomarker", "code": "MSI", "label": "MSI", "value_text": "MSS"},
        {"category": "medication", "label": "Testoplatina", "value_text": "podaná"},
        {"category": "imaging", "label": "Uzlina", "value_num": 12, "flag": "H"},
        {"category": "imaging", "label": "Ložisko", "value_num": 8},
        {"category": "vital", "code": "WEIGHT", "label": "Hmotnosť", "value_num": 70},
    ]
    for f in facts:
        f.update(occurred_at=DAY, page_number=1, verification="machine")
    await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=DOC_B, facts=facts, extracted_by="rule:test"
    )


async def test_rebuilt_view_returns_the_same_rows_for_pre_existing_kinds(seeded):
    db = seeded
    await _old_view(db)
    await _seed_pre_existing(db)
    old = await _rows_of(db, "timeline_old_v")
    assert len(old) > len(["encounter", "document", "treatment", "facts"])
    # (a) pre-existing data only: exactly the same rows, column for column; the
    # only addition is the new kind imaging (unflagged imaging facts)
    unflagged_imaging = "entity = 'fact' AND kind = 'imaging' AND flag IS NULL"
    assert await _rows_of(db, "timeline_v", f"NOT ({unflagged_imaging})") == old
    # (b) with extracted rows added: the old rows are all still there, unchanged,
    # and every extra row is a new kind (hard tables, planned, unflagged imaging)
    await _store(db, DOC_A, PAGES_A)
    planned = "entity = 'encounter' AND kind = 'planned'"
    new_only = (
        f"entity IN {NEW_ENTITIES} OR ({planned}) "
        "OR (entity = 'fact' AND kind = 'imaging' AND flag IS NULL)"
    )
    # planned encounters are new data, which the old view showed as plain encounters
    async with db.db.execute("SELECT id FROM encounters WHERE status = 'planned'") as cur:
        planned_ids = {r[0] for r in await cur.fetchall()}
    assert planned_ids
    old_after = [
        r for r in await _rows_of(db, "timeline_old_v")
        if not (r[OLD_COLUMNS.index("entity")] == "encounter"
                and r[OLD_COLUMNS.index("entity_id")] in planned_ids)
    ]  # fmt: skip
    assert await _rows_of(db, "timeline_v", f"NOT ({new_only})") == old_after
    assert set(old) <= set(old_after)


# ── pre-store retention: KEEP copies in TOTAL across job kinds ──────────


def test_pre_store_keeps_five_copies_in_total_across_job_kinds(file_db):
    from datetime import datetime, timedelta

    start = datetime.fromisoformat("2027-03-01T00:00:00+00:00")
    kinds = ["reread", "clinical", "reocr", "clinical", "reread", "clinical", "reread"]
    names = [
        pre_store.make_copy(file_db, kind, now=start + timedelta(minutes=n))
        for n, kind in enumerate(kinds)
    ]
    directory = pre_store.pre_store_dir()
    (directory / "notes.txt").write_text("not a copy")  # never touched by the cleanup
    names.append(pre_store.make_copy(file_db, "clinical", now=start + timedelta(hours=1)))
    left = sorted(n for n in os.listdir(directory) if n.endswith(".db"))
    assert pre_store.KEEP == 5 and len(left) == pre_store.KEEP  # in total, not per kind
    assert left == sorted(names)[-pre_store.KEEP :]  # the oldest go first, whatever the kind
    assert {n.rsplit("-", 1)[1] for n in left} == {"reread.db", "clinical.db"}
    assert (directory / "notes.txt").exists()
    assert all(pre_store.NAME_RE.fullmatch(n) for n in names)  # both kinds match the cleanup
