"""Offline expected diff of a lab vocabulary / verifier change (#814).

Before a change to ``fact_codes`` (a migration) or to the lab verifier ships,
infra runs this on a snapshot COPY of the production DB and compares the
result with what the deployed re-index then does. Command::

    uv run python -m oncofiles.index_diff --db /tmp/x/oncofiles.db

    # exit 2 when the named patient would change at all (a stop signal):
    uv run python -m oncofiles.index_diff --db /tmp/x/oncofiles.db \\
        --stop-if-changed <patient uuid>

It runs from a plain checkout (``uv sync`` + ``uv run``), also a read-only
one: ``config.DATA_DIR`` is pointed at a fresh temp dir before any import
(``ONCOFILES_DATA_DIR``). No environment and no secret is needed (it works
under ``env -i``). Guarantees:

* **Read-only.** The DB is opened through a ``file:…?mode=ro`` URI plus
  ``PRAGMA query_only``; the new vocabulary is built in a private in-memory
  database. Nothing is ever written to the copy.
* **Offline.** No Drive, no model, no HTTP: the socket layer is disabled
  (``block_network``) before any ``oncofiles`` module is imported, so any
  code path that tried to open a connection would fail loudly. The text the
  rule parsers read is the stored sidecar text (``document_pages`` rows with
  model ``curated-sidecar``), exactly what the curated index parsed. Reading
  sidecar files is not supported (``--sidecars`` accepts only ``off``).
* **Counts and codes only.** Output holds patient ids, ``fact_codes`` codes,
  migration names, versions and counts — never a value, a date, a label or
  evidence text.

What it computes, per patient:

* ``rule``: the lab rule parsers (``facts_rules.extract_lab_facts``, the same
  arguments as the curated index) over each curated document's stored text,
  once with the DB's current vocabulary (old) and once with the vocabulary
  after the pending migrations (new). Facts are paired by page, evidence and
  label: ``new`` / ``lost`` per code, ``recoded`` (``OLD>NEW``, ``NULL`` for
  uncoded), ``verdict`` changes (``machine>unverified`` / ``unverified>machine``
  per code) and the canonical value per code: ``canonical_set`` (none → a
  value, a printed unit became convertible), ``canonical_cleared`` and
  ``canonical_changed`` (a different value: a conversion factor changed),
  counted only for facts that keep their code.
  This is what the re-index (``INDEX_VERSION`` bump) does to the rule facts;
  date corroboration afterwards is not modelled.
* ``baseline_mismatch_documents``: documents whose stored rule facts differ
  from the old recomputation (page, evidence, label, code). Non-zero means the
  baseline is not exactly production and the diff is approximate there.
* ``reindex``: the STORED rule facts against the new recomputation (the
  parsers of this checkout with the vocabulary after the pending migrations).
  ``rule`` runs the same parser code on both sides, so a change of the parser
  itself is invisible there; this section is where it shows. Paired like
  ``rule``; per code: ``new_facts`` / ``lost``, ``recoded``, and for facts that
  keep their code ``unit_*``, ``range_*`` (the printed reference range),
  ``flag_*`` and ``canonical_*``, each as ``_set`` (none → a value),
  ``_cleared`` or ``_changed``. ``evidence_extended`` counts facts whose stored
  evidence is the start of the new one (a wrapped line now read whole); they
  are compared as the same fact, not as lost + new. Facts a person verified or corrected are left
  out (the re-index never touches them). ``--stop-if-reindex-changed`` exits 2
  when the named patient has any such difference.
* ``llm_lab_if_rewritten``: stored LLM lab facts looked up again with the new
  verifier (label normalisation) and vocabulary — what the NEXT lab write job
  would decide. The re-index does not touch these rows.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import quote

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"
TOOL_VERSION = 2  # 2: the ``reindex`` section (stored facts vs the new recomputation, #899)
UNCODED = "NULL"
PROTECTED = ("verified", "corrected")  # a person's decision: the re-index keeps these rows
REINDEX_KEYS = (
    "new_facts", "lost", "recoded", "evidence_extended",
    "unit_set", "unit_cleared", "unit_changed",
    "range_set", "range_cleared", "range_changed",
    "flag_set", "flag_cleared", "flag_changed",
    "canonical_set", "canonical_cleared", "canonical_changed",
)  # fmt: skip
# tools.curated._LOCAL_ENGINES (not imported: that module pulls in the server).
# A test keeps the two equal.
LOCAL_ENGINES = ("qwen", "local", "google-export", "utf-8", "pymupdf")
SIDECAR_MODEL = "curated-sidecar"


class NetworkForbiddenError(RuntimeError):
    """index_diff is offline: no socket may be opened."""


def block_network() -> None:
    """Make every outgoing connection and name lookup raise. Called before
    any ``oncofiles`` module is imported, so nothing can reach Drive, LM
    Studio or the internet from this process."""
    import socket

    def _refuse(*_a, **_k):
        raise NetworkForbiddenError("oncofiles.index_diff runs offline: network is disabled")

    socket.socket.connect = _refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = _refuse  # type: ignore[method-assign]
    socket.create_connection = _refuse  # type: ignore[assignment]
    socket.getaddrinfo = _refuse  # type: ignore[assignment]


# ── database (read-only) ────────────────────────────────────────────────


def open_readonly(path: str | Path) -> sqlite3.Connection:
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"no database file at {p}")
    conn = sqlite3.connect(f"file:{quote(str(p))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def _vocab_rows(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for r in conn.execute(
        "SELECT code, category, name_sk, name_en, unit_canonical, aliases, unit_conversions "
        "FROM fact_codes ORDER BY code"
    ):
        d = dict(r)
        d["aliases"] = json.loads(d["aliases"] or "[]")
        d["unit_conversions"] = json.loads(d["unit_conversions"] or "{}")
        rows.append(d)
    return rows


def pending_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR):
    applied = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    return [f for f in sorted(migrations_dir.glob("*.sql")) if f.stem not in applied]


def _apply_script(mem: sqlite3.Connection, sql: str) -> int:
    """The production ``migrate`` semantics on the vocabulary copy: the
    whole script, else statement by statement best-effort. Returns how many
    statements failed (they touch tables the copy does not hold)."""
    try:
        mem.executescript(sql)
        return 0
    except sqlite3.Error:
        failed = 0
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if not stmt or stmt.startswith("--"):
                continue
            try:
                mem.execute(stmt)
                mem.commit()
            except sqlite3.Error:
                failed += 1
        return failed


def vocabulary_after(conn: sqlite3.Connection, migrations: list[Path]):
    """(new vocabulary rows, {migration: failed statements}). A private
    in-memory copy of ``fact_codes`` gets the pending migrations."""
    mem = sqlite3.connect(":memory:")
    mem.row_factory = sqlite3.Row
    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'fact_codes'"
    ).fetchone()
    if ddl is None:
        raise RuntimeError("the database has no fact_codes table")
    mem.execute(ddl[0])
    cols = [r[1] for r in conn.execute("PRAGMA table_info(fact_codes)")]
    marks = ", ".join("?" for _ in cols)
    for r in conn.execute(f"SELECT {', '.join(cols)} FROM fact_codes"):
        mem.execute(f"INSERT INTO fact_codes ({', '.join(cols)}) VALUES ({marks})", tuple(r))
    mem.commit()
    failed = {m.stem: _apply_script(mem, m.read_text()) for m in migrations}
    rows = _vocab_rows(mem)
    mem.close()
    return rows, failed


def _vocab_summary(old: list[dict], new: list[dict]) -> dict:
    o = {r["code"]: r for r in old}
    n = {r["code"]: r for r in new}
    return {
        "codes_added": sorted(set(n) - set(o)),
        "codes_removed": sorted(set(o) - set(n)),
        "codes_with_new_aliases": sorted(
            c for c in set(o) & set(n) if set(n[c]["aliases"]) - set(o[c]["aliases"])
        ),
        "codes_with_changed_conversions": sorted(
            c for c in set(o) & set(n) if n[c]["unit_conversions"] != o[c]["unit_conversions"]
        ),
    }


# ── documents ───────────────────────────────────────────────────────────


def _documents(conn: sqlite3.Connection):
    """Active curated documents with their stored sidecar pages."""
    pages: dict[int, dict[int, str]] = defaultdict(dict)
    for r in conn.execute(
        "SELECT document_id, page_number, extracted_text FROM document_pages WHERE model = ?",
        (SIDECAR_MODEL,),
    ):
        pages[r["document_id"]][int(r["page_number"] or 1)] = r["extracted_text"] or ""
    for r in conn.execute(
        "SELECT id, patient_id, category, document_date, structured_metadata FROM documents "
        "WHERE file_id LIKE 'gdrive:%' AND deleted_at IS NULL ORDER BY id"
    ):
        by_no = pages.get(r["id"], {})
        n = max(by_no, default=0)
        try:
            meta = json.loads(r["structured_metadata"] or "{}")
        except ValueError:
            meta = {}
        yield {
            "id": r["id"],
            "patient_id": r["patient_id"],
            "category": r["category"] or "",
            "day": (r["document_date"] or "")[:10] or None,
            "meta": meta if isinstance(meta, dict) else {},
            "pages": [by_no.get(i, "") for i in range(1, n + 1)],
        }


def _sexes(conn: sqlite3.Connection) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    try:
        rows = conn.execute("SELECT patient_id, context_json FROM patient_context").fetchall()
    except sqlite3.Error:
        return out
    for r in rows:
        try:
            out[r["patient_id"]] = (json.loads(r["context_json"] or "{}") or {}).get("sex")
        except ValueError:
            continue
    return out


def _stored(conn: sqlite3.Connection, like: str) -> dict[int, list[dict]]:
    out: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute(
        "SELECT id, patient_id, source_document_id, page_number, evidence, param, code, unit, "
        "value_num, value_canonical, ref_range_low, ref_range_high, flag, verification, "
        "metadata_json FROM clinical_records "
        "WHERE extracted_by LIKE ? AND deleted_at IS NULL",
        (like,),
    ):
        out[r["source_document_id"]].append(dict(r))
    return out


# ── diff ────────────────────────────────────────────────────────────────


def _key(page, evidence, label) -> tuple:
    return (int(page or 1), (evidence or "").strip()[:300], (label or "").strip())


def _pair(old: list[dict], new: list[dict]):
    """(pairs, old only, new only) — paired by page, evidence and label, in
    parser order within equal keys."""
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for f in new:
        by_key[_key(f["page_number"], f["evidence"], f["label"])].append(f)
    pairs, lost = [], []
    for f in old:
        bucket = by_key.get(_key(f["page_number"], f["evidence"], f["label"]))
        if bucket:
            pairs.append((f, bucket.pop(0)))
        else:
            lost.append(f)
    added = [f for bucket in by_key.values() for f in bucket]
    return pairs, lost, added


def _code(f: dict) -> str:
    return f.get("code") or UNCODED


def _canon_change(a, b) -> str | None:
    """``canonical_set`` (none → a value: a unit became convertible),
    ``canonical_cleared`` or ``canonical_changed`` (a different value)."""
    if a is None and b is None:
        return None
    if a is None:
        return "canonical_set"
    if b is None:
        return "canonical_cleared"
    return "canonical_changed" if abs(float(a) - float(b)) > 1e-9 else None


def _empty_patient() -> dict:
    return {
        "documents": 0, "documents_with_text": 0, "extractor_changed_documents": 0,
        "baseline_mismatch_documents": 0,
        "rule": {"old": 0, "new": 0, "new_facts": Counter(), "lost": Counter(),
                 "recoded": Counter(), "verdict": defaultdict(Counter),
                 "canonical_set": Counter(), "canonical_cleared": Counter(),
                 "canonical_changed": Counter()},
        "reindex": {"stored": 0, "new": 0, "protected": 0, **{k: Counter() for k in REINDEX_KEYS}},
        "llm_lab_if_rewritten": {"facts": 0, "recoded": Counter(),
                                 "verdict": defaultdict(Counter)},
    }  # fmt: skip


def _field_change(name: str, a, b) -> str | None:
    """``<name>_set`` / ``_cleared`` / ``_changed`` for one stored value ``a``
    against the recomputed ``b``; numbers compare with a tolerance."""
    a = None if a in ("", None) else a
    b = None if b in ("", None) else b
    if a is None and b is None:
        return None
    if a is None:
        return f"{name}_set"
    if b is None:
        return f"{name}_cleared"
    if isinstance(a, int | float) and isinstance(b, int | float):
        return f"{name}_changed" if abs(float(a) - float(b)) > 1e-9 else None
    return f"{name}_changed" if str(a).strip() != str(b).strip() else None


def _reindex_diff(acc: dict, stored: list[dict], new: list[dict]) -> None:
    """What the re-index does to the STORED rule facts of one document."""
    out = acc["reindex"]
    protected = [r for r in stored if r["verification"] in PROTECTED]
    out["protected"] += len(protected)
    rows = [{**r, "label": r["param"]} for r in stored if r["verification"] not in PROTECTED]
    out["stored"] += len(rows)
    out["new"] += len(new)
    pairs, lost, added = _pair(rows, new)
    # A parser that now reads a wrapped item whole stores a longer evidence
    # (#907): the stored text is the start of the new one. Same page and label.
    for old_fact in list(lost):
        head = " ".join((old_fact["evidence"] or "").split())
        twin = next(
            (
                f for f in added
                if head
                and int(f["page_number"] or 1) == int(old_fact["page_number"] or 1)
                and (f["label"] or "").strip() == (old_fact["label"] or "").strip()
                and "".join((f["evidence"] or "").split()).startswith("".join(head.split()))
            ),
            None,
        )  # fmt: skip
        if twin is not None:
            lost.remove(old_fact)
            added.remove(twin)
            pairs.append((old_fact, twin))
            out["evidence_extended"][_code(twin)] += 1
    # a recomputed fact that only repeats a protected row is not a new fact
    held = Counter(_key(r["page_number"], r["evidence"], r["param"]) for r in protected)
    for f in lost:
        out["lost"][_code(f)] += 1
    for f in added:
        key = _key(f["page_number"], f["evidence"], f["label"])
        if held[key]:
            held[key] -= 1
            continue
        out["new_facts"][_code(f)] += 1
    for a, b in pairs:
        if _code(a) != _code(b):
            out["recoded"][f"{_code(a)}>{_code(b)}"] += 1
            continue
        code = _code(b)
        if kind := _field_change("unit", a.get("unit"), b.get("unit")):
            out[kind][code] += 1
        lows = _field_change("range", a.get("ref_range_low"), b.get("ref_low"))
        highs = _field_change("range", a.get("ref_range_high"), b.get("ref_high"))
        if kinds := {k for k in (lows, highs) if k}:
            # one count per fact: a range that gains one bound and changes the other "changed"
            out[kinds.pop() if len(kinds) == 1 else "range_changed"][code] += 1
        if kind := _field_change("flag", a.get("flag"), b.get("flag")):
            out[kind][code] += 1
        stored_canon = a.get("value_canonical")
        if kind := _field_change("canonical", stored_canon, b.get("value_canonical")):
            out[kind][code] += 1


def _rule_diff(acc: dict, old: list[dict], new: list[dict]) -> None:
    rule = acc["rule"]
    rule["old"] += len(old)
    rule["new"] += len(new)
    pairs, lost, added = _pair(old, new)
    for f in lost:
        rule["lost"][_code(f)] += 1
    for f in added:
        rule["new_facts"][_code(f)] += 1
    for a, b in pairs:
        if _code(a) != _code(b):
            rule["recoded"][f"{_code(a)}>{_code(b)}"] += 1
        if a["verification"] != b["verification"]:
            rule["verdict"][f"{a['verification']}>{b['verification']}"][_code(b)] += 1
        same_code = _code(a) == _code(b)  # a re-coded fact is counted under ``recoded``
        if same_code and (kind := _canon_change(a.get("value_canonical"),
                                                b.get("value_canonical"))):  # fmt: skip
            rule[kind][_code(b)] += 1


def _baseline_matches(stored: list[dict], old: list[dict]) -> bool:
    def sig(rows, label_key):
        return Counter(
            (*_key(r["page_number"], r["evidence"], r[label_key]), r.get("code") or UNCODED)
            for r in rows
        )

    return sig(stored, "param") == sig(old, "label")


def _llm_diff(acc: dict, rows: list[dict], old_vocab, new_vocab) -> None:
    from oncofiles.curated import verifier

    out = acc["llm_lab_if_rewritten"]
    for r in rows:
        out["facts"] += 1
        label, unit, value = r["param"] or "", r["unit"], r["value_num"]
        old_row, _, old_reasons = verifier.vocab_check(old_vocab, label, unit, value,
                                                       normalise=False)  # fmt: skip
        new_row, _, new_reasons = verifier.vocab_check(new_vocab, label, unit, value)
        old_code = (old_row or {}).get("code") or UNCODED
        new_code = (new_row or {}).get("code") or UNCODED
        if old_code != new_code:
            out["recoded"][f"{old_code}>{new_code}"] += 1
        try:
            meta = json.loads(r["metadata_json"] or "{}")
        except ValueError:
            meta = {}
        stored = set((meta or {}).get("unverified_reasons") or ())
        other = stored - set(verifier.VOCAB_REASONS)
        before = "unverified" if other | set(old_reasons) else "machine"
        after = "unverified" if other | set(new_reasons) else "machine"
        if before != after:
            out["verdict"][f"{before}>{after}"][new_code] += 1


def _plain(obj):
    if isinstance(obj, dict):
        return {
            k: _plain(v)
            for k, v in sorted(obj.items())
            if not (isinstance(v, Counter | dict) and not v)
        }
    return obj


def changed(patient: dict) -> bool:
    """Whether a patient's rule facts or verdicts would change at all."""
    rule = patient.get("rule") or {}
    return any(rule.get(k) for k in ("new_facts", "lost", "recoded", "verdict", "canonical_set",
                                     "canonical_cleared", "canonical_changed"))  # fmt: skip


