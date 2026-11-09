"""Clinical facts model (#603, migration 072). Synthetic data only."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.cloud_guard import CloudForbiddenError, local_only
from oncofiles.curated.facts_rules import Vocabulary, parse_bory, parse_okb
from oncofiles.persistent_oauth import (
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID
from tests.test_curated_runner import LABS, FakeDrive

BOB_UUID = "00000000-0000-4000-8000-000000000002"
BOB_SLUG = "bob-test"

BORY_SIDECAR = """---
doc_date: "2026-01-29"
institution: "BoryNemocnica"
doctor: "MUDr. Jana Testova"
ocr_engine: "qwen3.8-27b-mlx"
handwritten: false
summary: "Krvny obraz a biochemia."
---

<!-- page 1 -->
Pacient: Jana NOVAKOVA   RČ: 0000000000
Odber: 29.01.2026 12:24,
Hematológia,
Krvný obraz: Leukocyty [WBC] ...10.89 ▲  [10^9/l] {3.80 - 10.70 } ; \
Hemoglobín [HGB] ...105.0 ▼  [g/l] {120.0 - 155.0 } ; \
Trombocyty [PLT] ...2[?]5  [10^9/l] {150 - 400 }
Sérum: S-Glukóza ...8.40 ▲  [mmol/l] {3.30 - 5.59 } ; \
S-Kreatinín ...39.00 ▼  [µmol/l] {42.00 - 79.00 }
Moč: U- pH ...5.5 {5.5 - 6.5 } ; U- Krv ...NEGAT [arb]
Strana 1 / 1
"""

OKB_TEXT = """Odobrané: 13.03.2026 08:52    Prijaté: 13.03.2026 09:07
Test                                Hodnota    Jednotky    Ref. medze        Hodnotenie    SOP
markery v sére
CEA v sére                          732.9      ng/ml                                          /
CA 19-9 v sére                      22294.3    U/ml      0.0 - 30.9        ****            /
eGF CKD-EPI                         1.92       ml/s      > 1.49            v norme         /
Kreatínin v sére                    42.2       µmol/l    44.0 - 71.0       *               /
"""

FILED = "20260129-JanaNovakova-BoryNemocnica-Labs-Testova-BoryRDG-KrvnyObraz.pdf"


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


# ── migration ──


async def test_migration_seeds_codes_and_views(db):
    rows = await db.fact_code_vocabulary()
    codes = {r["code"] for r in rows}
    assert {"PLT", "HGB", "WBC", "CEA", "CA19_9", "CREA", "KRAS"} <= codes
    assert len(rows) >= 60
    for view in ("fact_series_v", "latest_fact_v", "timeline_v"):
        async with db.db.execute(f"SELECT COUNT(*) FROM {view}") as cur:
            assert (await cur.fetchone())[0] == 0


# ── rule parsers ──


async def test_bory_parser(vocab):
    facts = parse_bory([BORY_SIDECAR.split("<!-- page 1 -->")[1]], vocab)
    by = {f["code"] or f["label"]: f for f in facts}
    wbc = by["WBC"]
    assert wbc["value_num"] == 10.89 and wbc["flag"] == "H" and wbc["unit"] == "10^9/l"
    assert wbc["ref_low"] == 3.8 and wbc["ref_high"] == 10.7
    assert wbc["occurred_at"] == "2026-01-29T12:24"
    assert "10.89" in wbc["evidence"] and wbc["verification"] == "machine"
    assert by["HGB"]["flag"] == "L" and by["HGB"]["value_canonical"] == 105.0
    assert by["PLT"]["verification"] == "unverified"  # "[?]" in the evidence
    assert by["GLU"]["value_num"] == 8.4 and by["CREA"]["flag"] == "L"
    assert by["U_ERY"]["value_text"] == "NEGAT" and by["U_ERY"]["value_num"] is None


async def test_okb_parser(vocab):
    facts = parse_okb([OKB_TEXT], vocab)
    by = {f["code"]: f for f in facts}
    assert by["CEA"]["value_num"] == 732.9 and by["CEA"]["unit_canonical"] == "µg/L"
    assert by["CEA"]["value_canonical"] == 732.9  # ng/ml == µg/L
    assert by["CA19_9"]["flag"] == "H" and by["CA19_9"]["ref_high"] == 30.9
    assert by["EGFR"]["ref_low"] == 1.49 and by["EGFR"]["ref_text"] == "> 1.49"
    assert by["CREA"]["flag"] == "L"
    assert all(f["occurred_at"] == "2026-03-13T08:52" for f in facts)
    assert "markery" not in {f["label"] for f in facts}


# ── index → entities and facts ──


@pytest.fixture
def lab_env():
    d = FakeDrive()
    src = d.add(FILED, d.folder[LABS], b"%PDF", modified="2026-11-02T00:00:00Z")
    side = d.add(FILED + ".md", d.folder[LABS], BORY_SIDECAR.encode(), modified="2026-11-02T1")
    gdrive = MagicMock()
    with (
        patch(
            "oncofiles.server._create_patient_clients",
            AsyncMock(return_value=(gdrive, None, None, d.root)),
        ),
        patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: d),
    ):
        yield d, src, side


class _Ctx:
    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


async def test_index_builds_entities_facts_and_timeline(db, lab_env):
    _, src, _ = lab_env
    stats = await tool.index_patient(db, ERIKA_UUID)
    assert stats["facts"] >= 7 and stats["errors"] == 0
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    row = await db.document_row(patient_id=ERIKA_UUID, document_id=doc.id)
    assert row["organization"] == "BoryNemocnica" and row["department"] == "BoryRDG"
    assert row["practitioner"] == "MUDr. Jana Testova"
    assert row["source_url"].endswith(f"/d/{src}/view")
    assert row["title"].startswith("Laboratórne výsledky · BoryNemocnica")
    assert row["encounter_id"] is not None

    ctx = _Ctx(db)
    series = json.loads(await cf.fact_series(ctx, codes=["wbc", "PLT"]))["series"]
    assert series["WBC"]["values"][0]["source"]["url"] == row["source_url"]
    assert series["WBC"]["stats"]["max"] == 10.89
    assert series["PLT"]["values"] == [] and len(series["PLT"]["unverified"]) == 1
    assert series["PLT"]["stats"] is None  # an unverified value never becomes a statistic

    tl = json.loads(await cf.timeline(ctx))["rows"]
    kinds = {r["kind"] for r in tl}
    assert {"encounter", "document", "lab"} <= kinds  # abnormal labs are on the timeline
    overview = json.loads(await cf.patient_overview(ctx))
    assert overview["documents_by_type"]["labs"]["n"] == 1
    assert overview["unverified_values"] == 1
    people = json.loads(await cf.people_and_places(ctx))
    assert people["practitioners"][0]["surname_token"] == "Testova"
    detail = json.loads(await cf.document_detail(ctx, document_id=doc.id))
    assert len(detail["facts"]) == stats["facts"]
    enc = json.loads(await cf.encounter_detail(ctx, encounter_id=row["encounter_id"]))
    assert enc["documents"][0]["id"] == doc.id


async def test_reextraction_soft_deletes_with_audit_and_keeps_verified(db, lab_env):
    d, src, side = lab_env
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    await db.db.execute(
        "UPDATE clinical_records SET verification = 'verified' WHERE code = 'HGB' "
        "AND source_document_id = ?",
        (doc.id,),
    )
    await db.db.commit()
    d.items[side]["content"] = BORY_SIDECAR.replace("10.89", "10.95").encode()
    d.items[side]["modified"] = "2026-11-03T00:00:00Z"
    await tool.index_patient(db, ERIKA_UUID)
    async with db.db.execute(
        "SELECT code, value_num, verification, deleted_at FROM clinical_records "
        "WHERE source_document_id = ? AND code IN ('WBC', 'HGB')",
        (doc.id,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    wbc = [r for r in rows if r["code"] == "WBC"]
    assert {r["value_num"] for r in wbc if r["deleted_at"] is None} == {10.95}
    assert any(r["deleted_at"] for r in wbc)  # soft-deleted, still there
    hgb = [r for r in rows if r["code"] == "HGB"]
    assert len(hgb) == 1 and hgb[0]["verification"] == "verified"  # untouched, not duplicated
    async with db.db.execute(
        "SELECT COUNT(*) FROM clinical_record_audit WHERE action = 'delete'"
    ) as cur:
        assert (await cur.fetchone())[0] >= 1


async def test_links_never_cross_patients(db, lab_env):
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test"),
    )
    await db.db.commit()
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(lab_env[1], patient_id=ERIKA_UUID)
    with pytest.raises(ValueError, match="same patient"):
        await db.add_link(
            patient_id=BOB_UUID, from_type="document", from_id=doc.id,
            to_type="document", to_id=doc.id, relation="mentions",
        )  # fmt: skip


# ── access: every tool is patient-scoped ──


@pytest.fixture
def non_admin_bound_to_erika():
    tokens = (
        _verified_caller_is_admin.set(False),
        _verified_caller_email.set(""),
        _verified_patient_id.set(ERIKA_UUID),
    )
    yield
    _verified_caller_is_admin.reset(tokens[0])
    _verified_caller_email.reset(tokens[1])
    _verified_patient_id.reset(tokens[2])


@pytest.mark.parametrize(
    "call",
    [
        lambda ctx: cf.patient_overview(ctx, patient_slug=BOB_SLUG),
        lambda ctx: cf.timeline(ctx, patient_slug=BOB_SLUG),
        lambda ctx: cf.fact_catalog(ctx, patient_slug=BOB_SLUG),
        lambda ctx: cf.fact_series(ctx, codes=["PLT"], patient_slug=BOB_SLUG),
        lambda ctx: cf.document_detail(ctx, document_id=1, patient_slug=BOB_SLUG),
        lambda ctx: cf.encounter_detail(ctx, encounter_id=1, patient_slug=BOB_SLUG),
        lambda ctx: cf.people_and_places(ctx, patient_slug=BOB_SLUG),
    ],
)
async def test_every_fact_tool_denies_a_foreign_patient(db, non_admin_bound_to_erika, call):
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Test", "bob@example.com"),
    )
    await db.db.commit()
    with pytest.raises(ValueError, match="access denied"):
        await call(_Ctx(db))


async def test_document_of_another_patient_is_not_found(db, lab_env):
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(lab_env[1], patient_id=ERIKA_UUID)
    assert await db.document_row(patient_id=BOB_UUID, document_id=doc.id) is None
    assert await db.facts_of_document(patient_id=BOB_UUID, document_id=doc.id) == []


def test_fact_tools_are_annotated_read_only():
    mcp = MagicMock()
    cf.register(mcp)
    assert all(c.kwargs["annotations"]["readOnlyHint"] for c in mcp.tool.call_args_list)
    assert mcp.tool.call_count == 7


# ── local only: the index and extraction never get a cloud client ──


async def test_index_job_cannot_reach_the_anthropic_api(db, lab_env):
    from oncofiles import enhance
    from oncofiles.files_api import FilesClient

    seen = []

    async def extract_and_try_cloud(*args, **kwargs):
        for fn in (
            enhance._get_client,
            lambda: FilesClient(api_key="test")._guarded(),
        ):
            try:
                fn()
            except CloudForbiddenError:
                seen.append("refused")
        try:  # also from a worker thread (asyncio.to_thread copies the context)
            await asyncio.to_thread(enhance._get_client)
        except CloudForbiddenError:
            seen.append("refused-thread")
        return 0

    with patch.object(tool, "_extract_facts", extract_and_try_cloud):
        stats = await tool.index_patient(db, ERIKA_UUID)
    assert stats["created"] == 1, stats
    assert seen == ["refused", "refused", "refused-thread"]


def test_local_only_zone_is_scoped():
    from oncofiles.cloud_guard import check_cloud_allowed

    with local_only("test job"), pytest.raises(CloudForbiddenError, match="test job"):
        check_cloud_allowed("x")
    check_cloud_allowed("x")  # outside the zone: allowed (user-initiated paths)


async def test_rows_from_an_older_index_version_are_reprocessed_once(db, lab_env):
    from oncofiles.curated import indexer

    with (
        patch.object(indexer, "INDEX_VERSION", 1),
        patch.object(tool, "_extract_facts", AsyncMock(return_value=0)),
    ):
        await tool.index_patient(db, ERIKA_UUID)  # old code: no facts
    upgraded = await tool.index_patient(db, ERIKA_UUID)  # new version: re-processed
    assert upgraded["unchanged"] == 0 and upgraded["facts"] >= 7
    again = await tool.index_patient(db, ERIKA_UUID)
    assert again["unchanged"] == 1 and again["facts"] == 0
