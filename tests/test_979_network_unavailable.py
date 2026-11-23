"""#979: a network/DNS failure talking to Google is transient, never a disconnect.

Synthetic ids only (public repo). No network: every Google call is faked.
"""

import socket
import types
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from oncofiles import curated_folder, oauth
from oncofiles import server as srv
from oncofiles.network_errors import NETWORK_UNAVAILABLE, is_network_unavailable
from oncofiles.tools import curated as tool

PID = "00000000-0000-4000-8000-00000000c979"  # invented
OTHER = "00000000-0000-4000-8000-00000000c980"
TZ = ZoneInfo("Europe/Bratislava")
DISCONNECT_WORDS = ("not connected", "disconnect", "re-auth", "reauth")


def _dns_error() -> Exception:
    """httpx wraps the resolver error, as in the 2026-11-23 outage."""
    try:
        raise socket.gaierror(8, "nodename nor servname provided")
    except socket.gaierror as inner:
        try:
            raise httpx.ConnectError("[Errno 8] nodename nor servname provided") from inner
        except httpx.ConnectError as exc:
            return exc


def _server_not_found() -> Exception:
    import httplib2

    return httplib2.ServerNotFoundError("Unable to find the server at www.googleapis.com")


def _transport_error() -> Exception:
    from google.auth.exceptions import TransportError

    try:
        raise ConnectionRefusedError(61, "Connection refused")
    except ConnectionRefusedError as inner:
        try:
            raise TransportError(inner) from inner
        except TransportError as exc:
            return exc


def _invalid_grant() -> Exception:
    from google.auth.exceptions import RefreshError

    return RefreshError("invalid_grant: Token has been expired or revoked.")


def _status_400() -> Exception:
    req = httpx.Request("POST", "https://oauth2.example.invalid/token")
    resp = httpx.Response(400, request=req, json={"error": "invalid_grant"})
    return httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)


@pytest.fixture(autouse=True)
def _clean():
    def reset():
        srv._client_unavailable.clear()
        srv._network_unavailable_stats.clear()
        srv._patient_clients_cache.clear()
        srv._invalid_grant_tokens.clear()
        srv._hard_auth_failures.clear()
        curated_folder._detect_errors.clear()  # curated state: conftest resets it

    reset()
    yield
    reset()


# ── classifier ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "make",
    [
        lambda: socket.gaierror(-2, "Name or service not known"),
        _dns_error,
        _server_not_found,
        _transport_error,
        lambda: ConnectionRefusedError(61, "Connection refused"),
        lambda: ConnectionResetError(54, "Connection reset by peer"),
        lambda: TimeoutError("timed out"),
        lambda: httpx.ConnectTimeout("timed out"),
        lambda: httpx.ReadTimeout("timed out"),
    ],
)
def test_network_errors_are_network_unavailable(make):
    assert is_network_unavailable(make())


def test_a_wrapped_network_error_is_found_through_the_chain():
    try:
        try:
            raise _server_not_found()
        except Exception as inner:
            raise RuntimeError("drive call failed") from inner
    except RuntimeError as exc:
        assert is_network_unavailable(exc)


@pytest.mark.parametrize(
    "make", [_invalid_grant, _status_400, lambda: ValueError("bad"), lambda: KeyError("x")]
)
def test_credential_and_other_errors_are_not_network(make):
    assert not is_network_unavailable(make())


def test_refresh_error_is_never_network_even_when_it_wraps_a_socket_error():
    exc = _invalid_grant()
    exc.__cause__ = socket.gaierror(8, "x")
    assert not is_network_unavailable(exc)


# ── client build: network vs credential vs kill switch ───────────────


def _fake_db():
    token = types.SimpleNamespace(
        patient_id=PID,
        provider="google",
        access_token="synthetic-access",
        refresh_token="synthetic-refresh",
        token_expiry=None,  # expired -> a refresh is attempted
        gdrive_folder_id="synthetic-folder-0001",
        owner_email="",
        granted_scopes=None,
    )
    return types.SimpleNamespace(
        get_oauth_token=AsyncMock(return_value=token), update_oauth_access_token=AsyncMock()
    )


