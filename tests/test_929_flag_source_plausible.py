"""#929: ``flag_source`` wherever a flag is served; plausibility ranges in
``fact_codes`` (migration 094). Synthetic data only (invented values, 2027+)."""

from __future__ import annotations

import json

import pytest

from oncofiles import facts_timeline
from oncofiles.curated import verifier
from oncofiles.curated.facts_rules import (
    Vocabulary,
    extract_lab_facts,
    parse_inline_results,
    plausible_range,
)
from oncofiles.fact_labels import flag_source
from oncofiles.fhir import terminology as term
from oncofiles.fhir.ids import derived_id
from oncofiles.tools import clinical_facts as cf
from oncofiles.tools import curated as tool
from tests.helpers import PATIENT_A_UUID
from tests.test_clinical_facts import BORY_SIDECAR, OKB_TEXT, _Ctx
from tests.test_dashboard_feed import curated_env  # noqa: F401
from tests.test_fhir_read import _index, world  # noqa: F401

# The removed in-code constant ``facts_rules.PLAUSIBLE`` as it was before #929:
# the expected snapshot of what migration 094 seeds (canonical units).
OLD_PLAUSIBLE = {  # one code per line
    "NEUT": (0, 50),
    "LYMPH": (0, 20),
    "MONO": (0, 5),
    "EOS": (0, 5),
    "BASO": (0, 5),
    "NEUT_PCT": (0, 100),
    "LYMPH_PCT": (0, 100),
    "MONO_PCT": (0, 100),
    "EOS_PCT": (0, 100),
    "BASO_PCT": (0, 100),
    "WBC": (0, 300),
    "RBC": (0, 10),
    "HGB": (20, 250),
    "HCT": (0, 1),
    "PLT": (0, 3000),
    "MCV": (40, 150),
    "MCH": (10, 60),
    "MCHC": (200, 450),
    "CREA": (5, 2000),
    "UREA": (0, 100),
    "GLU": (0, 60),
    "NA": (90, 200),
    "K": (1, 10),
    "CRP": (0, 1000),
    "ALB": (5, 70),
    "TP": (20, 120),
    "P": (0, 10),
    "CA": (0, 6),
}  # fmt: skip

# Synthetic pages in the formats the rule parsers know, with values inside and
# outside the plausible ranges (a count of 4870 PLT, a percentage as a count,
# a glucose of 77, a sodium of 30, a hematocrit of 42 read as L/L).
INLINE_PAGES = [
    "Lab 05.06.2027: WBC 6,10; Neutrofily 61,2; abs.počet NEUT 3,73; "
    "Lymfocyty 24,8; abs.počet LYMPH 1,51; PLT 4870; HGB 118; CRP 12,0",
    "Lab 12.06.2027: WBC 350; NEUT 61,0 10^9/l; GLU 77,0; NA 30; K 4,1; HCT 42; CEA 88,0",
    "Lab 19.06.2027: MCV 91; MCH 30,1; MCHC 330; CREA 1,2; ALB 41; TP 70; CA 2,4; P 1,1",
]


async def _vocab(db) -> Vocabulary:
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


def _with_old_constant(rows: list[dict]) -> Vocabulary:
    """The pre-#929 semantics: the range looked up by code in the old constant."""
    out = []
    for r in rows:
        lo, hi = OLD_PLAUSIBLE.get(r["code"], (None, None))
        out.append({**r, "plausible_low": lo, "plausible_high": hi})
    return Vocabulary.from_rows(out)


# ── migration 094: the data equals the removed constant ──


async def test_loaded_ranges_equal_the_old_constant(db):
    vocab = await _vocab(db)
    for code, expected in OLD_PLAUSIBLE.items():
        row = vocab.by_code.get(code)
        assert row is not None, code
        assert plausible_range(row) == (float(expected[0]), float(expected[1])), code
    seeded = {c for c, r in {**vocab.by_code, **vocab.biomarker_by_code}.items()
              if plausible_range(r) is not None}  # fmt: skip
    assert seeded == set(OLD_PLAUSIBLE)  # no other code got a range


async def test_migration_is_idempotent_and_keeps_a_maintainer_range(db):
    await db.db.execute("UPDATE fact_codes SET plausible_high = 400 WHERE code = 'PLT'")
    await db.db.execute(
        "DELETE FROM schema_migrations WHERE version = '094_fact_codes_plausible_ranges'"
    )
    await db.db.commit()
    await db.migrate()
    vocab = await _vocab(db)
    assert plausible_range(vocab.by_code["PLT"]) == (0.0, 400.0)  # never overwritten
    assert plausible_range(vocab.by_code["WBC"]) == (0.0, 300.0)


