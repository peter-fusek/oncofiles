"""HCT printed as a ratio ("pomer" = L/L), migration 092 (#814).

The only effect on stored facts: HCT facts printed in "pomer" gain a
canonical value (``index_diff``: ``canonical_set {HCT: n}``); values, codes
and verdicts stay. Synthetic data only: invented values and dates (2027+).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from oncofiles import index_diff
from oncofiles.curated import indexer, verifier
from oncofiles.curated.facts_rules import Vocabulary, extract_lab_facts
from oncofiles.database import Database

ROOT = Path(__file__).parent.parent
MIGRATION = ROOT / "migrations" / "092_hct_ratio_unit.sql"
PID = "00000000-0000-4000-8000-0000000000c3"
PAGE = """Odber: 14.04.2027 07:40,
Krvný obraz: Hematokrit [HCT] ...0.401  [pomer] {0.350 - 0.470 } ; \
Hemoglobín [HGB] ...138.0  [g/l] {120.0 - 155.0 } ; \
Hematokrit [HCT] ...40.1  [%] {35.0 - 47.0 }
"""


async def test_pomer_converts_like_the_canonical_ratio(db):
    vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    row = vocab.by_code["HCT"]
    assert row["unit_canonical"] == "L/L"
    assert row["unit_conversions"] == {"%": 0.01, "pomer": 1}
    for unit, value, canonical in (("pomer", 0.401, 0.401), ("l/l", 0.401, 0.401),
                                   ("%", 40.1, 0.401)):  # fmt: skip
        hit, value_canonical, reasons = verifier.vocab_check(vocab, "Hematokrit", unit, value)
        assert hit["code"] == "HCT" and reasons == [], unit
        assert abs(value_canonical - canonical) < 1e-9, unit


async def test_migration_is_idempotent(db):
    before = await db.fact_code_vocabulary()
    await db.db.executescript(MIGRATION.read_text())
    await db.db.commit()
    assert await db.fact_code_vocabulary() == before


def test_index_version_is_bumped():
    assert indexer.INDEX_VERSION >= 21


async def test_index_diff_reports_only_canonical_set_for_hct(tmp_path, monkeypatch):
    import oncofiles.database._base as base

    old_dir = tmp_path / "migrations_old"
    old_dir.mkdir()
    for f in (ROOT / "migrations").glob("*.sql"):
        if f.stem != MIGRATION.stem:
            shutil.copy(f, old_dir / f.name)
    monkeypatch.setattr(base, "MIGRATIONS_DIR", old_dir)
    path = tmp_path / "copy.db"
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    old_vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    await db.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, patient_id, category,"
        " document_date, structured_metadata) VALUES (1, 'gdrive:h1', 'h.pdf', 'h.pdf', ?,"
        " 'labs', '2027-04-14', ?)",
        (PID, json.dumps({"ocr_engine": "qwen-local"})),
    )
    conn.execute(
        "INSERT INTO document_pages (document_id, page_number, extracted_text, model)"
        " VALUES (1, 1, ?, 'curated-sidecar')",
        (PAGE,),
    )
    facts, extractor = extract_lab_facts([PAGE], old_vocab, report_date="2027-04-14")
    for f in facts:
        conn.execute(
            "INSERT INTO clinical_records (patient_id, record_type, source, source_document_id,"
            " page_number, evidence, param, code, unit, value_num, verification, extracted_by)"
            " VALUES (?, 'lab', 'test', 1, ?, ?, ?, ?, ?, ?, ?, ?)",
            (PID, f["page_number"], f["evidence"], f["label"], f["code"], f["unit"],
             f["value_num"], f["verification"], extractor),
        )  # fmt: skip
    conn.commit()
    conn.close()

    report = index_diff.run(path)
    assert report["pending_migrations"] == [MIGRATION.stem]
    assert report["vocabulary"]["codes_with_changed_conversions"] == ["HCT"]
    assert report["vocabulary"]["codes_added"] == []
    rule = report["patients"][PID]["rule"]
    assert rule["old"] == rule["new"] == 3
    assert rule["canonical_set"] == {"HCT": 1}  # only the "pomer" fact
    for key in ("new_facts", "lost", "recoded", "verdict", "canonical_cleared",
                "canonical_changed"):  # fmt: skip
        assert key not in rule, key
