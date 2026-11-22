# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950: 6-character address tokens (maintainer decision 2026-11-22).

Covers the short token, the 30-day grace of the 10-character addresses that
were live before (lazy supersede on the first dashboard read, routing through
the stored hash, lapse by a time check, nothing deleted, rotation ends the
grace), the token-hash collision retry and the global bad-token counter with
its ``bad_tokens_high`` alert on operator ``/readiness`` and the admin panel.

Synthetic only: invented patients, addresses and bytes from the phase-one
test module.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from oncofiles import inbound_email, server
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures
    CARER,
    FILENAMES,
    PID,
    _env,
    _request,
    drive,
    idb,
    mail,
    mails,
    pdf,
    signed_headers,
)


async def _insert_legacy(db, *, version: int = 1, created_at: str, active: int = 1) -> str:
    """A pre-migration-100 row (10-character token, token_length NULL)."""
    token = inbound_email.derive_token(PID, version, inbound_email.LEGACY_TOKEN_LEN)
    await db.db.execute(
        """INSERT INTO inbound_addresses (patient_id, token_hash, version, active, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (PID, inbound_email.token_hash(token), version, active, created_at),
    )
    await db.db.commit()
    return inbound_email.format_address(token, CARER)


ON, OFF = 1, 0
SHORT = inbound_email.TOKEN_LEN


def _iso(dt) -> str:
    return inbound_email._iso(dt)


async def _send(db, addr: str, n: int = 0):
    body = mail(addr, files=[(f"{n}{FILENAMES[0]}", pdf(n), "application/pdf")]).as_bytes()
    resp = await server.inbound_email_webhook(_request(db, body, signed_headers(body, addr)))
    assert resp.status_code == 202
    return resp


async def _rows(db):
    async with db.db.execute(
        "SELECT version, active, token_length, grace_ends_at FROM inbound_addresses "
        "WHERE patient_id = ? ORDER BY version",
        (PID,),
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


def _clock(monkeypatch, dt):
    monkeypatch.setattr(inbound_email, "_utcnow", lambda: dt)


# ── token shape ──────────────────────────────────────────────────────────


def test_token_is_six_crockford_characters_and_both_lengths_parse():
    token = inbound_email.derive_token(PID, 1)
    assert len(token) == inbound_email.TOKEN_LEN == 6
    # the short token is the prefix of the old one of the same version (one HMAC)
    assert inbound_email.derive_token(PID, 1, 10).startswith(token)
    domain = inbound_email.INBOUND_DOMAIN
    assert inbound_email.token_from_address(f"synthetic-{token}@{domain}") == token
    legacy = inbound_email.derive_token(PID, 1, 10)
    assert inbound_email.token_from_address(f"synthetic-{legacy}@{domain}") == legacy
    for bad in ("abcde", "abcdefg", "abcdefghj"):  # too short, between, too long
        assert inbound_email.token_from_address(f"x-{bad}@{domain}") is None


async def test_new_patient_gets_a_six_character_address(idb):
    addr = (await inbound_email.ensure_address(idb, PID))["address"]
    token = inbound_email.token_from_address(addr)
    assert len(token) == 6 and addr == f"synthetic.carer950-{token}@{inbound_email.INBOUND_DOMAIN}"
    assert await _rows(idb) == [
        {"version": 1, "active": ON, "token_length": SHORT, "grace_ends_at": None}
    ]


# ── transition ───────────────────────────────────────────────────────────


def test_grace_end_is_thirty_days_from_the_deploy_or_a_newer_row():
    since = inbound_email.SHORT_TOKEN_SINCE
    old = {"token_length": None, "active": ON}
    old["created_at"] = _iso(since - timedelta(days=200))
    assert inbound_email.legacy_grace_end(old) == since + timedelta(days=30)
    newer = dict(old, created_at=_iso(since + timedelta(days=3)))
    assert inbound_email.legacy_grace_end(newer) == since + timedelta(days=33)
    graced = dict(old, grace_ends_at=_iso(since + timedelta(days=7)))
    assert inbound_email.legacy_grace_end(graced) == since + timedelta(days=7)
    assert inbound_email.LEGACY_GRACE_DAYS == 30


async def test_legacy_address_still_files_within_the_grace(idb, drive, mails, monkeypatch):
    since = inbound_email.SHORT_TOKEN_SINCE
    _clock(monkeypatch, since + timedelta(days=1))
    legacy = await _insert_legacy(idb, created_at=_iso(since - timedelta(days=10)))
    await _send(idb, legacy)
    first_name = f"0{FILENAMES[0]}"
    assert [f[0] for f in drive.files] == [first_name]
    assert inbound_email.bad_tokens_overview()["last_hour"] == 0


async def test_dashboard_read_supersedes_the_legacy_row_lazily(idb, drive, mails, monkeypatch):
    since = inbound_email.SHORT_TOKEN_SINCE
    _clock(monkeypatch, since + timedelta(days=2))
    legacy = await _insert_legacy(idb, created_at=_iso(since - timedelta(days=10)))
    card = await inbound_email.ensure_address(idb, PID)
    short = inbound_email.token_from_address(card["address"])
    assert len(short) == 6 and card["version"] == 2
    assert short == inbound_email.derive_token(PID, 2)
    # a second read changes nothing
    assert (await inbound_email.ensure_address(idb, PID))["address"] == card["address"]
    grace = _iso(since + timedelta(days=30))
    assert await _rows(idb) == [
        {"version": 1, "active": OFF, "token_length": None, "grace_ends_at": grace},
        {"version": 2, "active": ON, "token_length": SHORT, "grace_ends_at": None},
    ]
    # both addresses route through their stored hashes, to the same patient
    for addr in (legacy, card["address"]):
        row, reason = await inbound_email.lookup_address(
            idb, inbound_email.token_from_address(addr)
        )
        assert reason is None and row["patient_id"] == PID
    await _send(idb, legacy, 1)
    await _send(idb, card["address"], 2)
    assert len(drive.files) == 2


async def test_legacy_address_lapses_after_the_grace_and_is_never_deleted(
    idb, drive, mails, monkeypatch
):
    since = inbound_email.SHORT_TOKEN_SINCE
    _clock(monkeypatch, since + timedelta(days=2))
    legacy = await _insert_legacy(idb, created_at=_iso(since - timedelta(days=10)))
    card = await inbound_email.ensure_address(idb, PID)
    _clock(monkeypatch, since + timedelta(days=30, seconds=1))
    await _send(idb, legacy, 1)
    # dropped silently: nothing filed, nothing sent, counted as lapsed
    assert drive.files == [] and mails == []
    assert inbound_email.unmatched_overview()["totals"]["lapsed_token"] == 1
    assert inbound_email.bad_tokens_overview()["last_hour"] == 1
    assert len(await _rows(idb)) == 2  # the old row stays
    await _send(idb, card["address"], 2)
    assert len(drive.files) == 1


async def test_a_never_upgraded_legacy_row_lapses_too(idb, drive, mails, monkeypatch):
    since = inbound_email.SHORT_TOKEN_SINCE
    legacy = await _insert_legacy(idb, created_at=_iso(since - timedelta(days=10)))
    _clock(monkeypatch, since + timedelta(days=31))
    await _send(idb, legacy)
    assert drive.files == [] and mails == []
    assert inbound_email._state.counters["lapsed_token"] == 1
    # the next dashboard read gives a short address, the old one is not graced
    card = await inbound_email.ensure_address(idb, PID)
    assert len(inbound_email.token_from_address(card["address"])) == 6
    assert (await _rows(idb))[0] == {
        "version": 1,
        "active": 0,
        "token_length": None,
        "grace_ends_at": None,
    }


async def test_rotation_ends_the_grace_at_once(idb, drive, mails, monkeypatch):
    since = inbound_email.SHORT_TOKEN_SINCE
    _clock(monkeypatch, since + timedelta(days=2))
    legacy = await _insert_legacy(idb, created_at=_iso(since - timedelta(days=10)))
    short = (await inbound_email.ensure_address(idb, PID))["address"]
    rotated = await inbound_email.rotate_address(idb, PID)
    assert rotated["version"] == 3
    assert len(inbound_email.token_from_address(rotated["address"])) == 6
    rows = await _rows(idb)
    assert [r["active"] for r in rows] == [OFF, OFF, ON]
    assert all(r["grace_ends_at"] is None for r in rows)
    await _send(idb, legacy, 1)
    await _send(idb, short, 2)
    assert drive.files == []
    totals = inbound_email.unmatched_overview()["totals"]
    assert totals["inactive_token"] == 2
    await _send(idb, rotated["address"], 3)
    assert len(drive.files) == 1


# ── collisions ───────────────────────────────────────────────────────────


async def test_token_hash_collision_bumps_the_version(idb, monkeypatch):
    first = await inbound_email.ensure_address(idb, PID)
    taken = inbound_email.token_from_address(first["address"])
    real = inbound_email.derive_token

    def colliding(pid, version, length=inbound_email.TOKEN_LEN):
        return taken if version == 2 else real(pid, version, length)

    monkeypatch.setattr(inbound_email, "derive_token", colliding)
    rotated = await inbound_email.rotate_address(idb, PID)
    assert rotated["version"] == 3
    rows = await _rows(idb)
    assert [(r["version"], r["active"]) for r in rows] == [(1, OFF), (3, ON)]


async def test_collisions_on_every_attempt_change_nothing(idb, monkeypatch):
    first = await inbound_email.ensure_address(idb, PID)
    taken = inbound_email.token_from_address(first["address"])
    monkeypatch.setattr(inbound_email, "derive_token", lambda *a, **k: taken)
    with pytest.raises(RuntimeError):
        await inbound_email.rotate_address(idb, PID)
    assert [(r["version"], r["active"]) for r in await _rows(idb)] == [(1, 1)]
    assert inbound_email.ACTIVATE_ATTEMPTS == 5


# ── bad-token counter ────────────────────────────────────────────────────


async def test_bad_token_mails_are_counted_and_dropped_silently(idb, drive, mails):
    domain = inbound_email.INBOUND_DOMAIN
    good = (await inbound_email.ensure_address(idb, PID))["address"]
    await _send(idb, good)
    for n in range(3):
        await _send(idb, f"guess-{str(n) * 6}@{domain}", n + 1)
    assert len(drive.files) == 1 and len(mails) == 1  # only the real mail's receipt
    overview = inbound_email.bad_tokens_overview()
    assert overview == {"last_hour": 3, "alert_above": 20, "alerting": False}
    assert inbound_email.unmatched_overview()["bad_tokens"] == overview
    assert inbound_email.alerts_overview()["bad_tokens_high"] == 0


def test_alert_fires_above_twenty_per_hour_and_the_window_slides():
    t0 = 1_900_000_000.0
    for i in range(20):
        inbound_email.note_bad_token("unknown_token", now=t0 + i)
    assert inbound_email.alerts_overview()["bad_tokens_high"] == 0
    inbound_email.note_bad_token("inactive_token", now=t0 + 20)
    assert inbound_email.alerts_overview()["bad_tokens_high"] == 1
    assert inbound_email.bad_tokens_overview(now=t0 + 21)["alerting"] is True
    # an hour later the old ones no longer count
    later = t0 + inbound_email.BAD_TOKENS_WINDOW_S + 30
    assert inbound_email.bad_tokens_overview(now=later)["last_hour"] == 0
    inbound_email.note_bad_token("unknown_token", now=later)
    assert inbound_email.alerts_overview()["bad_tokens_high"] == 1  # no new alert
    assert inbound_email.BAD_TOKENS_ALERT_PER_HOUR == 20


async def test_bad_tokens_are_on_readiness_never_on_health(idb, monkeypatch):
    import inspect

    for _ in range(21):
        inbound_email.note_bad_token("unknown_token")
    monkeypatch.setattr(server, "_readiness_caller_is_operator", lambda request: True)
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": idb}
    req = _request(idb, b"", {})
    req.scope["app"] = app
    body = json.loads((await server.readiness(req)).body)
    assert body["inbound_bad_tokens"]["last_hour"] == 21
    assert body["inbound_alerts"]["bad_tokens_high"] == 1
    assert "bad_tokens" not in inspect.getsource(server.health)


def test_admin_panel_renders_the_bad_token_alert():
    js = server.__file__.rsplit("/", 1)[0] + "/dashboard.js"
    with open(js, encoding="utf-8") as fh:
        text = fh.read()
    assert "bad_tokens_high" in text and "data.bad_tokens" in text
    assert "lapsed_token" in text


def test_migration_only_adds_nullable_columns():
    from pathlib import Path

    sql = Path(server.__file__).parents[2] / "migrations" / "100_inbound_short_token.sql"
    code = "\n".join(
        line for line in sql.read_text().splitlines() if not line.strip().startswith("--")
    )
    assert code.count("ALTER TABLE inbound_addresses ADD COLUMN") == 2
    for verb in ("INSERT", "UPDATE", "DELETE", "DROP", "NOT NULL", "DEFAULT"):
        assert verb not in code.upper(), verb
