"""Verify-only re-reads (#678). Synthetic data only (invented values, 2027+)."""

from __future__ import annotations

import json

import pytest

from oncofiles.curated import reread_verify as rv
from tests.helpers import ERIKA_UUID

COWORK = {"unverified_reasons": ["cowork_ocr"]}
DOC = 4242


def old(i, code, value, day="2027-05-11", page=1, meta=None):
    return {
        "id": i, "code": code, "value_canonical": value, "occurred_at": day,
        "page_number": page, "meta": meta or dict(COWORK), "verification": "unverified",
    }  # fmt: skip


def new(code, value, page=1, evidence="x"):
    return {"code": code, "value_canonical": value, "page_number": page, "evidence": evidence}


def test_digits_and_date_confirm_a_cowork_value():
    olds = [old(1, "WBC", 6.12), old(2, "PLT", 233.0)]
    got = rv.assess(olds, [new("WBC", 6.12), new("PLT", 233.0)], ["Odobrané 11.5.2027 07:10"])
    assert got == {"confirmed": [1, 2], "date_unconfirmed": [], "unconfirmed": []}


def test_a_missing_date_keeps_the_value_unverified():
    got = rv.assess([old(1, "WBC", 6.12)], [new("WBC", 6.12)], ["no date here"])
    assert got["date_unconfirmed"] == [1] and not got["confirmed"]


def test_one_match_never_confirms_a_repeated_trend_value():
    olds = [old(1, "CEA", 3.1, "2027-05-11"), old(2, "CEA", 3.1, "2027-06-02")]
    got = rv.assess(olds, [new("CEA", 3.1)], ["11.5.2027 2.6.2027"])
    assert got["unconfirmed"] == [1, 2]


def test_a_line_the_passes_disagree_on_confirms_nothing():
    got = rv.assess([old(1, "ALT", 0.71)], [new("ALT", 0.71, evidence="ALT 0,71 [?]")],
                    ["11.5.2027"])  # fmt: skip
    assert got["unconfirmed"] == [1]


def test_wrong_page_or_value_confirms_nothing():
    olds = [old(1, "HGB", 127.0, page=2), old(2, "K", 4.37)]
    got = rv.assess(olds, [new("HGB", 127.0, page=1), new("K", 4.73)], ["11.5.2027", "x"])
    assert got["unconfirmed"] == [1, 2]


def test_a_range_needs_both_ends_printed():
    meta = {**COWORK, "date_range_start": "2027-05-03", "date_range_end": "2027-05-19"}
    o = old(1, "NA", 140.6, "2027-05-11", meta=meta)
    assert rv.assess([o], [new("NA", 140.6)], ["3.5.2027 - 19.5.2027:"])["confirmed"] == [1]
    assert rv.assess([o], [new("NA", 140.6)], ["19.5.2027:"])["date_unconfirmed"] == [1]


def test_date_formats():
    assert rv.date_printed("2027-05-03", "Lab 3.5.: WBC")
    assert rv.date_printed("2027-05-03", "03.05.2027")
    assert rv.date_printed("2027-05-03", "3. 5. 27")
    assert rv.date_printed("2027-05-03", "2027-05-03")
    assert not rv.date_printed("2027-05-03", "13.5.2027")
    assert not rv.date_printed("2027-05-03", "3.5.2028")


def test_only_cowork_ocr_is_ever_removed():
    facts = [
        old(1, "WBC", 6.12),
        old(2, "PLT", 233.0, meta={"unverified_reasons": ["cowork_ocr", "handwritten"]}),
        old(3, "HGB", 127.0, meta={"unverified_reasons": ["label_fuzzy"]}),
        {**old(4, "K", 4.37), "verification": "machine", "meta": {}},
        old(5, "NA", 140.6),
    ]
    key = lambda f: list(rv.fact_key(f["code"], f["value_canonical"], f["occurred_at"], 1))  # noqa: E731
    result = {"confirmed": [key(f) for f in facts[:4]], "date_unconfirmed": [key(facts[4])]}
    plan = {fid: (v, m.get("unverified_reasons")) for fid, v, m in rv.apply_plan(result, facts)}
    assert plan == {
        1: ("machine", None),
        2: ("unverified", ["handwritten"]),
        5: ("unverified", ["date_unconfirmed"]),
    }  # 3 has no cowork_ocr, 4 is machine: untouched