def _no_disconnect_state(pid: str = PID) -> None:
    assert srv._invalid_grant_tokens == set()
    assert srv._hard_auth_failures == {}
    assert not any(k[0] == pid for k in srv._invalid_grant_tokens)


@pytest.mark.parametrize("make", [_dns_error, _server_not_found, _transport_error])
async def test_refresh_network_error_is_network_unavailable_not_disconnect(
    monkeypatch, make, caplog
):
    monkeypatch.setattr(srv, "GOOGLE_INTEGRATION_ENABLED", True)

    def boom(_rt):
        raise make()

    monkeypatch.setattr(oauth, "refresh_access_token", boom)
    caplog.set_level("INFO")
    assert await srv._create_patient_clients(_fake_db(), PID) is None
    assert srv.patient_client_unavailable_reason(PID) == NETWORK_UNAVAILABLE
    assert srv.network_unavailable_stats()[PID]["count"] == 1
    _no_disconnect_state()
    text = caplog.text.lower()
    assert NETWORK_UNAVAILABLE in text
    assert "synthetic-refresh" not in text and "synthetic-access" not in text
    assert not any(w in text for w in DISCONNECT_WORDS)


async def test_refresh_credential_error_keeps_its_old_behaviour(monkeypatch):
    monkeypatch.setattr(srv, "GOOGLE_INTEGRATION_ENABLED", True)

    def boom(_rt):
        raise _status_400()

    monkeypatch.setattr(oauth, "refresh_access_token", boom)
    assert await srv._create_patient_clients(_fake_db(), PID) is None
    assert srv.patient_client_unavailable_reason(PID) == "refresh_failed"
    assert srv.network_unavailable_stats() == {}


def test_invalid_grant_still_becomes_needs_reauth():
    srv._handle_sync_exception(PID, "gdrive", _invalid_grant())
    assert (PID, "gdrive") in srv._invalid_grant_tokens


def test_network_error_in_sync_never_becomes_needs_reauth_or_hard_auth():
    srv._handle_sync_exception(PID, "gdrive", _dns_error())
    srv._handle_sync_exception(PID, "gdrive", _server_not_found())
    _no_disconnect_state()


async def test_kill_switch_is_unchanged_and_clears_a_network_code(monkeypatch):
    srv._client_unavailable[PID] = NETWORK_UNAVAILABLE
    monkeypatch.setattr(srv, "GOOGLE_INTEGRATION_ENABLED", False)
    db = _fake_db()
    assert await srv._create_patient_clients(db, PID) is None
    db.get_oauth_token.assert_not_called()  # no token load, no refresh
    assert srv.patient_client_unavailable_reason(PID) is None


# ── curated _prepare wording ─────────────────────────────────────────


@pytest.fixture
def llm_ready(monkeypatch):
    from oncofiles.curated import llm

    monkeypatch.setattr(llm, "configured", lambda: True)
    monkeypatch.setattr(llm, "base_url_refusal", lambda: None)


async def test_prepare_says_network_unavailable_not_disconnected(monkeypatch, llm_ready):
    async def fake_clients(db, pid):
        srv._note_client_unavailable(pid, NETWORK_UNAVAILABLE)
        return None

    monkeypatch.setattr(srv, "_create_patient_clients", fake_clients)
    err, ctx = await tool._prepare(None, PID, dry_run=True, max_docs=None, max_actions=40)
    assert ctx is None
    assert err["code"] == NETWORK_UNAVAILABLE and err["deferred"] is True
    assert not any(w in err["error"].lower() for w in DISCONNECT_WORDS)


async def test_prepare_without_a_connection_keeps_the_old_message(monkeypatch, llm_ready):
    monkeypatch.setattr(srv, "_create_patient_clients", AsyncMock(return_value=None))
    err, _ = await tool._prepare(None, PID, dry_run=True, max_docs=None, max_actions=40)
    assert "not connected" in err["error"] and "code" not in err