def test_the_constant_is_gone():
    from oncofiles.curated import facts_rules

    assert not hasattr(facts_rules, "PLAUSIBLE")
    assert not hasattr(verifier, "PLAUSIBLE")


def test_plausible_range_open_sides():
    assert plausible_range(None) is None
    assert plausible_range({"code": "X"}) is None
    assert plausible_range({"plausible_low": 1, "plausible_high": None}) == (1.0, float("inf"))
    assert plausible_range({"plausible_low": None, "plausible_high": 2}) == (float("-inf"), 2.0)


# ── extraction output is byte-identical ──


async def test_rule_extraction_is_identical_to_the_old_constant(db):
    rows = await db.fact_code_vocabulary()
    new, old = Vocabulary.from_rows(rows), _with_old_constant(rows)
    cases = [
        ([BORY_SIDECAR.split("-->", 1)[1]], {"report_date": "2027-04-14"}),
        ([OKB_TEXT], {}),
        (INLINE_PAGES, {"report_date": "2027-06-20", "inline": True, "sex": "female"}),
    ]
    bare = Vocabulary.from_rows(
        [{**r, "plausible_low": None, "plausible_high": None} for r in rows]
    )
    exercised = 0
    for pages, kw in cases:
        facts, _ = extract_lab_facts(pages, new, **kw)
        assert facts  # every synthetic case yields facts
        exercised += facts != extract_lab_facts(pages, bare, **kw)[0]
        a = json.dumps(extract_lab_facts(pages, new, **kw), sort_keys=True, ensure_ascii=False)
        b = json.dumps(extract_lab_facts(pages, old, **kw), sort_keys=True, ensure_ascii=False)
        assert a == b
    assert exercised  # the cases really hit the range check (without it they differ)
    for page in INLINE_PAGES:
        a = parse_inline_results([page], new, report_date="2027-06-20")
        b = parse_inline_results([page], old, report_date="2027-06-20")
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


async def test_the_ranges_are_really_read_from_the_vocabulary(db):
    rows = await db.fact_code_vocabulary()
    bare = Vocabulary.from_rows(
        [{**r, "plausible_low": None, "plausible_high": None} for r in rows]
    )
    with_data = parse_inline_results([INLINE_PAGES[0]], await _vocab(db), report_date="2027-06-20")
    without = parse_inline_results([INLINE_PAGES[0]], bare, report_date="2027-06-20")
    assert "PLT" not in {f["code"] for f in with_data}  # 4870 ×10^9/L: never under PLT
    assert "PLT" in {f["code"] for f in without}


async def test_verifier_implausible_is_identical_to_the_old_constant(db):
    rows = await db.fact_code_vocabulary()
    new, old = Vocabulary.from_rows(rows), _with_old_constant(rows)
    probes = [
        ("PLT", 4870.0, "10^9/l"), ("PLT", 250.0, "10^9/l"), ("GLU", 77.0, "mmol/l"),
        ("NA", 30.0, "mmol/l"), ("HCT", 42.0, None), ("HGB", 9.8, "g/dl"), ("CEA", 9999.0, None),
    ]  # fmt: skip
    for label, value, unit in probes:
        assert verifier.vocab_check(new, label, unit, value) == verifier.vocab_check(
            old, label, unit, value
        ), label
    assert "implausible" in verifier.vocab_check(new, "PLT", "10^9/l", 4870.0)[2]


# ── flag_source: printed | generic | None ──


def test_flag_source_values():
    assert flag_source(None, {"flag_source": "generic"}) is None
    assert flag_source("H", {"flag_source": "generic"}) == "generic"
    assert flag_source("H", '{"flag_source": "generic"}') == "generic"
    assert flag_source("L", {"flag_source": "arrow"}) == "printed"  # a printed arrow
    assert flag_source("L", None) == "printed"
    assert flag_source("L", "not json") == "printed"


async def test_generic_flag_from_the_parser_is_served_as_generic(db):
    line = "Lab 4.10.2027: CEA 88,0; WBC 2,10↓; PLT 140"
    by = {f["code"]: f for f in parse_inline_results([line], await _vocab(db),
                                                     report_date="2027-10-08")}  # fmt: skip
    assert flag_source(by["CEA"]["flag"], by["CEA"].get("metadata")) == "generic"
    assert flag_source(by["WBC"]["flag"], by["WBC"].get("metadata")) == "printed"


