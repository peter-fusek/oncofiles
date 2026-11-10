"""Shared test fixtures."""

import pytest

from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _reset_anthropic_client():
    """Reset shared Anthropic client singleton so test mocks take effect."""
    import oncofiles.enhance

    oncofiles.enhance._shared_client = None
    yield
    oncofiles.enhance._shared_client = None


@pytest.fixture(autouse=True)
def _cloud_ai_configured(monkeypatch):
    """Tests run as a server that HAS an Anthropic key (#618 guard passes).

    Only ``config.ANTHROPIC_API_KEY`` is set — the value the guard reads at
    call time. The clients themselves are still built from the empty
    module-level copies, so an unmocked API call still fails locally instead
    of reaching the network. ``tests/test_cloud_ai_disabled.py`` patches it
    back to "" to cover the disabled path.
    """
    monkeypatch.setattr("oncofiles.config.ANTHROPIC_API_KEY", "test-key-not-real")


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


@pytest.fixture
def admin_scope():
    """Promote the in-test caller to admin scope for the duration.

    Use as `pytestmark = pytest.mark.usefixtures("admin_scope")` at the top
    of any test file that exercises cross-patient slug routing — the routing
    mechanism is admin-only behavior under the #497/#498 ACL gate. Tests that
    specifically verify the ACL itself (cross-patient denial) must NOT use
    this fixture.
    """
    from oncofiles.persistent_oauth import _verified_caller_is_admin

    tok = _verified_caller_is_admin.set(True)
    yield
    _verified_caller_is_admin.reset(tok)


@pytest.fixture(autouse=True)
def _reset_curated_cloud_guard():
    """The curated-document cloud guard (#603) is process-global: ids from one
    test's in-memory DB must not refuse another test's documents."""
    from oncofiles.curated import indexer

    yield
    indexer._curated_ids.clear()
    indexer.mark_guard_ready()
