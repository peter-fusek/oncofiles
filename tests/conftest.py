"""Shared test fixtures."""

import pytest

from erika_files_mcp.database import Database


@pytest.fixture
async def db():
    """Create an in-memory database for testing."""
    database = Database(":memory:")
    await database.connect()
    await database.migrate()
    yield database
    await database.close()
