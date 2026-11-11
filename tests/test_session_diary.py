"""Session diary (#721 follow-up, maintainer decision 2026-11-11).

``log_conversation`` / ``diary_add`` and the read tools are kept as a
patient-scoped session diary for Claude.ai / Claude Code / ChatGPT sessions.
These tests lock the rules from the infra + privacy review:

* scope: patient resolved via ``_resolve_patient_id``; an admin must pass a
  slug; foreign ids answer ``not found``;
* limits: size caps, daily cap, participant list, control characters;
* ``document_ids`` must belong to the same patient;
* soft delete + restore only, audited with ids only;
* diary text never reaches activity_log, Drive, a cloud model or a fact
  extractor/indexer;
* MCP annotations; the #721 deprecation does not apply to these tools.

All values are synthetic (invented text, dates in 2027).
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from oncofiles.audit_middleware import _summarize_input
from oncofiles.database import Database
from oncofiles.manifest import export_manifest
from oncofiles.models import ActivityLogQuery, ConversationEntry
from oncofiles.persistent_oauth import _verified_caller_is_admin
from oncofiles.tools import conversations as diary
from oncofiles.tools._sunset import ONCOTEAM_SUNSET_PREFIX, ONCOTEAM_SUNSET_TOOLS
from tests.conftest import ERIKA_UUID
from tests.helpers import make_doc

ERIKA_SLUG = "q1b"
NUL, BEL, ESC, DEL, RLO = chr(0), chr(7), chr(27), chr(127), chr(0x202E)
BOB_UUID = "00000000-0000-4000-8000-000000000002"
BOB_SLUG = "bob-diary-test"

DIARY_TOOL_NAMES = {
    "log_conversation",
    "diary_add",
    "search_conversations",
    "get_conversation",
    "get_conversation_stats",
    "delete_conversation",
    "restore_conversation",
}


class _Ctx:
    """Minimal FastMCP context stub (lifespan db only)."""

    session_id = None

    class _Req:
        def __init__(self, db):
            self.lifespan_context = {"db": db}

    def __init__(self, db):
        self.request_context = self._Req(db)


@pytest.fixture
async def two(db: Database):
    """Jana (bound patient of the non-admin caller) and Bob, one entry each."""
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (BOB_UUID, BOB_SLUG, "Bob Invented", "bob@example.invalid"),
    )
    await db.db.commit()
    jana = await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2027, 2, 1),
            title="Jana invented note",
            content="quokka jana body",
            tags=["shared-tag"],
            source="live",
        ),
        patient_id=ERIKA_UUID,
    )
    bob = await db.insert_conversation_entry(
        ConversationEntry(
            entry_date=date(2027, 2, 2),
            title="Bob invented note",
            content="quokka bob secret body",
            tags=["shared-tag"],
            source="live",
        ),
        patient_id=BOB_UUID,
    )
    return SimpleNamespace(db=db, ctx=_Ctx(db), jana=jana.id, bob=bob.id)


@pytest.fixture
def as_admin():
    tok = _verified_caller_is_admin.set(True)
    yield
    _verified_caller_is_admin.reset(tok)


def _j(raw: str) -> dict:
    return json.loads(raw)


async def _add(ctx, **kw) -> dict:
    kw.setdefault("title", "Invented title")
    kw.setdefault("content", "Invented body text.")
    return _j(await diary.log_conversation(ctx, **kw))


# ── scope / isolation ────────────────────────────────────────────────────


async def test_foreign_entry_id_is_not_found_everywhere(two):
    ctx = two.ctx
    for raw in (
        await diary.get_conversation(ctx, two.bob),
        await diary.delete_conversation(ctx, two.bob),
        await diary.restore_conversation(ctx, two.bob),
    ):
        out = _j(raw)
        assert out["code"] == "not_found"
        assert "Bob" not in raw and "secret" not in raw
    # Bob's row is untouched by the attempted delete.
    assert await two.db.get_conversation_entry(two.bob, patient_id=BOB_UUID) is not None
    # A non-existent id answers exactly like a foreign one (no existence oracle).
    missing = _j(await diary.get_conversation(ctx, 999_999))
    assert missing["code"] == "not_found"


async def test_non_admin_cannot_name_another_patient(two):
    for call in (
        diary.get_conversation(two.ctx, two.bob, patient_slug=BOB_SLUG),
        diary.search_conversations(two.ctx, text="quokka", patient_slug=BOB_SLUG),
        diary.get_conversation_stats(two.ctx, patient_slug=BOB_SLUG),
        diary.log_conversation(two.ctx, title="t", content="c", patient_slug=BOB_SLUG),
        diary.delete_conversation(two.ctx, two.bob, patient_slug=BOB_SLUG),
    ):
        out = _j(await call)
        assert "access denied" in out["error"]
    async with two.db.db.execute(
        "SELECT COUNT(*) AS c FROM conversation_entries WHERE patient_id = ?", (BOB_UUID,)
    ) as cur:
        assert (await cur.fetchone())["c"] == 1


async def test_search_and_stats_are_scoped(two):
    found = _j(await diary.search_conversations(two.ctx, text="quokka"))
    assert [e["id"] for e in found["entries"]] == [two.jana]
    by_tag = _j(await diary.search_conversations(two.ctx, tags="shared-tag"))
    assert [e["id"] for e in by_tag["entries"]] == [two.jana]
    stats = _j(await diary.get_conversation_stats(two.ctx))
    assert stats["total"] == 1 and stats["patient_id"] == ERIKA_UUID
    assert stats["top_tags"] == [{"tag": "shared-tag", "count": 1}]


@pytest.mark.usefixtures("as_admin")
async def test_admin_must_pass_slug(two):
    calls = [
        diary.log_conversation(two.ctx, title="t", content="c"),
        diary.diary_add(two.ctx, title="t", content="c"),
        diary.search_conversations(two.ctx),
        diary.get_conversation(two.ctx, two.jana),
        diary.get_conversation_stats(two.ctx),
        diary.delete_conversation(two.ctx, two.jana),
        diary.restore_conversation(two.ctx, two.jana),
    ]
    for call in calls:
        out = _j(await call)
        assert "patient_slug" in out["error"], out
    # With a slug the admin reaches exactly that patient.
    bob = _j(await diary.get_conversation(two.ctx, two.bob, patient_slug=BOB_SLUG))
    assert bob["title"] == "Bob invented note"
    jana_view = _j(await diary.get_conversation(two.ctx, two.bob, patient_slug=ERIKA_SLUG))
    assert jana_view["code"] == "not_found"
    added = _j(await diary.diary_add(two.ctx, title="t", content="c", patient_slug=BOB_SLUG))
    assert await two.db.check_conversation_entry_ownership(added["id"], BOB_UUID)


# ── write validation, caps ───────────────────────────────────────────────


async def test_write_round_trip_and_alias(db: Database):
    ctx = _Ctx(db)
    a = await _add(ctx, tags="x, y ,x", entry_date="2027-03-04", entry_type="decision")
    assert a["tags"] == ["x", "y"] and a["entry_type"] == "decision"
    b = _j(await diary.diary_add(ctx, title="Alias", content="Alias body", participant="chatgpt"))
    assert b["participant"] == "chatgpt"
    full = _j(await diary.get_conversation(ctx, a["id"]))
    assert full["entry_date"] == "2027-03-04" and full["source"] == "live"


@pytest.mark.parametrize("participant", ["claude.ai", "claude-code", "chatgpt", "gemini", "other"])
async def test_valid_participants(db: Database, participant):
    out = await _add(_Ctx(db), participant=participant)
    assert out["participant"] == participant


@pytest.mark.parametrize("participant", ["oncoteam", "Claude.ai", "", "x" * 300, "claude.ai\n"])
async def test_invalid_participant_rejected(db: Database, participant):
    out = await _add(_Ctx(db), participant=participant)
    assert "Invalid participant" in out["error"]


async def test_invalid_entry_type_rejected(db: Database):
    assert "Invalid entry_type" in (await _add(_Ctx(db), entry_type="diagnosis"))["error"]


async def test_content_size_cap(db: Database):
    ctx = _Ctx(db)
    ok = await _add(ctx, content="a" * diary.DIARY_MAX_CONTENT_BYTES)
    assert "id" in ok
    # The cap is in UTF-8 bytes: 2-byte characters hit it at half the length.
    big = await _add(ctx, content="é" * (diary.DIARY_MAX_CONTENT_BYTES // 2 + 1))
    assert big["code"] == "too_large"
    long_title = await _add(ctx, title="t" * (diary.DIARY_MAX_TITLE_CHARS + 1))
    assert long_title["code"] == "too_large"


async def test_tag_and_document_id_caps(db: Database):
    ctx = _Ctx(db)
    many = ",".join(f"t{i}" for i in range(diary.DIARY_MAX_TAGS + 1))
    assert "At most" in (await _add(ctx, tags=many))["error"]
    assert "longer than" in (await _add(ctx, tags="t" * 51))["error"]
    ids = ",".join(str(i) for i in range(diary.DIARY_MAX_DOCUMENT_IDS + 1))
    assert "At most" in (await _add(ctx, document_ids=ids))["error"]


async def test_empty_after_cleaning_rejected(db: Database):
    out = await _add(_Ctx(db), title="\x00\x01", content="body")
    assert "must not be empty" in out["error"]


async def test_daily_cap(db: Database, monkeypatch):
    monkeypatch.setattr(diary, "DIARY_MAX_ENTRIES_PER_DAY", 2)
    ctx = _Ctx(db)
    first = await _add(ctx)
    await _add(ctx)
    capped = await _add(ctx)
    assert capped["code"] == "rate_limited"
    # Deleting does not free a slot (no delete/re-add loop).
    await diary.delete_conversation(ctx, first["id"])
    assert (await _add(ctx))["code"] == "rate_limited"


async def test_daily_cap_is_per_patient(two, monkeypatch, as_admin):
    # The fixture already wrote one live entry per patient today.
    monkeypatch.setattr(diary, "DIARY_MAX_ENTRIES_PER_DAY", 2)
    assert "id" in await _add(two.ctx, patient_slug=ERIKA_SLUG)
    assert (await _add(two.ctx, patient_slug=ERIKA_SLUG))["code"] == "rate_limited"
    assert "id" in await _add(two.ctx, patient_slug=BOB_SLUG)


async def test_control_characters_stripped(db: Database):
    ctx = _Ctx(db)
    out = await _add(
        ctx,
        title="Ti" + NUL + "tle" + RLO,
        content="first\r\nsecond" + ESC + "[m\tend" + DEL,
        tags="ok" + BEL + "tag",
    )
    full = _j(await diary.get_conversation(ctx, out["id"]))
    assert full["title"] == "Title"
    assert full["content"] == "first\nsecond[m\tend"
    assert full["tags"] == ["oktag"]


async def test_document_ids_must_belong_to_patient(two):
    db = two.db
    mine = await db.insert_document(make_doc(file_id="f-diary-mine"), patient_id=ERIKA_UUID)
    theirs = await db.insert_document(make_doc(file_id="f-diary-bob"), patient_id=BOB_UUID)

    ok = await _add(two.ctx, document_ids=str(mine.id))
    assert ok["document_ids"] == [mine.id]

    for ids in (str(theirs.id), f"{mine.id},{theirs.id}", "987654"):
        out = await _add(two.ctx, document_ids=ids)
        assert out["code"] == "not_found", ids
        assert "Bob" not in json.dumps(out)

    await db.delete_document(mine.id, patient_id=ERIKA_UUID)
    trashed = await _add(two.ctx, document_ids=str(mine.id))
    assert trashed["code"] == "not_found"


# ── soft delete, restore, audit ──────────────────────────────────────────


async def test_delete_and_restore_tools(db: Database):
    ctx = _Ctx(db)
    eid = (await _add(ctx, title="Trash me"))["id"]

    out = _j(await diary.delete_conversation(ctx, eid))
    assert out["deleted"] is True
    assert _j(await diary.get_conversation(ctx, eid))["code"] == "not_found"
    assert _j(await diary.search_conversations(ctx))["total"] == 0
    assert _j(await diary.delete_conversation(ctx, eid))["code"] == "not_found"
    # Still in the table (soft delete only).
    async with db.db.execute(
        "SELECT deleted_at FROM conversation_entries WHERE id = ?", (eid,)
    ) as cur:
        assert (await cur.fetchone())["deleted_at"]

    restored = _j(await diary.restore_conversation(ctx, eid))
    assert restored["restored"] is True and restored["title"] == "Trash me"
    assert _j(await diary.get_conversation(ctx, eid))["title"] == "Trash me"
    assert _j(await diary.restore_conversation(ctx, eid))["code"] == "not_found"


async def test_every_write_is_audited_with_ids_only(db: Database):
    ctx = _Ctx(db)
    eid = (await _add(ctx, title="Sensitive invented title", content="Sensitive body"))["id"]
    await diary.delete_conversation(ctx, eid)
    await diary.restore_conversation(ctx, eid)

    rows = await db.search_activity_log(ActivityLogQuery(patient_id=ERIKA_UUID, limit=50))
    names = [r.tool_name for r in rows]
    assert "diary_add" in names
    assert "soft_delete:conversation_entries" in names
    assert "restore:conversation_entries" in names
    add = next(r for r in rows if r.tool_name == "diary_add")
    assert add.input_summary == f"conversation_entries id={eid}"
    dumped = json.dumps([r.model_dump(mode="json") for r in rows])
    assert "Sensitive" not in dumped


def test_audit_middleware_redacts_diary_text():
    msg = SimpleNamespace(
        arguments={
            "title": "Invented secret title",
            "content": "Invented secret body",
            "tags": "secret-tag",
            "participant": "claude.ai",
            "patient_slug": "q1b",
        }
    )
    for tool in ("log_conversation", "diary_add"):
        summary = _summarize_input(msg, tool)
        assert "secret" not in summary
        assert "participant=claude.ai" in summary and "patient_slug=q1b" in summary
    search = _summarize_input(
        SimpleNamespace(arguments={"text": "secret words"}), "search_conversations"
    )
    assert "secret" not in search
    # Since #758 every tool goes through the same allowlist renderer.
    assert "secret" not in _summarize_input(msg, "search_documents")


# ── search bugs fixed in the review ──────────────────────────────────────


async def test_punctuation_only_text_matches_nothing(two):
    out = _j(await diary.search_conversations(two.ctx, text="?!*"))
    assert out["total"] == 0


async def test_tag_filter_is_exact(db: Database):
    ctx = _Ctx(db)
    await _add(ctx, tags="abc")
    await _add(ctx, tags="a_c")
    hits = {"a%": False, "a_c": True, "abc": True, "ab": False}
    for pattern, expected in hits.items():
        total = _j(await diary.search_conversations(ctx, tags=pattern))["total"]
        assert bool(total) is expected, pattern


async def test_paging(db: Database):
    ctx = _Ctx(db)
    for i in range(5):
        await _add(ctx, title=f"Entry {i}", entry_date=f"2027-01-0{i + 1}")
    first = _j(await diary.search_conversations(ctx, limit=2))
    assert first["has_more"] is True
    assert len(first["entries"]) == 2
    last = _j(await diary.search_conversations(ctx, limit=2, offset=4))
    assert last["has_more"] is False
    assert len(last["entries"]) == 1
    newest = [e["title"] for e in first["entries"]]
    assert newest == ["Entry " + str(i) for i in (4, 3)]
    clamped = _j(await diary.search_conversations(ctx, limit=10_000, offset=-5))
    assert clamped["limit"] == diary.DIARY_SEARCH_MAX_LIMIT and clamped["offset"] == 0


async def test_malformed_legacy_tags_do_not_break_reads(db: Database):
    ctx = _Ctx(db)
    good = (await _add(ctx, tags="fine"))["id"]
    await db.db.execute(
        "INSERT INTO conversation_entries (entry_date, title, content, tags, document_ids, "
        "patient_id) VALUES ('2027-04-01', 'Legacy', 'legacy body', 'not json', '[1, \"x\"]', ?)",
        (ERIKA_UUID,),
    )
    await db.db.commit()
    found = _j(await diary.search_conversations(ctx))
    assert found["total"] == 2
    assert _j(await diary.search_conversations(ctx, tags="fine"))["entries"][0]["id"] == good
    stats = _j(await diary.get_conversation_stats(ctx))
    assert stats["total"] == 2 and stats["top_tags"] == [{"tag": "fine", "count": 1}]


# ── local only: never a fact source, never exported off the server ───────

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
DIARY_ACCESS_RE = re.compile(
    r"conversation_entr|get_conversation_timeline|search_conversation_entries"
    r"|get_conversation_entry|ConversationEntry|tools\.conversations"
)
# Modules that may touch the diary, each with its reason. May only shrink.
DIARY_ACCESS_ALLOWLIST = {
    "tools/conversations.py": "the diary tools themselves",
    "database/_conversations.py": "diary DB layer",
    "database/_converters.py": "row → ConversationEntry",
    "database/_facts.py": "link ownership check (no content read)",
    "models.py": "ConversationEntry model",
    "manifest.py": "user-requested export_manifest (EGRESS); Drive copy excludes the diary",
    "import_transcripts.py": "manual CLI import into one patient's diary",
    "tools/db_query.py": "patient-scoped query_db allowlist (user acting)",
    "server.py": "tool registration",
}
# Extractors, indexers and every module that talks to a model.
FORBIDDEN = ("curated/", "enhance.py", "doc_analysis.py", "tools/curated.py",
             "tools/clinical_facts.py", "tools/clinical_records.py", "tools/enhance_tools.py",
             "sync.py", "gdrive_sync", "gmail_sync.py", "calendar_sync.py")  # fmt: skip


def _diary_readers() -> set[str]:
    found = set()
    for path in SRC.rglob("*.py"):
        code = "\n".join(
            line for line in path.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        )  # fmt: skip
        if DIARY_ACCESS_RE.search(code):
            found.add(path.relative_to(SRC).as_posix())
    return found


def test_no_extractor_or_indexer_reads_the_diary():
    readers = _diary_readers()
    unexpected = readers - set(DIARY_ACCESS_ALLOWLIST)
    assert not unexpected, (
        "session diary text must stay local and is never a fact source; "
        f"new modules touching conversation_entries: {sorted(unexpected)}"
    )
    for rel in readers:
        assert not any(rel.startswith(f) or rel == f for f in FORBIDDEN), rel


def test_diary_access_allowlist_has_no_stale_entries():
    assert set(DIARY_ACCESS_ALLOWLIST) <= _diary_readers()


def test_diary_tools_never_call_a_cloud_model():
    text = (SRC / "tools" / "conversations.py").read_text(encoding="utf-8")
    for needle in ("_get_client", "anthropic", "FilesClient", "enhance", "doc_analysis"):
        assert needle not in text, needle


async def test_drive_manifest_leaves_the_diary_out(db: Database):
    await _add(_Ctx(db), title="Local only note")
    full = await export_manifest(db, patient_id=ERIKA_UUID)
    drive = await export_manifest(db, patient_id=ERIKA_UUID, include_diary=False)
    assert [e["title"] for e in full["conversation_entries"]] == ["Local only note"]
    assert drive["conversation_entries"] == []
    sync_src = (SRC / "sync.py").read_text(encoding="utf-8")
    assert "include_diary=False" in sync_src
    assert "conversation-log" not in sync_src


def test_diary_table_is_never_hard_deleted():
    pattern = re.compile(r"(?i:DELETE\s+FROM)\s+conversation_entries\b")
    for path in SRC.rglob("*.py"):
        assert not pattern.search(path.read_text(encoding="utf-8")), path


# ── annotations and the #721 deprecation ────────────────────────────────


async def test_tool_annotations():
    from oncofiles.server import mcp

    tools = {t.name: t for t in await mcp.list_tools()}
    assert set(tools) >= DIARY_TOOL_NAMES

    def ann(name):
        return tools[name].annotations

    for name in ("search_conversations", "get_conversation", "get_conversation_stats"):
        assert ann(name).readOnlyHint is True, name
        assert ann(name).destructiveHint is False, name
    for name in ("log_conversation", "diary_add", "delete_conversation", "restore_conversation"):
        assert ann(name).readOnlyHint is False, name
    assert ann("delete_conversation").destructiveHint is True
    for name in DIARY_TOOL_NAMES:
        assert ann(name).openWorldHint is False, name
    # A bulk export of the diary is EGRESS: never read-only.
    export = tools["export_manifest"].annotations
    assert export is None or export.readOnlyHint is not True


async def test_diary_tools_are_not_deprecated():
    from oncofiles.server import mcp

    assert not (DIARY_TOOL_NAMES & ONCOTEAM_SUNSET_TOOLS)
    for t in await mcp.list_tools():
        if t.name in DIARY_TOOL_NAMES:
            desc = t.description or ""
            assert not desc.startswith(ONCOTEAM_SUNSET_PREFIX), t.name
            assert "DEPRECATED" not in desc, t.name
            assert "session diary" in desc.lower() or "session-diary" in desc.lower(), t.name


async def test_log_conversation_stores_again(db: Database):
    out = await _add(_Ctx(db))
    assert "deprecated" not in json.dumps(out)
    assert await db.get_conversation_entry(out["id"], patient_id=ERIKA_UUID) is not None
