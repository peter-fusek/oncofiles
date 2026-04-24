"""Regression lock for #488 — /health must not leak patient identifiers.

Prior behavior returned `folder_404_suspended` and `needs_reauth` as dicts
keyed by patient_id on the unauthenticated liveness probe. That surfaced
active UUIDs + which caregivers were in sync/OAuth pain, enabling
correlation with any other identifier leak (#484 class).
"""

from __future__ import annotations

import inspect


def test_health_source_surfaces_counts_not_ids():
    """Source-level invariant: the health handler must compute counts,
    not per-patient dicts, from the internal telemetry maps."""
    from oncofiles.server import health

    source = inspect.getsource(health)
    # New contract — counts only.
    assert "folder_404_suspended_count" in source
    assert "needs_reauth_count" in source
    # Old contract — must be gone.
    assert 'folder_404_suspended"]' not in source
    assert '"needs_reauth"' not in source
    # The per-patient key-building pattern must not be present.
    assert 'f"{pid}:{svc}"' not in source


def test_health_does_not_iterate_patients_for_response():
    """Per-patient payload shape must be absent; only scalar counts used."""
    from oncofiles.server import health

    source = inspect.getsource(health)
    # Should not build a dict keyed by pid for the response.
    assert "pid: count" not in source
