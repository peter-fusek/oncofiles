"""Lock tests for the FUP document cap and DOC_QUERY_LIMIT (#548).

The cap rose 200 -> 500. Separately, 25 "every document for this patient" reads
across the codebase hardcoded ceilings (200 / 500 / 1000 / 10000) that would
silently truncate. The dangerous one is `_cleanup_orphan_ocr`, which *trashes*
any `*_OCR.txt` missing from its view — a truncated read soft-deletes real
sidecars.

Two invariants are pinned here:
  1. DOC_QUERY_LIMIT is decoupled from the cap and monotonic, so lowering the
     cap can never shrink a whole-corpus read over already-existing rows.
  2. No new hardcoded numeric limit creeps back into a list_documents call.
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from oncofiles import config

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"


# ---------------------------------------------------------------------------
# Config invariants
# ---------------------------------------------------------------------------


def test_default_document_cap_is_500():
    """The FUP default is 500 (#548)."""
    assert config.MAX_DOCUMENTS_PER_PATIENT == 500


def test_doc_query_limit_is_at_least_the_cap():
    """A whole-corpus read must be able to return every document the cap allows."""
    assert config.DOC_QUERY_LIMIT >= config.MAX_DOCUMENTS_PER_PATIENT


def test_doc_query_limit_never_below_the_most_generous_legacy_literal():
    """Adopting DOC_QUERY_LIMIT must never *lower* a pre-existing limit.

    The most generous literal in the codebase before #548 was 10000
    (enhance_tools cross-reference passes), so the shared ceiling has to clear it.
    """
    assert config.DOC_QUERY_LIMIT >= 10_000


@pytest.mark.parametrize(
    ("cap_env", "expected_cap", "expected_ceiling"),
    [
        ("500", 500, 10_000),  # default
        ("200", 200, 10_000),  # cap LOWERED -> ceiling must NOT follow it down
        ("0", 0, 10_000),  # 0 = unlimited; LIMIT 0 would return no rows
        ("50000", 50_000, 50_000),  # cap raised above the floor -> ceiling follows up
    ],
)
def test_doc_query_limit_derivation_against_real_config(
    monkeypatch, cap_env, expected_cap, expected_ceiling
):
    """Exercise the real module, not a reimplementation of its arithmetic.

    The `cap_env="200"` case is the actual bug this guards: q1b already holds
    ~296 documents under a cap of 200, so a ceiling that tracked the cap exactly
    would truncate reads over existing rows — and make _cleanup_orphan_ocr trash
    their sidecars.
    """
    monkeypatch.setenv("MAX_DOCUMENTS_PER_PATIENT", cap_env)
    try:
        reloaded = importlib.reload(config)
        assert expected_cap == reloaded.MAX_DOCUMENTS_PER_PATIENT
        assert expected_ceiling == reloaded.DOC_QUERY_LIMIT
        assert reloaded.DOC_QUERY_LIMIT > 0, "LIMIT 0 returns no rows"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


# ---------------------------------------------------------------------------
# Source sweep: no hardcoded whole-corpus limits may reappear
# ---------------------------------------------------------------------------

_NUMERIC_LIMIT_RE = re.compile(r"list_documents\(\s*limit\s*=\s*(\d+)")

# Callers that still hardcode a limit AND are already broken for an unrelated
# reason: they omit the required keyword-only `patient_id` added by #499, so they
# raise TypeError before the limit matters. Tracked separately — fixing them means
# deciding patient scope, which is out of scope for #548. Remove from this
# allowlist as each is fixed; do NOT add new entries.
_KNOWN_BROKEN_UNSCOPED_CALLERS = {
    "resources.py",  # files://catalog MCP resource, registered at server.py:7054
    "migrate_to_turso.py",
    "backfill_gdrive_ids.py",
    "backfill_ocr.py",
}


def _offenders() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        hits = _NUMERIC_LIMIT_RE.findall(path.read_text(encoding="utf-8"))
        if hits:
            found[path.name] = hits
    return found


def test_no_new_hardcoded_document_limits_anywhere_in_src():
    """Every whole-corpus list_documents read must use config.DOC_QUERY_LIMIT."""
    offenders = _offenders()
    unexpected = {f: h for f, h in offenders.items() if f not in _KNOWN_BROKEN_UNSCOPED_CALLERS}
    assert not unexpected, (
        f"Hardcoded numeric list_documents limits found: {unexpected}. "
        "Use config.DOC_QUERY_LIMIT so the read tracks MAX_DOCUMENTS_PER_PATIENT "
        "and can never silently truncate."
    )


def test_known_broken_allowlist_does_not_grow_silently():
    """The allowlist should only ever shrink."""
    offenders = set(_offenders())
    stale = _KNOWN_BROKEN_UNSCOPED_CALLERS - offenders
    assert not stale, (
        f"These files no longer hardcode a limit: {stale}. "
        "Remove them from _KNOWN_BROKEN_UNSCOPED_CALLERS."
    )


@pytest.mark.parametrize(
    "filename",
    ["sync.py", "manifest.py", "hygiene.py", "enhance_tools.py", "export.py", "naming.py"],
)
def test_whole_corpus_modules_reference_doc_query_limit(filename: str):
    """Modules doing whole-corpus reads must actually use the shared ceiling."""
    matches = list(SRC.rglob(filename))
    assert matches, f"{filename} not found under {SRC}"
    source = matches[0].read_text(encoding="utf-8")
    assert "DOC_QUERY_LIMIT" in source, f"{filename} no longer uses DOC_QUERY_LIMIT"


def test_truncation_is_logged_not_silent():
    """list_documents must warn when a whole-corpus read fills its ceiling."""
    source = (SRC / "database" / "_documents.py").read_text(encoding="utf-8")
    assert "DOC_QUERY_LIMIT" in source, "list_documents lost its truncation guard"
    assert "TRUNCATED" in source, "truncation must be logged loudly, not silently"
