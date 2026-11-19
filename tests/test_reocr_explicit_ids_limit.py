"""curated_reocr never silently truncates explicit ``document_ids``.

Before this fix the default ``limit`` cut a longer explicit id list down to
its first few entries. Now, with ids, ``limit`` defaults to their count; more than
10 ids or a ``limit`` below their count is refused with an error. Without ids
the default of 3 is unchanged.

No Drive or model calls: target selection and the job start are stubbed.
Synthetic ids only.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles.tools import curated as tool


def _ctx(db):
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


def _doc(doc_id: int) -> dict:
    return {"document_id": doc_id, "id": doc_id, "filename": f"f{doc_id}.pdf",
            "category": "labs", "reasons": ["cowork_ocr"], "unverified_facts": 1}  # fmt: skip


@pytest.fixture
def stubbed(monkeypatch):
    """Targets = the given ids (or 12 candidates without ids); _launch captured."""
    monkeypatch.setattr(tool, "_reocr_jobs", {})

    async def fake_targets(_db, _pid, document_ids, **_kw):
        return [_doc(i) for i in (document_ids or range(9001, 9013))]

    launch = AsyncMock(side_effect=lambda _db, _pid, job, _fn, **_kw: {**job, "started": True})
    with (
        patch.object(tool, "_reocr_targets", fake_targets),
        patch.object(tool, "_reocr_log_names", AsyncMock(return_value=[])),
        patch.object(tool, "_launch", launch),
    ):
        yield launch


async def _call(db, **kw) -> dict:
    return json.loads(await tool.curated_reocr(_ctx(db), **kw))


async def test_explicit_ids_default_limit_is_their_count(db, stubbed):
    ids = list(range(8001, 8010))  # 9 ids, as in production
    out = await _call(db, document_ids=ids)
    assert "error" not in out
    assert [d["document_id"] for d in out["documents"]] == ids
    assert stubbed.call_count == 1


async def test_more_than_ten_ids_is_refused(db, stubbed):
    out = await _call(db, document_ids=list(range(8001, 8012)))  # 11
    assert "at most 10" in out["error"] and "11" in out["error"]
    assert not stubbed.called and tool._reocr_jobs == {}


async def test_explicit_limit_below_id_count_is_refused(db, stubbed):
    out = await _call(db, document_ids=list(range(8001, 8010)), limit=3)
    assert "limit 3" in out["error"] and "9 document_ids" in out["error"]
    assert not stubbed.called


async def test_explicit_limit_at_or_above_id_count_runs_all(db, stubbed):
    ids = [8001, 8002, 8003, 8004]
    for limit in (4, 10):
        out = await _call(db, document_ids=ids, limit=limit)
        assert [d["document_id"] for d in out["documents"]] == ids


async def test_without_ids_the_default_limit_stays_three(db, stubbed):
    out = await _call(db)
    assert len(out["documents"]) == 3 and out["candidates"] == 12
    out = await _call(db, limit=5)
    assert len(out["documents"]) == 5
