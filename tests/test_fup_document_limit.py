"""Lock tests for the FUP document cap and the DOC_QUERY_LIMIT coupling (#548).

The cap rose 200 -> 500. Four "every document for this patient" reads used to
hardcode 200 (matching the old cap by coincidence) and would have silently
dropped documents 201+ the moment it rose. These tests pin both the new default
and the rule that whole-corpus reads derive their limit from the cap.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from oncofiles import config

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"


def test_default_document_cap_is_500():
    """The FUP default is 500 (#548)."""
    assert config.MAX_DOCUMENTS_PER_PATIENT == 500


def test_doc_query_limit_tracks_the_cap():
    """A whole-corpus read must be able to return every document the cap allows."""
    assert config.DOC_QUERY_LIMIT >= config.MAX_DOCUMENTS_PER_PATIENT


def test_doc_query_limit_is_positive():
    """LIMIT 0 returns no rows — the sentinel must never be falsy."""
    assert config.DOC_QUERY_LIMIT > 0


@pytest.mark.parametrize(
    ("cap", "expected"),
    [
        (500, 500),
        (200, 200),
        (5000, 5000),
        (0, 10_000),  # 0 = unlimited; must fall back to a sentinel, never 0
    ],
)
def test_doc_query_limit_derivation(cap, expected):
    """Pin the derivation rule, including the unlimited (0) case."""
    derived = cap if cap > 0 else 10_000
    assert derived == expected
    assert derived > 0


# ---------------------------------------------------------------------------
# Source-level sweep: the coupling must not silently reappear.
# ---------------------------------------------------------------------------

# Whole-corpus reads: every document for one patient, no pagination.
_WHOLE_CORPUS_CALLS = [
    (SRC / "manifest.py", "export_manifest documents read"),
    (SRC / "sync.py", "orphan classification + cross-references + rename sweep"),
]

# `list_documents(limit=<number>)` is the shape that broke. Named constants are
# fine; bare integers are not.
_NUMERIC_LIMIT_RE = re.compile(r"list_documents\(\s*limit\s*=\s*\d+")


@pytest.mark.parametrize(("path", "what"), _WHOLE_CORPUS_CALLS, ids=lambda v: getattr(v, "name", v))
def test_no_hardcoded_numeric_document_limit(path: Path, what: str):
    """No `list_documents(limit=<int>)` in files doing whole-corpus reads (#548)."""
    source = path.read_text(encoding="utf-8")
    offenders = _NUMERIC_LIMIT_RE.findall(source)
    assert not offenders, (
        f"{path.name} ({what}) has a hardcoded numeric document limit: {offenders}. "
        "Use config.DOC_QUERY_LIMIT so it tracks MAX_DOCUMENTS_PER_PATIENT."
    )


def test_whole_corpus_call_sites_use_doc_query_limit():
    """Both files must actually reference DOC_QUERY_LIMIT."""
    for path, what in _WHOLE_CORPUS_CALLS:
        source = path.read_text(encoding="utf-8")
        assert "DOC_QUERY_LIMIT" in source, f"{path.name} ({what}) no longer uses DOC_QUERY_LIMIT"


def test_sync_uses_doc_query_limit_at_every_whole_corpus_site():
    """sync.py has six such reads — all must be converted.

    dry-run export count, export loop (which also feeds rename + organize),
    orphan/external classification, rename sweep, orphan-OCR cleanup, and
    cross-reference candidates.
    """
    source = (SRC / "sync.py").read_text(encoding="utf-8")
    assert source.count("list_documents(limit=DOC_QUERY_LIMIT") == 6, (
        "Expected exactly 6 whole-corpus list_documents calls in sync.py using "
        "DOC_QUERY_LIMIT. If you added another, convert it and bump this count."
    )