def reindex_changed(patient: dict) -> bool:
    """Whether the re-index would change a stored rule fact of this patient
    (also through a change of the parser code itself)."""
    section = patient.get("reindex") or {}
    return any(section.get(k) for k in REINDEX_KEYS)


def run(db_path: str | Path, migrations_dir: Path = MIGRATIONS_DIR) -> dict:
    """The whole diff as a JSON-ready dict (counts and codes only)."""
    from oncofiles.curated import verifier
    from oncofiles.curated.facts_rules import INLINE_CATEGORIES, Vocabulary, extract_lab_facts
    from oncofiles.curated.indexer import INDEX_VERSION

    conn = open_readonly(db_path)
    try:
        old_rows = _vocab_rows(conn)
        pending = pending_migrations(conn, migrations_dir)
        new_rows, failed = vocabulary_after(conn, pending)
        old_vocab, new_vocab = Vocabulary.from_rows(old_rows), Vocabulary.from_rows(new_rows)
        sexes = _sexes(conn)
        stored_rule = _stored(conn, "rule:%")
        stored_llm = _stored(conn, "llm-lab%")
        patients: dict[str, dict] = defaultdict(_empty_patient)
        for doc in _documents(conn):
            acc = patients[doc["patient_id"]]
            acc["documents"] += 1
            if not any(p.strip() for p in doc["pages"]):
                continue
            acc["documents_with_text"] += 1
            meta = doc["meta"]
            engine = str(meta.get("ocr_engine") or "").lower()
            kwargs = {
                "unreliable": meta.get("handwritten") is True
                or not any(k in engine for k in LOCAL_ENGINES),
                "report_date": doc["day"],
                "inline": doc["category"] in INLINE_CATEGORIES,
                "sex": sexes.get(doc["patient_id"]),
            }
            old, old_x = extract_lab_facts(doc["pages"], old_vocab, **kwargs)
            new, new_x = extract_lab_facts(doc["pages"], new_vocab, **kwargs)
            if old_x != new_x:
                acc["extractor_changed_documents"] += 1
            _rule_diff(acc, old, new)
            _reindex_diff(acc, stored_rule.get(doc["id"], []), new)
            if not _baseline_matches(stored_rule.get(doc["id"], []), old):
                acc["baseline_mismatch_documents"] += 1
            _llm_diff(acc, stored_llm.get(doc["id"], []), old_vocab, new_vocab)
    finally:
        conn.close()
    report = {
        "tool": "oncofiles.index_diff",
        "tool_version": TOOL_VERSION,
        "index_version": INDEX_VERSION,
        "verifier_version": verifier.VERIFIER_VERSION,
        "pending_migrations": [m.stem for m in pending],
        "migration_statements_skipped": failed,
        "vocabulary": _vocab_summary(old_rows, new_rows),
        "patients": {pid: _plain(acc) for pid, acc in sorted(patients.items())},
    }
    report["patients_changed"] = sorted(p for p, a in report["patients"].items() if changed(a))
    report["patients_reindex_changed"] = sorted(
        p for p, a in report["patients"].items() if reindex_changed(a)
    )
    return report


