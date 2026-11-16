"""Shared test fixtures."""

import pytest

from oncofiles.database import Database
from oncofiles.patient_middleware import _current_patient_id
from tests import network_guard

# Installed at import, before collection, so module-level code is covered too.
network_guard.install()

ERIKA_UUID = "00000000-0000-4000-8000-000000000001"


@pytest.fixture(scope="session", autouse=True)
def _no_network_session():
    """Tests cannot open non-loopback connections (see ``tests/network_guard.py``).

    Process-wide patch of the socket entry points, so it also holds in
    ``asyncio.to_thread`` workers and every other thread. No opt-out marker.
    """
    network_guard.install()
    yield
    network_guard.uninstall()


@pytest.fixture(autouse=True)
def _no_network():
    """Fail the test that tried to reach the network, even when the code under
    test swallowed the guard's exception (a broad ``except Exception`` around
    I/O is common here). The message names the call and host/port only.

    A test may replace the socket functions itself through monkeypatch, which
    restores the guard; ``install()`` at the start of every test puts it back
    if anything else left it off.
    """
    network_guard.install()
    # No drain here: a refusal recorded during collection, or by a thread that
    # outlived the previous test, must fail a test instead of vanishing.
    yield
    message = network_guard.failure_message()
    if message:
        pytest.fail(message, pytrace=False)


@pytest.fixture(autouse=True)
def _reset_anthropic_client():
    """Reset shared Anthropic client singleton so test mocks take effect."""
    import oncofiles.enhance

    oncofiles.enhance._shared_client = None
    yield
    oncofiles.enhance._shared_client = None


@pytest.fixture(autouse=True)
def _cloud_ai_configured(monkeypatch):
    """Tests run as a server with cloud AI switched on AND a key (#618 guard passes).

    Production defaults to ``CLOUD_AI_ENABLED=false``; here the flag and
    ``config.ANTHROPIC_API_KEY`` are set — the values the guard reads at call
    time. The clients themselves are still built from the empty module-level
    key copies, so an unmocked API call still fails locally instead of
    reaching the network. ``tests/test_cloud_ai_disabled.py`` patches them
    back off to cover the disabled paths.
    """
    monkeypatch.setattr("oncofiles.config.CLOUD_AI_ENABLED", True)
    monkeypatch.setattr("oncofiles.config.ANTHROPIC_API_KEY", "test-key-not-real")


@pytest.fixture
def gmail_calendar_on(monkeypatch):
    """Opt a test into GMAIL_CALENDAR_ENABLED=true (production default is off, #618/#699).

    Deliberately NOT autouse: the default-off behaviour must stay the one
    every other test sees. Tests of the enabled Gmail/Calendar paths request
    this fixture explicitly.
    """
    monkeypatch.setattr("oncofiles.config.GMAIL_CALENDAR_ENABLED", True)


@pytest.fixture(autouse=True)
def _set_patient_context():
    """Seed the test patient's name in the per-patient cache (keyed by ERIKA_UUID).

    There is no legacy/default patient (#733): code that parses filenames
    must resolve a patient — explicitly or via the request ContextVar, which
    the `db` fixture (or `request_patient`) sets to ERIKA_UUID.
    """
    from oncofiles import patient_context

    original_contexts = dict(patient_context._contexts)
    patient_context._contexts.clear()
    patient_context._contexts[ERIKA_UUID] = {"name": "Jana Testova"}

    yield

    patient_context._contexts.clear()
    patient_context._contexts.update(original_contexts)


@pytest.fixture
def request_patient():
    """Bind the request ContextVar to ERIKA_UUID for tests without the `db` fixture.

    Stands in for PatientResolutionMiddleware so pure helpers (filename
    parsing) resolve the test patient the same way they do inside a request.
    """
    token = _current_patient_id.set(ERIKA_UUID)
    yield ERIKA_UUID
    _current_patient_id.reset(token)


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


@pytest.fixture(autouse=True)
def _ocr_orientation_off(monkeypatch):
    """Page orientation (#609) adds a model call per vision page. Tests of the
    other OCR behaviour count model calls and renders, so it is off here;
    ``tests/test_ocr_orientation_charts.py`` switches it on (the production
    default) and covers it."""
    monkeypatch.setattr("oncofiles.config.OCR_ORIENTATION", "off")