async def test_prepare_detection_network_failure_is_network_unavailable(monkeypatch, llm_ready):
    class Drive:
        def list_folder(self, *_a, **_k):
            raise _server_not_found()

    drive = Drive()
    monkeypatch.setattr(
        srv, "_create_patient_clients", AsyncMock(return_value=(drive, None, None, "fold-0001"))
    )

    def failing_detect(gdrive, folder_id):
        raise _server_not_found()

    monkeypatch.setattr(curated_folder, "_detect", failing_detect)
    err, _ = await tool._prepare(None, PID, dry_run=True, max_docs=None, max_actions=40)
    assert err["code"] == NETWORK_UNAVAILABLE
    assert curated_folder.detection_error("fold-0001") == NETWORK_UNAVAILABLE


# ── nightly deferral and same-night retry ────────────────────────────


def _night(h: int, m: int = 0) -> datetime:
    return datetime(2027, 6, 15, h, m, tzinfo=TZ)


def test_deferral_books_a_retry_15_then_30_minutes_later():
    now = _night(1, 3)
    tool._schedule_nightly_retry("2027-06-15", {PID}, now, attempted=False)
    r = tool._nightly_retry
    assert r["next_at"] == now + timedelta(minutes=15)
    assert r["deadline"] == _night(5, 45)
    assert tool._due_nightly_retry(now + timedelta(minutes=14)) is None
    later = now + timedelta(minutes=15)
    assert tool._due_nightly_retry(later) == [PID]
    tool._schedule_nightly_retry("2027-06-15", {PID}, later, attempted=True)
    assert tool._nightly_retry["next_at"] == later + timedelta(minutes=30)
    stats = tool.nightly_retry_stats()
    assert stats["pending"]["code"] == NETWORK_UNAVAILABLE
    assert stats["pending"]["deferred"] == 1 and stats["pending"]["attempts"] == 1
    assert PID not in str(stats)  # counts only


def test_retries_are_bounded_by_attempts_and_the_cutoff():
    now = _night(1, 0)
    tool._schedule_nightly_retry("s", {PID}, now, attempted=False)
    n = 0
    while (r := tool._nightly_retry) is not None:
        start = r["next_at"]
        assert start < _night(5, 45)  # every attempt STARTS before the cutoff
        assert tool._due_nightly_retry(start) == [PID]
        tool._schedule_nightly_retry("s", {PID}, start, attempted=True)
        n += 1
        assert n <= tool.NIGHTLY_RETRY_MAX_ATTEMPTS
    assert tool._nightly_retry_outcome == "gave_up"
    assert tool._nightly_retry_counts["gave_up_total"] == 1


def test_no_retry_is_booked_past_the_cutoff():
    tool._schedule_nightly_retry("s", {PID}, _night(5, 35), attempted=False)
    assert tool._nightly_retry is None
    assert tool._nightly_retry_outcome == "gave_up"


def test_a_late_loop_never_starts_a_retry_past_the_cutoff():
    tool._schedule_nightly_retry("s", {PID}, _night(5, 20), attempted=False)
    assert tool._nightly_retry["next_at"] == _night(5, 35)
    assert tool._due_nightly_retry(_night(5, 50)) is None
    assert tool._nightly_retry is None


def test_a_successful_retry_clears_the_state():
    tool._schedule_nightly_retry("s", {PID, OTHER}, _night(1), attempted=False)
    tool._schedule_nightly_retry("s", set(), _night(1, 15), attempted=True)
    assert tool._nightly_retry is None and tool._nightly_retry_outcome == "recovered"
    st = tool.nightly_retry_stats()
    assert st["recovered_total"] == 2 and st["retries_total"] == 1 and st["pending"] is None


def test_nothing_deferred_books_nothing():
    tool._schedule_nightly_retry("s", set(), _night(1), attempted=False)
    assert tool._nightly_retry is None
    assert tool._nightly_retry_outcome is None


