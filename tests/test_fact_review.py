"""Human verification of facts (#636): verify / correct / unverify. Synthetic data only."""

from __future__ import annotations

import json

import pytest

from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.secrets_keys import caller_email_hash
from oncofiles.tools import fact_review as fr
from tests.helpers import ERIKA_UUID

BOB_UUID = "00000000-0000-4000-8000-000000000002"
BOB_SLUG = "bob-test"
DOC = 4101
BOB_DOC = 4102
DAY = "2027-03-09"
CARER = "carer@example.com"


def _agent() -> str:
    return f"user:{caller_email_hash(CARER)}"


COUNT_UNIT = "10^9/l"
DAY2 = "2027-03-10"


class _Ctx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


@pytest.fixture(autouse=True)
def _hash_key(monkeypatch):
    monkeypatch.setenv("AUDIT_EMAIL_HASH_SECRET", "test-audit-key")


@pytest.fixture
def caller():
    """A non-admin OAuth caller bound to Erika, with an email."""
    tokens = (
        _verified_caller_is_admin.set(False),
        _verified_caller_email.set(CARER),
        _verified_patient_id.set(ERIKA_UUID),
    )
    yield
    _verified_caller_is_admin.reset(tokens[0])
    _verified_caller_email.reset(tokens[1])
    _verified_patient_id.reset(tokens[2])


def _lab(code: str, value: float, unit: str = "g/l", day: str = DAY) -> dict:
    return {
        "code": code,
        "category": "lab",
        "occurred_at": day,
        "label": code,
        "value_num": value,
        "value_canonical": value,
        "unit": unit,
        "page_number": 1,
        "evidence": f"{code} synthetic line",
        "metadata": {"date_source": "printed"},
    }


async def _seed(db, *, pid=ERIKA_UUID, doc=DOC, facts=None) -> dict[str, int]:
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, "
        "mime_type, size_bytes, patient_id) VALUES (?, ?, ?, ?, 'labs', 'application/pdf', 1, ?)",
        (doc, f"t:{doc}", f"d{doc}.pdf", f"d{doc}.pdf", pid),
    )
    await db.db.commit()
    facts = facts or [
        _lab("HGB", 121.0),
        _lab("PLT", 233.0, COUNT_UNIT),
    ]
    await db.replace_document_facts(
        patient_id=pid, document_id=doc, facts=facts, extracted_by="rule:test"
    )
    async with db.db.execute(
        "SELECT id, code FROM clinical_records WHERE patient_id = ? AND source_document_id = ? "
        "AND deleted_at IS NULL",
        (pid, doc),
    ) as cur:
        return {r["code"]: r["id"] for r in await cur.fetchall()}


async def _row(db, fact_id: int) -> dict:
    async with db.db.execute("SELECT * FROM clinical_records WHERE id = ?", (fact_id,)) as cur:
        return dict(await cur.fetchone())


async def _all(db, sql: str, params: tuple = ()) -> list[dict]:
    async with db.db.execute(sql, params) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _add_bob(db):
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()


# ── verify / unverify ──


async def test_verify_round_trip_writes_audit_and_provenance(db, caller):
    ids = await _seed(db)
    out = json.loads(await fr.verify_fact(_Ctx(db), ids["HGB"], note="checked p1"))
    assert out["status"] == "verified" and out["fact"]["verification"] == "verified"
    row = await _row(db, ids["HGB"])
    assert row["verification"] == "verified" and row["updated_by"] == _agent()

    [prov] = await db.list_provenance("fact", ids["HGB"], patient_id=ERIKA_UUID)
    assert (prov.activity, prov.agent_role, prov.agent_user) == ("verify", "verifier", _agent())
    assert prov.source_document_id == DOC and prov.page_number == 1
    assert prov.quote == "HGB synthetic line" and prov.reason == "checked p1"
    assert out["provenance_id"] == prov.id

    audit = await db.list_clinical_record_audit(ids["HGB"])
    assert audit[0].action == "update" and audit[0].changed_fields == "verification"
    assert json.loads(audit[0].before_json) == {"verification": "machine"}
    assert audit[0].changed_by == _agent()

    again = json.loads(await fr.verify_fact(_Ctx(db), ids["HGB"]))
    assert again["status"] == "already_verified"
    assert len(await db.list_provenance("fact", ids["HGB"], patient_id=ERIKA_UUID)) == 1

    out = json.loads(await fr.unverify_fact(_Ctx(db), ids["HGB"]))
    assert out["status"] == "unverified" and out["fact"]["verification"] == "machine"
    audit = await db.list_clinical_record_audit(ids["HGB"])
    assert json.loads(audit[0].after_json) == {"verification": "machine"}
    assert audit[0].reason == "unverify"


