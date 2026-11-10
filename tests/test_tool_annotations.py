"""#608: MCP tool annotations (readOnlyHint / destructiveHint).

Locks the declarative classification in ``oncofiles.tool_annotations``:
every registered tool is classified, read-only tools carry
``readOnlyHint=true`` on the wire, and nothing that writes is marked
read-only.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from oncofiles.tool_annotations import (
    DESTRUCTIVE_TOOLS,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
)

# Modules that annotate their own tools or belong to another work stream.
SELF_MANAGED_MODULES = ("oncofiles.tools.clinical_facts", "oncofiles.tools.curated")


@pytest.fixture(scope="module")
def tools():
    from oncofiles.server import mcp

    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def _managed(tools):
    return {n: t for n, t in tools.items() if t.fn.__module__ not in SELF_MANAGED_MODULES}


def test_sets_are_disjoint():
    assert not READ_ONLY_TOOLS & DESTRUCTIVE_TOOLS
    assert not READ_ONLY_TOOLS & WRITE_TOOLS
    assert not DESTRUCTIVE_TOOLS & WRITE_TOOLS


def test_every_tool_is_classified(tools):
    """A new tool must be put in READ_ONLY_TOOLS, DESTRUCTIVE_TOOLS or
    WRITE_TOOLS (oncofiles/tool_annotations.py) before it ships."""
    classified = READ_ONLY_TOOLS | DESTRUCTIVE_TOOLS | WRITE_TOOLS
    missing = sorted(set(_managed(tools)) - classified)
    assert missing == [], f"classify these tools in tool_annotations.py: {missing}"


def test_no_stale_names(tools):
    classified = READ_ONLY_TOOLS | DESTRUCTIVE_TOOLS | WRITE_TOOLS
    assert sorted(classified - set(tools)) == []


def test_read_only_tools_annotated(tools):
    for name in READ_ONLY_TOOLS:
        ann = tools[name].annotations
        assert ann is not None, name
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is False, name


def test_destructive_tools_annotated(tools):
    for name in DESTRUCTIVE_TOOLS:
        ann = tools[name].annotations
        assert ann is not None and ann.readOnlyHint is False and ann.destructiveHint is True


def test_write_tools_keep_spec_defaults(tools):
    for name in WRITE_TOOLS:
        ann = tools[name].annotations
        assert ann is None or ann.readOnlyHint is not True, name


def test_self_managed_annotations_untouched(tools):
    """clinical_facts sets its own READ_ONLY; curated tools are not touched here."""
    for name, t in tools.items():
        if t.fn.__module__ == "oncofiles.tools.curated":
            assert name not in READ_ONLY_TOOLS | DESTRUCTIVE_TOOLS | WRITE_TOOLS


_WRITE_NAME = re.compile(
    r"^(add|delete|update|set|store|upload|restore|rename|repair|reassign|backfill|"
    r"enhance|extract|detect|sync|setup|fetch|log|select|unblock|validate|consolidate)_"
)


def test_read_only_names_do_not_look_like_writes():
    assert [n for n in READ_ONLY_TOOLS if _WRITE_NAME.match(n)] == []


# Calls that write somewhere; none may appear in a read-only tool's body.
_WRITE_CALL = re.compile(
    r"\b(?:db|gdrive|files)\.(?:insert|update|delete|set|save|add|upsert|store|record|"
    r"restore|upload|rename|move|trash|create|mark|replace|log)_?\w*\(|"
    r"_ensure_ocr_text|_get_client\(|\bcommit\(|create_task\(",
)


def test_read_only_bodies_have_no_write_calls(tools):
    offenders = {}
    for name in READ_ONLY_TOOLS:
        hits = _WRITE_CALL.findall(inspect.getsource(tools[name].fn))
        if hits:
            offenders[name] = hits
    assert offenders == {}


def test_annotations_reach_the_wire():
    """What an MCP client actually receives from tools/list."""
    from fastmcp import Client

    from oncofiles.server import mcp

    async def _list():
        async with Client(mcp) as client:
            return {t.name: t for t in await client.list_tools()}

    wire = asyncio.run(_list())
    assert wire["list_documents"].annotations.readOnlyHint is True
    assert wire["query_db"].annotations.readOnlyHint is True
    assert wire["delete_document"].annotations.destructiveHint is True
    upload = wire["upload_document"].annotations
    assert upload is None or upload.readOnlyHint is not True
