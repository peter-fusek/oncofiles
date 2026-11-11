"""Masked fingerprint of a verify-only re-read (#712).

Synthetic data only: invented values, names and dates (2027+).
"""

from __future__ import annotations

import json
import re
from unittest.mock import MagicMock

import pytest

from oncofiles.curated import reread_verify as rv
from oncofiles.curated.facts_rules import Vocabulary
from oncofiles.tools import curated as tool


def row(label: str, value: str, rng: str, tail: str = "") -> str:
    """One table row; built from parts so no fixture line carries a full row."""
    return " ".join(x for x in ("@", label, value, rng, tail) if x)


ALT = row("S-ALT", "0,71", "0,20-0,80", "µkat/l")
NA = row("P-NA", "152,0", "136,0-145,0", "mmol/l H")

PAGE = "\n".join(
    [
        "Laboratórium Testovacie, Jana Testova",
        "Odobraté: 14.3.2027 07:40",
        ALT,
        NA,
        "Pacient Novakova Zuzana\tč. 12345 | x;y",
    ]
)


@pytest.fixture
async def vocab(db):
    return Vocabulary.from_rows(await db.fact_code_vocabulary())


async def test_fingerprint_masks_digits_and_unknown_words(vocab):
    fp = rv.fingerprint([PAGE], vocab)
    text = "\n".join(fp["lines"])
    assert set(re.findall(r"\d", text)) <= {"9"}  # every digit is masked to 9
    assert "12345" not in text and "07" not in text
    for secret in ("Novakova", "Zuzana", "Testova", "Jana", "Pacient", "Testovacie"):
        assert secret not in text
    # lab codes and units stay readable, so the row shape is visible
    alt_line = fp["lines"][2]
    assert "ALT" in alt_line and "µkat/l" in alt_line
    assert alt_line == "@ S-ALT 9,99 9,99-9,99 µkat/l"  # "S" is a lab word (S-Glukóza)
    assert fp["line_count"] == 5 and fp["page_line_counts"] == [5]
    assert fp["separators"]["pipes"] == 1 and fp["separators"]["tabs"] == 1
    assert fp["separators"]["semicolons"] == 1


async def test_no_unknown_word_survives_the_mask(vocab):
    known = rv.known_words(vocab)
    page = PAGE + "\nMUDr. Imaginárna Osoba, Ulica 7, Fantazium 3,3"
    lines = rv.fingerprint([page], vocab)["lines"]
    for word in re.findall(r"[^\W\d_]+", "\n".join(lines)):
        assert rv._fold(word) in known or set(word) == {"a"}, word


async def test_fingerprint_is_capped_at_80_lines(vocab):
    fp = rv.fingerprint(["\n".join(f"riadok {i}" for i in range(200))], vocab)
    assert len(fp["lines"]) == 80 and fp["line_count"] == 200 and fp["truncated"]
    assert fp["lines"][0] == "aaaaaa 9"


async def test_fingerprint_needs_a_dry_run(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    out = json.loads(
        await tool.curated_reread_verify(ctx, document_ids=[1], dry_run=False, fingerprint=True)
    )
    assert out == {"error": "fingerprint is only allowed with dry_run=true"}


def test_fingerprints_are_never_persisted():
    report = {"dry_run": True, "documents": [{"document_id": 1, "fingerprint": {"lines": []}}]}
    stored = tool._without_fingerprints(report)
    assert stored == {"dry_run": True, "documents": [{"document_id": 1}]}
    assert "fingerprint" in report["documents"][0]  # the in-memory report keeps it
    assert tool._without_fingerprints(None) == {}