def private_data_dir() -> str:
    """Point ``config.DATA_DIR`` (created at import) at a fresh temp dir, so
    the tool runs from a read-only checkout and never touches its data dir.
    Must run before any ``oncofiles`` module is imported."""
    import os
    import tempfile

    path = tempfile.mkdtemp(prefix="index_diff_")
    os.environ["ONCOFILES_DATA_DIR"] = path
    return path


def main(argv: list[str] | None = None) -> int:
    block_network()  # before anything below imports an oncofiles module
    private_data_dir()
    ap = argparse.ArgumentParser(
        prog="python -m oncofiles.index_diff", description=__doc__.split("\n")[0]
    )
    ap.add_argument("--db", required=True, help="path to a COPY of the oncofiles SQLite DB")
    ap.add_argument(
        "--sidecars", default="off", choices=["off"],
        help="sidecar text source: only 'off' (the stored document_pages text)",
    )  # fmt: skip
    ap.add_argument(
        "--stop-if-changed", action="append", default=[], metavar="PATIENT_ID",
        help="exit 2 if this patient's rule facts would change at all",
    )  # fmt: skip
    ap.add_argument(
        "--stop-if-reindex-changed", action="append", default=[], metavar="PATIENT_ID",
        help="exit 2 if the re-index would change any stored rule fact of this patient "
        "(section 'reindex': also catches a parser code change)",
    )  # fmt: skip
    args = ap.parse_args(argv)
    report = run(args.db)
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if set(args.stop_if_changed) & set(report["patients_changed"]):
        return 2
    if set(args.stop_if_reindex_changed) & set(report["patients_reindex_changed"]):
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
