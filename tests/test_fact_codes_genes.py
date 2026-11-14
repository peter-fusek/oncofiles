"""Gene and marker vocabulary in ``fact_codes`` (migration 087).

Gene symbols and marker names are vocabulary data for the clinical biomarker
verifier only. The lab rule parsers (and the lab LLM verifier) read the lab
side of the Vocabulary, which never holds a ``genetic_variant`` / ``biomarker``
row, so a short symbol such as MET, KIT or RET can never become a lab fact.

Synthetic data only: invented lines and dates (2028).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from oncofiles.curated import verifier
from oncofiles.curated import verifier_clinical as vc
from oncofiles.curated.facts_rules import (
    BIOMARKER_CATEGORIES,
    Vocabulary,
    extract_lab_facts,
)

MIGRATION = Path(__file__).parent.parent / "migrations" / "087_fact_codes_genes_markers.sql"
DAY = "2028-03-03"


async def _rows(db) -> list[dict]:
    return await db.fact_code_vocabulary()


async def _vocab(db) -> Vocabulary:
    return Vocabulary.from_rows(await _rows(db))


# ── seeded data ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("code", "category"),
    [
        ("PIK3CA", "genetic_variant"), ("TP53", "genetic_variant"), ("MLH1", "genetic_variant"),
        ("BRCA2", "genetic_variant"), ("NTRK3", "genetic_variant"), ("MET", "genetic_variant"),
        ("KIT", "genetic_variant"), ("EGFR_GENE", "genetic_variant"), ("RNF43", "genetic_variant"),
        ("TMB", "biomarker"), ("PDL1", "biomarker"), ("KI67", "biomarker"),
        ("MSI", "biomarker"), ("HER2", "biomarker"),
        ("EGFR", "lab"),  # eGFR keeps its code, the gene is EGFR_GENE
    ],
)  # fmt: skip
async def test_codes_are_seeded_with_their_category(db, code, category):
    row = next(r for r in await _rows(db) if r["code"] == code)
    assert row["category"] == category


async def test_migration_is_idempotent(db):
    before = await _rows(db)
    await db.db.executescript(MIGRATION.read_text())
    await db.db.commit()
    assert await _rows(db) == before


async def test_existing_aliases_are_kept_and_new_ones_appended(db):
    rows = {r["code"]: r for r in await _rows(db)}
    her = rows["HER2"]["aliases"]
    seeded = ["HER2", "ERBB2"]  # from migration 072, kept first and in order
    assert her[: len(seeded)] == seeded
    for alias in ("Her-2/neu", "HER2/neu"):
        assert alias in her
    for alias in ("HER2 IHC", "HER2 ISH"):
        assert alias in her
    msi = rows["MSI"]["aliases"]
    assert msi[:4] == ["MSI", "MMR", "dMMR", "pMMR"]
    assert {"MSS", "MSI-H"} <= set(msi)
    for r in rows.values():
        if r["category"] not in BIOMARKER_CATEGORIES:
            continue  # lab rows are not touched here
        assert len(r["aliases"]) == len(set(r["aliases"])), r["code"]


async def test_no_alias_is_shared_between_two_biomarker_codes(db):
    from oncofiles.curated.facts_rules import norm_label

    seen: dict[str, str] = {}
    for r in await _rows(db):
        if r["category"] not in BIOMARKER_CATEGORIES:
            continue
        for alias in [r["code"], *r["aliases"]]:
            key = norm_label(alias)
            assert seen.setdefault(key, r["code"]) == r["code"], (alias, seen[key], r["code"])


# ── the clinical verifier maps gene / marker labels ─────────────────────

PAGE = "\n".join([
    "Molekulárne vyšetrenie NGS 3.3.2028",
    "KRAS bez mutácie",
    "ERBB2 bez amplifikácie",
    "Her-2/neu negatívny",
    "MET bez amplifikácie",
    "EGFR bez mutácie",
    "TMB nízka",
    "MSS",
])  # fmt: skip
LINES = PAGE.splitlines()


def _marker(marker: str, result: str | None) -> dict:
    line = next(ln for ln in LINES if ln.startswith(marker))
    return {"kind": "biomarkers", "marker_as_printed": marker, "variant_as_printed": None,
            "result_as_printed": result, "method_as_printed": None, "date_as_printed": None,
            "evidence": line, "page_number": 1}  # fmt: skip


@pytest.mark.parametrize(
    ("marker", "result", "code", "record_type"),
    [
        ("KRAS", "bez mutácie", "KRAS", "genetic_variant"),
        ("ERBB2", "bez amplifikácie", "HER2", "biomarker"),
        ("Her-2/neu", "negatívny", "HER2", "biomarker"),
        ("MET", "bez amplifikácie", "MET", "genetic_variant"),
        ("EGFR", "bez mutácie", "EGFR_GENE", "genetic_variant"),
        ("TMB", "nízka", "TMB", "biomarker"),
        ("MSS", None, "MSI", "biomarker"),
    ],
)  # fmt: skip
async def test_printed_gene_label_maps_and_can_be_machine(db, marker, result, code, record_type):
    item = vc.verify_item(_marker(marker, result), page_text=PAGE, vocab=await _vocab(db),
                          report_date=DAY, birth_dates=set())  # fmt: skip
    assert item["derived"]["code"] == code
    assert item["derived"]["record_type"] == record_type
    assert item["verification"] == "machine", item["reasons"]


@pytest.mark.parametrize(
    ("spelling", "code"),
    [
        ("HER2", "HER2"),
        ("ERBB2", "HER2"),
        ("Her-2/neu", "HER2"),
        ("HER2/neu", "HER2"),
        ("her2", "HER2"),
        ("HER-2", "HER2"),
        ("c-erbB-2", "HER2"),
        ("MSI", "MSI"),
        ("MSS", "MSI"),
        ("MSI-H", "MSI"),
        ("dMMR", "MSI"),
        ("pMMR", "MSI"),
        ("PD-L1", "PDL1"),
        ("pd-l1", "PDL1"),
        ("Ki-67", "KI67"),
        ("MIB-1", "KI67"),
        ("MET", "MET"),
        ("c-MET", "MET"),
        ("KIT", "KIT"),
        ("c-KIT", "KIT"),
        ("LKB1", "STK11"),
        ("EGFR", "EGFR_GENE"),
        ("egfr", "EGFR_GENE"),
        ("HER1", "EGFR_GENE"),
    ],
)  # fmt: skip
async def test_alias_spellings_map_to_one_code(db, spelling, code):
    row, fuzzy = vc._vocab_row(await _vocab(db), spelling)
    assert row is not None and row["code"] == code and not fuzzy


async def test_a_lab_label_printed_as_marker_is_still_not_a_biomarker(db):
    row, _ = vc._vocab_row(await _vocab(db), "CEA")
    assert row["category"] == "tumor_marker"


async def test_unknown_gene_stays_unmapped(db):
    cand = _marker("KRAS", None)
    cand["marker_as_printed"] = "XYZ9"
    cand["evidence"] = "XYZ9 bez mutácie"
    item = vc.verify_item(cand, page_text="XYZ9 bez mutácie", vocab=await _vocab(db),
                          report_date=DAY, birth_dates=set())  # fmt: skip
    assert "label_unmapped" in item["reasons"]


# ── the lab side never sees genetic / biomarker codes ───────────────────


async def test_lab_side_of_vocabulary_holds_no_biomarker_rows(db):
    vocab = await _vocab(db)
    assert not any(r["category"] in BIOMARKER_CATEGORIES for r in vocab.by_code.values())
    assert not any(r["category"] in BIOMARKER_CATEGORIES for r in vocab.by_alias.values())
    for word in ("met", "kit", "ret", "alk", "kras", "her2", "tmb", "er", "pr", "atm"):
        assert vocab.match(word) is None, word
    assert vocab.match("eGFR")["code"] == "EGFR"  # the lab code still resolves
    assert vocab.match("EGFR")["category"] == "lab"


# Invented lab pages: the gene symbols appear as plain words, as labels next
# to numbers with lab-like units and inside a Bory section. Only the CEA and
# eGFR lines are lab results.
OKB_PAGE = "\n".join([
    "Odobrané: 14.03.2028 08:10   Prijaté: 14.03.2028 09:00",
    "Test                 Hodnota   Jednotky  Ref. medze",
    "CEA v sére           4.2       ug/l      0.0 - 5.0",
    "eGF CKD-EPI          1.61      ml/s      > 1.49         v norme",
    "MET                  2.4       ng/ml     0.0 - 5.0",
    "KIT                  12        U/ml      0 - 30",
    "RET  3",
    "ALK                  0.8       ug/l",
    "Poznámka: MET a KIT vyšetrenie podľa KIT 2 a MET 3 je plánované",
    "KRAS                 1         /ul       0 - 2",
])  # fmt: skip
BORY_PAGE = "\n".join([
    "Odber: 14.03.2028 08:10,",
    "Nádorové markery: CEA ...4.2 [ug/l] {0.0 - 5.0 } ; MET ...2.4 [ng/ml] {0.0 - 5.0 } ; "
    "KIT ...12 [U/ml] {0 - 30 }",
])  # fmt: skip


async def _lab_only_vocab(db) -> Vocabulary:
    rows = [r for r in await _rows(db) if r["category"] not in BIOMARKER_CATEGORIES]
    return Vocabulary.from_rows(rows)


@pytest.mark.parametrize("page", [OKB_PAGE, BORY_PAGE])
@pytest.mark.parametrize("inline", [False, True])
async def test_gene_words_on_a_lab_page_add_no_lab_facts(db, page, inline):
    full = await _vocab(db)
    facts, extractor = extract_lab_facts([page], full, report_date=DAY, inline=inline)
    lab_only, lab_extractor = extract_lab_facts(
        [page], await _lab_only_vocab(db), report_date=DAY, inline=inline
    )
    # identical output with and without the gene vocabulary in the database
    assert (facts, extractor) == (lab_only, lab_extractor)
    codes = {f.get("code") for f in facts}
    assert "CEA" in codes
    biomarker_codes = set(full.biomarker_by_code)
    assert not codes & biomarker_codes
    for f in facts:
        if f["label"].split()[0].upper() in {"MET", "KIT", "RET", "ALK", "KRAS"}:
            assert f.get("code") is None  # never coded as a gene


async def test_gene_only_lines_produce_no_lab_facts(db):
    page = "\n".join([
        "Odobrané: 14.03.2028 08:10   Prijaté: 14.03.2028 09:00",
        "Test                 Hodnota   Jednotky  Ref. medze",
        "Výsledok NGS: KRAS, NRAS, BRAF, MET, KIT, RET, ALK bez zmien",
        "MET  2",
        "KIT 3 položky, RET 1 položka",
        "TMB  4",
    ])  # fmt: skip
    for inline in (False, True):
        facts, _ = extract_lab_facts([page], await _vocab(db), report_date=DAY, inline=inline)
        assert facts == []


async def test_lab_llm_verifier_never_codes_a_gene(db):
    vocab = await _vocab(db)
    for label in ("MET", "KIT", "RET", "KRAS", "HER2", "TMB"):
        row, _ = verifier._code_for(vocab, label, "ng/ml")
        assert row is None, label
    row, _ = verifier._code_for(vocab, "eGFR", "ml/s")
    assert row["code"] == "EGFR"