async def test_sweep_defers_only_network_unavailable(monkeypatch):
    async def fake_seq(db, pid, **kw):
        if pid == PID:
            return [{"dry_run": True, **tool._network_unavailable_error()}]
        return [{"dry_run": True, "status": "ok", "documents": []}]

    monkeypatch.setattr(tool, "nightly_for_patient", fake_seq)
    monkeypatch.setattr(tool, "_scheduling_reason", AsyncMock(return_value=None))
    tool._watch[PID] = tool._Watch(failures=1)
    deferred = await tool._nightly_sweep(None, [PID, OTHER], max_runs=1, reuse_preview=False)
    assert deferred == {PID}
    assert PID in tool._watch  # a deferred patient was not "retried everything"


async def test_sweep_does_not_defer_a_disconnected_patient(monkeypatch):
    async def fake_seq(db, pid, **kw):
        return [{"error": "Google Drive not connected for this patient (or integration disabled)"}]

    monkeypatch.setattr(tool, "nightly_for_patient", fake_seq)
    monkeypatch.setattr(tool, "_scheduling_reason", AsyncMock(return_value=None))
    assert await tool._nightly_sweep(None, [PID], max_runs=1, reuse_preview=False) == set()


async def test_loop_runs_a_due_retry_for_the_deferred_patients_only(monkeypatch):
    """One loop iteration with a due retry: the deferred patient is swept again
    (no continuous round in that iteration); recovery clears the retry."""
    monkeypatch.setenv("CURATED_NIGHTLY_ENABLED", "true")
    now = datetime.now(TZ)
    monkeypatch.setenv("CURATED_NIGHTLY_HOUR", str((now.hour + 12) % 24))
    tool._nightly_retry = {
        "slot": "s",
        "pids": [PID],
        "attempts": 0,
        "next_at": now - timedelta(minutes=1),
        "deadline": now + timedelta(hours=2),
    }
    swept: list[str] = []

    async def fake_seq(db, pid, **kw):
        swept.append(pid)
        return [{"dry_run": True, "status": "ok", "documents": []}]

    rounds = AsyncMock(return_value=False)
    db = types.SimpleNamespace(
        mark_interrupted_curated_runs=AsyncMock(return_value=0),
        list_curated_nightly_patients=AsyncMock(return_value=[PID, OTHER]),
    )
    monkeypatch.setattr(tool, "nightly_for_patient", fake_seq)
    monkeypatch.setattr(tool, "continuous_round", rounds)
    monkeypatch.setattr(tool, "_scheduling_reason", AsyncMock(return_value=None))
    monkeypatch.setattr(tool, "_startup_index_targets", AsyncMock(return_value=set()))
    real_sleep = tool.asyncio.sleep
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            tool._stopping.set()
        await real_sleep(0)

    monkeypatch.setattr(tool.asyncio, "sleep", fake_sleep)
    try:
        await tool.curated_nightly_loop(db, startup_grace_s=0, probe_delay_s=0)
    finally:
        tool._stopping.clear()
    assert swept == [PID]
    rounds.assert_not_called()
    assert tool._nightly_retry is None
    assert tool._nightly_retry_outcome == "recovered"


async def test_continuous_round_is_unaffected(monkeypatch):
    """A probe that cannot reach Drive is still the ordinary ``drive_unavailable``
    skip of a continuous round; no nightly retry is booked by it."""

    async def fake_probe(db, pid, idle):
        return None

    monkeypatch.setattr(tool, "probe_patient", fake_probe)
    tool._probe_stats.pop(PID, None)
    assert await tool.continuous_round(None, [PID], settle_s=120, chunk=5) is False
    assert tool._probe_stats[PID].probes_skipped == {"drive_unavailable": 1}
    assert tool._nightly_retry is None


def test_readiness_exposes_counts_and_health_does_not():
    import inspect

    src = inspect.getsource(srv.readiness)
    assert "network_unavailable_stats()" in src and "nightly_retry_stats()" in src
    health = inspect.getsource(srv.health)
    assert "network_unavailable" not in health and "nightly_retry" not in health
