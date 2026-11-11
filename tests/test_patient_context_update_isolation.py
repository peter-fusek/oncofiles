"""Isolation of the patient-context writer (#713).

Three bugs in `update_context` / `update_patient_context`:
1. Default pollution: a shallow copy + in-place nested `.update()` mutated the
   nested dicts of `_DEFAULT_CONTEXT` on a cold cache.
2. Legacy-global leak: every update was mirrored into the legacy `_context`,
   even with an explicit pid. (#733 removed that global entirely.)
3. No hydrate before merge: on a cold cache the tool saved default + update
   over the patient's stored row.

All names and values are synthetic.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from oncofiles import patient_context
from oncofiles.database import Database
from oncofiles.tools import patient as patient_tools

PID_A = "a0000000-0000-4000-8000-00000000aaaa"
PID_B = "b0000000-0000-4000-8000-00000000bbbb"


@pytest.fixture
def isolated_contexts():
    original_contexts = dict(patient_context._contexts)
    patient_context._contexts.clear()
    yield
    patient_context._contexts.clear()
    patient_context._contexts.update(original_contexts)


@pytest.fixture
async def mem_db():
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


class TestDefaultPollution:
    def test_update_on_cold_cache_never_changes_default(self, isolated_contexts):
        before = patient_context._default_context()
        patient_context.update_context(
            {"treatment": {"regimen": "SYNTH-REGIMEN-A"}, "biomarkers": {"GENE_X": "pos"}},
            patient_id=PID_A,
        )
        assert before == patient_context._default_context()
        assert patient_context._default_context()["treatment"] == {}
        assert patient_context._default_context()["biomarkers"] == {}

    def test_patient_a_update_does_not_reach_patient_b(self, isolated_contexts):
        patient_context.update_context({"treatment": {"regimen": "SYNTH-REGIMEN-A"}}, PID_A)
        patient_context.update_context({"metastases": ["synthetic-site"]}, PID_A)

        ctx_b = patient_context.get_context(PID_B)  # cold → default
        assert ctx_b["treatment"] == {}
        assert ctx_b["metastases"] == []

        patient_context.update_context({"name": "Testa Beta"}, PID_B)
        ctx_b = patient_context.get_context(PID_B)
        assert ctx_b["treatment"] == {}
        assert ctx_b["name"] == "Testa Beta"
        assert patient_context.get_context(PID_A)["treatment"] == {"regimen": "SYNTH-REGIMEN-A"}
        assert patient_context.get_context(PID_A).get("name", "") == ""

    def test_default_handout_is_a_deep_copy(self, isolated_contexts):
        ctx = patient_context.get_context(PID_A)
        ctx["treatment"]["regimen"] = "SYNTH-MUTATION"
        ctx["metastases"].append("synthetic-site")
        assert patient_context._default_context()["treatment"] == {}
        assert patient_context._default_context()["metastases"] == []
        assert patient_context.get_context(PID_B)["treatment"] == {}

    def test_cached_nested_dict_not_shared_with_returned_copy(self, isolated_contexts):
        patient_context._contexts[PID_A] = {"name": "Testa Alpha", "treatment": {"cycle": 1}}
        cached_treatment = patient_context._contexts[PID_A]["treatment"]
        updated = patient_context.update_context({"treatment": {"cycle": 2}}, PID_A)
        # The previously cached nested dict is not mutated in place …
        assert cached_treatment == {"cycle": 1}
        # … and mutating the returned dict does not change the cache.
        updated["treatment"]["cycle"] = 99
        assert patient_context.get_context(PID_A)["treatment"] == {"cycle": 2}


class TestNoLegacyGlobal:
    def test_module_has_no_legacy_global(self):
        assert not hasattr(patient_context, "_context")
        assert not hasattr(patient_context, "_DEFAULT_CONTEXT")
        assert not hasattr(patient_context, "load_from_json")

    def test_explicit_pid_update_is_invisible_to_no_pid_reader(self, isolated_contexts):
        patient_context.update_context({"name": "Testa Alpha"}, PID_A)
        # A no-pid reader cannot see patient A: it is refused outright (#733).
        with pytest.raises(ValueError, match="patient_slug"):
            patient_context.get_context()


class TestNoPidUpdateRefused:
    @pytest.mark.parametrize("pid", [None, ""])
    def test_update_without_pid_raises_naming_patient_slug(self, isolated_contexts, pid):
        with pytest.raises(ValueError, match="patient_slug"):
            patient_context.update_context({"note": "synthetic"}, patient_id=pid)
        assert patient_context._contexts == {}

    def test_set_germline_finding_without_pid_raises(self, isolated_contexts):
        with pytest.raises(ValueError, match="patient_slug"):
            patient_context.set_germline_finding("GENEX", "benign")
        assert patient_context._contexts == {}


class TestPidWithoutRowGetsEmptyTemplate:
    def test_new_pid_gets_no_other_patient_data(self, isolated_contexts, monkeypatch):
        patient_context._contexts[PID_A] = {
            "name": "Testa Alpha",
            "diagnosis": "synthetic diagnosis A",
            "treatment": {"regimen": "SYNTH-REGIMEN-A"},
            "biomarkers": {"GENE_X": "pos"},
        }
        # The removed PATIENT_NAME env default must not come back for a new pid.
        monkeypatch.setenv("PATIENT_NAME", "Env Default Synthetic")

        ctx = patient_context.get_context(PID_B)

        assert ctx["name"] == ""
        assert ctx["diagnosis"] == ""
        assert ctx["treatment"] == {}
        assert ctx["biomarkers"] == {}
        assert ctx["patient_type"] == "oncology"
        non_empty = {k for k, v in ctx.items() if v}
        assert non_empty == {"patient_type"}
        assert set(ctx) == set(patient_context._default_context())

    def test_request_pid_without_row_gets_empty_template(self, isolated_contexts):
        from oncofiles.patient_middleware import _current_patient_id

        patient_context._contexts[PID_A] = {"name": "Testa Alpha", "diagnosis": "synthetic A"}
        token = _current_patient_id.set(PID_B)
        try:
            ctx = patient_context.get_context()
        finally:
            _current_patient_id.reset(token)
        assert ctx["name"] == ""
        assert ctx["diagnosis"] == ""


class TestHydrateBeforeMerge:
    async def test_cold_cache_update_keeps_stored_keys(self, isolated_contexts, mem_db):
        stored = {
            "name": "Testa Alpha",
            "diagnosis": "synthetic diagnosis",
            "treatment": {"regimen": "SYNTH-REGIMEN-A", "cycle": 2},
        }
        await patient_context.save_to_db(mem_db.db, stored, patient_id=PID_A)
        patient_context._contexts.clear()  # cold cache (e.g. after a restart)

        async def fake_resolve(slug, ctx):
            return PID_A

        fake_ctx = SimpleNamespace()
        mp = pytest.MonkeyPatch()
        mp.setattr(patient_tools, "_resolve_patient_id", fake_resolve)
        mp.setattr(patient_tools, "_get_db", lambda ctx: mem_db)
        try:
            out = await patient_tools.update_patient_context(
                fake_ctx, json.dumps({"treatment": {"cycle": 3}})
            )
        finally:
            mp.undo()
        assert json.loads(out)["status"] == "updated"

        patient_context._contexts.clear()
        reloaded = await patient_context.load_from_db(mem_db.db, patient_id=PID_A)
        assert reloaded["name"] == "Testa Alpha"
        assert reloaded["diagnosis"] == "synthetic diagnosis"
        assert reloaded["treatment"] == {"regimen": "SYNTH-REGIMEN-A", "cycle": 3}

    async def test_cold_cache_update_does_not_touch_other_patient_row(
        self, isolated_contexts, mem_db
    ):
        await patient_context.save_to_db(
            mem_db.db, {"name": "Testa Beta", "diagnosis": "synthetic B"}, patient_id=PID_B
        )
        await patient_context.save_to_db(
            mem_db.db, {"name": "Testa Alpha", "diagnosis": "synthetic A"}, patient_id=PID_A
        )
        patient_context._contexts.clear()

        await patient_context.ensure_loaded(mem_db.db, PID_A)
        updated = patient_context.update_context({"note": "synthetic note"}, PID_A)
        await patient_context.save_to_db(mem_db.db, updated, patient_id=PID_A)

        patient_context._contexts.clear()
        b = await patient_context.load_from_db(mem_db.db, patient_id=PID_B)
        a = await patient_context.load_from_db(mem_db.db, patient_id=PID_A)
        assert b == {"name": "Testa Beta", "diagnosis": "synthetic B"}
        assert a["diagnosis"] == "synthetic A"
        assert a["note"] == "synthetic note"

    async def test_ensure_loaded_propagates_db_errors(self, isolated_contexts):
        class BrokenDB:
            def execute(self, *a, **kw):
                raise RuntimeError("synthetic DB failure")

        with pytest.raises(RuntimeError):
            await patient_context.ensure_loaded(BrokenDB(), PID_A)
        assert PID_A not in patient_context._contexts

    async def test_ensure_loaded_rejects_empty_pid(self, isolated_contexts, mem_db):
        with pytest.raises(ValueError):
            await patient_context.ensure_loaded(mem_db.db, "")
