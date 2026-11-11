"""Lock tests for #733: no legacy patient global, every caller names its patient.

- `patient_context` holds no module-level mutable patient state except the
  per-pid cache `_contexts`.
- `get_context()` (and every reader built on it) without an explicit pid and
  without a request pid raises ValueError naming patient_slug.
- Every migrated caller passes its own pid: filename parsing/renaming, the
  analysis context block, export_document_package, gmail/calendar keyword
  builders, gmail attachment import and the local import CLI.
- A source sweep fails the build if a pid-less call comes back.

All names, ids and values are synthetic.
"""

from __future__ import annotations

import ast
import json
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from oncofiles import patient_context
from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id
from tests.conftest import ERIKA_UUID

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"

PID_A = ERIKA_UUID  # the request patient bound by the `db` fixture
PID_B = "00000000-0000-4000-8000-0000000007b2"
SLUG_B = "testa-zorvan"
NAME_A = "Testa Quilla"
NAME_B = "Testa Zorvan"


@pytest.fixture
def two_patients():
    """Cache two synthetic patients; restore the cache after."""
    original = dict(patient_context._contexts)
    patient_context._contexts.clear()
    patient_context._contexts[PID_A] = {"name": NAME_A, "note": "synthetic A"}
    patient_context._contexts[PID_B] = {"name": NAME_B, "note": "synthetic B"}
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original)


@pytest.fixture
def no_request_pid():
    token = _current_patient_id.set("")
    yield
    _current_patient_id.reset(token)


# ── Module state ────────────────────────────────────────────────────────────


def test_no_module_level_mutable_patient_state():
    """Only `_contexts` (keyed by pid) may be a mutable container at module level."""
    mutable = (dict, list, set, bytearray)
    offenders = sorted(
        name
        for name, value in vars(patient_context).items()
        if isinstance(value, mutable) and not name.startswith("__") and name != "_contexts"
    )
    assert offenders == [], f"module-level mutable state in patient_context: {offenders}"
    for removed in ("_context", "_DEFAULT_CONTEXT", "load_from_json"):
        assert not hasattr(patient_context, removed), removed


def test_filename_parser_has_no_patient_cache():
    import oncofiles.filename_parser as fp

    assert not hasattr(fp, "_cached_patient_re")


def test_legacy_config_and_env_are_gone():
    import oncofiles.config as config

    assert not hasattr(config, "PATIENT_CONTEXT_PATH")
    source = (SRC / "patient_context.py").read_text()
    assert "PATIENT_NAME" not in source.replace("`PATIENT_NAME`", "")
    # No SQL string literal may address the legacy single row by primary key.
    sql_literals = [
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and "SELECT" in node.value
    ]
    assert sql_literals, "expected the per-patient SELECT"
    assert all("WHERE patient_id = ?" in q for q in sql_literals)


def test_default_template_never_reads_patient_name_env(monkeypatch):
    monkeypatch.setenv("PATIENT_NAME", "Env Synthetic")
    assert patient_context._default_context()["name"] == ""


# ── get_context without a patient ───────────────────────────────────────────


@pytest.mark.parametrize("pid", [None, ""])
def test_get_context_without_any_pid_raises(two_patients, no_request_pid, pid):
    with pytest.raises(ValueError, match="patient_slug"):
        patient_context.get_context(pid)


def test_request_pid_isolates_two_patients(two_patients):
    for pid, name in ((PID_A, NAME_A), (PID_B, NAME_B)):
        token = _current_patient_id.set(pid)
        try:
            assert patient_context.get_patient_name() == name
            assert patient_context.get_context()["name"] == name
        finally:
            _current_patient_id.reset(token)


async def test_initialize_hydrates_two_patients_separately():
    db = Database(":memory:")
    await db.connect()
    await db.migrate()
    original = dict(patient_context._contexts)
    try:
        for pid, slug, name in ((PID_A, "testa-quilla", NAME_A), (PID_B, SLUG_B, NAME_B)):
            await db.db.execute(
                "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
                (pid, slug, name),
            )
            await patient_context.save_to_db(db.db, {"name": name}, patient_id=pid)
        patient_context._contexts.clear()

        assert await patient_context.initialize(db) == 2
        assert patient_context.get_patient_name(PID_A) == NAME_A
        assert patient_context.get_patient_name(PID_B) == NAME_B
    finally:
        patient_context._contexts.clear()
        patient_context._contexts.update(original)
        await db.close()


