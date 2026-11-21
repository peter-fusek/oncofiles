"""#868: audit trail for dashboard access (#531 follow-up).

Sign-ins, ACL refusals and admin reads of a foreign patient are written to
``dashboard_access_log`` (migration 090) with a keyed caller hash, never the
e-mail. The write is telemetry: a failing write must never change the ACL
result or break a sign-in. Counters live in memory for operator
``/readiness``; ``/health`` stays DB-free.

Synthetic patients and e-mails (example.test) only.
"""

from __future__ import annotations

import inspect
import json
import logging
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import dashboard_audit, server
from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
from oncofiles.database import Database
from oncofiles.secrets_keys import caller_email_hash
from tests.helpers import PRIMARY_TEST_UUID

A_UUID = PRIMARY_TEST_UUID
B_UUID = "00000000-0000-4000-8000-0000000008b1"
A_EMAIL = "carer-a@example.test"
B_EMAIL = "carer-b@example.test"
NOBODY_EMAIL = "stranger@example.test"
ADMIN_EMAIL = "operator@example.test"
SECRET = "test-secret-868"


@pytest.fixture(autouse=True)
def _fresh_audit_state():
    dashboard_audit._reset_for_tests()
    with patch("oncofiles.config.MCP_BEARER_TOKEN", SECRET):
        yield
    dashboard_audit._reset_for_tests()


