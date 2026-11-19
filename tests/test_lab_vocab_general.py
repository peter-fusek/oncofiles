"""General-health lab vocabulary + verifier label normalisation (#814, #831).

Migration 091 adds codes and aliases (data); verifier v4 normalises a printed
label generically before the lookup (``label_keys``), drops section headers
and reads a 2-digit-year date only with a 4-digit anchor. Everything here is
invented: labels are generic test names, values and dates (2027+) made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oncofiles.curated import indexer, llm_extract, scorecard, verifier
from oncofiles.curated.facts_rules import Vocabulary, extract_lab_facts

MIGRATION = Path(__file__).parent.parent / "migrations" / "091_fact_codes_general_labs.sql"
DAY = "14.04.2027"
ISO = "2027-04-14"


async def _vocab(db) -> Vocabulary:
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


def _cand(label, value, unit=None, *, evidence=None, date=DAY):
    evidence = evidence or " ".join(x for x in (label, value, unit or "") if x).strip()
    return {
        "label_as_printed": label, "value": value, "comparator": None,
        "unit_as_printed": unit, "ref_range_as_printed": None, "flag_as_printed": None,
        "date_as_printed": date, "date_role": "sample", "evidence": evidence,
        "page_number": 1,
    }  # fmt: skip


def _verify(vocab, cand, *, page=None, report_date=None, births=(), normalise=True):
    page = page if page is not None else f"Odber {DAY}\n{cand['evidence']}"
    return verifier.verify(cand, page_text=page, vocab=vocab, report_date=report_date,
                           birth_dates=births, normalise_label=normalise)  # fmt: skip


# ── every listed label maps to its code through the verifier ────────────

LABELS = [
    # urine sediment, both printed forms
    ("U-SED-Baktérie", "12", "/µl", "USED_BACT"),
    ("U-SED - Baktérie", "12", "/µl", "USED_BACT"),
    ("U-SED-Epitélie ploché", "3", "/µl", "USED_EPI"),
    ("U-SED - Epitélie ploché", "3", "/µl", "USED_EPI"),
    ("U-SED-Erytrocyty", "4", "/µl", "U_ERY"),
    ("U-SED - Erytrocyty", "4", "/µl", "U_ERY"),
    ("U-SED-Hlienové vlákna", "8", "/µl", "USED_MUCUS"),
    ("U-SED - Hlienové vlákna", "8", "/µl", "USED_MUCUS"),
    ("U-SED-Kryštály", "1", "/µl", "USED_CRYST"),
    ("U-SED - Kryštály", "1", "/µl", "USED_CRYST"),
    ("U-SED-Valce", "0", "/µl", "USED_CAST"),
    ("U-SED - Valce", "0", "/µl", "USED_CAST"),
    ("U-SED-Kvasinky", "0", "/µl", "USED_YEAST"),
    ("U-SED - Kvasinky", "0", "/µl", "USED_YEAST"),
    ("U-SED-Malé gulaté epitélie", "2", "/µl", "USED_SMALL_EPI"),
    ("U-SED - Malé guľaté epitélie", "2", "/µl", "USED_SMALL_EPI"),
    ("U-SED-Patologické valce", "0", "/µl", "USED_PCAST"),
    ("U-SED - Patologické valce", "0", "/µl", "USED_PCAST"),
    ("* U-SED - Baktérie", "12", "/µl", "USED_BACT"),
    # sedimentation (Fahraeus-Westergren)
    ("P-FW po 1. hodine", "11", "mm", "ESR"),
    # blood count with bracketed abbreviations
    ("Stred.farebná konc. HGB v RBC [MCHC]", "331", "g/l", "MCHC"),
    ("Stred.hmot.HGB v RBC [MCH]", "29.4", "pg", "MCH"),
    ("Stred.obj.RBC [MCV]", "88.1", "fL", "MCV"),
    ("Šírka distr. RBC [RDW]", "13.1", "%", "RDW"),
    ("Erytrocyty [RBC]", "4.61", "10^12/l", "RBC"),
    ("Hematokrit [HCT]", "0.412", "l/l", "HCT"),
    ("Hemoglobín [HGB]", "141", "g/l", "HGB"),
    ("Leukocyty [WBC]", "6.12", "10^9/l", "WBC"),
    ("Trombocyty [PLT]", "251", "10^9/l", "PLT"),
    ("Stredný objem PLT [MPV]", "10.2", "fL", "MPV"),
    ("Šírka distr. PLT [PDW]", "12.4", "fL", "PDW"),
    ("Šírka distr. PLT [PDW]", "16.1", "%", "PDW_PCT"),
    ("Bazofily [BA]", "0.6", "%", "BASO_PCT"),
    ("Eozinofily [EO]", "2.1", "%", "EOS_PCT"),
    ("Lymfocyty [LY]", "31.2", "%", "LYMPH_PCT"),
    ("Monocyty [MO]", "7.4", "%", "MONO_PCT"),
    ("Neutrofily [NE]", "57.3", "%", "NEUT_PCT"),
    ("Neutrofilie [NE]", "57.3", "%", "NEUT_PCT"),
    ("LUC", "1.4", "%", "LUC"),
    # chemistry
    ("S-Cholesterol", "4.81", "mmol/l", "CHOL"),
    ("* S-Cholesterol", "4.81", "mmol/l", "CHOL"),
    ("S-Triacylglyceroly", "1.21", "mmol/l", "TRIG"),
    ("S-Bilirubín konjugovaný", "3.1", "µmol/l", "BILI_DIRECT"),
    ("GF podľa rovnice CKD EPI", "1.52", "ml/s", "EGFR"),
    ("GF podľ'a rovnice CKD EPI", "1.52", "ml/s", "EGFR"),
    ("P- PT- INR (liečení pacienti)", "2.4", None, "INR"),
]  # fmt: skip


@pytest.mark.parametrize(("label", "value", "unit", "code"), LABELS)
async def test_each_listed_label_maps_through_the_verifier(db, label, value, unit, code):
    f = _verify(await _vocab(db), _cand(label, value, unit))
    assert f["code"] == code, (label, f["metadata"])
    assert f["verification"] == "machine", (label, f["metadata"])
    assert f["value_canonical"] is not None


@pytest.mark.parametrize(
    ("unit", "value", "canonical"),
    [("10^9/l", "6.12", 6.12), ("10^12/l", "4.61", 4.61), ("l/l", "0.412", 0.412)],
)
async def test_count_and_ratio_units_convert(db, unit, value, canonical):
    label = {"10^9/l": "Leukocyty [WBC]", "10^12/l": "Erytrocyty [RBC]",
             "l/l": "Hematokrit [HCT]"}[unit]  # fmt: skip
    f = _verify(await _vocab(db), _cand(label, value, unit))
    assert f["value_canonical"] == canonical
    assert "unit_unconvertible" not in f["metadata"].get("unverified_reasons", [])


async def test_a_sediment_erythrocyte_never_becomes_a_blood_count(db):
    f = _verify(await _vocab(db), _cand("U-SED - Erytrocyty", "4", "/µl"))
    assert f["code"] == "U_ERY"
    assert "Erytrocyty" not in verifier.label_keys("U-SED - Erytrocyty")


async def test_the_v3_lookup_left_the_bracket_labels_unmapped(db):
    vocab = await _vocab(db)
    for label in ("Bazofily [BA]", "Neutrofilie [NE]", "Hemoglobín [HGB]", "Stred.obj.RBC [MCV]"):
        assert verifier.lookup(vocab, label, normalise=False) == (None, False), label
        assert verifier.lookup(vocab, label)[0] is not None, label


def test_label_keys_are_generic():
    assert verifier.label_keys("* S-Cholesterol") == ["S-Cholesterol", "Cholesterol"]
    assert verifier.label_keys("Hemoglobín [HGB]") == ["Hemoglobín [HGB]", "HGB", "Hemoglobín"]
    assert verifier.label_keys("U-SED-Valce")[:2] == ["U-SED-Valce", "USED - Valce"]
    assert verifier.label_keys("P- PT- INR") == ["P- PT- INR", "PT-INR"]
    assert verifier.label_keys("Glukóza") == ["Glukóza"]  # nothing to normalise


@pytest.mark.parametrize(
    ("label", "header"),
    [("* (U-SED)", True), ("(U-SED)", True), ("* Moč | Záver", True), ("*", True),
     ("Hemoglobín [HGB]", False), ("Močovina (urea)", False), ("* S-Cholesterol", False)],
)  # fmt: skip
def test_section_headers(label, header):
    assert verifier.is_section_header(label) is header


def test_section_headers_are_dropped_not_counted_as_unmapped():
    raw = [
        {"label_as_printed": "* (U-SED)", "value": "x", "evidence": "* (U-SED) x"},
        {"label_as_printed": "* Moč | Záver", "value": "neg", "evidence": "* Moč | Záver neg"},
    ]
    assert [llm_extract.normalize_item(r) for r in raw] == [(None, "section_header")] * 2


# ── the rule parsers: new aliases, no garbage ───────────────────────────

GENERAL_BORY = f"""Odber: {DAY} 07:40,
Lipidy: S-Cholesterol ...4.81  [mmol/l] {{2.90 - 5.00 }} ; \
S-Triacylglyceroly ...1.21  [mmol/l] {{0.45 - 1.70 }}
Krvný obraz: Leukocyty [WBC] ...6.12  [10^9/l] {{3.80 - 10.70 }} ; \
Stred.obj.RBC [MCV] ...88.1  [fL] {{80.0 - 97.0 }}
Text: Poznámka ...bez nálezu
"""


async def test_rule_parsers_code_the_new_vocabulary_and_nothing_else(db):
    facts, name = extract_lab_facts([GENERAL_BORY], await _vocab(db))
    assert name == "rule:bory-v1"
    assert [(f["label"], f["code"]) for f in facts] == [
        ("S-Cholesterol", "CHOL"), ("S-Triacylglyceroly", "TRIG"),
        ("Leukocyty", "WBC"), ("Stred.obj.RBC", "MCV"),
    ]  # fmt: skip
    assert all(f["verification"] == "machine" for f in facts)


PROSE = """Lekárska správa zo dňa 14.04.2027
Pacientka bez ťažkostí, FW a TAG kontrola o 3 mesiace, LUC neurčené.
Cholesterol v norme, odporúčaná diéta. Kontrola 6/2027.
"""


@pytest.mark.parametrize("inline", [False, True])
async def test_short_new_aliases_do_not_turn_prose_into_facts(db, inline):
    facts, _ = extract_lab_facts([PROSE], await _vocab(db), report_date=ISO, inline=inline)
    new_codes = {"ESR", "CHOL", "TRIG", "BILI_DIRECT", "LUC", "PDW_PCT", "USED_SMALL_EPI"}
    assert not [f for f in facts if f["code"] in new_codes]


# ── migration ───────────────────────────────────────────────────────────


async def test_migration_is_idempotent(db):
    before = await db.fact_code_vocabulary()
    await db.db.executescript(MIGRATION.read_text())
    await db.db.commit()
    assert await db.fact_code_vocabulary() == before


async def test_migration_only_appends_aliases(db):
    rows = {r["code"]: r for r in await db.fact_code_vocabulary()}
    assert rows["U_ERY"]["aliases"][:3] == ["U- Krv", "U-Krv", "USED - Erytrocyty"]
    assert "unit_conversions =" not in MIGRATION.read_text()  # 091 changes no unit
    for code in ("ESR", "CHOL", "TRIG", "BILI_DIRECT", "LUC", "PDW_PCT", "USED_SMALL_EPI"):
        assert rows[code]["category"] == "lab"


def test_migration_holds_no_patient_data():
    sql = MIGRATION.read_text()
    assert "patients" not in sql and "patient_id" not in sql
    assert ";" not in "".join(line for line in sql.splitlines() if line.startswith("--"))


# ── 2-digit years: anchored, never guessed ──────────────────────────────


async def test_two_digit_year_anchored_by_a_page_date(db):
    page = "Odber 14.04.2027\nS-Cholesterol 4.81 mmol/l 14.04.27 07:40"
    line = "S-Cholesterol 4.81 mmol/l 14.04.27 07:40"
    c = _cand("S-Cholesterol", "4.81", "mmol/l", evidence=line, date="14.04.27 07:40")
    f = _verify(await _vocab(db), c, page=page)
    assert f["occurred_at"] == "2027-04-14T07:40"
    assert f["metadata"]["year_anchored"] == "page"
    assert f["metadata"]["date_source"] == "printed"
    assert f["verification"] == "machine", f["metadata"]


async def test_two_digit_year_anchored_by_the_document_date(db):
    page = "Výsledky 14.04.27\nS-Cholesterol 4.81 mmol/l"
    c = _cand("S-Cholesterol", "4.81", "mmol/l", date="14.04.27")
    f = _verify(await _vocab(db), c, page=page, report_date=ISO)
    assert f["occurred_at"] == ISO
    assert f["metadata"]["year_anchored"] == "document"
    assert "date_unparsed" not in f["metadata"].get("unverified_reasons", [])


@pytest.mark.parametrize(
    ("page", "report_date"),
    [
        ("Odber 14.04.2028\nS-Cholesterol 4.81 mmol/l 14.04.27", None),  # yy differs
        ("S-Cholesterol 4.81 mmol/l 14.04.27", None),  # no anchor at all
        ("S-Cholesterol 4.81 mmol/l 14.04.27", "2028-04-14"),  # document year differs
        ("14.04.2027 a 14.04.2127\nS-Cholesterol 4.81 mmol/l 14.04.27", None),  # ambiguous
        ("Odber 14.04.2127\nS-Cholesterol 4.81 mmol/l 14.04.27", ISO),  # ambiguous
    ],
)
async def test_two_digit_year_without_one_anchor_stays_unparsed(db, page, report_date):
    c = _cand("S-Cholesterol", "4.81", "mmol/l", date="14.04.27")
    f = _verify(await _vocab(db), c, page=page, report_date=report_date)
    assert "date_unparsed" in f["metadata"]["unverified_reasons"]
    assert "year_anchored" not in f["metadata"]
    assert f["occurred_at"] is None


async def test_birth_date_guard_applies_after_anchoring(db):
    page = "Narodený 14.04.2027\nS-Cholesterol 4.81 mmol/l 14.04.27"
    c = _cand("S-Cholesterol", "4.81", "mmol/l", date="14.04.27")
    f = _verify(await _vocab(db), c, page=page, births=[ISO])
    assert f["occurred_at"] == ISO
    assert "birth_date" in f["metadata"]["unverified_reasons"]
    assert f["verification"] == "unverified"


def test_anchor_helper_rejects_other_shapes():
    assert verifier.anchor_two_digit_year("14.04.2027", "14.04.2027", None) is None
    assert verifier.anchor_two_digit_year("31.02.27", "31.02.2027", None) is None


# ── versions ────────────────────────────────────────────────────────────


def test_versions_are_bumped():
    assert verifier.VERIFIER_VERSION == 4
    assert indexer.INDEX_VERSION >= 20
    v = scorecard.versions(extractor=llm_extract.EXTRACTOR_VERSION,
                           verifier=verifier.VERIFIER_VERSION,
                           clinical_extractor="llm-clinical-v1", clinical_verifier=5)  # fmt: skip
    assert v["verifier_version"] == 4
