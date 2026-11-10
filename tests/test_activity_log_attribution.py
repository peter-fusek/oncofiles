"""#663: auto-logged tool calls are attributed to the patient the call acted on.

Before the fix every row written by AuditMiddleware took the migration-029
column default, so calls made for a second patient landed under the first.
Synthetic patients only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from unittest.mock import MagicMock

import pytest

from oncofiles import audit_middleware
from oncofiles.audit_middleware import AuditMiddleware, attributed_patient
from oncofiles.constants import NO_PATIENT_ACCESS_SENTINEL
from oncofiles.database import Database
from oncofiles.persistent_oauth import _verified_caller_is_admin
from oncofiles.tools._helpers import _get_patient_id, _resolve_patient_id
from tests.conftest import ERIKA_UUID

BOB_UUID = "00000000-0000-4000-8000-0000000000b2"
BOB_SLUG = "bob-test"


@pytest.fixture
async def two(db: Database) -> Database:
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, 'Bob Test', 'bob.caregiver@example.com')",
        (BOB_UUID, BOB_SLUG),
    )
    await db.db.commit()
    tok = _verified_caller_is_admin.set(False)
    yield db
    _verified_caller_is_admin.reset(tok)


class _Ctx:
    def __init__(self, db):
        self.request_context = MagicMock()
        self.request_context.lifespan_context = {"db": db}


def _mw_context(db, tool: str, args: dict):
    context = MagicMock()
    context.message.name = tool
    context.message.arguments = args
    context.timestamp.strftime.return_value = "20270101-1200"
    context.fastmcp_context = _Ctx(db)
    return context


async def _call(db, tool, args, body):
    """Run `body` as a tool behind AuditMiddleware and wait for the audit row."""

    async def call_next(_ctx):
        return await body()

    with contextlib.suppress(ValueError):
        await AuditMiddleware().on_call_tool(_mw_context(db, tool, args), call_next)
    await asyncio.gather(*list(audit_middleware._audit_tasks))
    async with db.db.execute(
        "SELECT patient_id, tags FROM activity_log WHERE tool_name = ? ORDER BY id DESC LIMIT 1",
        (tool,),
    ) as cur:
        return dict(await cur.fetchone())


async def test_call_targeting_another_slug_logs_that_patient(two):
    _verified_caller_is_admin.set(True)  # bound to Erika, targets Bob
    row = await _call(
        two, "t_slug", {"patient_slug": BOB_SLUG},
        lambda: _resolve_patient_id(BOB_SLUG, _Ctx(two)),
    )  # fmt: skip
    assert row["patient_id"] == BOB_UUID
    assert row["tags"] == audit_middleware.ATTRIBUTED_TAG


async def test_call_on_the_bound_patient_logs_it(two):
    async def body():
        return _get_patient_id()

    assert (await _call(two, "t_bound", {}, body))["patient_id"] == ERIKA_UUID


async def test_call_without_a_patient_logs_none(two):
    async def body():
        return "ok"

    assert (await _call(two, "t_none", {}, body))["patient_id"] == ""


async def test_denied_call_is_not_attributed_to_anyone(two):
    row = await _call(
        two, "t_denied", {"patient_slug": BOB_SLUG},
        lambda: _resolve_patient_id(BOB_SLUG, _Ctx(two)),
    )  # fmt: skip
    assert row["patient_id"] == ""


def test_several_or_sentinel_patients_are_not_attributed():
    assert attributed_patient({ERIKA_UUID, BOB_UUID}) == ""
    assert attributed_patient({NO_PATIENT_ACCESS_SENTINEL}) == ""
    assert attributed_patient({BOB_UUID, ""}) == BOB_UUID


async def test_non_admin_caller_never_sees_the_other_patients_calls(two):
    from oncofiles.tools.activity import search_activity_log

    _verified_caller_is_admin.set(True)
    await _call(
        two, "t_bob_only", {"patient_slug": BOB_SLUG},
        lambda: _resolve_patient_id(BOB_SLUG, _Ctx(two)),
    )  # fmt: skip
    _verified_caller_is_admin.set(False)  # Erika's caregiver
    seen = json.loads(await search_activity_log(_Ctx(two)))
    assert all(e["tool_name"] != "t_bob_only" for e in seen["entries"])


_PREFIX_AUTO = [  # pre-fix auto rows carry the column default and tags '[]'
    ("auto", "patient_slug=bob-test, limit=5", "[]"),
    ("auto", "limit=5", "[]"),
    ("auto", "patient_slug=nobody-here", "[]"),
]


async def _seed_prefix_rows(db):
    rows = [*_PREFIX_AUTO, ("oncoteam", "patient_slug=bob-test", "[]")]  # manual: untouched
    for agent, summary, tags in rows:
        await db.db.execute(
            "INSERT INTO activity_log (session_id, agent_id, tool_name, input_summary, tags) "
            "VALUES ('s', ?, 'old_tool', ?, ?)",
            (agent, summary, tags),
        )
    await db.db.commit()


async def test_backfill_is_admin_only(two):
    from oncofiles.tools.activity import backfill_activity_log_attribution

    out = json.loads(await backfill_activity_log_attribution(_Ctx(two)))
    assert "error" in out


async def test_backfill_reattributes_by_slug_and_marks_the_rest(two):
    from oncofiles.tools.activity import backfill_activity_log_attribution

    await _seed_prefix_rows(two)
    _verified_caller_is_admin.set(True)
    preview = json.loads(await backfill_activity_log_attribution(_Ctx(two)))
    assert preview["dry_run"] is True
    assert preview["scanned"] == preview["remaining"] == len(_PREFIX_AUTO)
    assert preview["attributed_from_slug"] == 1
    assert preview["attribution_unknown"] == len(_PREFIX_AUTO) - 1
    first = json.loads(await backfill_activity_log_attribution(_Ctx(two), 2, dry_run=False))
    assert first["scanned"] == 2 and first["remaining"] == 1
    last = json.loads(await backfill_activity_log_attribution(_Ctx(two), 2, dry_run=False))
    assert last["remaining"] == 0
    async with two.db.execute(
        "SELECT agent_id, input_summary, patient_id, tags FROM activity_log ORDER BY id"
    ) as cur:
        got = [dict(r) for r in await cur.fetchall()]
    assert len(got) == 4  # nothing deleted
    assert got[0]["patient_id"] == BOB_UUID
    assert {got[1]["patient_id"], got[2]["patient_id"]} == {"__system_no_patient__"}
    assert all("attribution_unknown" in g["tags"] for g in got[1:3])
    assert got[3]["tags"] == "[]" and got[3]["agent_id"] == "oncoteam"