@pytest.fixture
async def audit_db(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("audit-a", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "audit-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.commit()
    return db


async def synthetic_dashboard_route():  # stands in for a dashboard endpoint
    return None


def _request(db: Database, patient_id: str | None = None, *, tid: str | None = None):
    request = MagicMock()
    request.query_params = {"patient_id": patient_id} if patient_id is not None else {}
    request.path_params = {}
    request.scope = {"endpoint": synthetic_dashboard_route}
    headers = {}
    if tid:
        headers["authorization"] = f"Bearer session:someone|9999999999|{tid}|{'0' * 32}"
    request.headers = headers
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    return request


@contextmanager
def _caller(email: str | None, *, bearer: bool = False, admin: bool = False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=None if bearer else unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


async def _rows(db: Database) -> list[dict]:
    return await db.list_dashboard_access(limit=10_000)


# ── schema ───────────────────────────────────────────────────────────────


async def test_migration_creates_table_and_index(db: Database):
    async with db.db.execute("PRAGMA table_info('dashboard_access_log')") as cur:
        cols = {r["name"] for r in await cur.fetchall()}
    assert cols == {
        "id",
        "event",
        "caller_hash",
        "is_admin",
        "target_patient_id",
        "route",
        "created_at",
    }
    async with db.db.execute("PRAGMA index_list('dashboard_access_log')") as cur:
        names = {r["name"] for r in await cur.fetchall()}
    assert "idx_dashboard_access_log_created" in names


async def test_unknown_event_is_rejected(db: Database):
    with pytest.raises(ValueError):
        await db.insert_dashboard_access(
            event="drop_table", caller_hash=None, is_admin=False, target_patient_id=None, route="x"
        )


# ── events ───────────────────────────────────────────────────────────────


async def test_acl_refused_written_for_foreign_patient(audit_db):
    with _caller(A_EMAIL):
        assert await server._get_dashboard_patient_id(_request(audit_db, "audit-b")) == A_UUID
    (row,) = await _rows(audit_db)
    assert row["event"] == "acl_refused"
    assert row["caller_hash"] == caller_email_hash(A_EMAIL)
    assert row["is_admin"] == 0
    assert row["target_patient_id"] == B_UUID
    assert row["route"]
    assert dashboard_audit.counters()["acl_refusals"] == 1


async def test_acl_refused_written_for_stranger_sentinel(audit_db):
    with _caller(NOBODY_EMAIL):
        result = await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
    assert result == NO_PATIENT_ACCESS_SENTINEL
    (row,) = await _rows(audit_db)
    assert (row["event"], row["target_patient_id"]) == ("acl_refused", B_UUID)


async def test_acl_refused_for_unknown_patient_stores_no_slug(audit_db):
    with _caller(A_EMAIL):
        result = await server._get_dashboard_patient_id(_request(audit_db, "no-such-patient"))
    assert result == A_UUID  # #867/#590: falls back to own, like a foreign patient
    (row,) = await _rows(audit_db)
    assert row["event"] == "acl_refused"
    assert row["target_patient_id"] is None
    assert "no-such-patient" not in json.dumps(row)


async def test_anonymous_refusal_is_counted_not_written(audit_db):
    with _caller(None):
        result = await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
    assert result == NO_PATIENT_ACCESS_SENTINEL
    assert await _rows(audit_db) == []
    assert dashboard_audit.counters()["acl_refusals_anonymous"] == 1


async def test_no_patient_selected_is_not_a_refusal(audit_db):
    with _caller(NOBODY_EMAIL):
        result = await server._get_dashboard_patient_id(_request(audit_db))
    assert result == NO_PATIENT_ACCESS_SENTINEL
    assert await _rows(audit_db) == []
    assert dashboard_audit.counters()["acl_refusals"] == 0


async def test_caregiver_and_bearer_reads_are_not_logged(audit_db):
    with _caller(B_EMAIL):
        assert await server._get_dashboard_patient_id(_request(audit_db, "audit-b")) == B_UUID
    with _caller(None, bearer=True):
        assert await server._get_dashboard_patient_id(_request(audit_db, "audit-b")) == B_UUID
    assert await _rows(audit_db) == []


async def test_admin_cross_read_written_once_per_session_and_patient(audit_db):
    with _caller(ADMIN_EMAIL, admin=True):
        for _ in range(3):
            got = await server._get_dashboard_patient_id(
                _request(audit_db, "audit-b", tid="a" * 16)
            )
            assert got == B_UUID
        # another patient in the same session, and the same patient in a new session
        assert (
            await server._get_dashboard_patient_id(_request(audit_db, "audit-a", tid="a" * 16))
            == A_UUID
        )
        assert (
            await server._get_dashboard_patient_id(_request(audit_db, "audit-b", tid="b" * 16))
            == B_UUID
        )
    rows = await _rows(audit_db)
    assert [r["event"] for r in rows] == ["patient_read"] * 3
    assert {r["target_patient_id"] for r in rows} == {A_UUID, B_UUID}
    assert all(r["is_admin"] == 1 for r in rows)
    assert all(r["caller_hash"] == caller_email_hash(ADMIN_EMAIL) for r in rows)
    assert dashboard_audit.counters()["admin_cross_reads"] == 3


async def test_admin_reading_own_patient_is_not_a_cross_read(audit_db):
    with _caller(B_EMAIL, admin=True):
        assert await server._get_dashboard_patient_id(_request(audit_db, "audit-b")) == B_UUID
    assert await _rows(audit_db) == []


async def test_read_dedupe_expires_after_an_hour(audit_db):
    clock = [1000.0]
    with (
        _caller(ADMIN_EMAIL, admin=True),
        patch.object(dashboard_audit.time, "monotonic", side_effect=lambda: clock[0]),
    ):
        for step in (0, 1800, 1801):  # t=0 written, t=1800 deduped, t=3601 written
            clock[0] += step
            await server._get_dashboard_patient_id(_request(audit_db, "audit-b", tid="c" * 16))
    rows = await _rows(audit_db)
    assert len(rows) == 2
    assert rows[0]["route"] == "synthetic_dashboard_route"


async def test_sign_in_written(audit_db):
    request = MagicMock()
    request.json = AsyncMock(return_value={"credential": "synthetic-id-token"})
    request.scope = {"endpoint": server.dashboard_verify}
    request.app.state.fastmcp_server._lifespan_result = {"db": audit_db}
    resp = MagicMock(status_code=200)
    resp.json.return_value = {
        "aud": "client-868",
        "email": ADMIN_EMAIL,
        "email_verified": "true",
        "name": "Synthetic Operator",
    }
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    with (
        patch.object(server, "_check_rate_limit", return_value=None),
        patch.object(server, "MCP_BEARER_TOKEN", SECRET),
        patch.object(server, "GOOGLE_OAUTH_CLIENT_ID", "client-868"),
        patch("httpx.AsyncClient", return_value=client),
        patch.object(server, "_is_admin_email", return_value=True),
    ):
        out = await server.dashboard_verify(request)
    assert out.status_code == 200
    (row,) = await _rows(audit_db)
    assert row["event"] == "sign_in"
    assert row["is_admin"] == 1
    assert row["target_patient_id"] is None
    assert row["caller_hash"] == caller_email_hash(ADMIN_EMAIL)
    assert row["route"] == "dashboard_verify"
    assert dashboard_audit.counters()["sign_ins"] == 1


# ── telemetry never changes the result ───────────────────────────────────


@pytest.mark.parametrize(
    ("email", "admin", "requested", "expected"),
    [
        (A_EMAIL, False, "audit-b", A_UUID),  # refused, falls back to own
        (NOBODY_EMAIL, False, "audit-b", NO_PATIENT_ACCESS_SENTINEL),  # refused, sentinel
        (A_EMAIL, False, "no-such-patient", A_UUID),  # unknown = foreign (#867, #590)
        (ADMIN_EMAIL, True, "audit-b", B_UUID),  # admin cross read
    ],
)
async def test_failing_audit_write_does_not_change_acl_result(
    audit_db, email, admin, requested, expected
):
    boom = AsyncMock(side_effect=RuntimeError("disk full"))
    with _caller(email, admin=admin), patch.object(audit_db, "insert_dashboard_access", boom):
        got = await server._get_dashboard_patient_id(_request(audit_db, requested))
    assert got == expected
    assert boom.await_count == 1
    assert dashboard_audit.counters()["write_failures"] == 1


async def test_failing_audit_write_does_not_break_sign_in(audit_db):
    boom = AsyncMock(side_effect=RuntimeError("disk full"))
    await dashboard_audit.record(
        MagicMock(insert_dashboard_access=boom),
        "sign_in",
        email=A_EMAIL,
        is_admin=False,
        patient_id=None,
        route="dashboard_verify",
    )  # must not raise
    assert dashboard_audit.counters()["write_failures"] == 1


async def test_record_never_raises_even_when_hashing_fails():
    with patch.object(dashboard_audit, "caller_hash", side_effect=RuntimeError("no key")):
        await dashboard_audit.record(
            MagicMock(), "acl_refused", email=A_EMAIL, is_admin=False, patient_id=None, route="r"
        )
    assert dashboard_audit.counters()["write_failures"] == 1


# ── no e-mail in rows or logs; throttled log lines ───────────────────────


async def test_no_email_in_rows_or_logs(audit_db, caplog):
    caplog.set_level(logging.INFO)
    with _caller(A_EMAIL):
        await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
    with _caller(ADMIN_EMAIL, admin=True):
        await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
    rows = json.dumps(await _rows(audit_db))
    text = "\n".join(r.getMessage() for r in caplog.records)
    for email in (A_EMAIL, ADMIN_EMAIL):
        assert email not in rows
        assert email not in text
        assert "example.test" not in text
    assert "event=acl_refused" in text
    assert "event=patient_read" in text


async def test_refusal_log_line_is_throttled(audit_db, caplog):
    caplog.set_level(logging.WARNING, logger="oncofiles.dashboard_audit")
    with _caller(A_EMAIL):
        for _ in range(5):
            await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
    lines = [r for r in caplog.records if "event=acl_refused" in r.getMessage()]
    assert len(lines) == 1
    assert len(await _rows(audit_db)) == 1  # deduped: one row per caller+patient+route/min
    assert dashboard_audit.counters()["acl_refusals"] == 5
    assert dashboard_audit.counters()["acl_refusals_suppressed"] == 4


# ── a signed-in stranger cannot grow the table without bound ─────────────


async def test_refusal_burst_same_minute_writes_one_row(audit_db):
    with _caller(NOBODY_EMAIL):
        for _ in range(1000):
            got = await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
            assert got == NO_PATIENT_ACCESS_SENTINEL
    assert len(await _rows(audit_db)) == 1
    c = dashboard_audit.counters()
    assert c["acl_refusals"] == 1000
    assert c["acl_refusals_suppressed"] == 999


async def test_refusal_burst_is_capped_per_caller_per_day(audit_db):
    clock = [0.0]

    def tick():  # every call is a minute later, so only the daily cap holds
        clock[0] += 61.0
        return clock[0]

    with (
        _caller(NOBODY_EMAIL),
        patch.object(dashboard_audit.time, "monotonic", side_effect=tick),
        patch.object(dashboard_audit, "_utc_day", return_value="2027-01-01"),
    ):
        for _ in range(1000):
            got = await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
            assert got == NO_PATIENT_ACCESS_SENTINEL
    assert len(await _rows(audit_db)) == dashboard_audit.REFUSAL_DAILY_CAP
    c = dashboard_audit.counters()
    assert c["acl_refusals"] == 1000
    assert c["acl_refusals_suppressed"] == 1000 - dashboard_audit.REFUSAL_DAILY_CAP
    assert c["write_failures"] == 0

    # a new UTC day opens a new budget; another caller has its own budget
    with (
        _caller(NOBODY_EMAIL),
        patch.object(dashboard_audit.time, "monotonic", side_effect=tick),
        patch.object(dashboard_audit, "_utc_day", return_value="2027-01-02"),
    ):
        await server._get_dashboard_patient_id(_request(audit_db, "audit-b"))
    with _caller(A_EMAIL), patch.object(dashboard_audit, "_utc_day", return_value="2027-01-01"):
        assert await server._get_dashboard_patient_id(_request(audit_db, "audit-b")) == A_UUID
    assert len(await _rows(audit_db)) == dashboard_audit.REFUSAL_DAILY_CAP + 2


# ── one audit row per view, not per thumbnail batch (#948 grid) ─────────


def _thumb_request(db: Database, patient: str, n: int, *, tid: str = "d" * 16):
    """The n-th ``/api/doc-thumbnails`` batch of one dashboard grid view."""
    request = _request(db, patient, tid=tid)
    ids = ",".join(str(n * 24 + i + 1) for i in range(24))
    request.query_params = {"patient_id": patient, "ids": ids}
    request.scope = {"endpoint": server.api_doc_thumbnails}
    return request


@contextmanager
def _thumbnail_route():
    with (
        patch.object(server, "_check_dashboard_auth", return_value=None),
        patch("oncofiles.doc_thumbnails.thumbnails_for", AsyncMock(side_effect=lambda *a, **k: {})),
    ):
        yield


async def test_grid_of_thumbnail_batches_by_admin_writes_one_patient_read(audit_db):
    """A 413-document grid asks for ~18 batches; 20 batches by an admin of a
    foreign patient write ONE ``patient_read`` row (hourly per session +
    patient), and the route answers every batch."""
    with _caller(ADMIN_EMAIL, admin=True), _thumbnail_route():
        for n in range(20):
            resp = await server.api_doc_thumbnails(_thumb_request(audit_db, "audit-b", n))
            assert resp.status_code == 200
    (row,) = await _rows(audit_db)
    assert (row["event"], row["target_patient_id"]) == ("patient_read", B_UUID)
    assert row["route"] == "api_doc_thumbnails"
    assert dashboard_audit.counters()["admin_cross_reads"] == 1


async def test_refused_thumbnail_batches_write_one_row_per_minute(audit_db):
    """20 refused batches in the same minute: one ``acl_refused`` row; the next
    minute's 20 batches: one more. Every call is still counted."""
    clock = [5000.0]
    with (
        _caller(NOBODY_EMAIL),
        _thumbnail_route(),
        patch.object(dashboard_audit.time, "monotonic", side_effect=lambda: clock[0]),
    ):
        for _minute in range(2):
            for n in range(20):
                clock[0] += 1.0  # 20 s of batches inside one minute
                resp = await server.api_doc_thumbnails(_thumb_request(audit_db, "audit-b", n))
                assert resp.status_code == 200
            clock[0] += 61.0  # the next minute
    rows = await _rows(audit_db)
    assert [r["event"] for r in rows] == ["acl_refused", "acl_refused"]
    assert {r["route"] for r in rows} == {"api_doc_thumbnails"}
    assert {r["target_patient_id"] for r in rows} == {B_UUID}
    c = dashboard_audit.counters()
    assert c["acl_refusals"] == 40
    assert c["acl_refusals_suppressed"] == 38


def test_every_patient_read_write_goes_through_the_hourly_dedupe():
    """``record`` dedupes ``acl_refused`` itself; ``patient_read`` relies on
    the caller asking ``should_record_read`` first. Lock: the only write site
    sits inside that check, so no high-frequency route can bypass it."""
    src = inspect.getsource(server)
    assert src.count('"patient_read"') == 1
    site = src.index('"patient_read"')
    guard = src.rfind("dashboard_audit.should_record_read(", 0, site)
    assert guard != -1 and site - guard < 300


def test_refusal_dedupe_memory_is_bounded():
    with patch.object(dashboard_audit, "_MAX_KEYS", 10):
        for i in range(50):
            dashboard_audit._refusal_write_allowed(f"caller{i}", B_UUID, "r")
            dashboard_audit._refusal_write_allowed("one-caller", f"patient{i}", "r")
        assert len(dashboard_audit._refusal_seen) <= 10
        assert len(dashboard_audit._refusal_daily) <= 10


def test_full_daily_map_suppresses_new_callers_instead_of_growing():
    with (
        patch.object(dashboard_audit, "_MAX_KEYS", 3),
        patch.object(dashboard_audit, "_utc_day", return_value="2027-01-01"),
    ):
        assert all(dashboard_audit._refusal_write_allowed(f"c{i}", None, "r") for i in range(3))
        assert not dashboard_audit._refusal_write_allowed("c-new", None, "r")
        assert len(dashboard_audit._refusal_daily) == 3


def test_in_memory_maps_are_bounded():
    with patch.object(dashboard_audit, "_MAX_KEYS", 10):
        for i in range(50):
            assert dashboard_audit.should_record_read(f"s{i}", B_UUID)
        assert len(dashboard_audit._read_seen) <= 10


# ── /readiness yes, /health no ───────────────────────────────────────────


def test_counters_on_operator_readiness_only():
    source = inspect.getsource(server.readiness)
    public_part, operator_part = source.split("_readiness_caller_is_operator(request)", 1)
    assert "dashboard_access" not in public_part
    assert "dashboard_audit.counters()" in operator_part


def test_health_does_not_touch_the_audit():
    source = inspect.getsource(server.health)
    assert "dashboard_audit" not in source
    assert "dashboard_access" not in source


def test_counters_need_no_db():
    assert not inspect.iscoroutinefunction(dashboard_audit.counters)
    assert set(dashboard_audit.counters()) >= {
        "since",
        "sign_ins",
        "acl_refusals",
        "admin_cross_reads",
        "write_failures",
    }
