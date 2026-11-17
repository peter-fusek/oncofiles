"""``python -m oncofiles.index_diff`` — offline expected diff (#814).

A synthetic DB is migrated WITHOUT migration 091 (the "production copy"
before deploy); the tool must report what 091 + verifier v4 change, read the
DB only, never touch the network, and print counts and codes only. A second
part runs every existing synthetic lab fixture through the old and the new
vocabulary / label normalisation and allows only the intended changes.
Synthetic data only: invented labels, values and dates (2027+).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

from oncofiles import index_diff
from oncofiles.curated import verifier
from oncofiles.curated.facts_rules import Vocabulary, extract_lab_facts
from oncofiles.database import Database

ROOT = Path(__file__).parent.parent
NEW_MIGRATION = "091_fact_codes_general_labs"
GENERAL = "00000000-0000-4000-8000-0000000000a1"
ONCO = "00000000-0000-4000-8000-0000000000b2"

GENERAL_PAGE = """Odber: 14.04.2027 07:40,
Lipidy: S-Cholesterol ...4.81  [mmol/l] {2.90 - 5.00 } ; \
S-Triacylglyceroly ...1.21  [mmol/l] {0.45 - 1.70 }
Krvný obraz: Leukocyty [WBC] ...6.12  [10^9/l] {3.80 - 10.70 } ; \
Hematokrit [HCT] ...0.401  [pomer] {0.350 - 0.470 } ; \
Sedimentácia ...11
"""
ONCO_PAGE = """Odber: 03.05.2027 08:15,
Krvný obraz: Leukocyty [WBC] ...5.42  [10^9/l] {3.80 - 10.70 } ; \
Hemoglobín [HGB] ...131.0  [g/l] {120.0 - 155.0 }
Sérum: S-Kreatinín ...61.00  [µmol/l] {42.00 - 79.00 }
"""
SECRET_TEXT = ("4.81", "1.21", "6.12", "0.401", "Hematokrit", "5.42", "131", "61.0", "2027",
               "Cholesterol",
               "Triacylglyceroly", "Leukocyty", "Sedimentácia", "Hemoglobín", "Kreatinín",
               "Stred", "Odber")  # fmt: skip


def _old_migrations(tmp_path: Path) -> Path:
    d = tmp_path / "migrations_old"
    d.mkdir()
    for f in (ROOT / "migrations").glob("*.sql"):
        if f.stem != NEW_MIGRATION:
            shutil.copy(f, d / f.name)
    return d


async def _build_db(tmp_path: Path, monkeypatch) -> Path:
    """A DB migrated up to (not incl.) 091, with one general and one oncology
    patient, their stored sidecar pages and the stored facts the OLD index made."""
    import oncofiles.database._base as base

    monkeypatch.setattr(base, "MIGRATIONS_DIR", _old_migrations(tmp_path))
    path = tmp_path / "copy" / "oncofiles.db"
    path.parent.mkdir()
    db = Database(str(path))
    await db.connect()
    await db.migrate()
    old_vocab = Vocabulary.from_rows(await db.fact_code_vocabulary())
    await db.close()
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO patient_context (patient_id, context_json) VALUES (?, ?)",
        (GENERAL, json.dumps({"sex": "male"})),
    )
    for doc_id, pid, page in ((1, GENERAL, GENERAL_PAGE), (2, ONCO, ONCO_PAGE)):
        meta = {"curated": {"index_version": 19}, "ocr_engine": "qwen-local"}
        conn.execute(
            "INSERT INTO documents (id, file_id, filename, original_filename, patient_id, "
            "category, document_date, structured_metadata) VALUES (?,?,?,?,?,?,?,?)",
            (doc_id, f"gdrive:syn{doc_id}", f"doc{doc_id}.pdf", f"doc{doc_id}.pdf", pid,
             "labs", "2027-04-14", json.dumps(meta)),
        )  # fmt: skip
        conn.execute(
            "INSERT INTO document_pages (document_id, page_number, extracted_text, model) "
            "VALUES (?, 1, ?, 'curated-sidecar')",
            (doc_id, page),
        )
        facts, extractor = extract_lab_facts([page], old_vocab, report_date="2027-04-14")
        for f in facts:  # what the old index stored
            conn.execute(
                "INSERT INTO clinical_records (patient_id, record_type, source, "
                "source_document_id, page_number, evidence, param, code, unit, value_num, "
                "verification, extracted_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, "lab", "test", doc_id, f["page_number"], f["evidence"], f["label"],
                 f["code"], f["unit"], f["value_num"], f["verification"], extractor),
            )  # fmt: skip
    # one stored LLM lab fact the next lab write job would code
    conn.execute(
        "INSERT INTO clinical_records (patient_id, record_type, source, source_document_id, "
        "page_number, evidence, param, code, unit, value_num, verification, extracted_by, "
        "metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (GENERAL, "lab", "test", 1, 1, "Stred.obj.RBC [MCV] 88.1 fL", "Stred.obj.RBC [MCV]",
         None, "fL", 88.1, "unverified", "llm-lab-v10/test",
         json.dumps({"unverified_reasons": ["label_unmapped"]})),
    )  # fmt: skip
    conn.commit()
    conn.close()
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_report_counts_the_intended_change_only(tmp_path, monkeypatch):
    path = await _build_db(tmp_path, monkeypatch)
    before = _digest(path)
    report = index_diff.run(path)
    assert _digest(path) == before  # nothing written
    assert report["pending_migrations"] == [NEW_MIGRATION]
    assert report["migration_statements_skipped"] == {NEW_MIGRATION: 0}
    assert "CHOL" in report["vocabulary"]["codes_added"]
    assert report["vocabulary"]["codes_removed"] == []

    general = report["patients"][GENERAL]
    assert general["rule"]["recoded"] == {"NULL>CHOL": 1, "NULL>TRIG": 1}
    assert general["rule"]["new_facts"] == {"ESR": 1}  # unitless, uncoded before → dropped
    assert "lost" not in general["rule"] and "verdict" not in general["rule"]
    # HCT printed as "pomer" stays unconvertible here (no unit change in 091)
    assert "canonical_set" not in general["rule"]
    assert "canonical_changed" not in general["rule"]
    assert "canonical_cleared" not in general["rule"]
    assert general["baseline_mismatch_documents"] == 0
    assert general["llm_lab_if_rewritten"]["recoded"] == {"NULL>MCV": 1}
    assert general["llm_lab_if_rewritten"]["verdict"] == {"unverified>machine": {"MCV": 1}}

    onco = report["patients"][ONCO]
    assert not index_diff.changed(onco)
    assert onco["rule"]["old"] == onco["rule"]["new"] == 3
    assert onco["baseline_mismatch_documents"] == 0
    assert report["patients_changed"] == [GENERAL]


def _strings(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)
    elif isinstance(obj, str):
        yield obj


async def test_output_is_counts_and_codes_only(tmp_path, monkeypatch):
    path = await _build_db(tmp_path, monkeypatch)
    report = index_diff.run(path)
    text = json.dumps(report, ensure_ascii=False)
    for secret in SECRET_TEXT:
        assert secret not in text, secret
    known = {GENERAL, ONCO, NEW_MIGRATION, "oncofiles.index_diff"}
    for s in _strings(report):
        if s in known:
            continue
        # everything else is a code, a code pair, a verdict pair or a field name
        assert re.fullmatch(r"[A-Za-z0-9_]+(>[A-Za-z0-9_]+)?", s), s
        assert not re.search(r"\d\.\d", s), s
    # numbers are counts or versions: whole numbers, never a lab value
    for n in re.findall(r"\d+(?:\.\d+)?", re.sub(r'"[^"]*"', '""', text)):
        assert "." not in n, n


def _run_cli(args, *, cwd, env=None):
    return subprocess.run(
        [sys.executable, "-m", "oncofiles.index_diff", *args],
        cwd=cwd, env=env if env is not None else {}, capture_output=True, text=True, timeout=120,
    )  # fmt: skip


async def test_cli_runs_with_an_empty_environment(tmp_path, monkeypatch):
    path = await _build_db(tmp_path, monkeypatch)
    before = _digest(path)
    res = _run_cli(["--db", str(path)], cwd=tmp_path)
    assert res.returncode == 0, res.stderr
    report = json.loads(res.stdout)
    assert report["patients_changed"] == [GENERAL]
    assert _digest(path) == before
    stop = _run_cli(["--db", str(path), "--stop-if-changed", GENERAL], cwd=tmp_path)
    assert stop.returncode == 2
    keep = _run_cli(["--db", str(path), "--stop-if-changed", ONCO], cwd=tmp_path)
    assert keep.returncode == 0


def test_cli_rejects_reading_sidecar_files(tmp_path):
    res = _run_cli(["--db", str(tmp_path / "none.db"), "--sidecars", "/some/dir"], cwd=tmp_path)
    assert res.returncode != 0
    assert "invalid choice" in res.stderr


def test_missing_db_is_an_error_and_never_created(tmp_path):
    res = _run_cli(["--db", str(tmp_path / "none.db")], cwd=tmp_path)
    assert res.returncode != 0
    assert not (tmp_path / "none.db").exists()


def test_network_is_blocked_in_the_cli_process(tmp_path):
    code = (
        "import socket, sys\n"
        "from oncofiles import index_diff\n"
        "index_diff.block_network()\n"
        "import oncofiles.curated.verifier, oncofiles.curated.indexer\n"
        "for call in (lambda: socket.create_connection(('127.0.0.1', 9)),\n"
        "             lambda: socket.getaddrinfo('example.org', 443),\n"
        "             lambda: socket.socket().connect(('127.0.0.1', 9))):\n"
        "    try:\n"
        "        call()\n"
        "    except index_diff.NetworkForbiddenError:\n"
        "        continue\n"
        "    sys.exit('network call was not refused')\n"
        "bad = [m for m in ('oncofiles.server', 'oncofiles.tools.curated', 'anthropic',\n"
        "                   'googleapiclient') if m in sys.modules]\n"
        "sys.exit(f'imported {bad}' if bad else 0)\n"
    )
    res = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env={},
                         capture_output=True, text=True, timeout=120)  # fmt: skip
    assert res.returncode == 0, res.stdout + res.stderr


def test_main_blocks_the_network_before_any_import():
    src = inspect.getsource(index_diff.main)
    assert src.index("block_network()") < src.index("run(")
    head = inspect.getsource(index_diff).split("def block_network")[0]
    assert not re.search(r"^\s*(from|import) oncofiles", head, re.M)


def test_db_is_opened_read_only(tmp_path):
    path = tmp_path / "x.db"
    sqlite3.connect(path).execute("CREATE TABLE t (a)").connection.commit()
    conn = index_diff.open_readonly(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO t VALUES (1)")
    conn.close()
    assert "mode=ro" in inspect.getsource(index_diff.open_readonly)


def test_local_engines_match_the_indexer():
    from oncofiles.tools import curated

    assert index_diff.LOCAL_ENGINES == curated._LOCAL_ENGINES


# ── existing lab fixtures: old vs new vocabulary and normalisation ──────

# intended effects of migration 091 / verifier v4 on the existing fixtures
# (label → (old code, new code)); everything else must be identical
EXPECTED_RULE_CHANGES: dict[str, tuple] = {}
EXPECTED_VERIFIER_CHANGES: dict[str, tuple] = {}


async def _vocabs(tmp_path, monkeypatch) -> tuple[Vocabulary, Vocabulary]:
    path = await _build_db(tmp_path, monkeypatch)
    conn = index_diff.open_readonly(path)
    old = index_diff._vocab_rows(conn)
    new, failed = index_diff.vocabulary_after(conn, index_diff.pending_migrations(conn))
    conn.close()
    assert failed == {NEW_MIGRATION: 0}
    return Vocabulary.from_rows(old), Vocabulary.from_rows(new)


def _corpus() -> list[tuple[str, str]]:
    """Every module-level lab text of the rule-parser and verifier tests."""
    import tests.test_clinical_facts as tcf

    out = [(f"test_clinical_facts.{k}", v) for k, v in vars(tcf).items()
           if isinstance(v, str) and len(v) > 40 and re.search(r"\d", v)]  # fmt: skip
    import tests.test_verifier_dot_leaders as tvd

    out.append(("test_verifier_dot_leaders.PAGE", tvd.PAGE))
    out += [(f"dot_leaders.ROWS[{i}]", row[0]) for i, row in enumerate(tvd.ROWS)]
    return out


class Sig(NamedTuple):
    label: str
    code: str | None
    verification: str


def _signature(facts):
    return [Sig(f["label"], f["code"], f["verification"]) for f in facts]


async def test_existing_fixtures_parse_identically_with_the_new_vocabulary(tmp_path, monkeypatch):
    old_v, new_v = await _vocabs(tmp_path, monkeypatch)
    corpus = _corpus()
    assert len(corpus) >= 10
    for name, text in corpus:
        for inline in (False, True):
            kw = {"report_date": "2027-04-14", "inline": inline}
            old_facts, old_name = extract_lab_facts([text], old_v, **kw)
            new_facts, new_name = extract_lab_facts([text], new_v, **kw)
            pairs = zip(_signature(old_facts), _signature(new_facts), strict=False)
            diff = [(a, b) for a, b in pairs if a != b]
            unexpected = [
                (a, b) for a, b in diff
                if EXPECTED_RULE_CHANGES.get(a.label) != (a.code, b.code)
            ]  # fmt: skip
            assert old_name == new_name, name
            assert len(old_facts) == len(new_facts), name
            assert not unexpected, (name, diff)


async def test_existing_fixtures_verify_identically_with_label_normalisation(tmp_path, monkeypatch):
    """Every rule fact of the fixtures, asked as an LLM candidate: the v3
    lookup on the old vocabulary and the v4 lookup on the new one agree on
    code and verdict (bar the explicit list)."""
    old_v, new_v = await _vocabs(tmp_path, monkeypatch)
    checked = 0
    for name, text in _corpus():
        facts, _ = extract_lab_facts([text], new_v, report_date="2027-04-14", inline=True)
        for f in facts:
            raw = f["evidence"]
            value = str(f["value_text"] or "")
            if f["value_num"] is not None:
                m = re.search(r"-?\d+(?:[.,]\d+)?", raw[raw.find(f["label"]) + len(f["label"]) :])
                value = m.group(0) if m else ""
            cand = {"label_as_printed": f["label"], "value": value, "comparator": None,
                    "unit_as_printed": f["unit"], "ref_range_as_printed": None,
                    "flag_as_printed": None, "date_as_printed": None, "date_role": None,
                    "evidence": raw, "page_number": 1}  # fmt: skip
            a = verifier.verify(cand, page_text=text, vocab=old_v, report_date="2027-04-14",
                                normalise_label=False)  # fmt: skip
            b = verifier.verify(cand, page_text=text, vocab=new_v, report_date="2027-04-14")
            checked += 1
            if (a["code"], a["verification"]) != (b["code"], b["verification"]):
                assert EXPECTED_VERIFIER_CHANGES.get(f["label"]) == (a["code"], b["code"]), (
                    name,
                    f["label"],
                    a["code"],
                    b["code"],
                    a["metadata"],
                    b["metadata"],
                )
    assert checked >= 20


def test_scrubbed_env_has_no_secret_dependency():
    # the module reads no environment variable; it only sets its own data dir
    src = inspect.getsource(index_diff)
    assert "os.environ.get" not in src and "getenv" not in src
    assert re.findall(r"os\.environ\[[^\]]+\]", src) == ['os.environ["ONCOFILES_DATA_DIR"]']


def test_cli_runs_from_a_read_only_checkout(tmp_path):
    """config.DATA_DIR is created at import; the CLI points it at a temp dir
    first, so a read-only source tree (no ``data/``) works."""
    import stat

    tree = tmp_path / "checkout"
    shutil.copytree(ROOT / "src" / "oncofiles", tree / "src" / "oncofiles",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    shutil.copytree(ROOT / "migrations", tree / "migrations")
    db = tmp_path / "x.db"
    sqlite3.connect(db).executescript(
        "CREATE TABLE schema_migrations (version TEXT PRIMARY KEY);"
        "CREATE TABLE fact_codes (code TEXT PRIMARY KEY, category TEXT NOT NULL,"
        " name_sk TEXT NOT NULL, name_en TEXT NOT NULL, loinc TEXT, unit_canonical TEXT,"
        " aliases TEXT NOT NULL DEFAULT '[]', unit_conversions TEXT NOT NULL DEFAULT '{}');"
        "CREATE TABLE documents (id INTEGER, patient_id TEXT, category TEXT, document_date TEXT,"
        " structured_metadata TEXT, file_id TEXT, deleted_at TEXT);"
        "CREATE TABLE document_pages (document_id INTEGER, page_number INTEGER,"
        " extracted_text TEXT, model TEXT);"
        "CREATE TABLE clinical_records (id INTEGER, patient_id TEXT, source_document_id INTEGER,"
        " page_number INTEGER, evidence TEXT, param TEXT, code TEXT, unit TEXT, value_num REAL,"
        " verification TEXT, metadata_json TEXT, extracted_by TEXT, deleted_at TEXT);"
    )
    paths = [p for p in tree.rglob("*")] + [tree]
    for p in paths:
        p.chmod(p.stat().st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    try:
        res = subprocess.run(
            [sys.executable, "-c",
             "import sys, oncofiles, oncofiles.index_diff as d\n"
             "assert oncofiles.__file__.startswith(sys.argv[1]), oncofiles.__file__\n"
             "sys.exit(d.main(['--db', sys.argv[2]]))",
             str(tree), str(db)],
            cwd=tmp_path, env={"PYTHONPATH": str(tree / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True, text=True, timeout=120,
        )  # fmt: skip
    finally:
        for p in paths:
            p.chmod(p.stat().st_mode | stat.S_IWUSR)
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["pending_migrations"]
    assert not (tree / "data").exists()


async def test_existing_unit_conversions_are_unchanged(tmp_path, monkeypatch):
    """No existing code gains or changes a unit: same canonical unit, same
    conversion map, and every unit converts to the same value as before."""
    old_v, new_v = await _vocabs(tmp_path, monkeypatch)
    checked = 0
    for code, old in old_v.by_code.items():
        new = new_v.by_code[code]
        assert new["unit_canonical"] == old["unit_canonical"], code
        assert new["unit_conversions"] == (old.get("unit_conversions") or {}), code
        units = [*(old.get("unit_conversions") or {}), old["unit_canonical"] or ""]
        label = code  # every code is its own alias
        for unit in units:
            a = verifier.vocab_check(old_v, label, unit or None, 1.0, normalise=False)
            b = verifier.vocab_check(new_v, label, unit or None, 1.0)
            assert (a[0] or {}).get("code") == (b[0] or {}).get("code"), (code, unit)
            assert a[1] == b[1], (code, unit)
            checked += 1
    assert checked > 50