async def test_unverify_restores_unverified_state(db, caller):
    ids = await _seed(db, facts=[{**_lab("HGB", 121.0), "verification": "unverified"}])
    await fr.verify_fact(_Ctx(db), ids["HGB"])
    await fr.unverify_fact(_Ctx(db), ids["HGB"])
    assert (await _row(db, ids["HGB"]))["verification"] == "unverified"
    out = json.loads(await fr.unverify_fact(_Ctx(db), ids["HGB"]))
    assert out["status"] == "not_verified"


async def test_verify_accepts_fhir_id_and_string_id(db, caller):
    ids = await _seed(db)
    await db.db.execute(
        "UPDATE clinical_records SET fhir_id = ? WHERE id = ?", ("fact-uuid-a", ids["PLT"])
    )
    await db.db.commit()
    out = json.loads(await fr.verify_fact(_Ctx(db), "fact-uuid-a"))
    assert out["status"] == "verified" and out["fact"]["id"] == ids["PLT"]
    out = json.loads(await fr.verify_fact(_Ctx(db), str(ids["HGB"])))
    assert out["status"] == "verified"


# ── correct ──


async def test_correct_never_edits_in_place(db, caller):
    ids = await _seed(db)
    old_before = await _row(db, ids["HGB"])
    out = json.loads(
        await fr.correct_fact(_Ctx(db), ids["HGB"], reason="misread digit", value_num=127.0)
    )
    assert out["status"] == "corrected"
    new_id = out["fact"]["id"]
    assert new_id != ids["HGB"]

    old = await _row(db, ids["HGB"])
    assert old["deleted_at"] is not None and old["deleted_by"] == _agent()
    for col in ("value_num", "value_canonical", "verification", "version_id", "metadata_json"):
        assert old[col] == old_before[col]  # the old version is untouched apart from deletion

    new = await _row(db, new_id)
    assert new["deleted_at"] is None and new["verification"] == "corrected"
    assert new["value_num"] == 127.0 and new["value_canonical"] == 127.0
    assert new["version_id"] == old["version_id"] + 1
    for col in ("source_document_id", "page_number", "evidence", "code", "param", "occurred_at"):
        assert new[col] == old[col]
    meta = json.loads(new["metadata_json"])
    assert meta["corrected_from"] == {
        "fact_id": ids["HGB"], "version_id": 1, "verification": "machine", "fields": ["value_num"],
    }  # fmt: skip
    assert new["source"] == "fact-review" and new["created_by"] == _agent()

    [prov] = await db.list_provenance("fact", new_id, patient_id=ERIKA_UUID)
    assert (prov.activity, prov.agent_role, prov.target_version) == ("correct", "verifier", 2)
    assert prov.reason.startswith(f"entity=revision fact/{ids['HGB']}/_history/1;")
    assert prov.source_document_id == DOC

    old_audit = await db.list_clinical_record_audit(ids["HGB"])
    assert old_audit[0].action == "delete"
    assert json.loads(old_audit[0].after_json) == {"superseded_by": new_id}
    [new_audit] = await db.list_clinical_record_audit(new_id)
    assert new_audit.action == "create" and new_audit.reason == "correct: misread digit"

    # the corrected version is what readers see
    active = await _all(
        db,
        "SELECT id FROM fact_series_v WHERE patient_id = ? AND code = 'HGB'",
        (ERIKA_UUID,),
    )
    assert active == [{"id": new_id}]


async def test_correct_keeps_fhir_id_and_chains_versions(db, caller):
    ids = await _seed(db)
    # Stable fact ids (#728) make fhir_id unique among ACTIVE rows; mirror that
    # index here so the test holds both before and after #728 merges.
    await db.db.execute("DROP INDEX IF EXISTS uq_cr_fhir_id")
    await db.db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_cr_fhir_id_active ON clinical_records(fhir_id) "
        "WHERE fhir_id IS NOT NULL AND deleted_at IS NULL"
    )
    await db.db.execute(
        "UPDATE clinical_records SET fhir_id = ? WHERE id = ?", ("fact-uuid-b", ids["HGB"])
    )
    await db.db.commit()
    first = json.loads(
        await fr.correct_fact(_Ctx(db), "fact-uuid-b", reason="unit", unit="g/dl", value_num=12.7)
    )
    second = json.loads(
        await fr.correct_fact(_Ctx(db), "fact-uuid-b", reason="date", occurred_at=DAY2)
    )
    assert first["fact"]["fhir_id"] == second["fact"]["fhir_id"] == "fact-uuid-b"
    assert second["fact"]["version_id"] == 3
    assert second["fact"]["occurred_at"] == DAY2
    assert second["fact"]["value_num"] == 12.7 and second["fact"]["unit"] == "g/dl"
    meta = json.loads((await _row(db, second["fact"]["id"]))["metadata_json"])
    assert meta["corrected_from"]["fact_id"] == first["fact"]["id"]
    assert meta["corrected_from"]["verification"] == "corrected"
    assert meta["date_source"] == "corrected"