async def _make_hgb_generic(db) -> None:
    """Turn the indexed HGB fact into one flagged from a generic range."""
    await db.db.execute(
        "UPDATE clinical_records SET ref_range_low = NULL, ref_range_high = NULL, "
        "metadata_json = json_set(COALESCE(metadata_json, '{}'), '$.flag_source', 'generic') "
        "WHERE patient_id = ? AND code = 'HGB'",
        (PATIENT_A_UUID,),
    )
    await db.db.commit()


async def test_every_read_tool_serves_flag_source(db, curated_env):  # noqa: F811
    await tool.index_patient(db, PATIENT_A_UUID)
    ctx = _Ctx(db)
    feed = json.loads(await cf.dashboard_feed(ctx))["facts"]
    assert next(f for f in feed if f["code"] == "HGB")["flag_source"] == "printed"
    assert all(("flag" in f) == ("flag_source" in f) for f in feed)

    await _make_hgb_generic(db)
    feed = json.loads(await cf.dashboard_feed(ctx))["facts"]
    hgb = next(f for f in feed if f["code"] == "HGB")
    assert hgb["flag"] == "L" and hgb["flag_source"] == "generic"

    doc_id = hgb["document_id"]
    detail = json.loads(await cf.document_detail(ctx, doc_id))["facts"]
    by = {f["code"]: f for f in detail}
    assert by["HGB"]["flag_source"] == "generic"
    assert by["WBC"]["flag"] == "H" and by["WBC"]["flag_source"] == "printed"
    assert all((f["flag"] is None) == (f["flag_source"] is None) for f in detail)

    overview = json.loads(await cf.patient_overview(ctx))["latest_key_values"]
    assert next(r for r in overview if r["code"] == "HGB")["flag_source"] == "generic"

    series = json.loads(await cf.fact_series(ctx, codes=["HGB", "WBC"]))["series"]
    assert series["HGB"]["values"][-1]["flag_source"] == "generic"
    assert series["HGB"]["latest_any"]["flag_source"] == "generic"
    assert series["WBC"]["values"][-1]["flag_source"] == "printed"


# ── FHIR: a generic flag is never the lab's interpretation ──


def test_fhir_interpretation_skips_a_generic_flag():
    generic = {"flag": "H", "metadata_json": json.dumps({"flag_source": "generic"})}
    assert term.interpretation_code(generic) is None
    assert term.interpretation_code({"flag": "H", "metadata_json": "{}"}) == "H"
    assert (
        term.interpretation_code({"flag": "L", "metadata_json": '{"flag_source": "arrow"}'}) == "L"
    )
    # an explicit interpretation code stored by a writer still wins
    assert term.interpretation_code({**generic, "interpretation_code": "A"}) == "A"


async def test_fhir_observation_with_a_generic_flag_has_no_interpretation(world):  # noqa: F811
    db, ids = world
    await db.db.execute(
        "UPDATE clinical_records SET metadata_json = "
        "json_set(COALESCE(metadata_json, '{}'), '$.flag_source', 'generic') WHERE id = ?",
        (ids["hgb"],),
    )
    await db.db.commit()
    obs = (await _index(db)).get("Observation", derived_id("clinical_records", ids["hgb"]))
    assert "interpretation" not in obs
    assert obs["valueQuantity"]["value"] == 98.0  # the value is exported unchanged


@pytest.mark.parametrize("flag", ["H", "L"])
def test_fhir_printed_flag_keeps_its_interpretation(flag):
    assert term.interpretation_code({"flag": flag}) == flag


# ── follow-up: timeline readers, fact_series stats, FHIR text, corrections ──


async def test_timeline_rows_carry_flag_source(db, curated_env):  # noqa: F811
    await tool.index_patient(db, PATIENT_A_UUID)
    ctx = _Ctx(db)
    rows = json.loads(await cf.timeline(ctx))["rows"]
    flagged = [r for r in rows if r.get("flag")]
    assert flagged and all(r["flag_source"] == "printed" for r in flagged)
    assert all(r["flag_source"] is None for r in rows if not r.get("flag"))

    await _make_hgb_generic(db)
    hgb_ids = await _fact_ids(db, "HGB")
    rows = json.loads(await cf.timeline(ctx))["rows"]
    flagged = [r for r in rows if r.get("flag")]
    hgb = [r for r in flagged if r["entity"] == "fact" and r["entity_id"] in hgb_ids]
    assert hgb and all(r["flag_source"] == "generic" for r in hgb)
    others = [r for r in flagged if r not in hgb]
    assert others and all(r["flag_source"] == "printed" for r in others)


