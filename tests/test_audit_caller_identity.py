"""Audit rows record the caller's auth method + a keyed email hash (migration 085).

Each auth path goes through the real ``PersistentOAuthProvider.verify_token``
and then a tool call behind ``AuditMiddleware``; the resulting
``activity_log`` row must carry that path's ``auth_method``. For OAuth the
row stores ``caller_email_hash`` (HMAC), never the email. Synthetic only.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from oncofiles import audit_middleware
from oncofiles.audit_middleware import AuditMiddleware
from oncofiles.patient_middleware import _current_patient_id
from oncofiles.persistent_oauth import (
    PersistentOAuthProvider,
    _verified_auth_method,
    _verified_caller_email,
    _verified_caller_is_admin,
    _verified_patient_id,
)
from oncofiles.secrets_keys import caller_email_hash
from tests.conftest import ERIKA_UUID
from tests.test_persistent_oauth import _mint_oauth_token_with_email

OWN_EMAIL = "alpha.caregiver@example.com"
STATIC_TOKEN = "static-operator-token-for-audit-tests"


@pytest.fixture(autouse=True)
def _scope(monkeypatch):
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", STATIC_TOKEN)
    monkeypatch.delenv("AUDIT_EMAIL_HASH_SECRET", raising=False)
    vars_ = (
        _verified_caller_is_admin,
        _verified_caller_email,
        _verified_patient_id,
        _verified_auth_method,
    )
    toks = [v.set(d) for v, d in zip(vars_, (False, "", "", ""), strict=True)]
    yield
    for v, t in zip(vars_, toks, strict=True):
        v.reset(t)


@pytest.fixture
async def pdb(db):
    await db.db.execute(
        "UPDATE patients SET slug = 'alpha-test', caregiver_email = ? WHERE patient_id = ?",
        (OWN_EMAIL, ERIKA_UUID),
    )
    await db.db.commit()
    return db


def _mw_context(db, tool: str, transport: str):
    context = MagicMock()
    context.message.name = tool
    context.message.arguments = {}
    context.timestamp.strftime.return_value = "20270101-1200"
    context.fastmcp_context.request_context.lifespan_context = {
        "db": db,
        "transport": transport,
    }
    return context


async def _audited_row(db, tool: str, transport: str = "streamable-http") -> dict:
    async def call_next(_ctx):
        return "ok"

    await AuditMiddleware().on_call_tool(_mw_context(db, tool, transport), call_next)
    await asyncio.gather(*list(audit_middleware._audit_tasks))
    async with db.db.execute(
        "SELECT auth_method, caller_email_hash, input_summary, session_id, agent_id "
        "FROM activity_log WHERE tool_name = ? ORDER BY id DESC LIMIT 1",
        (tool,),
    ) as cur:
        return dict(await cur.fetchone())


def _no_raw_email(row: dict) -> None:
    for value in row.values():
        assert OWN_EMAIL not in str(value)


async def test_oauth_call_logs_method_and_email_hash(pdb):
    provider = PersistentOAuthProvider(db=pdb)
    tok = await _mint_oauth_token_with_email(provider, email=OWN_EMAIL, challenge="a1")
    assert await provider.verify_token(tok.access_token) is not None
    _current_patient_id.set(_verified_patient_id.get())

    row = await _audited_row(pdb, "t_oauth")
    assert row["auth_method"] == "oauth"
    assert row["caller_email_hash"] == caller_email_hash(OWN_EMAIL)
    assert len(row["caller_email_hash"]) == 64
    _no_raw_email(row)


async def test_patient_bearer_call_logs_method(pdb):
    token = await pdb.create_patient_token(ERIKA_UUID, label="test")
    provider = PersistentOAuthProvider(db=pdb, bearer_token=STATIC_TOKEN)
    assert await provider.verify_token(token) is not None

    row = await _audited_row(pdb, "t_patient_bearer")
    assert row["auth_method"] == "patient_bearer"
    assert row["caller_email_hash"] is None


async def test_static_bearer_call_logs_method(pdb):
    provider = PersistentOAuthProvider(db=pdb, bearer_token=STATIC_TOKEN)
    assert await provider.verify_token(STATIC_TOKEN) is not None

    row = await _audited_row(pdb, "t_static")
    assert row["auth_method"] == "static_bearer"
    assert row["caller_email_hash"] is None


async def test_stdio_call_logs_method(pdb):
    row = await _audited_row(pdb, "t_stdio", transport="stdio")
    assert row["auth_method"] == "stdio"
    assert row["caller_email_hash"] is None


async def test_http_call_without_identity_logs_none(pdb):
    row = await _audited_row(pdb, "t_none")
    assert row["auth_method"] == "none"
    assert row["caller_email_hash"] is None


async def test_failed_tool_call_still_logs_method(pdb):
    provider = PersistentOAuthProvider(db=pdb, bearer_token=STATIC_TOKEN)
    await provider.verify_token(STATIC_TOKEN)

    async def call_next(_ctx):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await AuditMiddleware().on_call_tool(_mw_context(pdb, "t_err", ""), call_next)
    await asyncio.gather(*list(audit_middleware._audit_tasks))
    async with pdb.db.execute(
        "SELECT auth_method, status FROM activity_log WHERE tool_name = 't_err'"
    ) as cur:
        row = dict(await cur.fetchone())
    assert row == {"auth_method": "static_bearer", "status": "error"}


# ── the hash itself ──────────────────────────────────────────────────────


def test_email_hash_is_keyed_and_normalised(monkeypatch):
    import hashlib

    h = caller_email_hash(OWN_EMAIL)
    assert h == caller_email_hash(f"  {OWN_EMAIL.upper()} ")
    # Keyed: not a bare SHA-256 that a list of addresses could reverse.
    assert h != hashlib.sha256(OWN_EMAIL.encode()).hexdigest()
    # Override pins the key independent of MCP_BEARER_TOKEN rotation.
    monkeypatch.setenv("AUDIT_EMAIL_HASH_SECRET", "pinned")
    pinned = caller_email_hash(OWN_EMAIL)
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "rotated")
    assert caller_email_hash(OWN_EMAIL) == pinned != h


def test_email_hash_none_without_email_or_key(monkeypatch):
    assert caller_email_hash("") is None
    monkeypatch.setattr("oncofiles.config.MCP_BEARER_TOKEN", "")
    assert caller_email_hash(OWN_EMAIL) is None


# ── migration 085 ────────────────────────────────────────────────────────


async def test_migration_085_columns_and_rerun(db):
    async with db.db.execute("PRAGMA table_info(activity_log)") as cur:
        cols = {dict(r)["name"]: dict(r) for r in await cur.fetchall()}
    assert cols["auth_method"]["dflt_value"] == "''"
    assert cols["auth_method"]["notnull"] == 1
    assert "caller_email_hash" in cols
    # Additive + re-runnable: forgetting the version and migrating again is safe.
    await db.db.execute(
        "DELETE FROM schema_migrations WHERE version = '085_activity_log_caller_identity'"
    )
    await db.db.commit()
    await db.migrate()
