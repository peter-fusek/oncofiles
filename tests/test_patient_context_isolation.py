"""Tests for cross-patient isolation in patient_context (#429).

The bug: `get_patient_context(patient_slug='mattias-cesnak')` returned Nora's
data in prod. Root cause was `get_context(mattias_pid)` falling back to the
legacy `_context` global when Mattias wasn't cached — and `_context` held
Nora's data from a prior `load_from_db` call.

These tests lock down the isolation guarantee:
- Explicit pid that's not cached → return default, NEVER legacy global
- `load_from_db(patient_id=X)` populates only `_contexts[X]`, not `_context`
"""

from __future__ import annotations

import pytest

from oncofiles import patient_context

PID_MATTIAS = "faa4ca8e-6791-47de-85c1-87ceb3725b3a"
PID_NORA = "fcd4374c-6bf4-456e-9960-44122d490018"


@pytest.fixture
def isolated_contexts():
    """Reset both caches to a known state; restore after."""
    original_contexts = dict(patient_context._contexts)
    original_context = dict(patient_context._context)
    patient_context._contexts.clear()
    patient_context._context.clear()
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original_contexts)
    patient_context._context.clear()
    patient_context._context.update(original_context)


class TestGetContextIsolation:
    def test_explicit_pid_not_cached_returns_default_not_legacy(self, isolated_contexts):
        # Nora's data leaks into legacy _context (e.g. via earlier load)
        patient_context._context.update({"name": "Nora Antalová", "diagnosis": "BC HR+/HER2-"})

        # Now ask for Mattias (not cached)
        result = patient_context.get_context(PID_MATTIAS)

        # MUST NOT return Nora's data — cross-patient leak (#429)
        assert result.get("name") != "Nora Antalová"
        assert result.get("diagnosis") != "BC HR+/HER2-"
        # Should be the empty default instead
        assert result.get("name") == ""

    def test_explicit_pid_cached_returns_correct_patient(self, isolated_contexts):
        patient_context._contexts[PID_MATTIAS] = {"name": "Mattias Cesnak"}
        patient_context._contexts[PID_NORA] = {"name": "Nora Antalová"}

        assert patient_context.get_context(PID_MATTIAS).get("name") == "Mattias Cesnak"
        assert patient_context.get_context(PID_NORA).get("name") == "Nora Antalová"

    def test_no_pid_legacy_fallback_still_works(self, isolated_contexts):
        # Backward compat: no pid given, legacy _context populated → use it
        patient_context._context.update({"name": "Legacy Patient"})
        result = patient_context.get_context()
        assert result.get("name") == "Legacy Patient"

    def test_no_pid_no_legacy_returns_default(self, isolated_contexts):
        # Nothing loaded anywhere → default
        result = patient_context.get_context()
        assert result.get("name") == ""

    def test_get_patient_name_respects_isolation(self, isolated_contexts):
        # Same leak scenario via the helper
        patient_context._context.update({"name": "Nora Antalová"})
        patient_context._contexts[PID_MATTIAS] = {"name": "Mattias Cesnak"}

        # Explicit Mattias pid → Mattias, not Nora
        assert patient_context.get_patient_name(PID_MATTIAS) == "Mattias Cesnak"

        # Explicit unknown pid → empty, NOT Nora's legacy data
        unknown_pid = "99999999-9999-4999-8999-999999999999"
        assert patient_context.get_patient_name(unknown_pid) == ""

    def test_get_medical_record_name_respects_isolation(self, isolated_contexts):
        patient_context._contexts[PID_MATTIAS] = {
            "name": "Mattias Cesnak",
            "medical_record_name": "Gonsorčíková",
        }
        patient_context._contexts[PID_NORA] = {"name": "Nora Antalová"}

        assert patient_context.get_medical_record_name(PID_MATTIAS) == "Gonsorčíková"
        # Nora has no chart-name distinct from display
        assert patient_context.get_medical_record_name(PID_NORA) == ""


class TestLoadFromDbIsolation:
    """Verify load_from_db doesn't leak into legacy _context when patient_id is given."""

    async def test_load_with_patient_id_doesnt_update_legacy(self, isolated_contexts):
        # Fake minimal DB — a simple object with .execute that yields the right row
        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            async def fetchone(self):
                return self._rows[0] if self._rows else None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeDB:
            def execute(self, query, params=None):
                import json

                return FakeCursor([(json.dumps({"name": "Mattias Cesnak"}),)])

        # Pre-populate legacy with Nora
        patient_context._context.update({"name": "Nora Antalová"})

        # Load Mattias explicitly
        result = await patient_context.load_from_db(FakeDB(), patient_id=PID_MATTIAS)

        # Mattias's context populated correctly
        assert result.get("name") == "Mattias Cesnak"
        assert patient_context._contexts[PID_MATTIAS]["name"] == "Mattias Cesnak"

        # Legacy _context MUST remain Nora — NOT overwritten by Mattias load (#429)
        assert patient_context._context.get("name") == "Nora Antalová"

    async def test_load_without_patient_id_updates_legacy(self, isolated_contexts):
        """Legacy path still works for backward compat."""

        class FakeCursor:
            def __init__(self, rows):
                self._rows = rows

            async def fetchone(self):
                return self._rows[0] if self._rows else None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeDB:
            def execute(self, query, params=None):
                import json

                return FakeCursor([(json.dumps({"name": "Legacy Only"}),)])

        await patient_context.load_from_db(FakeDB(), patient_id=None)
        assert patient_context._context.get("name") == "Legacy Only"

    async def test_empty_string_pid_does_not_read_legacy_row(self, isolated_contexts):
        """#476 defense: explicit empty-string pid MUST NOT silently fall through
        to the legacy id=1 row via the `if patient_id:` falsy check.

        Pre-fix: `load_from_db(db, patient_id='')` → took `else` branch → read
        legacy id=1 row → returned whatever patient was last loaded there.
        Post-fix: `is not None` guard → takes per-patient branch → queries
        `WHERE patient_id = ''` → zero matches → returns {}.
        """
        queries: list[tuple[str, tuple]] = []

        class FakeCursor:
            async def fetchone(self):
                return None  # no match

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

        class FakeDB:
            def execute(self, query, params=()):
                queries.append((query, params))
                return FakeCursor()

        # Pre-populate the legacy id=1 row with Nora's data (the leak vector)
        patient_context._context.update({"name": "Nora Antalová"})

        result = await patient_context.load_from_db(FakeDB(), patient_id="")

        # Empty-string pid must hit the per-patient query, not the legacy one
        assert any("patient_id = ?" in q for q, _ in queries), (
            f"expected per-patient query with patient_id='', got {queries}"
        )
        assert not any("id = 1" in q for q, _ in queries), (
            f"must NOT query legacy id=1 row for empty-string pid, got {queries}"
        )
        # No row matched, so no data loaded
        assert result == {}
        # Legacy cache must remain untouched
        assert patient_context._context.get("name") == "Nora Antalová"
        # And empty-string pid must NOT be cached in _contexts either
        assert "" not in patient_context._contexts
