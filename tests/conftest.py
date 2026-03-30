"""Shared test fixtures."""

import pytest

from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _set_patient_context():
    """Set patient context name for tests so filename parsing works with test data."""
    from oncofiles import patient_context

    # Save original state
    original_context = dict(patient_context._context)
    import oncofiles.filename_parser as fp

    original_re = dict(fp._cached_patient_re)
    original_contexts = dict(patient_context._contexts)

    # Set test patient name in both legacy global and per-patient cache
    patient_context._context["name"] = "Erika Fusekova"
    patient_context._contexts.clear()
    patient_context._contexts[ERIKA_UUID] = {"name": "Erika Fusekova"}
    # Invalidate cached regex so it rebuilds with the test patient name
    fp._cached_patient_re.clear()

    yield

    # Restore original state
    patient_context._context.clear()
    patient_context._context.update(original_context)
    patient_context._contexts.clear()
    patient_context._contexts.update(original_contexts)
    fp._cached_patient_re.clear()
    fp._cached_patient_re.update(original_re)


@pytest.fixture
async def db():
    """Create an in-memory database for testing."""
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    # Set the middleware context variable so _get_patient_id() returns the UUID
    token = _current_patient_id.set(ERIKA_UUID)
    yield database
    _current_patient_id.reset(token)
    await database.close()