async def test_correct_recomputes_canonical_value_through_unit_conversion(db, caller):
    ids = await _seed(db)
    out = json.loads(
        await fr.correct_fact(_Ctx(db), ids["HGB"], reason="unit", value_num=12.7, unit="g/dl")
    )
    assert out["fact"]["value_canonical"] == pytest.approx(127.0)  # g/dl x 10 = g/L
    assert out["fact"]["unit_canonical"] == "g/L"
    out = json.loads(
        await fr.correct_fact(_Ctx(db), out["fact"]["id"], reason="unit", unit="furlong")
    )
    assert out["fact"]["value_canonical"] is None  # unknown unit: never under the canonical one


async def test_correct_text_value_and_input_validation(db, caller):
    ids = await _seed(db)
    out = json.loads(
        await fr.correct_fact(_Ctx(db), ids["PLT"], reason="clumped sample", value_text="clumped")
    )
    assert out["fact"]["value_text"] == "clumped" and out["fact"]["value_num"] is None
    assert out["fact"]["value_canonical"] is None

    for kwargs in (
        {"reason": "x"},
        {"reason": "", "value_num": 1.0},
        {"reason": "x", "value_num": 1.0, "value_text": "a"},
        {"reason": "x", "occurred_at": "not-a-date"},
    ):
        err = json.loads(await fr.correct_fact(_Ctx(db), ids["HGB"], **kwargs))
        assert err["error"] == "invalid_input"
    assert (await _row(db, ids["HGB"]))["deleted_at"] is None


async def test_verify_leaves_a_corrected_fact_alone(db, caller):
    ids = await _seed(db)
    out = json.loads(await fr.correct_fact(_Ctx(db), ids["HGB"], reason="r", value_num=125.0))
    new_id = out["fact"]["id"]
    assert json.loads(await fr.verify_fact(_Ctx(db), new_id))["status"] == "corrected"
    assert json.loads(await fr.unverify_fact(_Ctx(db), new_id))["status"] == "not_verified"
    # the superseded version is gone for review
    assert json.loads(await fr.verify_fact(_Ctx(db), ids["HGB"]))["error"] == "not_found"


# ── re-extraction keeps human decisions ──


async def test_reextraction_keeps_verified_and_corrected_facts(db, caller):
    facts = [
        _lab("HGB", 121.0),
        _lab("PLT", 233.0, COUNT_UNIT),
        _lab("WBC", 6.1, COUNT_UNIT),
    ]
    ids = await _seed(db, facts=facts)
    await fr.verify_fact(_Ctx(db), ids["HGB"])
    out = json.loads(await fr.correct_fact(_Ctx(db), ids["PLT"], reason="r", value_num=238.0))
    corrected_id = out["fact"]["id"]

    res = await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=DOC, facts=facts, extracted_by="rule:test2"
    )
    assert res["kept_verified"] == 2  # the verified HGB and the corrected PLT
    assert res["added"] == res["removed"] == len(["WBC"])  # only the machine fact is redone
    rows = await _all(
        db,
        "SELECT id, code, verification, value_num FROM clinical_records "
        "WHERE source_document_id = ? AND deleted_at IS NULL ORDER BY code",
        (DOC,),
    )
    by_code = {r["code"]: r for r in rows}
    assert len(rows) == 3
    assert by_code["HGB"]["id"] == ids["HGB"] and by_code["HGB"]["verification"] == "verified"
    assert by_code["PLT"]["id"] == corrected_id and by_code["PLT"]["value_num"] == 238.0
    assert by_code["WBC"]["verification"] == "machine" and by_code["WBC"]["id"] != ids["WBC"]


# ── isolation, permissions, activity log ──


async def test_foreign_patients_fact_is_not_found(db, caller):
    await _add_bob(db)
    bob_ids = await _seed(db, pid=BOB_UUID, doc=BOB_DOC)
    for call in (
        fr.verify_fact(_Ctx(db), bob_ids["HGB"]),
        fr.unverify_fact(_Ctx(db), bob_ids["HGB"]),
        fr.correct_fact(_Ctx(db), bob_ids["HGB"], reason="r", value_num=1.0),
    ):
        assert json.loads(await call)["error"] == "not_found"
    row = await _row(db, bob_ids["HGB"])
    assert row["verification"] == "machine" and row["deleted_at"] is None
    assert await db.list_provenance("fact", bob_ids["HGB"], patient_id=BOB_UUID) == []