# ── filename_parser ─────────────────────────────────────────────────────────


def test_filename_helpers_without_patient_raise(two_patients, no_request_pid):
    from oncofiles import filename_parser as fp

    fn = "20270301_TestaZorvan_NOU_Labs_SyntheticPanel.pdf"
    for call in (
        lambda: fp.parse_filename(fn),
        lambda: fp.is_standard_format(fn),
        lambda: fp.is_corrupted_filename(fn),
        lambda: fp.rename_to_standard(fn, patient_id=""),
        lambda: fp.rename_to_bilingual("20270301 TestaZorvan-NOU-Krv.pdf"),
    ):
        with pytest.raises(ValueError, match="patient_slug"):
            call()


def test_filename_helpers_use_the_explicit_patient_not_the_request(two_patients):
    from oncofiles import filename_parser as fp

    token = _current_patient_id.set(PID_A)  # request is patient A …
    try:
        fn_b = "20270301_TestaZorvan_NOU_Labs_SyntheticPanel.pdf"
        # … but every call names patient B explicitly.
        assert fp.is_standard_format(fn_b, patient_id=PID_B)
        assert not fp.is_standard_format(fn_b, patient_id=PID_A)
        parsed = fp.parse_filename(fn_b, patient_id=PID_B)
        assert parsed.institution == "NOU"
        assert parsed.description == "SyntheticPanel"

        renamed = fp.rename_to_standard(
            "20270301_NOU_labs_krvnyObraz.pdf", patient_id=PID_B, institution_override="NOU"
        )
        assert renamed.startswith("20270301_TestaZorvan_NOU_Labs")
        assert NAME_A.replace(" ", "") not in renamed

        bilingual = fp.rename_to_bilingual("20270301 TestaZorvan-NOU-Krv.pdf", patient_id=PID_B)
        assert bilingual.startswith("20270301 TestaZorvan-NOU-")
    finally:
        _current_patient_id.reset(token)


# ── analysis context block ──────────────────────────────────────────────────


def test_patient_context_text_requires_and_uses_pid(two_patients):
    from oncofiles.tools._helpers import _patient_context_text

    assert NAME_B in _patient_context_text(PID_B)
    assert NAME_A not in _patient_context_text(PID_B)
    with pytest.raises(ValueError):
        _patient_context_text("")


def test_analysis_tools_pass_pid_to_context_block():
    source = (SRC / "tools" / "analysis.py").read_text()
    assert "_patient_context_text()" not in source
    assert source.count("_patient_context_text(pid)") == 2


# ── export_document_package ─────────────────────────────────────────────────


@pytest.mark.usefixtures("admin_scope")
async def test_export_package_uses_slug_patient_context(db: Database):
    from oncofiles.tools import export as export_tools

    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (PID_B, SLUG_B, NAME_B),
    )
    await db.db.commit()
    await patient_context.save_to_db(db.db, {"name": NAME_A}, patient_id=PID_A)
    await patient_context.save_to_db(db.db, {"name": NAME_B}, patient_id=PID_B)
    patient_context._contexts.pop(PID_B, None)  # cold cache: export must hydrate

    ctx = types.SimpleNamespace(request_context=types.SimpleNamespace(lifespan_context={"db": db}))
    # Request patient is A (db fixture); the slug names B.
    pkg = json.loads(
        await export_tools.export_document_package(ctx, include_timeline=False, patient_slug=SLUG_B)
    )
    assert pkg["patient"]["name"] == NAME_B

    own = json.loads(await export_tools.export_document_package(ctx, include_timeline=False))
    assert own["patient"]["name"] == NAME_A


# ── gmail / calendar keyword builders ──────────────────────────────────────


def test_gmail_query_uses_its_patient(two_patients):
    from oncofiles.gmail_sync import _build_gmail_query

    token = _current_patient_id.set(PID_A)
    try:
        q = _build_gmail_query("2027/01/01", None, patient_id=PID_B)
    finally:
        _current_patient_id.reset(token)
    assert "Zorvan" in q
    assert "Quilla" not in q


