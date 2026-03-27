"""Shared test fixtures."""

import pytest

from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


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
