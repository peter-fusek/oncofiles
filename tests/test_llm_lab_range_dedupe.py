"""#826 follow-up, before any LLM lab write is enabled: a range-dated LLM lab
fact dedupes against a rule fact under the same printed range.

After #837 the generic verifier dates a value read under a printed range
(``start - end:``) at the range end
(``date_source=printed_range``, ``date_precision=range``, both ends kept),
the same way the hospital-report rule parser dates it. The write path
(``curated.lab_store``) must then see ONE printed result, in the dry run and
in a real store, and its stable id must key on the range.

Synthetic data only: invented labels, values and dates (2027+); the local
model is always mocked; the write flag is patched on for these tests only.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from oncofiles import config, fact_identity, pre_store
from oncofiles.curated import lab_store, llm
from oncofiles.database._facts import LLM_LAB_FAMILY
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID

DOB = "2027-01-02"  # invented
DOC = 8261
START, END = "2028-05-13", "2028-05-19"
RANGE = "13.5.2028 - 19.5.2028"
OTHER_START, OTHER_END = "2028-06-02", "2028-06-08"
OTHER_RANGE = "2.6.2028 - 8.6.2028"
ALT_LINE = "S-ALT 0,47 µkat/l"
GLU_LINE = "S-GLU 5,3 mmol/l"
# the hospital-report layout: a full-date range and a colon in front of the results
PAGE_1 = "\n".join(["Prepúšťacia správa", f"{RANGE}: {ALT_LINE}; {GLU_LINE}"])
# the same analyte and value under ANOTHER printed range, on the next page
PAGE_2 = "\n".join(["Pokračovanie správy", f"{OTHER_RANGE}: {ALT_LINE}"])
RANGE_META = {"date_source": "printed_range", "date_precision": "range",
              "date_range_start": START, "date_range_end": END}  # fmt: skip


def _item(label, value, unit, evidence, date):
    return {"label_as_printed": label, "value": value, "comparator": None,
            "unit_as_printed": unit, "ref_range_as_printed": None, "flag_as_printed": None,
            "date_as_printed": date, "date_role": "sample", "date_evidence": date,
            "evidence": evidence}  # fmt: skip


def model_chat(system, user, info=None, **kw):
    """The mocked local model: each page's results with the range as the date,
    colon included (what the model copies from the header, #826)."""
    if OTHER_RANGE in user:
        facts = [_item("S-ALT", "0,47", "µkat/l", ALT_LINE, f"{OTHER_RANGE}:")]
    else:
        facts = [_item("S-ALT", "0,47", "µkat/l", ALT_LINE, f"{RANGE}:"),
                 _item("S-GLU", "5,3", "mmol/l", GLU_LINE, f"{RANGE}:")]  # fmt: skip
    answer = json.dumps({"facts": facts}, ensure_ascii=False)
    if info is not None:
        info.update(finish_reason="stop", raw_content=answer)
    return answer


async def _rule_fact(db, *, day=END, meta=RANGE_META, value=0.47):
    """What the rule parser stores for the ALT result of PAGE_1."""
    return await db.replace_document_facts(
        patient_id=PRIMARY_TEST_UUID, document_id=DOC, extracted_by="rule:test",
        facts=[{"code": "ALT", "label": "S-ALT", "value_num": value, "value_canonical": value,
                "occurred_at": day, "page_number": 1, "evidence": ALT_LINE,
                "metadata": dict(meta or {})}],
    )  # fmt: skip


@pytest.fixture
async def seeded(db):
    meta = {"ocr_engine": "qwen-local", "curated": {"sidecar_id": f"s{DOC}"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata, document_date) "
        "VALUES (?, ?, ?, ?, 'discharge', 'application/pdf', 1, ?, ?, ?, ?)",
        (DOC, f"gdrive:g{DOC}", f"d{DOC}.pdf", f"d{DOC}.pdf", PRIMARY_TEST_UUID, f"g{DOC}",
         json.dumps(meta), "2028-06-09"),
    )  # fmt: skip
    await db.save_ocr_page(DOC, 1, PAGE_1, "curated-sidecar")
    await db.save_ocr_page(DOC, 2, PAGE_2, "curated-sidecar")
    await db.db.commit()
    return db


@pytest.fixture
def local_model(monkeypatch):
    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://local.invalid/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "mock-model")
    monkeypatch.setattr(config, "LLM_LAB_WRITE_ENABLED", True)  # this test only
    monkeypatch.setattr(llm, "chat", model_chat)
    monkeypatch.setattr(tool, "_reocr_jobs", {})
    monkeypatch.setattr(tool, "_readme_births", {PRIMARY_TEST_UUID: {DOB}})
    monkeypatch.setattr(pre_store, "make_copy", lambda db, job: f"oncofiles-copy-{job}.db")


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _run(db, **kw):
    started = json.loads(await tool.curated_lab_extract(_ctx(db), [DOC], **kw))
    assert "error" not in started, started
    await asyncio.wait_for(tool._reocr_jobs[PRIMARY_TEST_UUID]["task"], 10)
    job = tool._reocr_jobs[PRIMARY_TEST_UUID]
    assert job["state"] == "done", job
    return job["report"]["documents"][0]


async def _active(db, where="1=1"):
    async with db.db.execute(
        "SELECT * FROM clinical_records WHERE source_document_id = ? AND deleted_at IS NULL "
        f"AND {where} ORDER BY id",  # noqa: S608
        (DOC,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _llm(day, meta=None, code="ALT"):
    return {"code": code, "category": "lab", "occurred_at": day, "verification": "machine",
            "metadata": dict(meta or {})}  # fmt: skip


def _stored(day, meta=None, code="ALT"):
    return {"code": code, "occurred_at": day, "metadata_json": json.dumps(meta or {})}


def _range(start, end):
    return {"date_source": "printed_range", "date_precision": "range",
            "date_range_start": start, "date_range_end": end}  # fmt: skip


# ── (a) the same printed range: one fact, in the dry run and in the write ──


async def test_dry_run_sees_the_rule_fact_under_the_same_range_as_the_same_fact(
    seeded, local_model
):
    db = seeded
    await _rule_fact(db)
    before = await _active(db)
    doc = await _run(db)  # dry run
    assert doc["result"] == "previewed" and await _active(db) == before
    # the ALT of page 1 is the rule fact; GLU (page 1) and the ALT under the
    # other range (page 2) are new
    assert doc["skipped"] == {"skipped_rule_exists": 1}
    assert doc["would_store"] == {"machine": 2}
    by_code = doc["agreement_by_code"]
    one = len(["a result"])
    assert by_code["ALT"] == {"agree": one, "llm_only": one}
    assert by_code["GLU"] == {"llm_only": one}
    assert not doc["value_mismatch"] and not doc["date_mismatch"] and not doc["rule_only"]


async def test_write_stores_no_second_row_for_the_result_under_the_same_range(seeded, local_model):
    db = seeded
    await _rule_fact(db)
    doc = await _run(db, dry_run=False)
    assert doc["result"] == "stored" and doc["skipped"] == {"skipped_rule_exists": 1}
    assert doc["stored"]["added"] == 2 and doc["stored"]["machine"] == 2
    # the result printed under RANGE on page 1: exactly one active ALT row, the rule one
    same = await _active(db, f"code = 'ALT' AND date(occurred_at) = '{END}'")
    assert [r["extracted_by"] for r in same] == ["rule:test"]
    lab = {
        (r["code"], r["page_number"]): r for r in await _active(db, "extracted_by LIKE 'llm-lab%'")
    }
    assert sorted(lab) == [("ALT", 2), ("GLU", 1)]
    # the stored LLM facts are dated like the rule parser dates them: range end, both ends kept
    glu = lab["GLU", 1]
    meta = json.loads(glu["metadata_json"])
    assert glu["occurred_at"][:10] == END and glu["verification"] == "machine"
    assert {k: meta[k] for k in RANGE_META} == RANGE_META
    # a second write changes nothing: same ids, still one ALT row for that range
    ids = {k: r["fhir_id"] for k, r in lab.items()}
    tool._reocr_jobs.clear()
    again = await _run(db, dry_run=False)
    assert again["skipped"] == {"skipped_rule_exists": 1}
    rows = await _active(db, "extracted_by LIKE 'llm-lab%'")
    assert {(r["code"], r["page_number"]): r["fhir_id"] for r in rows} == ids
    assert len(await _active(db, f"code = 'ALT' AND date(occurred_at) = '{END}'")) == 1


# ── (b) the stable id keys on the range; only the family salt differs ──────

# Computed ONCE with fact_identity (family="llm-lab") on the synthetic "range"
# case of tests/test_llm_lab_write.py::GOLDEN_CASES. They must never change.
GOLDEN_FACT = {"code": "CRP", "label": "CRP", "occurred_at": "2027-09-09", "page_number": 1,
               "metadata": {"date_range_start": "2027-09-01",
                            "date_range_end": "2027-09-09"}}  # fmt: skip
GOLDEN_DOC = 7141
GOLDEN_LLM_RANGE_IDS = ("749dfb27-d5e0-59f8-b767-c81116a31508",
                        "cc2155a3-6c91-5ebd-8f79-fc5e62439310")  # fmt: skip
GOLDEN_RULE_RANGE_ID = "ddf5a185-2e43-57a1-b02d-37d5cb765550"  # the #818 golden of main


def test_a_range_dated_llm_fact_id_keys_on_the_range_and_differs_by_the_salt_only():
    twins = [{**GOLDEN_FACT, "evidence": "a"}, {**GOLDEN_FACT, "evidence": "b"}]
    ids = fact_identity.ids_for_new_facts(
        PRIMARY_TEST_UUID, GOLDEN_DOC, twins, set(), family=LLM_LAB_FAMILY
    )
    assert tuple(ids) == GOLDEN_LLM_RANGE_IDS
    rule_key = fact_identity.fact_key(PRIMARY_TEST_UUID, GOLDEN_DOC, GOLDEN_FACT)
    llm_key = fact_identity.fact_key(
        PRIMARY_TEST_UUID, GOLDEN_DOC, GOLDEN_FACT, family=LLM_LAB_FAMILY
    )
    # the key holds the range (start/end), not the day the value was placed on …
    assert json.loads(rule_key)[3] == "2027-09-01/2027-09-09"
    moved = {**GOLDEN_FACT, "occurred_at": "2027-09-05"}  # e.g. the old midpoint
    assert (
        fact_identity.fact_key(PRIMARY_TEST_UUID, GOLDEN_DOC, moved, family=LLM_LAB_FAMILY)
        == llm_key
    )
    # … and is the rule key with the family salt in front, nothing else
    assert llm_key == f"{LLM_LAB_FAMILY}|{rule_key}"
    assert fact_identity.fact_uuid(rule_key, 0) == GOLDEN_RULE_RANGE_ID
    assert GOLDEN_RULE_RANGE_ID not in ids
    # another range, or the range end as a plain day, is another fact
    other = {**GOLDEN_FACT, "metadata": {"date_range_start": "2027-09-02",
                                         "date_range_end": "2027-09-09"}}  # fmt: skip
    plain = {**GOLDEN_FACT, "metadata": {}}
    for f in (other, plain):
        assert (
            fact_identity.fact_key(PRIMARY_TEST_UUID, GOLDEN_DOC, f, family=LLM_LAB_FAMILY)
            != llm_key
        )


async def test_the_stored_range_fact_has_the_range_keyed_id(seeded, local_model):
    db = seeded
    await _run(db, dry_run=False)  # no rule fact: all three results are stored
    rows = {(r["code"], r["page_number"]): r for r in await _active(db)}
    first, second = range(1, 3)  # page numbers
    assert sorted(rows) == [("ALT", first), ("ALT", second), ("GLU", first)]
    for (code, page), (start, end) in {("ALT", 1): (START, END), ("GLU", 1): (START, END),
                                       ("ALT", 2): (OTHER_START, OTHER_END)}.items():  # fmt: skip
        row = rows[code, page]
        key = fact_identity.natural_key(
            PRIMARY_TEST_UUID, DOC, code=code, label=None, occurred_at=end, page=page,
            meta={"date_range_start": start, "date_range_end": end}, family=LLM_LAB_FAMILY,
        )  # fmt: skip
        assert f'"{start}/{end}"' in key
        assert row["fhir_id"] == fact_identity.fact_uuid(key, 0)
        assert (
            row["fhir_id"]
            == fact_identity.ids_for_rows(
                PRIMARY_TEST_UUID, DOC, [{**row, "fhir_id": None}], family=LLM_LAB_FAMILY
            )[row["id"]]
        )
        # the rule parser's id for the same printed result is another id
        rule = fact_identity.natural_key(
            PRIMARY_TEST_UUID, DOC, code=code, label=None, occurred_at=end, page=page,
            meta={"date_range_start": start, "date_range_end": end},
        )  # fmt: skip
        assert key == f"{LLM_LAB_FAMILY}|{rule}"
        assert row["fhir_id"] != fact_identity.fact_uuid(rule, 0)


# ── (c) another printed range is another result ────────────────────────────


async def test_the_same_code_and_value_under_a_different_range_is_no_duplicate(seeded, local_model):
    db = seeded
    await _rule_fact(db)
    await _run(db, dry_run=False)
    alt = await _active(db, "code = 'ALT'")
    assert [(r["extracted_by"].split("-v")[0], r["occurred_at"][:10]) for r in alt] == [
        ("rule:test", END), (LLM_LAB_FAMILY, OTHER_END),
    ]  # fmt: skip
    assert alt[0]["value_canonical"] == alt[1]["value_canonical"]  # the very same value
    meta = json.loads(alt[1]["metadata_json"])
    assert (meta["date_range_start"], meta["date_range_end"]) == (OTHER_START, OTHER_END)


def test_disjoint_ranges_never_block_overlapping_ones_do():
    rule = [_stored(END, _range(START, END))]
    assert not lab_store._blocked(_llm(OTHER_END, _range(OTHER_START, OTHER_END)), rule)
    assert not lab_store._blocked(_llm("2028-05-12", _range("2028-05-06", "2028-05-12")), rule)
    # spans that share a day cannot be told apart: the rule fact wins
    assert lab_store._blocked(_llm("2028-05-22", _range("2028-05-19", "2028-05-22")), rule)
    assert lab_store._blocked(_llm("2028-05-16", _range("2028-05-10", "2028-05-16")), rule)
    assert not lab_store._blocked(_llm(END, _range(START, END), code="GLU"), rule)


# ── (d) a range against a single day: the range is the whole window ────────


@pytest.mark.parametrize(
    ("day", "blocked"),
    [(START, True), ("2028-05-16", True), (END, True), ("2028-05-12", False),
     ("2028-05-20", False)],
)  # fmt: skip
async def test_a_rule_range_blocks_an_llm_fact_dated_on_a_day_inside_it(seeded, day, blocked):
    db = seeded
    await _rule_fact(db)
    plan = await lab_store.plan_document(
        db,
        patient_id=PRIMARY_TEST_UUID,
        document_id=DOC,
        facts=[_llm(day, {"date_source": "printed"})],
    )
    assert plan["skipped"] == ({"skipped_rule_exists": 1} if blocked else {})
    assert len(plan["facts"]) == (0 if blocked else 1)


@pytest.mark.parametrize(
    ("day", "blocked"),
    [(START, True), ("2028-05-16", True), (END, True), ("2028-05-12", False),
     ("2028-05-20", False)],
)  # fmt: skip
async def test_an_llm_range_is_blocked_by_a_rule_fact_dated_on_a_day_inside_it(
    seeded, day, blocked
):
    """The other direction of the same rule (fixed with this test): the LLM
    fact's own range is its window too."""
    db = seeded
    await _rule_fact(db, day=day, meta={"date_source": "printed"})
    plan = await lab_store.plan_document(
        db, patient_id=PRIMARY_TEST_UUID, document_id=DOC, facts=[_llm(END, RANGE_META)]
    )
    assert plan["skipped"] == ({"skipped_rule_exists": 1} if blocked else {})
    assert len(plan["facts"]) == (0 if blocked else 1)


def test_single_days_and_undated_facts_dedupe_as_before():
    one = [_stored(END)]
    assert lab_store._blocked(_llm(END), one)
    assert lab_store._blocked(_llm(f"{END}T07:40"), one)
    assert not lab_store._blocked(_llm(START), one)
    assert lab_store._blocked(_llm(None), one)  # undated: any value of the code wins
    assert not lab_store._blocked(_llm(END), [_stored(None)])
    assert not lab_store._blocked(_llm(END), [{"code": "ALT", "occurred_at": START,
                                               "metadata_json": "not json"}])  # fmt: skip
