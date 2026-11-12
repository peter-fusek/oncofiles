"""#758: the audit trail keeps only allowlisted argument values.

AuditMiddleware used to store the first 100 characters of every tool argument
in ``activity_log.input_summary``. Now only ids, slugs, booleans, enums and
fact codes keep their value; everything else is ``<redacted:len=N>``, and the
same values are scrubbed from the stored error message and from log records.

All values here are synthetic (invented name, invented 2027 date).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any
from unittest.mock import MagicMock

import pytest

from oncofiles import audit_middleware
from oncofiles.audit_middleware import AuditMiddleware
from oncofiles.audit_redaction import redact_stored_summary, summarize_args
from oncofiles.database import Database
from oncofiles.persistent_oauth import _verified_caller_is_admin

FAKE_NAME = "Jana Testovska"
FAKE_VALUE = "137.5 g/L"
FAKE_DATE = "2027-03-14"
FAKE_SQL = "SELECT * FROM documents WHERE patient_name = 'Jana Testovska'"
FAKE_FILE = "20270314_JanaTestovska_TestClinic_Labs_SyntheticPanel.pdf"
FAKE_NUM = 1234.56
FAKE_ID = 424242
FAKE_SLUG = "synthetic-slug"

PHI = [FAKE_NAME, FAKE_VALUE, FAKE_DATE, FAKE_SQL, FAKE_FILE, str(FAKE_NUM), "Testovska"]
_STRINGS = [FAKE_NAME, FAKE_VALUE, FAKE_DATE, FAKE_SQL, FAKE_FILE]


def _types(schema: dict) -> set[str]:
    if "anyOf" in schema:
        out: set[str] = set()
        for s in schema["anyOf"]:
            out |= _types(s)
        return out
    t = schema.get("type")
    return {t} if isinstance(t, str) else set(t or [])


def _items_type(schema: dict) -> str:
    for s in schema.get("anyOf", [schema]):
        if s.get("type") == "array":
            return (s.get("items") or {}).get("type", "string")
    return "string"


def _synthetic_args(props: dict) -> dict[str, Any]:
    """PHI-looking args for every parameter; ids and slugs stay plain."""
    args: dict[str, Any] = {}
    for i, (name, schema) in enumerate(sorted(props.items())):
        types = _types(schema)
        if name.endswith("patient_slug"):
            args[name] = FAKE_SLUG
        elif "boolean" in types:
            args[name] = True
        elif "integer" in types:
            args[name] = FAKE_ID
        elif "number" in types:
            args[name] = FAKE_NUM
        elif "array" in types:
            args[name] = [FAKE_ID] if _items_type(schema) == "integer" else [FAKE_NAME, FAKE_DATE]
        else:
            args[name] = _STRINGS[i % len(_STRINGS)]
    return args


class _Ctx:
    def __init__(self, db):
        self.request_context = MagicMock()
        self.request_context.lifespan_context = {"db": db}


def _mw_context(db, tool: str, args: dict):
    context = MagicMock()
    context.message.name = tool
    context.message.arguments = args
    context.timestamp.strftime.return_value = "20270314-1200"
    context.fastmcp_context = _Ctx(db)
    return context


async def _last_row(db: Database, tool: str) -> dict:
    await asyncio.gather(*list(audit_middleware._audit_tasks))
    async with db.db.execute(
        "SELECT input_summary, error_message FROM activity_log "
        "WHERE tool_name = ? ORDER BY id DESC LIMIT 1",
        (tool,),
    ) as cur:
        return dict(await cur.fetchone())


async def test_sweep_every_registered_tool_redacts_phi(db: Database, caplog):
    """Every tool: no PHI in input_summary, error_message or logs; ids/slugs kept."""
    from oncofiles.server import mcp

    caplog.set_level(logging.DEBUG)
    tool_logger = logging.getLogger("oncofiles.tools.synthetic")
    tools = await mcp.list_tools()
    assert len(tools) > 100  # non-vacuous
    with_id = with_slug = 0
    for tool in tools:
        props = (tool.parameters or {}).get("properties") or {}
        args = _synthetic_args(props)

        async def call_next(_ctx, args=args, name=tool.name):
            # A tool that logs and echoes its arguments, plus a pydantic-style error.
            tool_logger.warning("tool %s got %s", name, args)
            tool_logger.error("failing with %r", args, exc_info=ZeroDivisionError(args))
            pydantic_tail = f"[type=x, input_value='{FAKE_NAME}', input_type=str]"
            raise ValueError(f"bad input {args} {pydantic_tail}")

        caplog.clear()
        with contextlib.suppress(ValueError):
            await AuditMiddleware().on_call_tool(_mw_context(db, tool.name, args), call_next)
        row = await _last_row(db, tool.name)
        stored = f"{row['input_summary']}\n{row['error_message']}"
        logged = (
            "\n".join(
                r.getMessage() + (r.exc_text or "") for r in caplog.records if r.name != "asyncio"
            )
            + caplog.text
        )
        assert f"tool {tool.name} got" in logged  # non-vacuous: the log was captured
        for phi in PHI:
            assert phi not in stored, f"{tool.name}: {phi!r} stored in activity_log"
            assert phi not in logged, f"{tool.name}: {phi!r} in logs"
        summary = row["input_summary"]
        # Parts past the 500-char cap are dropped and counted as <more:count=N>.
        shown = {part.split("=", 1)[0] for part in summary.split(", ") if part}
        id_keys = [k for k, v in args.items() if v == FAKE_ID and k.endswith("_id")]
        for k in id_keys:
            if k in shown:
                assert f"{k}={FAKE_ID}" in summary, tool.name
                with_id += 1
        if "patient_slug" in shown:
            assert f"patient_slug={FAKE_SLUG}" in summary, tool.name
            with_slug += 1
        for k, v in args.items():
            if k in shown and isinstance(v, str) and v in _STRINGS:
                assert f"{k}=<redacted:len={len(v)}>" in summary, (tool.name, k)
        assert len(shown) == len(args) or "<more:count=" in summary, tool.name
    assert with_id > 20 and with_slug > 50  # the kept-value checks actually ran


async def test_logs_outside_a_tool_call_are_untouched(caplog):
    AuditMiddleware()  # installs the record factory
    caplog.set_level(logging.INFO)
    logging.getLogger("oncofiles.synthetic").info("plain %s", FAKE_NAME)
    assert f"plain {FAKE_NAME}" in caplog.text


async def test_fastmcp_validation_warning_is_scrubbed():
    """FastMCP logs `Invalid arguments for tool` with the raw input; scrubbed."""
    from fastmcp import Client, FastMCP

    server = FastMCP("synthetic")
    server.add_middleware(AuditMiddleware())

    @server.tool
    async def synthetic_tool(doc_id: int, note: str = "") -> str:
        raise RuntimeError(f"failed on {note}")

    records: list[logging.LogRecord] = []

    class _Grab(logging.Handler):
        def emit(self, record):
            records.append(record)

    grab = _Grab(level=logging.DEBUG)
    fm_logger = logging.getLogger("fastmcp")
    fm_logger.addHandler(grab)
    try:
        async with Client(server) as client:
            for args in ({"doc_id": FAKE_NAME}, {"doc_id": 1, "note": FAKE_FILE}):
                with contextlib.suppress(Exception):
                    await client.call_tool("synthetic_tool", args)
    finally:
        fm_logger.removeHandler(grab)
    text = "\n".join(r.getMessage() + (r.exc_text or "") for r in records)
    assert "Invalid arguments" in text  # non-vacuous: the warning was emitted
    assert FAKE_NAME not in text and FAKE_FILE not in text


def test_summary_format_is_compatible():
    s = summarize_args(
        {
            "document_id": 7,
            "patient_slug": FAKE_SLUG,
            "category": "labs",
            "kinds": ["lab"],
            "codes": ["CEA", "PLT_X"],
            "dry_run": False,
            "limit": 20,
            "text": FAKE_NAME,
            "status": "high",  # clinical status, not an operational one
            "code": "lower case",
        }
    )
    assert s == (
        f"document_id=7, patient_slug={FAKE_SLUG}, category=labs, kinds=['lab'], "
        "codes=['CEA', 'PLT_X'], dry_run=False, limit=20, text=<redacted:len=14>, "
        "status=<redacted:len=4>, code=<redacted:len=10>"
    )
    assert summarize_args({}) == "" and summarize_args(None) == ""
    assert summarize_args({"patient_slug": "Jana Testovska"}) == "patient_slug=<redacted:len=14>"
    assert summarize_args({"document_ids": list(range(100, 200))}) == "document_ids=<ids:count=100>"


def test_stored_summary_rewrite_is_idempotent():
    legacy = f"doc_id=5, query={FAKE_NAME}, x, sql={FAKE_SQL}, patient_slug={FAKE_SLUG}"
    once = redact_stored_summary(legacy)
    assert FAKE_NAME not in once and "doc_id=5" in once and f"patient_slug={FAKE_SLUG}" in once
    assert redact_stored_summary(once) == once
    new = summarize_args({"document_ids": [7001, 7002], "date_from": FAKE_DATE, "limit": 37})
    assert redact_stored_summary(new) == new


# ── redact_activity_log_inputs maintenance tool ────────────────────────────

_LEGACY_ROWS = [
    f"document_id=7011, patient_slug={FAKE_SLUG}, query={FAKE_NAME}",
    f"sql={FAKE_SQL}",
    f"filename={FAKE_FILE}, dry_run=True",
    "document_id=7012, limit=37",  # already safe
    "",
]


async def _seed(db: Database) -> None:
    for i, summary in enumerate(_LEGACY_ROWS):
        await db.db.execute(
            "INSERT INTO activity_log (session_id, agent_id, tool_name, input_summary, "
            "output_summary, duration_ms, status, error_message, tags, patient_id) "
            "VALUES (?, 'auto', ?, ?, 'out', ?, 'ok', NULL, '[\"attributed\"]', 'pid-x')",
            (f"s{i}", f"tool_{i}", summary, i),
        )
    await db.db.commit()


async def _dump(db: Database) -> list[dict]:
    async with db.db.execute("SELECT * FROM activity_log ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


@pytest.fixture
def non_admin():
    tok = _verified_caller_is_admin.set(False)
    yield
    _verified_caller_is_admin.reset(tok)


async def test_redact_tool_refuses_non_admin(db: Database, non_admin):
    from oncofiles.tools.activity import redact_activity_log_inputs

    await _seed(db)
    before = await _dump(db)
    out = json.loads(await redact_activity_log_inputs(_Ctx(db), dry_run=False))
    assert "error" in out
    assert await _dump(db) == before


@pytest.mark.usefixtures("admin_scope")
async def test_redact_tool_dry_run_writes_nothing(db: Database):
    from oncofiles.tools.activity import redact_activity_log_inputs

    await _seed(db)
    before = await _dump(db)
    out = json.loads(await redact_activity_log_inputs(_Ctx(db)))
    assert out == {
        "scanned": 4,
        "unchanged": 1,
        "remaining": 3,
        "dry_run": True,
        "would_change": 3,
    }
    assert await _dump(db) == before


@pytest.mark.usefixtures("admin_scope")
async def test_redact_tool_rewrites_only_input_summary(db: Database):
    from oncofiles.tools.activity import redact_activity_log_inputs

    await _seed(db)
    before = await _dump(db)
    first = json.loads(await redact_activity_log_inputs(_Ctx(db), dry_run=False, batch_size=2))
    assert first["changed"] == 2 and first["remaining"] == 1
    second = json.loads(await redact_activity_log_inputs(_Ctx(db), dry_run=False))
    assert second["changed"] == 1 and second["remaining"] == 0
    third = json.loads(await redact_activity_log_inputs(_Ctx(db), dry_run=False))
    assert third["changed"] == 0 and third["remaining"] == 0

    after = await _dump(db)
    # Nothing deleted; one counts-only maintenance row per real run.
    assert len(after) == len(before) + 3
    for old, new in zip(before, after, strict=False):
        assert {k: v for k, v in old.items() if k != "input_summary"} == {
            k: v for k, v in new.items() if k != "input_summary"
        }
    summaries = [r["input_summary"] for r in after]
    blob = "\n".join(summaries)
    for phi in (FAKE_NAME, FAKE_SQL, FAKE_FILE):
        assert phi not in blob
    assert summaries[0] == (
        f"document_id=7011, patient_slug={FAKE_SLUG}, query=<redacted:len={len(FAKE_NAME)}>"
    )
    assert summaries[2] == f"filename=<redacted:len={len(FAKE_FILE)}>, dry_run=True"
    assert summaries[3] == "document_id=7012, limit=37"
    maint = [r for r in after[len(before) :] if r["tool_name"] == "redact_activity_log_inputs"]
    assert len(maint) == 3
    assert json.loads(maint[0]["output_summary"])["changed"] == 2
    assert maint[0]["patient_id"] == ""


# ── #769: LLM extractor A/B overrides are diagnostics, not secrets ─────────


def test_llm_override_modes_match_the_extractor():
    from oncofiles.audit_redaction import ENUM_VALUES, INT_KEYS
    from oncofiles.curated import llm_extract_call as c

    assert ENUM_VALUES["thinking"] == set(c.THINKING_MODES)
    assert ENUM_VALUES["sampling"] == set(c.SAMPLING_MODES)
    assert ENUM_VALUES["reasoning_marker"] == set(c.REASONING_MARKER_MODES)
    assert "timeout_s" in INT_KEYS


def test_the_llm_call_log_line_keeps_thinking_off_and_secrets_stay_masked():
    """``thinking="off"`` used to be a call secret (len 3, not allowlisted), so
    every log line of the job showed ``thinking=<redacted:len=3>``."""
    from oncofiles.app_log import RedactFilter
    from oncofiles.audit_redaction import scrub_text, sensitive_strings

    args = {
        "document_ids": [FAKE_ID],
        "patient_slug": FAKE_SLUG,
        "dry_run": True,
        "thinking": "off",
        "sampling": "qwen",
        "reasoning_marker": "off",
        "timeout_s": 300,
        "note": FAKE_NAME,
    }
    assert summarize_args(args) == (
        f"document_ids=[{FAKE_ID}], patient_slug={FAKE_SLUG}, dry_run=True, thinking=off, "
        "sampling=qwen, reasoning_marker=off, timeout_s=300, "
        f"note=<redacted:len={len(FAKE_NAME)}>"
    )
    secrets = sensitive_strings(args)
    assert secrets == (FAKE_NAME,)
    assert "<redacted" in summarize_args({"thinking": FAKE_NAME})  # only known modes pass
    line = (
        "local llm call: prompt_tokens=1200 completion_tokens=300 reasoning_tokens=0 "
        "max_tokens=4096 prompt_estimate=1180 finish=stop seconds=12.5 temperature=0.7 "
        "top_p=0.8 top_k=20 presence_penalty=1.0 thinking=off timeout=300 "
        f"note={FAKE_NAME}"
    )
    scrubbed = scrub_text(line, secrets)
    assert "thinking=off" in scrubbed and "timeout=300" in scrubbed
    assert "max_tokens=4096" in scrubbed and FAKE_NAME not in scrubbed

    secret_line = (
        scrubbed + " token=tokvalueqwerty secret=secvaluexyzzy password=pwvaluexyzzy "
        "api_key=keyvaluexyzzy Authorization: Bearer abcdefghijklmnop "
        "onco_abcdefghijkl jana.testovska@example.com"
    )
    record = logging.LogRecord("t", logging.INFO, __file__, 1, secret_line, None, None)
    RedactFilter().filter(record)
    out = record.getMessage()
    assert "thinking=off" in out and "finish=stop" in out and "max_tokens=4096" in out
    leaks = ("tokvalueqwerty", "secvaluexyzzy", "pwvaluexyzzy", "keyvaluexyzzy", "abcdefghijklmnop",
             "onco_abcdefghijkl", "jana.testovska@example.com")  # fmt: skip
    for leaked in leaks:
        assert leaked not in out, leaked