async def _fact_ids(db, code: str) -> set[int]:
    async with db.db.execute(
        "SELECT id FROM clinical_records WHERE patient_id = ? AND code = ? AND deleted_at IS NULL",
        (PATIENT_A_UUID, code),
    ) as cur:
        return {r[0] for r in await cur.fetchall()}


async def test_timeline_summary_spells_out_a_generic_flag(db, curated_env):  # noqa: F811
    await tool.index_patient(db, PATIENT_A_UUID)
    await _make_hgb_generic(db)
    rows = await facts_timeline.fact_rows(db, PATIENT_A_UUID)
    generic = [r for r in rows if r["flag_source"] == "generic"]
    printed = [r for r in rows if r["flag_source"] == "printed"]
    assert generic and printed
    for r in generic:
        assert f"[{r['flag']}, generic range]" in facts_timeline.summary_of(r)
    for r in printed:
        summary = facts_timeline.summary_of(r)
        assert f"[{r['flag']}]" in summary and "generic" not in summary


def test_summary_of_a_printed_flag_is_unchanged():
    row = {"value": "6.1", "unit": "g/L", "flag": "H", "detail": "x"}
    assert facts_timeline.summary_of(row) == "6.1 g/L [H] · x"
    assert facts_timeline.summary_of({**row, "flag_source": "printed"}) == "6.1 g/L [H] · x"
    assert facts_timeline.summary_of({**row, "flag": None}) == "6.1 g/L · x"


async def test_fact_series_counts_generic_flags_apart(db, curated_env):  # noqa: F811
    await tool.index_patient(db, PATIENT_A_UUID)
    ctx = _Ctx(db)
    stats = json.loads(await cf.fact_series(ctx, codes=["HGB"]))["series"]["HGB"]["stats"]
    assert stats["out_of_range"] >= 1 and stats["out_of_range_generic"] == 0
    await _make_hgb_generic(db)
    stats = json.loads(await cf.fact_series(ctx, codes=["HGB"]))["series"]["HGB"]["stats"]
    assert stats["out_of_range_generic"] == stats["out_of_range"] >= 1


def test_fhir_printed_flag_helper():
    generic = {"flag": "H", "metadata_json": json.dumps({"flag_source": "generic"})}
    assert term.printed_flag(generic) is None
    assert term.printed_flag({"flag": " L ", "metadata_json": "{}"}) == "L"
    assert term.printed_flag({"flag": None}) is None


async def test_fhir_explicit_code_with_a_generic_flag_uses_the_code_as_text(world):  # noqa: F811
    db, ids = world
    await db.db.execute(
        "UPDATE clinical_records SET interpretation_code = 'A', metadata_json = "
        "json_set(COALESCE(metadata_json, '{}'), '$.flag_source', 'generic') WHERE id = ?",
        (ids["hgb"],),
    )
    await db.db.commit()
    obs = (await _index(db)).get("Observation", derived_id("clinical_records", ids["hgb"]))
    (interp,) = obs["interpretation"]
    assert interp["coding"][0]["code"] == "A" and interp["text"] == "A"


async def test_a_corrected_value_drops_the_generic_label(db, curated_env):  # noqa: F811
    await tool.index_patient(db, PATIENT_A_UUID)
    await _make_hgb_generic(db)
    (fid,) = sorted(await _fact_ids(db, "HGB"))[:1]
    async with db.db.execute("SELECT value_num FROM clinical_records WHERE id = ?", (fid,)) as cur:
        (value,) = await cur.fetchone()
    out = await db.correct_fact(
        fid, patient_id=PATIENT_A_UUID, agent_user="test", reason="digit", value_num=value + 1
    )
    assert out["status"] == "corrected"
    async with db.db.execute(
        "SELECT flag, metadata_json FROM clinical_records WHERE patient_id = ? AND code = 'HGB' "
        "AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
        (PATIENT_A_UUID,),
    ) as cur:
        flag, meta = await cur.fetchone()
    assert "flag_source" not in json.loads(meta or "{}")
    # no printed range: a corrected value gets no flag, never a generic one
    assert flag is None
