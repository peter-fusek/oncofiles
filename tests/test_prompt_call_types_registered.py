"""Guardrail: every call_type string used by any analyze_* helper must be
registered in PromptCallType. Without this, a new helper ships green tests
(which mock the AI client) but crashes on first real use with a Pydantic
validation error — see the f6b4656 post-mortem on #460."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from oncofiles.models import PromptCallType

SOURCE_ROOTS = [
    Path(__file__).resolve().parent.parent / "src" / "oncofiles",
]


def _scan_for_call_types() -> set[str]:
    """Find every literal passed as `call_type=` to log_ai_call across src/."""
    pattern = re.compile(r'call_type\s*=\s*[\'"]([a-z_]+)[\'"]')
    found: set[str] = set()
    for root in SOURCE_ROOTS:
        for path in root.rglob("*.py"):
            try:
                text = path.read_text()
            except Exception:
                continue
            for m in pattern.finditer(text):
                found.add(m.group(1))
    return found


def test_every_call_type_used_in_source_is_registered_in_enum():
    """Prevent drift: if a helper uses `call_type='foo'`, `foo` must be
    present in PromptCallType, otherwise PromptLogEntry validation rejects
    the call at runtime (f6b4656)."""
    used = _scan_for_call_types()
    assert used, "scanner found no call_type literals — refine regex?"
    allowed = {ct.value for ct in PromptCallType}
    missing = sorted(used - allowed)
    assert not missing, (
        f"call_type string(s) in source but missing from PromptCallType enum: "
        f"{missing}. Add to src/oncofiles/models.py::PromptCallType."
    )


@pytest.mark.parametrize("ct", list(PromptCallType))
def test_prompt_call_type_is_plain_string(ct: PromptCallType):
    """PromptCallType values must be plain snake_case strings so they
    serialise cleanly across the Pydantic boundary and into prompt_log."""
    assert isinstance(ct.value, str)
    assert ct.value == ct.value.lower()
    assert " " not in ct.value
