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
doc_date: "2027-04-14"
institution: "BoryNemocnica"
doctor: "MUDr. Jana Testova"
ocr_engine: "qwen3.8-27b-mlx"
handwritten: false
summary: "Krvny obraz a biochemia."
---

<!-- page 1 -->
Pacient: Jana NOVAKOVA   RČ: 0000000000
Odber: 14.04.2027 09:10,
Hematológia,
Krvný obraz: Leukocyty [WBC] ...11.42 ▲  [10^9/l] {3.80 - 10.70 } ; \
Hemoglobín [HGB] ...101.0 ▼  [g/l] {120.0 - 155.0 } ; \
Trombocyty [PLT] ...2[?]5  [10^9/l] {150 - 400 }
Sérum: S-Glukóza ...7.10 ▲  [mmol/l] {3.30 - 5.59 } ; \
S-Kreatinín ...37.00 ▼  [µmol/l] {42.00 - 79.00 }
Moč: U- pH ...5.5 {5.5 - 6.5 } ; U- Krv ...NEGAT [arb]
Strana 1 / 1
"""

OKB_TEXT = """Odobrané: 21.05.2027 08:05    Prijaté: 21.05.2027 08:30
Test                                Hodnota    Jednotky    Ref. medze        Hodnotenie    SOP
markery v sére
CEA v sére                          41.7       ng/ml                                          /
CA 19-9 v sére                      812.4      U/ml      0.0 - 30.9        ****            /
eGF CKD-EPI                         1.64       ml/s      > 1.49            v norme         /
Kreatínin v sére                    40.8       µmol/l    44.0 - 71.0       *               /
"""

FILED = "20270414-JanaNovakova-BoryNemocnica-Labs-Testova-BoryRDG-KrvnyObraz.pdf"


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
    assert wbc["value_num"] == 11.42 and wbc["flag"] == "H" and wbc["unit"] == "10^9/l"
    assert wbc["ref_low"] == 3.8 and wbc["ref_high"] == 10.7
    assert wbc["occurred_at"] == "2027-04-14T09:10"
    assert "11.42" in wbc["evidence"] and wbc["verification"] == "machine"
    assert by["HGB"]["flag"] == "L" and by["HGB"]["value_canonical"] == 101.0
    assert by["PLT"]["verification"] == "unverified"  # "[?]" in the evidence
    assert by["GLU"]["value_num"] == 7.1 and by["CREA"]["flag"] == "L"
    assert by["U_ERY"]["value_text"] == "NEGAT" and by["U_ERY"]["value_num"] is None


async def test_okb_parser(vocab):
    facts = parse_okb([OKB_TEXT], vocab)
    by = {f["code"]: f for f in facts}
    assert by["CEA"]["value_num"] == 41.7 and by["CEA"]["unit_canonical"] == "µg/L"
    assert by["CEA"]["value_canonical"] == 41.7  # ng/ml == µg/L
    assert by["CA19_9"]["flag"] == "H" and by["CA19_9"]["ref_high"] == 30.9
    assert by["EGFR"]["ref_low"] == 1.49 and by["EGFR"]["ref_text"] == "> 1.49"
    assert by["CREA"]["flag"] == "L"
    assert all(f["occurred_at"] == "2027-05-21T08:05" for f in facts)
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
    assert series["WBC"]["stats"]["max"] == 11.42
    assert series["PLT"]["values"] == [] and len(series["PLT"]["unverified"]) == 1
    assert series["PLT"]["stats"] is None  # an unverified value never becomes a statistic
    assert series["PLT"]["latest_any"]["verification"] == "unverified"  # newest, labelled
    assert series["WBC"]["latest_any"]["value"] == 11.42

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
    d.items[side]["content"] = BORY_SIDECAR.replace("11.42", "11.55").encode()
    d.items[side]["modified"] = "2026-11-03T00:00:00Z"
    await tool.index_patient(db, ERIKA_UUID)
    async with db.db.execute(
        "SELECT code, value_num, verification, deleted_at FROM clinical_records "
        "WHERE source_document_id = ? AND code IN ('WBC', 'HGB')",
        (doc.id,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    wbc = [r for r in rows if r["code"] == "WBC"]
    assert {r["value_num"] for r in wbc if r["deleted_at"] is None} == {11.55}
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
def non_admin_bound_to_jana():
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
async def test_every_fact_tool_denies_a_foreign_patient(db, non_admin_bound_to_jana, call):
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


# ── parser quality (infra review of the first production run) ──

GARBAGE = """Odber: 14.04.2027 09:10,
......
Tel.(predvoľba): ....
medCOMP 2.0.8 build 2... 12 [arb]
**Typ podla README par.3 je Admin ...1 [x]
NOTE (agent): DUPLICATE (identical sha256 ...5 [x]
Identical content to consultation/ ...2 [x]
doctor is X, not 'Y' ...3 [x]
Výsledky uvoľnil(a): Meno ...1 [x]
'po CT simulacia ...2 [x]
"""

OKB_WITH_DOSING = """Odobrané: 21.05.2027 08:05
Test                                Hodnota    Jednotky    Ref. medze
CEA v sére                          41.7       ng/ml
K 4.                                4          -
HER2 gene amplification probe. Vysledok:   2   x
5-FU                                2400       mg/m2
Oxaliplatina                        85         mg/m2
Výsledky uvoľnil(a): Meno           1          x
"""

REAL_VARIANTS = """Odber: 14.04.2027 09:10,
Krvný obraz: abs. počet NEUT ...4.10 [10^9/l] {2.0 - 7.0 } ; Bazofilý abs.počet ...0.03 [10^9/l]
Sérum: ; S-Chloridy ...101 [mmol/l] {98 - 107 }
Moč: U- Ketóny ...NEGAT [arb] ; U-Bielkoviny ...1+ [arb]
"""


async def test_parser_rejects_notes_placeholders_and_staff_lines(vocab):
    assert parse_bory([GARBAGE], vocab) == []


async def test_okb_parser_keeps_labs_and_moves_dosing_to_medication(vocab):
    facts = parse_okb([OKB_WITH_DOSING], vocab)
    labs = [f for f in facts if f["category"] != "medication"]
    meds = [f for f in facts if f["category"] == "medication"]
    assert [f["code"] for f in labs] == ["CEA"]
    assert {f["label"] for f in meds} == {"5-FU", "Oxaliplatina"}


async def test_spelling_variants_map_to_codes(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    by = {f["code"]: f for f in parse_bory([REAL_VARIANTS], vocab)}
    assert by["NEUT"]["value_num"] == 4.1
    assert "BASO" in by and by["CL"]["value_num"] == 101.0
    assert by["U_KET"]["value_text"] == "NEGAT" and by["U_PROT"]["value_text"] == "1+"


async def test_migration_073_adds_codes(db):
    codes = {r["code"] for r in await db.fact_code_vocabulary()}
    assert {"NLR", "SII", "MPV", "PDW", "U_KET", "USED_BACT"} <= codes


async def test_document_whose_new_extraction_is_empty_loses_old_junk(db, lab_env):
    """#633 leftover: old unverified/machine rows of a document that now yields
    no facts must be soft-deleted too (both are machine output)."""
    d, src, side = lab_env
    await tool.index_patient(db, ERIKA_UUID)
    doc = await db.get_document_by_gdrive_id(src, patient_id=ERIKA_UUID)
    await db.db.execute(
        "UPDATE clinical_records SET verification = 'unverified', param = 'NOTE (agent): x' "
        "WHERE source_document_id = ? AND code = 'WBC'",
        (doc.id,),
    )
    await db.db.commit()
    d.items[side]["content"] = b"---\ndoc_date: 2027-04-14\n---\n\nno lab values here\n"
    d.items[side]["modified"] = "2026-11-04T00:00:00Z"
    await tool.index_patient(db, ERIKA_UUID)
    async with db.db.execute(
        "SELECT COUNT(*) FROM clinical_records WHERE source_document_id = ? AND deleted_at IS NULL",
        (doc.id,),
    ) as cur:
        assert (await cur.fetchone())[0] == 0


async def test_percent_spacing_variants_match(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    assert vocab.match("Neutrofilie%")["code"] == "NEUT_PCT"
    assert vocab.match("Neutrofilie %")["code"] == "NEUT_PCT"


MD_TABLE = """## Výsledky laboratórnych vyšetrení — krvný obraz
Odobrané: 03.03.2027 07:15 · Prijaté: 03.03.2027 07:15
| Test | Hodnota | Jednotky | Ref. medze |
|---|---|---|---|
| WBC | 4.21 | G/l | 3.5 - 10 |
| HGB | 114 (znížené) | g/l | 120 - 156 |
| Neutrofily | 51.2 | % | 40 - 78 |
| abs.počet NEUT | 2.16 | | |
| Monocyty | 12.6 (zvýšené) | % | 1 - 10 |
| eGFR | 1.58 | ml/s | > 1.49 |
Výsledky uvoľnil(a): 1) Meno Priezvisko
"""


async def test_markdown_table_parser_and_percent_codes(db):
    from oncofiles.curated.facts_rules import extract_lab_facts

    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    facts, extractor = extract_lab_facts([MD_TABLE], vocab)
    assert extractor == "rule:mdtable-v1"
    by = {f["code"]: f for f in facts}
    assert by["WBC"]["value_num"] == 4.21 and by["WBC"]["occurred_at"] == "2027-03-03T07:15"
    assert by["HGB"]["flag"] == "L"  # "(znížené)" and below range
    assert by["NEUT_PCT"]["value_num"] == 51.2  # % never filed under the absolute count
    assert by["NEUT"]["value_num"] == 2.16
    assert by["MONO_PCT"]["flag"] == "H"
    assert by["EGFR"]["ref_low"] == 1.49
    assert all("uvoľnil" not in f["evidence"] for f in facts)


# ── round 2 (#635): inline results, entities, dedupe ──

INLINE_REPORT = """## Lekárska správa
Laboratórne výsledky z 12.06.2027:
CEA S 38,2 ug/l; CA 19-9 655,1; KREA S 61,0 umol/l; KM S 310,4; NA v sére 139,0
Pacientka je v dobrom stave, váha 62 kg; TK 120/80; bez ťažkostí.
"""


async def test_inline_results_rules(db):
    from oncofiles.curated.facts_rules import parse_inline_results

    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    facts = parse_inline_results([INLINE_REPORT], vocab, report_date="2027-06-20")
    by = {f["code"]: f for f in facts}
    assert set(by) >= {"CEA", "CA19_9", "CREA", "URIC", "NA"}
    assert all(f["occurred_at"] == "2027-06-12" for f in facts)  # printed date, not report
    assert by["CEA"]["verification"] == "machine"  # printed unit + printed date
    # #659: a missing printed unit is not a reason to distrust the value
    assert by["CA19_9"]["verification"] == "machine"
    assert by["CA19_9"]["metadata"]["unit_source"] == "implied"
    assert "WEIGHT" not in by  # the prose line is not results (fewer than 2 coded items)


async def test_inline_results_without_printed_date_use_report_date_unverified(db):
    from oncofiles.curated.facts_rules import parse_inline_results

    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    text = "CEA S 38,2 ug/l; KREA S 61,0 umol/l; NA v sére 139,0 mmol/l\n"
    facts = parse_inline_results([text], vocab, report_date="2027-06-20")
    assert facts and all(f["occurred_at"] == "2027-06-20" for f in facts)
    assert all(f["verification"] == "unverified" for f in facts)
    assert all(f["metadata"]["date_source"] == "report" for f in facts)


def test_practitioner_and_department_normalisation():
    assert tool._surname("MUDrMgrJanaTestova") == "Testova"
    assert tool._surname("Testova") == "Testova"
    assert tool._person("prim. MUDr. Jana Testová (primár OKB — schvaľuje výsledky)") == (
        "prim. MUDr. Jana Testová"
    )
    assert tool._dept_ok("AmbulanciaOrdinacneHodiny111a") is None
    assert tool._dept_ok("KlinikaOnkohematologie") == "KlinikaOnkohematologie"


def test_series_dedupe_drops_duplicate_sheets_and_report_quotes():
    base = {"code": "CEA", "value_text": None, "metadata_json": "{}", "page_number": 1}
    report = '{"date_source": "report"}'
    rows = [
        {**base, "id": 501, "effective_at": "2027-03-03", "value_canonical": 44.4,
         "verification": "machine", "source_document_id": 10},
        {**base, "id": 502, "effective_at": "2027-03-03", "value_canonical": 44.4,
         "verification": "unverified", "source_document_id": 11},  # duplicated sheet
        {**base, "id": 503, "effective_at": "2027-03-20", "value_canonical": 44.4,
         "verification": "unverified", "source_document_id": 12,
         "metadata_json": report},  # quoted in a later report, together with CA 19-9
        {**base, "id": 504, "effective_at": "2027-06-12", "value_canonical": 38.2,
         "verification": "machine", "source_document_id": 13},
        {**base, "id": 577, "code": "CA19_9", "effective_at": "2027-03-03",
         "value_canonical": 777.7, "verification": "machine", "source_document_id": 10},
        {**base, "id": 578, "code": "CA19_9", "effective_at": "2027-03-20",
         "value_canonical": 777.7, "verification": "unverified", "source_document_id": 12,
         "metadata_json": report},
    ]  # fmt: skip
    kept, dropped = cf._dedupe(rows)
    assert sorted(r["id"] for r in kept) == [501, 504, 577]
    got = sorted((d["removed"]["fact_id"], d["reason"], d["kept"]["fact_id"]) for d in dropped)
    assert got == [(502, "exact", 501), (503, "quote", 501), (578, "quote", 577)]


def test_series_dedupe_keeps_a_single_coincidental_equal_value():
    """HGB 124 on two days is two measurements: one equal number is not a quote."""
    base = {"code": "HGB", "value_text": None, "page_number": 1}
    rows = [
        {**base, "id": 1, "effective_at": "2027-07-09", "value_canonical": 124.0,
         "verification": "unverified", "source_document_id": 20, "metadata_json": "{}"},
        {**base, "id": 2, "effective_at": "2027-08-02", "value_canonical": 124.0,
         "verification": "unverified", "source_document_id": 21,
         "metadata_json": '{"date_source": "report"}'},
        {**base, "id": 3, "code": "WBC", "effective_at": "2027-08-02", "value_canonical": 3.9,
         "verification": "unverified", "source_document_id": 21,
         "metadata_json": '{"date_source": "report"}'},
    ]  # fmt: skip
    kept, dropped = cf._dedupe(rows)
    assert dropped == [] and len(kept) == 3


def test_series_dedupe_range_hides_only_the_exact_same_value():
    """Guardrail (#635 v10): a value dated inside a printed range is hidden only
    when another document has the same code, value and unit inside the window;
    a different value is never hidden, and both documents are reported."""
    base = {"code": "WBC", "value_text": None, "page_number": 1, "unit_canonical": "10^9/L"}
    rng = (
        '{"date_source": "printed_range", "date_range_start": "2027-10-11", '
        '"date_range_end": "2027-10-13"}'
    )
    rows = [
        {**base, "id": 601, "effective_at": "2027-10-11", "value_canonical": 6.13,
         "verification": "machine", "source_document_id": 30, "metadata_json": "{}"},
        {**base, "id": 602, "effective_at": "2027-10-13", "value_canonical": 6.13,
         "verification": "machine", "source_document_id": 31, "metadata_json": rng},
        {**base, "id": 603, "code": "PLT", "effective_at": "2027-10-13", "value_canonical": 317.0,
         "verification": "machine", "source_document_id": 31, "metadata_json": rng},
        {**base, "id": 604, "code": "PLT", "effective_at": "2027-10-11", "value_canonical": 309.0,
         "verification": "machine", "source_document_id": 30, "metadata_json": "{}"},
        {**base, "id": 605, "effective_at": "2027-10-19", "value_canonical": 6.13,
         "verification": "machine", "source_document_id": 32, "metadata_json": "{}"},
    ]  # fmt: skip
    kept, dropped = cf._dedupe(rows)
    assert sorted(r["id"] for r in kept) == [601, 603, 604, 605]  # PLT differs; 605 outside
    assert dropped == [{
        "code": "WBC", "at": "2027-10-13", "value": 6.13, "reason": "range",
        "removed": {"fact_id": 602, "document_id": 31},
        "kept": {"fact_id": 601, "document_id": 30, "at": "2027-10-11"},
    }]  # fmt: skip


def _series_row(fid, doc, day, value, window=None):
    meta = {}
    if window:
        meta = {"date_source": "printed_range", "date_precision": "range",
                "date_range_start": window[0], "date_range_end": window[1]}  # fmt: skip
    return {"id": fid, "code": "HGB", "value_text": None, "page_number": 1,
            "unit_canonical": "g/L", "effective_at": day, "value_canonical": value,
            "verification": "machine", "source_document_id": doc,
            "metadata_json": json.dumps(meta)}  # fmt: skip


_RNG = ("2027-11-02", "2027-11-09")  # a printed range; its values sit at the end (#726)
_END = _RNG[1]
_DOC_A, _DOC_B, _DOC_C = 70, 71, 72


@pytest.mark.parametrize("reverse", [False, True])
def test_series_dedupe_range_end_day_prefers_the_exact_day(reverse):
    """#726: a range value now shares its end day with a value printed for that
    day. The exact one is kept and the reason is "range" in any input order."""
    rows = [_series_row(1, _DOC_A, _END, 131.0), _series_row(2, _DOC_B, _END, 131.0, _RNG)]
    kept, dropped = cf._dedupe(rows[::-1] if reverse else rows)
    assert [r["id"] for r in kept] == [1]
    assert [(d["removed"]["fact_id"], d["reason"], d["kept"]["fact_id"]) for d in dropped] == [
        (2, "range", 1)
    ]


@pytest.mark.parametrize("reverse", [False, True])
def test_series_dedupe_same_document_keeps_the_exact_day(reverse):
    """One report quoting the same value under its range and on the end day:
    both are stored (the parser keeps them apart), the exact one is shown."""
    rows = [_series_row(1, _DOC_A, _END, 131.0), _series_row(2, _DOC_A, _END, 131.0, _RNG)]
    kept, dropped = cf._dedupe(rows[::-1] if reverse else rows)
    assert [r["id"] for r in kept] == [1]
    assert [(d["removed"]["fact_id"], d["reason"]) for d in dropped] == [(2, "exact")]


def test_series_dedupe_two_documents_quoting_one_range_keep_one():
    """Two reports quoting the same range and value hid each other before
    #726 (each matched the other); now the first is kept, the second hidden."""
    rows = [_series_row(1, _DOC_A, _END, 131.0, _RNG), _series_row(2, _DOC_B, _END, 131.0, _RNG)]
    kept, dropped = cf._dedupe(rows)
    assert [r["id"] for r in kept] == [1]
    assert [(d["removed"]["fact_id"], d["reason"], d["kept"]["fact_id"]) for d in dropped] == [
        (2, "range", 1)
    ]
    # a value inside the window from a third document still wins over both
    exact = _series_row(3, _DOC_C, "2027-11-05", 131.0)
    kept, dropped = cf._dedupe([*rows, exact])
    assert [r["id"] for r in kept] == [3] and {d["reason"] for d in dropped} == {"range"}


async def test_migration_075_codes_and_encounter_sources(db, lab_env):
    codes = {r["code"] for r in await db.fact_code_vocabulary()}
    assert {"FE", "TRF", "TSAT", "TIBC", "FERR", "VITD"} <= codes
    await tool.index_patient(db, ERIKA_UUID)
    rows = await db.timeline_rows(patient_id=ERIKA_UUID, kinds=["encounter"])
    assert rows and all(r["source_document_id"] is not None for r in rows)


async def test_alias_normalisation_serum_suffix_and_page(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    assert vocab.match("KREA S")["code"] == "CREA"
    assert vocab.match("Celkové bielkoviny v sére (str. 2)")["code"] == "TP"
    assert vocab.match("Osmolalita séra výpočtom")["code"] == "OSMO"


async def test_shared_organizations_never_take_text_from_a_patient_readme(db, lab_env):
    """organizations are shared across patients: no README-derived names (#637 review)."""
    await tool.index_patient(db, ERIKA_UUID)
    async with db.db.execute("SELECT code, name FROM organizations") as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows and all(r["name"] is None for r in rows)


async def test_inline_differential_percent_vs_absolute_and_plausibility(db):
    """#637 review: plain differential labels are %, 'abs.počet' is the count;
    implausible values never get a code; a line quoted twice counts once."""
    from oncofiles.curated.facts_rules import parse_inline_results

    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    line = (
        "Lab 05.06.2027: WBC 6,10; Neutrofily 61,2; abs.počet NEUT 3,73; "
        "Lymfocyty 24,8; abs.počet LYMPH 1,51; PLT 4870"
    )
    facts = parse_inline_results([line + "\n" + line], vocab, report_date="2027-06-09")
    by = {f["code"]: f for f in facts}
    assert by["NEUT_PCT"]["value_num"] == 61.2 and by["NEUT"]["value_num"] == 3.73
    assert by["LYMPH_PCT"]["value_num"] == 24.8 and by["LYMPH"]["value_num"] == 1.51
    assert "PLT" not in by  # 4870 ×10^9/L is implausible: never stored under PLT
    assert sum(1 for f in facts if f["code"] == "WBC") == 1  # duplicate line counted once


# ── round 3 (#635 R3 retest): comma lists, "(abs X)", text references ──


async def test_inline_comma_lists_arrows_and_abs_counts(vocab):
    """Items after "krv EDTA:" are comma-separated; arrows are flags;
    "Neutrofily 58,1 (abs 3,02)" is the percentage AND the absolute count."""
    from oncofiles.curated.facts_rules import parse_inline_results

    text = (
        "Laboratórne 14.08.2027 (sérum): CEA 12,4; CA 19-9 77,0; kreatinín 58; "
        "krv EDTA: WBC 2,95↓, HGB 121, PLT 233, NEUT abs. 0,84↓.\n"
        "Lab 28.08.2027 (krv EDTA): WBC 5,40; HGB 125; Neutrofily 58,1 (abs 3,02); "
        "Lymfocyty 30,2; PLT 241.\n"
    )
    facts = parse_inline_results([text], vocab, report_date="2027-09-01")
    got = {(f["code"], f["occurred_at"]): f for f in facts}
    assert got[("WBC", "2027-08-14")]["value_num"] == 2.95
    assert got[("WBC", "2027-08-14")]["flag"] == "L"  # "↓" printed next to the value
    assert got[("PLT", "2027-08-14")]["value_num"] == 233
    assert got[("NEUT", "2027-08-14")]["value_num"] == 0.84  # "NEUT abs." is the count
    assert got[("NEUT_PCT", "2027-08-28")]["value_num"] == 58.1
    assert got[("NEUT", "2027-08-28")]["value_num"] == 3.02
    assert got[("PLT", "2027-08-28")]["value_num"] == 241  # trailing "." stripped
    assert all(f["verification"] == "machine" for f in facts)  # dated; units implied (#659)


async def test_inline_date_range_is_never_a_printed_day(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    line = "Laboratórne 3.–4.09.2027: CEA 9,1 ug/l; KREA S 60,0 umol/l; NA v sére 140,0 mmol/l"
    facts = parse_inline_results([line], vocab, report_date="2027-09-10")
    assert facts and all(f["metadata"]["date_source"] == "printed_range" for f in facts)
    # #659: a printed range is a date; #726: the fact sits at the range END
    assert all(f["verification"] == "machine" for f in facts)
    assert {f["occurred_at"] for f in facts} == {"2027-09-04"}
    assert all(f["metadata"]["date_range_start"] == "2027-09-03" for f in facts)
    assert all(f["metadata"]["date_range_end"] == "2027-09-04" for f in facts)
    assert all(f["metadata"]["date_precision"] == "range" for f in facts)


async def test_inline_prose_with_parentheses_is_not_results(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    line = "Kontrola: WBC 9,90 (zvýšené, pooperačne), HGB 101 (anémia), PLT 455 (trombocytóza)"
    assert parse_inline_results([line], vocab, report_date="2027-09-10") == []


async def test_text_reference_under_okb_row_and_in_markdown_table(vocab):
    from oncofiles.curated.facts_rules import parse_markdown_table

    okb = OKB_TEXT.replace(
        "/\nCA 19-9",
        "/\n    refer. medze: nefajčiar: < 3,0 ng/ml, fajčiar < 6 ng/ml\nCA 19-9",
        1,
    )
    cea = {f["code"]: f for f in parse_okb([okb], vocab)}["CEA"]
    assert cea["ref_high"] == 3.0 and cea["flag"] == "H"  # strictest bound
    assert "fajčiar" in cea["ref_text"]

    md = (
        "Odobrané: 07.09.2027 07:40\n| Test | Hodnota | Jednotky | Ref. medze | |\n"
        "|---|---|---|---|---|\n| CEA v sére | 2.2 | ng/ml | nefajčiar <3,0; fajčiar <6 | |\n"
        "| CA 19-9 v sére | 41.5 | U/ml | 0.0 - 30.9 | |\n"
    )
    by = {f["code"]: f for f in parse_markdown_table([md], vocab)}
    assert by["CEA"]["ref_high"] == 3.0 and by["CEA"]["flag"] is None
    assert by["CA19_9"]["flag"] == "H"


async def test_inline_vital_signs_and_continuation_lines(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    vitals = "Objektívne: TK 125/80, P 74, SpO2 97%, TT 36,7. Lab 2.9.2027: K 4,1, NA 139, CL 101"
    assert parse_inline_results([vitals], vocab, report_date="2027-09-10") == []
    stats = "In the trial cohort, OS 21.3 mo; HR 0.71; 95% CI 0.55-0.92; P .009; K 4.0; NA 139"
    assert parse_inline_results([stats], vocab, report_date="2027-09-10") == []
    # a wrapped line that starts with the previous line's last value keeps its items
    cont = "Lab 9.9.2027: WBC 5,1; HGB 128; PLT 210; MONO 0,6\n0,41; SII 512,3; Ne/Ly ratio 2,10"
    codes = {f["code"] for f in parse_inline_results([cont], vocab, report_date="2027-09-10")}
    assert {"SII", "NLR"} <= codes


async def test_inline_note_after_value_in_semicolon_list(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    line = "Lab 6.9.2027: **CEA 14,2; CA 19-9 812,4** (zakrúžkované); WBC 5,0; HGB 126"
    by = {f["code"]: f for f in parse_inline_results([line], vocab, report_date="2027-09-10")}
    assert by["CA19_9"]["value_num"] == 812.4


async def test_inline_segment_date_beats_another_reports_date(vocab):
    """ "…oproti správe 2.9.2027: …; labs 9.9.: ANC …, WBC …" — the values belong
    to 9.9., not to the other report's full date in the same line."""
    from oncofiles.curated.facts_rules import parse_inline_results

    line = (
        "Zmeny oproti správe 02.09.2027: CHT podaná; labs 16.09.: ANC 2,90, WBC 4,80, "
        "HGB 127, CEA 18,0, CA 19-9 410,5; kontrola 30.09.2027."
    )
    facts = parse_inline_results([line], vocab, report_date="2027-09-16")
    by = {f["code"]: f for f in facts}
    assert {f["occurred_at"] for f in facts} == {"2027-09-16"}
    assert by["NEUT"]["value_num"] == 2.9  # "ANC" is the absolute neutrophil count
    assert by["WBC"]["metadata"]["date_source"] == "printed_no_year"
    # the year is inferred only when unambiguous (_year_for), so the date is known
    assert all(f["verification"] == "machine" for f in facts)


def test_year_for_day_month_dates():
    from oncofiles.curated.facts_rules import _year_for

    assert _year_for(16, 9, "2027-09-16") == 2027
    assert _year_for(15, 12, "2028-01-05") == 2027  # "15.12." in a January report
    assert _year_for(2, 9, "2027-08-25") is None  # just after the report: not a sample date


async def test_generic_flags_only_without_a_printed_range(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results, parse_okb

    line = "Lab 4.10.2027: CEA 88,0; CA 19-9 20,0; WBC 2,60; HGB 118"
    by = {f["code"]: f for f in parse_inline_results([line], vocab, report_date="2027-10-08")}
    assert by["CEA"]["flag"] == "H" and by["CEA"]["metadata"]["flag_source"] == "generic"
    assert by["WBC"]["flag"] == "L" and by["CA19_9"]["flag"] is None
    assert by["HGB"]["flag"] is None  # sex-dependent: never a generic flag
    # a printed lab range always wins over the generic one
    okb = {f["code"]: f for f in parse_okb([OKB_TEXT], vocab)}
    assert okb["CA19_9"]["flag"] == "H" and "flag_source" not in (
        okb["CA19_9"].get("metadata") or {}
    )


async def test_inline_full_date_range_is_a_range(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    line = "Lab 14.07.2027-15.07.2027 (krv EDTA): WBC 4,10; HGB 122; PLT 230; MONO 0,5"
    facts = parse_inline_results([line], vocab, report_date="2027-07-20")
    assert facts and all(f["metadata"]["date_source"] == "printed_range" for f in facts)
    assert {f["occurred_at"] for f in facts} == {"2027-07-15"}  # the range end (#726)
    assert all(f["metadata"]["date_range_start"] == "2027-07-14" for f in facts)
    assert all(f["metadata"]["date_range_end"] == "2027-07-15" for f in facts)
    long = "Lab 29.05.2027-12.06.2027 (krv EDTA): WBC 4,10; HGB 122; PLT 230"
    facts = parse_inline_results([long], vocab, report_date="2027-06-20")
    assert {f["occurred_at"] for f in facts} == {"2027-06-12"}  # 29.5.–12.6. → 12.6., never 5.6.
    assert {f["metadata"]["date_range_start"] for f in facts} == {"2027-05-29"}


# ── v10 verification policy (#659, #635) ─────────────────────────────────


async def test_value_without_its_own_date_is_unverified_with_reason(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    line = "Laboratórne výsledky: WBC 4,60; HGB 124; PLT 251; CEA 7,7"
    facts = parse_inline_results([line], vocab, report_date="2027-10-02")
    assert facts and all(f["occurred_at"] == "2027-10-02" for f in facts)
    assert all(f["verification"] == "unverified" for f in facts)
    assert all(f["metadata"]["unverified_reasons"] == ["date_unknown"] for f in facts)


async def test_arrow_flag_is_labelled_arrow(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    line = "Lab 4.10.2027: WBC 2,10↓; HGB 118; PLT 140"
    by = {f["code"]: f for f in parse_inline_results([line], vocab, report_date="2027-10-08")}
    assert by["WBC"]["flag"] == "L"
    assert by["WBC"]["metadata"]["flag_source"] == "arrow"


async def test_hgb_generic_flag_only_with_known_sex(vocab):
    from oncofiles.curated.facts_rules import extract_lab_facts

    page = "Lab 4.10.2027: WBC 5,10; HGB 112; PLT 240"
    for sex, expected in ((None, None), ("female", "L"), ("male", "L")):
        facts, _ = extract_lab_facts([page], vocab, report_date="2027-10-08", inline=True, sex=sex)
        hgb = next(f for f in facts if f["code"] == "HGB")
        assert hgb["flag"] == expected
    facts, _ = extract_lab_facts(
        ["Lab 4.10.2027: WBC 5,10; HGB 130; PLT 240"], vocab,
        report_date="2027-10-08", inline=True, sex="female",
    )  # fmt: skip
    assert next(f for f in facts if f["code"] == "HGB")["flag"] is None  # in female range


@pytest.mark.parametrize(
    ("label", "value", "unit", "code"),
    [
        ("Neutrofilie", "55,2", "%", "NEUT_PCT"),
        ("Neutrofilie", "3,1", "10^9/l", "NEUT"),
        ("Neutrofily %", "48,0", "10^9/l", "NEUT"),
        ("NEUT", "2,7", "%", "NEUT_PCT"),
    ],
)
async def test_percent_and_count_never_swap_codes(vocab, label, value, unit, code):
    """Guardrail (#635 v10): a "%" value never lands under the absolute code,
    and a count with a real unit never under *_PCT."""
    from oncofiles.curated.facts_rules import _fact

    f = _fact(
        vocab, label=label, abbr=None, raw_value=value, unit=unit, lo=None, hi=None,
        ref_text=None, arrow=None, when="2027-10-04", page=1,
        evidence=f"{label} {value} {unit}", unreliable=False,
    )  # fmt: skip
    assert f is not None and f["code"] == code


def _row(i, doc, day, code, value, verification="machine", reasons=None):
    import json as _json

    meta = {"unverified_reasons": reasons} if reasons else {}
    return {"id": i, "source_document_id": doc, "occurred_at": day, "code": code,
            "value_canonical": value, "unit_canonical": "u", "verification": verification,
            "metadata_json": _json.dumps(meta)}  # fmt: skip


def test_corroboration_dates_a_quote_by_the_draw_it_quotes():
    from oncofiles.curated.facts_rules import corroborations

    q = {"verification": "unverified", "reasons": ["date_unknown"]}
    rows = [
        _row(41, 10, "2027-04-14", "WBC", 4.4), _row(42, 10, "2027-04-14", "PLT", 201.0),
        _row(43, 10, "2027-04-14", "CEA", 6.6),
        _row(44, 20, "2027-04-22", "WBC", 4.4, **q), _row(45, 20, "2027-04-22", "PLT", 201.0, **q),
        _row(46, 20, "2027-04-22", "CEA", 6.6, **q),
    ]  # fmt: skip
    wbc_draw, plt_draw, cea_draw, wbc_quote, plt_quote, cea_quote = rows
    day = wbc_draw["occurred_at"]
    got = {fid: (d, draw) for fid, d, draw in corroborations(rows)}
    assert got == {  # the whole block moves to the draw it quotes
        wbc_quote["id"]: (day, wbc_draw["id"]),
        plt_quote["id"]: (day, plt_draw["id"]),
        cea_quote["id"]: (day, cea_draw["id"]),
    }


def test_corroboration_never_tears_values_off_a_different_block():
    """#672: two equal numbers in a panel that otherwise differs are a coincidence."""
    from oncofiles.curated.facts_rules import corroborations

    q = {"verification": "unverified", "reasons": ["date_unknown"]}
    draw = [
        _row(1, 10, "2027-04-14", "NA", 139.5), _row(2, 10, "2027-04-14", "UREA", 6.1),
        _row(3, 10, "2027-04-14", "GLU", 5.3), _row(4, 10, "2027-04-14", "OSMO", 289.9),
        _row(5, 10, "2027-04-14", "K", 4.2),
    ]  # fmt: skip
    differing = [
        _row(11, 20, "2027-04-22", "NA", 141.5, **q), _row(12, 20, "2027-04-22", "UREA", 4.3, **q),
        _row(13, 20, "2027-04-22", "GLU", 5.3, **q), _row(14, 20, "2027-04-22", "OSMO", 289.9, **q),
        _row(15, 20, "2027-04-22", "K", 4.2, **q),
    ]  # fmt: skip
    assert corroborations(draw + differing) == []  # GLU, K equal, OSMO derived, NA/UREA differ
    derived_only = [
        _row(21, 30, "2027-04-22", "OSMO", 289.9, **q), _row(22, 30, "2027-04-22", "GLU", 5.3, **q),
    ]  # fmt: skip
    assert corroborations(draw + derived_only) == []  # one comparable code is not enough


def test_header_date_conflicting_with_another_draw_is_flagged():
    from oncofiles.curated.facts_rules import date_conflicts

    draw = [
        _row(1, 10, "2027-04-14", "WBC", 4.4), _row(2, 10, "2027-04-14", "PLT", 201.0),
        _row(3, 10, "2027-04-14", "HGB", 118.0),
    ]  # fmt: skip

    def header(fid, code, value, day="2027-04-20"):
        r = _row(fid, 20, day, code, value)
        r["metadata_json"] = json.dumps({"date_from": "block_header"})
        return r

    quoted = [header(11, "WBC", 4.4), header(12, "PLT", 201.0), header(13, "HGB", 118.0)]
    assert sorted(date_conflicts(draw + quoted)) == [(11, "2027-04-14"), (12, "2027-04-14"),
                                                     (13, "2027-04-14")]  # fmt: skip
    own = [header(21, "WBC", 5.9), header(22, "PLT", 201.0), header(23, "HGB", 131.0)]
    assert date_conflicts(draw + own) == []  # a different draw that shares one number
    same_day = [header(31, "WBC", 4.4, "2027-04-14"), header(32, "PLT", 201.0, "2027-04-14")]
    assert date_conflicts(draw + same_day) == []  # same date: no conflict


_LAB, _RANGED, _QUOTE = 10, 20, 30  # documents: a lab sheet, a range report, a later report
_DRAW_DAY, _R_LO, _R_HI = "2027-04-14", "2027-04-10", "2027-04-18"
_Q = {"verification": "unverified", "reasons": ["date_unknown"]}


def _pair(first_id, doc, day, wbc=4.4, plt=201.0, **kw):
    """WBC and PLT of one draw, fact ids ``first_id`` and the next one."""
    return [
        _row(first_id, doc, day, "WBC", wbc, **kw),
        _row(first_id + 1, doc, day, "PLT", plt, **kw),
    ]


def _in_range(rows, lo=_R_LO, hi=_R_HI, **extra):
    """Machine facts dated under a printed range: they sit at the range end."""
    for r in rows:
        r["occurred_at"] = hi
        meta = {"date_source": "printed_range", "date_precision": "range",
                "date_range_start": lo, "date_range_end": hi, **extra}  # fmt: skip
        r["metadata_json"] = json.dumps(meta)
    return rows


def _dated(quotes, draws, day):
    """Expected corroborations: each quote fact dated ``day`` by its draw fact."""
    return sorted((q["id"], day, d["id"]) for q, d in zip(quotes, draws, strict=True))


def test_corroboration_by_a_range_draw_uses_its_end_and_prefers_an_exact_day():
    from oncofiles.curated.facts_rules import corroborations

    ranged = _in_range(_pair(21, _RANGED, ""))
    quote = _pair(31, _QUOTE, "2027-04-25", **_Q)
    # only the range report matches: the quote is dated at the range end, never its middle
    assert sorted(corroborations(ranged + quote)) == _dated(quote, ranged, _R_HI)
    # the lab sheet inside the range is the same draw with its exact day: it wins
    lab = _pair(11, _LAB, _DRAW_DAY)
    assert sorted(corroborations(lab + ranged + quote)) == _dated(quote, lab, _DRAW_DAY)
    # a later exact draw outside the range stays the latest match, as before #726
    late = _pair(13, _LAB, "2027-04-20")
    assert {d for _, d, _ in corroborations(late + ranged + quote)} == {"2027-04-20"}


def test_a_range_draw_ending_on_or_after_the_quote_never_dates_it():
    from oncofiles.curated.facts_rules import corroborations

    ranged = _in_range(_pair(21, _RANGED, ""))
    quote = _pair(31, _QUOTE, _R_HI, **_Q)  # quoted on the range's last day
    assert corroborations(ranged + quote) == []  # a value cannot postdate the report quoting it


def test_a_range_header_overlapping_its_draw_is_no_date_conflict():
    """#726: a range block and a draw agree when the draw's day lies in the
    range, in both directions; disjoint dates still conflict."""
    from oncofiles.curated.facts_rules import date_conflicts

    hdr = {"date_from": "block_header"}
    ranged = _in_range(_pair(21, _RANGED, ""), **hdr)
    assert date_conflicts(_pair(11, _LAB, _DRAW_DAY) + ranged) == []  # lab day inside the range
    inside = _pair(31, _QUOTE, "2027-04-16")
    for r in inside:
        r["metadata_json"] = json.dumps(hdr)
    assert date_conflicts(ranged + inside) == []  # an exact header day inside the range
    outside = "2027-04-02"
    got = sorted(date_conflicts(_pair(11, _LAB, outside) + ranged))
    assert got == sorted((r["id"], outside) for r in ranged)  # disjoint: still a conflict


def test_corroboration_needs_two_matching_codes_and_the_window():
    from oncofiles.curated.facts_rules import corroborations

    q = {"verification": "unverified", "reasons": ["date_unknown"]}
    one_code = [_row(1, 10, "2027-03-01", "WBC", 4.4), _row(4, 20, "2027-03-09", "WBC", 4.4, **q)]
    assert corroborations(one_code) == []  # one equal number is a coincidence
    too_old = [
        _row(1, 10, "2027-01-01", "WBC", 4.4), _row(2, 10, "2027-01-01", "PLT", 201.0),
        _row(4, 20, "2027-03-09", "WBC", 4.4, **q), _row(5, 20, "2027-03-09", "PLT", 201.0, **q),
    ]  # fmt: skip
    assert corroborations(too_old) == []
    other_reason = [
        _row(1, 10, "2027-03-01", "WBC", 4.4), _row(2, 10, "2027-03-01", "PLT", 201.0),
        _row(4, 20, "2027-03-09", "WBC", 4.4, "unverified", ["cowork_ocr", "date_unknown"]),
        _row(5, 20, "2027-03-09", "PLT", 201.0, "unverified", ["cowork_ocr", "date_unknown"]),
    ]  # fmt: skip
    assert corroborations(other_reason) == []  # uncertain digits are not corroborated by date


def test_unverified_metric_counts_reasons():
    from oncofiles.curated.facts_rules import unverified_metric

    rows = [
        _row(1, 10, "2027-03-01", "WBC", 4.4),
        _row(2, 10, "2027-03-01", "PLT", 201.0, "unverified", ["cowork_ocr"]),
        _row(3, 10, "2027-03-01", "CEA", 6.6, "unverified", ["cowork_ocr", "date_unknown"]),
        _row(4, 10, "2027-03-01", "HGB", 120.0, "verified"),
    ]
    m = unverified_metric(rows)
    assert m["coded"] == 4 and m["unverified"] == 2 and m["unverified_pct"] == 50.0
    assert m["reasons"] == {"cowork_ocr": 2, "date_unknown": 1}


# ── re-OCR'd layouts (#660): wrapped lists, OCR typos, OKB variants ───────


async def test_inline_joins_a_visually_wrapped_list(vocab):
    from oncofiles.curated.facts_rules import parse_inline_results

    text = "Lab 3.3.2027: WBC 5,10; HGB 120; PLT 255; GLUK S\n6,40; NA v sére 140,0; K 4,10"
    by = {f["code"]: f for f in parse_inline_results([text], vocab, report_date="2027-03-09")}
    assert by["GLU"]["value_num"] == 6.4 and by["NA"]["value_num"] == 140.0
    assert {f["occurred_at"] for f in by.values()} == {"2027-03-03"}  # the list's date


async def test_inline_does_not_join_a_finished_sentence(vocab):
    from oncofiles.curated.facts_rules import _join_wrapped

    lines = ["Lab 3.3.2027: WBC 5,10; HGB 120; PLT 255.", "Kontrola o 2 týždne."]
    assert _join_wrapped(lines) == lines


async def test_fuzzy_label_is_unique_unverified_and_unit_aware(vocab):
    from oncofiles.curated.facts_rules import _fact

    def fact(label, unit=None):
        return _fact(vocab, label=label, abbr=None, raw_value="61,0", unit=unit, lo=None,
                     hi=None, ref_text=None, arrow=None, when="2027-03-03", page=1,
                     evidence=f"{label} 61,0", unreliable=False)  # fmt: skip

    f = fact("Neutrofilly")  # one typo, no unit → the percentage (same rule as exact)
    assert f["code"] == "NEUT_PCT" and f["verification"] == "unverified"
    assert "label_fuzzy" in f["metadata"]["unverified_reasons"]
    count = _fact(vocab, label="Neutrofilly", abbr=None, raw_value="3,1", unit="10^9/l",
                  lo=None, hi=None, ref_text=None, arrow=None, when="2027-03-03", page=1,
                  evidence="Neutrofilly 3,1 10^9/l", unreliable=False)  # fmt: skip
    assert count["code"] == "NEUT"  # a count unit → the absolute code
    assert vocab.match_fuzzy("WBCX") is None  # short labels never fuzzy


async def test_okb_variants_from_a_reread_sheet(vocab):
    from oncofiles.curated.facts_rules import parse_okb

    page1 = (
        "Odobrané: 14.06.2027 07:50\n"
        "Test                                Hodnota    Jednotky    Ref. medze\n"
        "Celkové bielkoviny v sére 71.3 g/l 64.0 - 83.0 / [?]\n"
        "Celk. väzobná kapacita železa v    61.20      umol/l     45.00 - 77.00   /\n"
        "sére\n"
    )
    page2 = "K                                   4.61       mmol/l     3.50 - 5.10     /\n"
    facts = parse_okb([page1, page2], vocab)
    by = {f["code"]: f for f in facts}
    tp = by["TP"]  # one space before the value, unit + range present; "[?]" kept
    assert tp["value_num"] == 71.3 and tp["verification"] == "unverified"
    assert "ocr_uncertain" in tp["metadata"]["unverified_reasons"]
    assert by["TIBC"]["value_num"] == 61.2  # wrapped label joined
    assert by["K"]["value_num"] == 4.61  # the table continues on page 2 …
    assert by["K"]["occurred_at"] == by["TIBC"]["occurred_at"]  # … with the same draw time


# ── #661: block-header dating ──

from oncofiles.curated.facts_rules import parse_inline_results  # noqa: E402

_BLOCK_REPORT = """Anamnéza: bez zmeny.
Vykonané vyšetrenia: laboratórne 11.4.2028 - 15.4.2028:
sérum OKB
KREA S 77,3; NA v sére 136,7; K S 4,61
krv EDTA
WBC 8,17; HGB 133; PLT 287

Odobrané 22.5.2028 06:55. Č. vzorky 000.000001.
CEA S 2,9; CA 19-9 17,4; KREA S 64,8

Pacientka sa cíti dobre, bez ťažkostí.
WBC 6,71; HGB 129; PLT 233
"""


async def test_block_header_dates_the_values_below_it(vocab):
    facts = parse_inline_results([_BLOCK_REPORT], vocab, report_date="2028-06-02")
    first = ("KREA S 77", "WBC 8,17")  # the range-dated block
    by = {f["code"]: f for f in facts if f["evidence"].startswith(first)}
    cea = next(f for f in facts if f["code"] == "CEA")
    # a range header dates its block at the range END (#726), through specimen labels
    for code in ("CREA", "NA", "K"):
        meta = by[code]["metadata"]
        assert by[code]["occurred_at"][:10] == "2028-04-15"
        assert by[code]["verification"] == "machine"
        assert meta["date_from"] == "block_header" and meta["date_range_start"] == "2028-04-11"
        assert meta["date_range_end"] == "2028-04-15" and meta["date_precision"] == "range"
        assert meta["date_evidence"].startswith("Vykonané vyšetrenia")
    assert by["PLT"]["occurred_at"][:10] == "2028-04-15"  # second specimen, same block
    assert "date_precision" not in cea["metadata"]  # a single printed day stays exact
    # the sampling line dates its block
    assert cea["occurred_at"][:10] == "2028-05-22" and cea["verification"] == "machine"


_RANGE_VALUES = "KREA S 66,1; NA v sére 139,4\n"


@pytest.mark.parametrize(
    "header",
    [
        "laboratórne: 17.03.2028 - 26.03.2028:\n",  # results right below the header
        "laboratórne: 17.03.2028 - 26.03.2028:\nsérum OKB\n",  # through a specimen label
        "Lab 17.03.2028 - 26.03.2028: ",  # on the result line itself
        "laboratórne 17.–26.3.2028:\n",  # short form
    ],
)
async def test_range_header_dates_its_values_at_the_range_end(vocab, header):
    """#726 (supersedes the #659 midpoint): a value under a printed date range
    is dated at the range END, the latest possible draw date; both ends and
    ``date_precision=range`` say it is a period, not a day. Before #726 the
    first shape was even dated at the range START as an exact printed day."""
    facts = parse_inline_results([header + _RANGE_VALUES], vocab, report_date="2028-04-02")
    assert {f["code"] for f in facts} == {"CREA", "NA"}
    for f in facts:
        meta = f["metadata"]
        assert f["occurred_at"] == "2028-03-26" and f["verification"] == "machine"
        assert meta["date_source"] == "printed_range" and meta["date_precision"] == "range"
        assert (meta["date_range_start"], meta["date_range_end"]) == ("2028-03-17", "2028-03-26")
        assert "date_range_open" not in meta


@pytest.mark.parametrize(
    "header",
    [
        "laboratórne: 30.03.2028 - 26.03.2028:\n",  # the start is after the end
        "laboratórne: 30.03.2028 - 26.03.2028:\nsérum OKB\n",
    ],
)
async def test_an_open_range_keeps_its_end_and_stays_unverified(vocab, header):
    facts = parse_inline_results([header + _RANGE_VALUES], vocab, report_date="2028-04-02")
    assert facts
    for f in facts:
        meta = f["metadata"]
        assert f["occurred_at"] == "2028-03-26" and meta["date_range_end"] == "2028-03-26"
        assert meta["date_range_open"] is True and "date_range_start" not in meta
        assert meta["date_precision"] == "range" and meta["date_source"] == "printed_range"
        assert f["verification"] == "unverified" and "date_unknown" in meta["unverified_reasons"]


@pytest.mark.parametrize(
    "header", ["laboratórne: 26.03.2028:\n", "laboratórne: 26.03.2028:\nsérum OKB\n"]
)
async def test_a_single_date_header_is_unchanged(vocab, header):
    facts = parse_inline_results([header + _RANGE_VALUES], vocab, report_date="2028-04-02")
    assert facts and {f["occurred_at"] for f in facts} == {"2028-03-26"}
    for f in facts:
        meta = f["metadata"]
        assert meta["date_source"] == "printed" and f["verification"] == "machine"
        assert not {"date_precision", "date_range_start", "date_range_end"} & set(meta)


async def test_a_range_value_and_an_exact_value_on_its_end_day_are_both_kept(vocab):
    """The range end can now equal a day printed in the same report: the parser
    keeps both (the read-time dedupe shows the exact one), never drops the
    exact one as "the same line quoted twice"."""
    text = (
        "laboratórne: 17.03.2028 - 26.03.2028:\n"
        + _RANGE_VALUES
        + "\nKontrola 26.03.2028: "
        + _RANGE_VALUES
    )
    facts = parse_inline_results([text], vocab, report_date="2028-04-02")
    crea = [f for f in facts if f["code"] == "CREA"]
    assert {f["occurred_at"] for f in crea} == {"2028-03-26"}
    assert sorted(f["metadata"]["date_source"] for f in crea) == ["printed", "printed_range"]


async def test_prose_ends_the_block_and_values_stay_unverified(vocab):
    facts = parse_inline_results([_BLOCK_REPORT], vocab, report_date="2028-06-02")
    late = [f for f in facts if f["evidence"].startswith("WBC 6,71")]
    assert late and all(f["occurred_at"][:10] == "2028-06-02" for f in late)
    assert all(f["verification"] == "unverified" for f in late)
    assert all("date_unknown" in f["metadata"]["unverified_reasons"] for f in late)


async def test_a_table_row_or_a_sentence_is_never_a_block_header(vocab):
    text = (
        "| Parameter | Stav | Zmena od 3. 4. 2028 |\n"
        "sérum OKB\n"
        "KREA S 70,9; NA v sére 141,3; K S 3,92\n\n"
        "Kontrola bola 9.4.2028 a pacientka udáva:\n"
        "krv EDTA\n"
        "WBC 5,52; HGB 141; PLT 199\n"
    )
    facts = parse_inline_results([text], vocab, report_date="2028-04-20")
    assert facts and all(f["metadata"]["date_source"] == "report" for f in facts)


async def test_nou_report_short_labels_are_mapped(vocab):
    """#672: short forms used in NOU reports."""
    from oncofiles.curated.facts_rules import parse_inline_results

    text = "CA KOR V 2,31; BI C S 7,4; CB S 68,2; NA v sére 139,8\n"
    by = {f["code"]: f for f in parse_inline_results([text], vocab, report_date="2027-05-02")}
    assert {"CA_CORR", "BILI", "TP", "NA"} <= set(by)


async def test_a_conflicted_block_never_dates_another_quote(db):
    """#672 review: after a block is flagged date_conflict it is no longer a draw."""
    from oncofiles.tools import curated as tool
    from tests.helpers import ERIKA_UUID

    async def fact(doc, day, code, value, verification, meta):
        await db.db.execute(
            "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
            "code, occurred_at, value_canonical, verification, metadata_json, source) "
            "VALUES (?, 'lab', ?, ?, ?, ?, ?, ?, ?, 't')",
            (ERIKA_UUID, doc, code, code, day, value, verification, json.dumps(meta)),
        )  # fmt: skip

    for doc in (10, 20, 30):
        await db.db.execute(
            "INSERT INTO documents (id, file_id, filename, original_filename, category, "
            "mime_type, size_bytes, patient_id) VALUES (?, ?, ?, ?, 'labs', 'application/pdf', "
            "1, ?)",
            (doc, f"t:{doc}", f"d{doc}.pdf", f"d{doc}.pdf", ERIKA_UUID),
        )
    hdr = {"date_from": "block_header"}
    quote = {"unverified_reasons": ["date_unknown"]}
    for code, v in (("WBC", 4.4), ("PLT", 201.0), ("HGB", 118.0)):
        await fact(10, "2027-04-14", code, v, "machine", {})  # the real draw
        await fact(20, "2027-04-20", code, v, "machine", hdr)  # header-dated copy
        await fact(30, "2027-04-28", code, v, "unverified", quote)  # a later report quote
    await db.db.commit()
    out = await tool._corroborate_and_measure(db, ERIKA_UUID)
    assert out["date_conflicts"] == 3
    async with db.db.execute(
        "SELECT source_document_id AS d, occurred_at AS day, verification AS v, "
        "metadata_json AS m FROM clinical_records WHERE source_document_id IN (20, 30)"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert all(r["v"] == "unverified" for r in rows if r["d"] == 20)
    # the quote is dated by the real draw (doc 10), never by the conflicted block
    quoted = [json.loads(r["m"]) for r in rows if r["d"] == 30]
    assert all(r["day"][:10] == "2027-04-14" for r in rows if r["d"] == 30)
    by = {q["corroborated_by"] for q in quoted}
    assert by and all(isinstance(b, int) for b in by)


# ── #726: dependents of the range-end date ──

_RANGE_META = {"date_source": "printed_range", "date_precision": "range",
               "date_range_start": _R_LO, "date_range_end": _R_HI}  # fmt: skip


async def _insert_docs(db, *docs):
    for doc in docs:
        await db.db.execute(
            "INSERT INTO documents (id, file_id, filename, original_filename, category, "
            "mime_type, size_bytes, patient_id) VALUES (?, ?, ?, ?, 'report', "
            "'application/pdf', 1, ?)",
            (doc, f"t:{doc}", f"d{doc}.pdf", f"d{doc}.pdf", ERIKA_UUID),
        )


async def _insert_fact(db, doc, day, code, value, verification, meta):
    await db.db.execute(
        "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
        "code, occurred_at, value_canonical, verification, metadata_json, source) "
        "VALUES (?, 'lab', ?, ?, ?, ?, ?, ?, ?, 't')",
        (ERIKA_UUID, doc, code, code, day, value, verification, json.dumps(meta)),
    )  # fmt: skip


@pytest.mark.parametrize("draw_is_range", [True, False])
async def test_a_corroborated_quote_takes_the_precision_of_its_draw(db, draw_is_range):
    """The quote's date now comes from the draw, and so does its precision: a
    range draw lends its range, an exact draw none. The quote's own (open)
    range keys described only the day it was quoted, kept as ``quoted_on``."""
    await _insert_docs(db, _RANGED, _QUOTE)
    quoted_on = "2027-04-25"
    quote = {"unverified_reasons": ["date_unknown"], "date_source": "printed_range",
             "date_precision": "range", "date_range_end": quoted_on,
             "date_range_open": True}  # fmt: skip
    draw_day, draw_meta = (_R_HI, _RANGE_META) if draw_is_range else (_DRAW_DAY, {})
    for code, value in (("WBC", 4.4), ("PLT", 201.0)):
        await _insert_fact(db, _RANGED, draw_day, code, value, "machine", draw_meta)
        await _insert_fact(db, _QUOTE, quoted_on, code, value, "unverified", quote)
    await db.db.commit()
    out = await tool._corroborate_and_measure(db, ERIKA_UUID)
    assert out["corroborated"] == 2
    async with db.db.execute(
        "SELECT occurred_at AS day, verification AS v, metadata_json AS m "
        "FROM clinical_records WHERE source_document_id = ?",
        (_QUOTE,),
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    assert rows and all(r["day"] == draw_day and r["v"] == "machine" for r in rows)
    for meta in (json.loads(r["m"]) for r in rows):
        assert meta["date_source"] == "corroborated" and meta["quoted_on"] == quoted_on
        assert "date_range_open" not in meta
        if draw_is_range:
            assert meta["date_precision"] == "range"
            assert (meta["date_range_start"], meta["date_range_end"]) == (_R_LO, _R_HI)
        else:
            assert not {"date_precision", "date_range_start", "date_range_end"} & set(meta)


async def test_reextraction_never_duplicates_a_verified_fact_inside_the_range(db):
    """A verified/corrected fact of an older index (dated at the old midpoint,
    or at the exact day a person set) inside a new range fact's window stands
    for it; a code without such a fact is re-extracted as usual."""
    await _insert_docs(db, _RANGED)
    await _insert_fact(db, _RANGED, _DRAW_DAY, "HGB", 127.0, "verified", {})
    await db.db.commit()

    def new(code, value):
        return {"code": code, "category": "lab", "occurred_at": _R_HI, "label": code,
                "value_num": value, "value_canonical": value,
                "metadata": dict(_RANGE_META)}  # fmt: skip

    res = await db.replace_document_facts(
        patient_id=ERIKA_UUID, document_id=_RANGED, extracted_by="rule:test",
        facts=[new("HGB", 127.0), new("PLT", 201.0)],
    )  # fmt: skip
    assert res["added"] == 1 and res["kept_verified"] == 1
    async with db.db.execute(
        "SELECT code, verification FROM clinical_records "
        "WHERE source_document_id = ? AND deleted_at IS NULL ORDER BY code",
        (_RANGED,),
    ) as cur:
        rows = [tuple(r) for r in await cur.fetchall()]
    assert rows == [("HGB", "verified"), ("PLT", "machine")]


async def test_fact_series_and_document_detail_show_the_range(db):
    """#726: a value dated under a printed range says so in the read tools:
    ``date_precision="range"`` with ``at_from``/``at_to``; ``at`` is the range
    end. A value with its own day carries none of these keys."""
    await _insert_docs(db, _LAB, _RANGED)
    await _insert_fact(db, _LAB, _DRAW_DAY, "HGB", 127.0, "machine", {"date_source": "printed"})
    await _insert_fact(db, _RANGED, _R_HI, "HGB", 131.0, "machine", _RANGE_META)
    await db.db.commit()
    ctx = _Ctx(db)
    out = json.loads(await cf.fact_series(ctx, codes=["HGB"]))
    by_at = {p["at"][:10]: p for p in out["series"]["HGB"]["values"]}
    ranged, exact = by_at[_R_HI], by_at[_DRAW_DAY]
    assert ranged["date_precision"] == "range"
    assert (ranged["at_from"], ranged["at_to"]) == (_R_LO, _R_HI)
    assert not {"date_precision", "at_from", "at_to"} & set(exact)
    assert out["series"]["HGB"]["latest_any"]["date_precision"] == "range"  # newest is the range
    assert "latest possible day, not a sample date" in out["note"]

    detail = json.loads(await cf.document_detail(ctx, document_id=_RANGED))
    [fact] = detail["facts"]
    assert fact["at"][:10] == _R_HI and fact["date_precision"] == "range"
    assert (fact["at_from"], fact["at_to"]) == (_R_LO, _R_HI)
    lab = json.loads(await cf.document_detail(ctx, document_id=_LAB))["facts"][0]
    assert not {"date_precision", "at_from", "at_to"} & set(lab)
    for tool_fn in (cf.fact_series, cf.document_detail):  # the tool descriptions say it
        doc = " ".join(tool_fn.__doc__.split())
        assert "for a range, ``at`` is the latest possible day, not a sample date" in doc


# ── #676: inline fallback on lab sheets ──

_INLINE_SHEET = """## Krvný obraz — Testovacie laboratórium
Odobrané 14.6.2028 07:25. Č. vzorky 000.000002.
WBC 7,43; RBC 4,61; HGB 138; HCT 0,412; PLT 251
Uvoľnil: laborant.
"""

_QUALITATIVE_SHEET = """## Krvná skupina — Testovacie laboratórium
Odobrané 14.6.2028 07:25.
Krvná skupina: 0 Rh(D) negat. Skríning protilátok: negatívny.
"""


async def test_a_lab_sheet_without_a_table_falls_back_to_inline(vocab):
    from oncofiles.curated.facts_rules import INLINE_CATEGORIES, extract_lab_facts

    assert "labs" in INLINE_CATEGORIES
    facts, ext = extract_lab_facts([_INLINE_SHEET], vocab, unreliable=True,
                                   report_date="2028-06-15", inline=True)  # fmt: skip
    assert ext == "rule:inline-v1"
    assert {f["code"] for f in facts} >= {"WBC", "RBC", "HGB", "PLT"}
    assert all(f["occurred_at"][:10] == "2028-06-14" for f in facts)  # the "Odobrané" line
    assert all(f["verification"] == "unverified" for f in facts)  # Cowork-era stays unverified


async def test_a_qualitative_lab_sheet_yields_nothing(vocab):
    from oncofiles.curated.facts_rules import extract_lab_facts

    facts, ext = extract_lab_facts([_QUALITATIVE_SHEET], vocab, report_date="2028-06-15",
                                   inline=True)  # fmt: skip
    assert facts == [] and ext is None


async def test_a_two_item_marker_line_is_read_and_keeps_its_block(vocab):
    """#661: "CEA …; CA 19-9 … (ref …)." is a result line; the block date carries on."""
    text = (
        "Odobrané 9.7.2028 07:05. Č. vzorky 000.000003.\n"
        "CEA 3,7 ng/ml; CA 19-9 21,3 U/ml (ref 0–37,0).\n"
        "Kreatinín 66,1; Močovina 4,9; Albumín 41,8; Glukóza 5,11\n"
    )
    facts = parse_inline_results([text], vocab, report_date="2028-07-12")
    by = {f["code"]: f for f in facts}
    assert {"CEA", "CA19_9", "CREA", "UREA", "ALB", "GLU"} <= set(by)
    assert all(f["occurred_at"][:10] == "2028-07-09" for f in facts)
    assert all(f["metadata"]["date_source"] == "printed" for f in facts)


async def test_a_medication_or_prose_line_with_a_number_ends_the_block(vocab):
    """#683 review (i): a non-result number never carries the block date down."""
    text = (
        "Odobrané 9.7.2028 07:05.\n"
        "Kreatinín 66,1; Močovina 4,9; Albumín 41,8\n"
        "Dexametazón 8 mg\n"
        "WBC 6,02; HGB 131; PLT 244\n"
    )
    facts = parse_inline_results([text], vocab, report_date="2028-07-12")
    late = [f for f in facts if f["code"] in ("WBC", "HGB", "PLT")]
    assert late and all(f["metadata"]["date_source"] == "report" for f in late)
    early = [f for f in facts if f["code"] in ("CREA", "UREA", "ALB")]
    assert early and all(f["occurred_at"][:10] == "2028-07-09" for f in early)


async def test_a_new_header_wins_and_a_table_ends_the_block(vocab):
    """#683 review (ii) a new dated header takes over; (iii) a table ends the block."""
    text = (
        "Odobrané 9.7.2028 07:05.\n"
        "Kreatinín 66,1; Močovina 4,9; Albumín 41,8\n"
        "Odobrané 10.7.2028 06:50.\n"
        "WBC 6,02; HGB 131; PLT 244\n"
        "| Liek | Dávka |\n"
        "\n"
        "NA v sére 139,4; K S 4,05; CL S 101\n"
    )
    facts = parse_inline_results([text], vocab, report_date="2028-07-12")
    by = {f["code"]: f for f in facts}
    assert by["WBC"]["occurred_at"][:10] == "2028-07-10"
    assert by["CREA"]["occurred_at"][:10] == "2028-07-09"
    assert by["NA"]["metadata"]["date_source"] == "report"
