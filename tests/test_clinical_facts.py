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
    assert by["CA19_9"]["verification"] == "unverified"  # unit implied → never machine
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
    rows = [
        {**base, "id": 1, "effective_at": "2027-03-03", "value_canonical": 44.4,
         "verification": "machine", "source_document_id": 10},
        {**base, "id": 2, "effective_at": "2027-03-03", "value_canonical": 44.4,
         "verification": "unverified", "source_document_id": 11},  # duplicated sheet
        {**base, "id": 3, "effective_at": "2027-03-20", "value_canonical": 44.4,
         "verification": "unverified", "source_document_id": 12,
         "metadata_json": '{"date_source": "report"}'},  # quoted in a later report
        {**base, "id": 4, "effective_at": "2027-06-12", "value_canonical": 38.2,
         "verification": "machine", "source_document_id": 13},
    ]  # fmt: skip
    kept, dropped = cf._dedupe(rows)
    assert [r["id"] for r in kept] == [1, 4] and dropped == 2


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