async def _seed(db, sha="sha-a", md5="md-a"):
    meta = {"curated": {"sidecar_id": "side", "sidecar_sha": sha, "source_md5": md5}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata) "
        "VALUES (?, ?, ?, ?, 'report', 'application/pdf', ?, ?, ?, ?)",
        (
            DOC,
            f"gdrive:g{DOC}",
            f"d{DOC}.pdf",
            f"d{DOC}.pdf",
            len(meta),
            ERIKA_UUID,
            f"g{DOC}",
            json.dumps(meta),
        ),
    )
    for code, value in (("WBC", 6.12), ("PLT", 233.0)):
        await db.db.execute(
            "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
            "code, occurred_at, value_canonical, verification, metadata_json, page_number, "
            "source) VALUES (?, 'lab', ?, ?, ?, '2027-05-11', ?, 'unverified', ?, ?, 't')",
            (ERIKA_UUID, DOC, code, code, value, json.dumps(COWORK), len(code) // len(code)),
        )
    await db.db.commit()


async def _verification(db):
    async with db.db.execute(
        "SELECT code, verification, metadata_json FROM clinical_records "
        "WHERE deleted_at IS NULL ORDER BY code"
    ) as cur:
        return {r["code"]: (r["verification"], json.loads(r["metadata_json"])) for r in
                await cur.fetchall()}  # fmt: skip


async def test_stored_result_is_applied_on_every_index_and_void_when_the_text_changes(db):
    from oncofiles.tools import curated as tool

    await _seed(db)
    result = {"confirmed": [list(rv.fact_key("WBC", 6.12, "2027-05-11", 1))],
              "date_unconfirmed": [list(rv.fact_key("PLT", 233.0, "2027-05-11", 1))]}  # fmt: skip
    await db.insert_fact_reread(patient_id=ERIKA_UUID, document_id=DOC, sidecar_sha="sha-a",
                                source_md5="md-a", engine="qwen-local", result=result)  # fmt: skip
    out = await tool._corroborate_and_measure(db, ERIKA_UUID)
    assert out["reread_applied"] == 2
    got = await _verification(db)
    assert got["WBC"][0] == "machine" and got["WBC"][1]["verified_by_reread"] is True
    assert got["PLT"] == ("unverified", got["PLT"][1])
    assert got["PLT"][1]["unverified_reasons"] == ["date_unconfirmed"]
    # the sidecar changed: the result is void and nothing more is applied
    await db.db.execute(
        "UPDATE documents SET structured_metadata = ? WHERE id = ?",
        (json.dumps({"curated": {"sidecar_sha": "sha-b", "source_md5": "md-a"}}), DOC),
    )
    await db.db.commit()
    assert await db.valid_fact_rereads(patient_id=ERIKA_UUID) == {}
    assert await db.valid_fact_rereads(patient_id="someone-else") == {}


async def test_the_tool_requires_explicit_document_ids(db):
    from unittest.mock import MagicMock

    from oncofiles.tools import curated as tool

    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    out = json.loads(await tool.curated_reread_verify(ctx, document_ids=[]))
    assert out == {"error": "document_ids is required"}


@pytest.mark.parametrize("kind", ["reocr", "reread"])
async def test_jobs_are_not_curated_runs(db, kind):
    run = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger=kind)
    await db.finish_curated_run(run, patient_id=ERIKA_UUID, status="ok", message="", report={})
    assert await db.get_last_curated_run(patient_id=ERIKA_UUID) is None


def test_a_bare_day_month_never_confirms_when_another_year_is_on_the_page():
    two_years = "Kontrola 3.5.2028. Laboratórium 3.5.:"
    assert not rv.date_printed("2027-05-03", two_years)
    assert rv.date_printed("2028-05-03", two_years)
    assert rv.date_printed("2027-05-03", "Lab 3.5.: WBC; kontrola o 6 týždňov")


def test_date_unconfirmed_rows_are_checked_not_verified():
    f = old(1, "NA", 140.6)
    result = {
        "confirmed": [],
        "date_unconfirmed": [list(rv.fact_key("NA", 140.6, f["occurred_at"], 1))],
    }
    [(_, verification, meta)] = rv.apply_plan(result, [f])
    assert verification == "unverified" and meta["reread_checked"] is True
    assert "verified_by_reread" not in meta


def test_a_reread_job_counts_as_an_active_run():
    from oncofiles.tools import curated as tool

    tool._reocr_jobs["p-test"] = {"state": "running", "kind": "reread"}
    try:
        assert tool.run_active() is True  # the CI deploy waits for it
    finally:
        tool._reocr_jobs.pop("p-test")
