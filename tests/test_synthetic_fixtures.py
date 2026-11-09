"""Fixtures are synthetic only (maintainer rule, 2026-11-09; see CLAUDE.md).

Lab-like lines in tests must use invented dates in year 2027 or later, so a
line copied from a real document (whose dates are real) fails CI. The value
check against real documents runs locally (pre-commit hook outside the
repo): a list of real values here would itself publish them.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).parent
LAB_LIKE = re.compile(
    r"(Odber|Odobran|Prijat|\[10\^9|ng/ml|µmol|umol|mmol/l|\bg/l\b"
    r"|\.\.\.\d|\| *(WBC|HGB|PLT|CEA)\b)",
    re.I,
)
REAL_ERA_DATE = re.compile(
    r"\b\d{1,2}\.\s?\d{1,2}\.\s?(19\d\d|20[01]\d|202[0-6])\b|\b(19\d\d|20[01]\d|202[0-6])-\d{2}-\d{2}\b"
)


def test_lab_like_fixture_lines_use_synthetic_years():
    offenders = []
    for path in sorted(TESTS.rglob("*")):
        if path.suffix not in {".py", ".md", ".txt", ".json"} or path.name == Path(__file__).name:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if LAB_LIKE.search(line) and REAL_ERA_DATE.search(line):
                offenders.append(f"{path.relative_to(TESTS)}:{n}: {line.strip()[:80]}")
    assert not offenders, "lab-like fixture lines with real-era dates:\n" + "\n".join(offenders)