def test_gmail_query_requires_patient_kwarg():
    from oncofiles.gmail_sync import _build_gmail_query

    with pytest.raises(TypeError):
        _build_gmail_query("2027/01/01", None)  # type: ignore[call-arg]


def test_calendar_keywords_use_their_patient(two_patients):
    from oncofiles.calendar_sync import _get_medical_keywords

    kws = _get_medical_keywords(PID_B)
    assert "Zorvan" in kws
    assert "Quilla" not in kws


async def test_calendar_classifier_fallback_matches_only_its_patient(two_patients, monkeypatch):
    from oncofiles.calendar_sync import _classify_event_medical

    monkeypatch.setattr("oncofiles.config.CLOUD_AI_ENABLED", False)
    hit, _, _ = await _classify_event_medical("Visit Zorvan", "", "", patient_id=PID_B)
    miss, _, _ = await _classify_event_medical("Visit Quilla", "", "", patient_id=PID_B)
    assert hit is True
    assert miss is False


async def test_gmail_attachment_is_parsed_and_stored_for_its_patient(db: Database, two_patients):
    from oncofiles.gmail_sync import _process_attachment
    from oncofiles.models import EmailEntry

    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (PID_B, SLUG_B, NAME_B),
    )
    await db.db.commit()
    gmail = MagicMock()
    gmail.get_attachment.return_value = b"%PDF-synthetic"
    files = MagicMock()
    files.upload.return_value = types.SimpleNamespace(id="file_synthetic_b")
    entry = EmailEntry(
        id=1,
        gmail_message_id="m1",
        thread_id="t1",
        subject="synthetic",
        sender="lab@example.test",
        date=datetime(2027, 3, 1, tzinfo=UTC),
        linked_document_ids="[]",
        patient_id=PID_B,
    )
    att = {
        "filename": "20270301_TestaZorvan_NOU_Labs_SyntheticPanel.pdf",
        "mime_type": "application/pdf",
        "attachment_id": "a1",
    }
    with patch("oncofiles.webhook.notify_oncoteam"):
        doc = await _process_attachment(db, files, gmail, "m1", att, entry, patient_id=PID_B)

    assert doc is not None
    assert doc.institution == "NOU"  # parsed with B's name, not the request patient A's
    assert await db.count_documents(patient_id=PID_B) == 1
    assert await db.count_documents(patient_id=PID_A) == 0


# ── import_local CLI ────────────────────────────────────────────────────────


async def test_import_local_requires_patient(tmp_path):
    from oncofiles.import_local import import_documents

    with pytest.raises(TypeError):
        await import_documents(tmp_path, dry_run=True)  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        await import_documents(tmp_path, dry_run=True, patient_id="")


# ── Source sweep ────────────────────────────────────────────────────────────

# Readers that resolve "the patient" — a call with no arguments relies on the
# request ContextVar alone. Production code must name the patient explicitly.
_READERS = {
    "get_context",
    "get_patient_name",
    "get_medical_record_name",
    "format_context_text",
    "_patient_context_text",
}
# filename_parser helpers: must get `patient_id=` (or a 2nd positional arg).
_FILENAME_HELPERS = {
    "parse_filename": 2,
    "is_standard_format": 2,
    "is_corrupted_filename": 2,
    "rename_to_standard": 4,
    "rename_to_bilingual": 3,
}
# curated/naming.py defines its own, unrelated `parse_filename(name)`.
_SWEEP_SKIP = {SRC / "curated" / "naming.py"}


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def test_no_pid_less_patient_calls_in_src():
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if path in _SWEEP_SKIP:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            where = f"{path.relative_to(SRC)}:{node.lineno} {name}"
            if name in _READERS and not node.args and not node.keywords:
                offenders.append(where)
            elif name in _FILENAME_HELPERS:
                has_kw = any(k.arg == "patient_id" for k in node.keywords)
                if not has_kw and len(node.args) < _FILENAME_HELPERS[name]:
                    offenders.append(where)
    assert offenders == [], "calls without an explicit patient:\n" + "\n".join(offenders)