async def test_foreign_patient_slug_is_denied_and_logged(db, caller):
    await _add_bob(db)
    bob_ids = await _seed(db, pid=BOB_UUID, doc=BOB_DOC)
    with pytest.raises(ValueError, match="access denied"):
        await fr.verify_fact(_Ctx(db), bob_ids["HGB"], patient_slug=BOB_SLUG)
    [log] = await _all(db, "SELECT * FROM activity_log WHERE tool_name = 'verify_fact'")
    assert log["status"] == "denied" and log["patient_id"] == ""


async def test_every_call_logs_ids_only(db, caller):
    ids = await _seed(db)
    await fr.verify_fact(_Ctx(db), ids["HGB"], note="private note text")
    await fr.correct_fact(_Ctx(db), ids["PLT"], reason="private reason text", value_num=239.5)
    await fr.unverify_fact(_Ctx(db), ids["HGB"])
    await fr.verify_fact(_Ctx(db), 999999)
    logs = await _all(db, "SELECT * FROM activity_log ORDER BY id")
    assert [x["tool_name"] for x in logs] == [
        "verify_fact", "correct_fact", "unverify_fact", "verify_fact",
    ]  # fmt: skip
    assert [x["status"] for x in logs] == ["ok", "ok", "ok", "not_found"]
    assert all(x["patient_id"] == ERIKA_UUID for x in logs)
    blob = json.dumps(logs)
    for secret in ("private", "239.5", "121", CARER):
        assert secret not in blob
    assert json.loads(logs[1]["output_summary"])["old_fact_id"] == ids["PLT"]


async def test_registered_as_write_tools():
    from oncofiles.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    for name in ("verify_fact", "correct_fact", "unverify_fact"):
        ann = tools[name].annotations
        assert ann.readOnlyHint is False and ann.destructiveHint is False
        assert "patient_slug" in tools[name].parameters["properties"]


async def test_agent_user_without_email_is_the_credential_kind(db):
    tokens = (
        _verified_caller_is_admin.set(False),
        _verified_caller_email.set(""),
        _verified_patient_id.set(ERIKA_UUID),
    )
    try:
        ids = await _seed(db)
        await fr.verify_fact(_Ctx(db), ids["HGB"])
    finally:
        _verified_caller_is_admin.reset(tokens[0])
        _verified_caller_email.reset(tokens[1])
        _verified_patient_id.reset(tokens[2])
    [prov] = await db.list_provenance("fact", ids["HGB"], patient_id=ERIKA_UUID)
    assert prov.agent_user == "mcp-bearer:patient"


async def test_correct_refuses_before_writing_under_the_all_rows_fhir_index(db, caller):
    """Before #728's migration the fhir_id index covers deleted rows too; a
    correction must then fail cleanly, not leave the old version deleted."""
    ids = await _seed(db)
    await db.db.execute("DROP INDEX IF EXISTS uq_cr_fhir_id_active")
    await db.db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_cr_fhir_id ON clinical_records(fhir_id) "
        "WHERE fhir_id IS NOT NULL"
    )
    await db.db.execute(
        "UPDATE clinical_records SET fhir_id = ? WHERE id = ?", ("fact-uuid-c", ids["HGB"])
    )
    await db.db.commit()
    err = json.loads(await fr.correct_fact(_Ctx(db), ids["HGB"], reason="r", value_num=125.0))
    assert err["error"] == "invalid_input"
    row = await _row(db, ids["HGB"])
    assert row["deleted_at"] is None and row["verification"] == "machine"


async def test_acting_user_is_a_keyed_hash_never_the_email(db, caller):
    ids = await _seed(db)
    await fr.verify_fact(_Ctx(db), ids["HGB"])
    await fr.correct_fact(_Ctx(db), ids["PLT"], reason="r", value_num=239.5)
    await fr.unverify_fact(_Ctx(db), ids["HGB"])
    agent = _agent()
    assert agent.startswith("user:") and len(agent) == len("user:") + 64
    for table in ("clinical_records", "clinical_record_audit", "provenance", "activity_log"):
        rows = await _all(db, f"SELECT * FROM {table}")
        assert rows and CARER not in json.dumps(rows), table
    provs = await _all(db, "SELECT DISTINCT agent_user FROM provenance")
    assert provs == [{"agent_user": agent}]
