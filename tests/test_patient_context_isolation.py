"""Tests for cross-patient isolation in patient_context (#429, #733).

The original bug (#429): `get_patient_context(patient_slug=<B>)` returned
patient A's data in prod, because `get_context(pid_b)` fell back to the legacy
`_context` global when B wasn't cached, and that global held A's data.

Since #733 the legacy global is gone entirely. These tests lock down:
- explicit pid that's not cached → empty template, never another patient
- no explicit pid and no request pid → ValueError naming patient_slug
- `load_from_db` only ever writes `_contexts[pid]`, never with an empty pid
"""

from __future__ import annotations

import json

import pytest

from oncofiles import patient_context
from oncofiles.patient_middleware import _current_patient_id

# Synthetic patients only.
PID_A = "a0000000-0000-4000-8000-0000000000a1"
PID_B = "b0000000-0000-4000-8000-0000000000b2"


@pytest.fixture
def isolated_contexts():
    """Reset the per-patient cache and the request pid; restore after."""
    original_contexts = dict(patient_context._contexts)
    patient_context._contexts.clear()
    token = _current_patient_id.set("")
    yield
    _current_patient_id.reset(token)
    patient_context._contexts.clear()
    patient_context._contexts.update(original_contexts)


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _RecordingDB:
    """Minimal DB double: returns `rows_by_pid[pid]` and records every query."""

    def __init__(self, rows_by_pid: dict[str, dict]):
        self.rows_by_pid = rows_by_pid
        self.queries: list[tuple[str, tuple]] = []

    def execute(self, query, params=()):
        self.queries.append((query, params))
        pid = params[0] if params else None
        row = self.rows_by_pid.get(pid)
        return _FakeCursor([(json.dumps(row),)] if row else [])


class TestGetContextIsolation:
    def test_explicit_pid_not_cached_returns_empty_template(self, isolated_contexts):
        patient_context._contexts[PID_A] = {"name": "Testa Alpha", "diagnosis": "synthetic A"}

        result = patient_context.get_context(PID_B)

        assert result.get("name") == ""
        assert result.get("diagnosis") == ""

    def test_explicit_pid_cached_returns_correct_patient(self, isolated_contexts):
        patient_context._contexts[PID_A] = {"name": "Testa Alpha"}
        patient_context._contexts[PID_B] = {"name": "Testa Beta"}

        assert patient_context.get_context(PID_A).get("name") == "Testa Alpha"
        assert patient_context.get_context(PID_B).get("name") == "Testa Beta"

    @pytest.mark.parametrize("pid", [None, ""])
    def test_no_pid_and_no_request_pid_raises(self, isolated_contexts, pid):
        # Even with patients cached, a pid-less read must not pick one of them.
        patient_context._contexts[PID_A] = {"name": "Testa Alpha"}
        with pytest.raises(ValueError, match="patient_slug"):
            patient_context.get_context(pid)

    @pytest.mark.parametrize(
        "fn",
        [
            patient_context.get_patient_name,
            patient_context.get_medical_record_name,
            patient_context.format_context_text,
        ],
    )
    def test_no_pid_readers_raise(self, isolated_contexts, fn):
        patient_context._contexts[PID_A] = {"name": "Testa Alpha"}
        with pytest.raises(ValueError, match="patient_slug"):
            fn()

    def test_request_pid_is_used_when_no_explicit_pid(self, isolated_contexts):
        patient_context._contexts[PID_A] = {"name": "Testa Alpha"}
        patient_context._contexts[PID_B] = {"name": "Testa Beta"}
        token = _current_patient_id.set(PID_B)
        try:
            assert patient_context.get_patient_name() == "Testa Beta"
            # An explicit pid always wins over the request pid.
            assert patient_context.get_patient_name(PID_A) == "Testa Alpha"
        finally:
            _current_patient_id.reset(token)

    def test_get_patient_name_respects_isolation(self, isolated_contexts):
        patient_context._contexts[PID_A] = {"name": "Testa Alpha"}

        assert patient_context.get_patient_name(PID_A) == "Testa Alpha"
        unknown_pid = "99999999-9999-4999-8999-999999999999"
        assert patient_context.get_patient_name(unknown_pid) == ""

    def test_get_medical_record_name_respects_isolation(self, isolated_contexts):
        patient_context._contexts[PID_A] = {
            "name": "Testa Alpha",
            "medical_record_name": "Chartova",
        }
        patient_context._contexts[PID_B] = {"name": "Testa Beta"}

        assert patient_context.get_medical_record_name(PID_A) == "Chartova"
        assert patient_context.get_medical_record_name(PID_B) == ""


class TestLoadFromDbIsolation:
    async def test_two_patients_load_into_separate_slots(self, isolated_contexts):
        db = _RecordingDB({PID_A: {"name": "Testa Alpha"}, PID_B: {"name": "Testa Beta"}})

        a = await patient_context.load_from_db(db, PID_A)
        b = await patient_context.load_from_db(db, PID_B)

        assert a["name"] == "Testa Alpha"
        assert b["name"] == "Testa Beta"
        assert patient_context.get_patient_name(PID_A) == "Testa Alpha"
        assert patient_context.get_patient_name(PID_B) == "Testa Beta"
        assert set(patient_context._contexts) == {PID_A, PID_B}
        # Every query is per-patient; the legacy `id = 1` row is never read.
        assert all("patient_id = ?" in q for q, _ in db.queries)
        assert not any("id = 1" in q for q, _ in db.queries)

    @pytest.mark.parametrize("pid", [None, ""])
    async def test_empty_pid_raises_without_querying(self, isolated_contexts, pid):
        """#476/#733: an empty pid must never read some other row."""
        db = _RecordingDB({"": {"name": "Testa Alpha"}})

        with pytest.raises(ValueError, match="patient_slug"):
            await patient_context.load_from_db(db, pid)

        assert db.queries == []
        assert patient_context._contexts == {}
