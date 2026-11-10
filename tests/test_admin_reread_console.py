"""Admin console for verify-only re-reads and re-OCR (#573). Synthetic data only
(invented values and filenames, dates 2027+)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import server
from oncofiles.tools import curated as tool
from tests.helpers import ERIKA_UUID, TEST_PATIENT_UUID

COWORK = {"unverified_reasons": ["cowork_ocr"]}
VALUE = 6.12  # invented; must never appear in an API response
DAY = "2027-05-11"
ELEVEN_IDS = list(range(700, 711))  # one over the per-job cap


@pytest.fixture(autouse=True)
def _reset_jobs():
    tool._reocr_jobs.clear()
    tool._runs.clear()
    yield
    for job in tool._reocr_jobs.values():
        task = job.get("task")
        if hasattr(task, "done") and not task.done():
            task.cancel()
    tool._reocr_jobs.clear()
    tool._runs.clear()


async def _doc(db, doc_id, pid, *, category="labs"):
    meta = {"curated": {"sidecar_id": f"side{doc_id}", "sidecar_sha": "sha", "source_md5": "md"}}
    await db.db.execute(
        "INSERT INTO documents (id, file_id, filename, original_filename, category, mime_type, "
        "size_bytes, patient_id, gdrive_id, structured_metadata) "
        "VALUES (?, ?, ?, ?, ?, 'application/pdf', 1, ?, ?, ?)",
        (doc_id, f"gdrive:g{doc_id}", f"20270511_Synth_{doc_id}.pdf",
         f"20270511_Synth_{doc_id}.pdf", category, pid, f"g{doc_id}", json.dumps(meta)),
    )  # fmt: skip


async def _fact(db, doc_id, pid, code, *, meta=COWORK, verification="unverified"):
    await db.db.execute(
        "INSERT INTO clinical_records (patient_id, record_type, source_document_id, param, "
        "code, occurred_at, value_canonical, verification, metadata_json, page_number, "
        "source, evidence) VALUES (?, 'lab', ?, ?, ?, ?, ?, ?, ?, 1, 't', ?)",
        (pid, doc_id, code, code, DAY, VALUE, verification, json.dumps(meta),
         f"{code} {VALUE} 11.5.2027"),
    )  # fmt: skip


async def _seed(db):
    await db.db.execute(
        "INSERT OR IGNORE INTO patients (patient_id, slug, display_name) VALUES (?, ?, ?)",
        (TEST_PATIENT_UUID, "synth-other", "Synthetic Other"),
    )
    await _doc(db, 501, ERIKA_UUID)
    await _doc(db, 502, ERIKA_UUID, category="report")
    await _doc(db, 503, ERIKA_UUID)  # only handwritten facts: not a candidate
    await _doc(db, 601, TEST_PATIENT_UUID)  # another patient's document
    await _fact(db, 501, ERIKA_UUID, "WBC")
    await _fact(db, 501, ERIKA_UUID, "PLT")
    await _fact(db, 501, ERIKA_UUID, "HGB", verification="machine", meta={})
    await _fact(db, 502, ERIKA_UUID, "NA")
    await _fact(db, 503, ERIKA_UUID, "K", meta={"unverified_reasons": ["handwritten"]})
    await _fact(db, 601, TEST_PATIENT_UUID, "WBC")
    await db.db.commit()


def _request(db, *, body=None, query=None, headers=None):
    req = MagicMock()
    req.app.state.fastmcp_server._lifespan_result = {"db": db}
    req.json = AsyncMock(return_value=body if body is not None else {})
    req.query_params = query or {}
    hdrs = headers or {}
    req.headers.get = lambda key, default=None: hdrs.get(key, default)
    return req


async def _call(endpoint, req, *, admin: bool, email="someone@example.com"):
    with (
        patch.object(server, "_check_bearer", return_value=object()),  # not a bearer
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        resp = await endpoint(req)
    return resp, json.loads(resp.body)


ENDPOINTS = [
    ("api_admin_reread", {"patient_slug": ERIKA_UUID, "document_ids": [501]}, None),
    ("api_admin_reocr", {"patient_slug": ERIKA_UUID, "document_ids": [501]}, None),
    ("api_admin_reocr_status", None, {"patient_slug": ERIKA_UUID}),
    ("api_admin_reread_candidates", None, {"patient_slug": ERIKA_UUID}),
]


# ── access control ───────────────────────────────────────────────────


@pytest.mark.parametrize("name,body,query", ENDPOINTS)
async def test_non_admin_gets_403_on_every_endpoint(db, name, body, query):
    await _seed(db)
    with (
        patch.object(tool, "_reread_job", AsyncMock()) as rr,
        patch.object(tool, "_reocr_job", AsyncMock()) as ro,
    ):
        resp, data = await _call(
            getattr(server, name), _request(db, body=body, query=query), admin=False
        )
    assert resp.status_code == 403 and data == {"error": "admin only"}
    assert not rr.called and not ro.called and tool._reocr_jobs == {}


@pytest.mark.parametrize("name,body,query", ENDPOINTS)
async def test_unauthenticated_gets_401(db, name, body, query):
    resp, _ = await _call(
        getattr(server, name), _request(db, body=body, query=query), admin=False, email=None
    )
    assert resp.status_code == 401


async def test_cross_origin_post_is_refused(db):
    await _seed(db)
    req = _request(
        db,
        body={"patient_slug": ERIKA_UUID, "document_ids": [501]},
        headers={"origin": "https://evil.example", "host": "oncofiles.test"},
    )
    with patch.object(tool, "_reread_job", AsyncMock()) as rr:
        resp, _ = await _call(server.api_admin_reread, req, admin=True)
    assert resp.status_code == 403 and not rr.called


# ── re-read ──────────────────────────────────────────────────────────


async def test_admin_dry_run_starts_a_reread_job(db):
    await _seed(db)
    req = _request(
        db,
        body={"patient_slug": ERIKA_UUID, "document_ids": [501, 502]},
        headers={"origin": "https://oncofiles.test", "host": "oncofiles.test"},
    )
    with patch.object(tool, "_reread_job", AsyncMock()) as rr:
        resp, data = await _call(server.api_admin_reread, req, admin=True)
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    assert resp.status_code == 202
    assert data["state"] == "running" and data["kind"] == "reread" and data["dry_run"] is True
    assert sorted(d["document_id"] for d in data["documents"]) == [501, 502]
    args, kwargs = rr.call_args
    assert args[1] == ERIKA_UUID and kwargs == {"dry_run": True}
    assert sorted(d["id"] for d in args[2]) == [501, 502]


async def test_reread_dry_run_is_the_default_and_only_explicit_false_stores(db):
    await _seed(db)
    for raw, expected in ((None, True), ("false", True), (0, True), (False, False)):
        tool._reocr_jobs.clear()
        body = {"patient_slug": ERIKA_UUID, "document_ids": [501]}
        if raw is not None:
            body["dry_run"] = raw
        with patch.object(tool, "_reread_job", AsyncMock()) as rr:
            resp, _ = await _call(server.api_admin_reread, _request(db, body=body), admin=True)
            await tool._reocr_jobs[ERIKA_UUID]["task"]
        assert resp.status_code == 202
        assert rr.call_args.kwargs == {"dry_run": expected}, raw


@pytest.mark.parametrize(
    "ids,fragment",
    [
        (ELEVEN_IDS, "at most 10"),
        ([501, 601], "not found for this patient"),  # another patient's document
        ([501, 31337], "not found for this patient"),  # no such document
        ("501", "list of integers"),
        ([True], "list of integers"),
        (["501"], "list of integers"),
        ([], "document_ids is required"),
        (None, "document_ids is required"),
    ],
)
async def test_reread_validation(db, ids, fragment):
    await _seed(db)
    body = {"patient_slug": ERIKA_UUID}
    if ids is not None:
        body["document_ids"] = ids
    with patch.object(tool, "_reread_job", AsyncMock()) as rr:
        resp, data = await _call(server.api_admin_reread, _request(db, body=body), admin=True)
    assert resp.status_code == 400 and fragment in data["error"]
    assert not rr.called and tool._reocr_jobs == {}


async def test_unknown_patient_is_404(db):
    body = {"patient_slug": "no-such-patient", "document_ids": [501]}
    resp, _ = await _call(server.api_admin_reread, _request(db, body=body), admin=True)
    assert resp.status_code == 404


async def test_busy_patient_is_409(db):
    await _seed(db)
    tool._reocr_jobs[ERIKA_UUID] = {"state": "running", "kind": "reocr"}
    body = {"patient_slug": ERIKA_UUID, "document_ids": [501]}
    with patch.object(tool, "_reread_job", AsyncMock()) as rr:
        resp, data = await _call(server.api_admin_reread, _request(db, body=body), admin=True)
    assert resp.status_code == 409 and "busy" in data["error"] and not rr.called


# ── re-OCR ───────────────────────────────────────────────────────────


async def test_admin_reocr_dry_run_reuses_the_tool_core(db):
    await _seed(db)
    body = {"patient_slug": ERIKA_UUID, "document_ids": [501]}
    with patch.object(tool, "_reocr_job", AsyncMock()) as ro:
        resp, data = await _call(server.api_admin_reocr, _request(db, body=body), admin=True)
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    assert resp.status_code == 202 and data["dry_run"] is True and data["kind"] == "reocr"
    assert [d["document_id"] for d in data["documents"]] == [501]
    assert ro.call_args.kwargs == {"dry_run": True, "passes": 2}


async def test_reocr_rejects_foreign_ids(db):
    await _seed(db)
    body = {"patient_slug": ERIKA_UUID, "document_ids": [601]}
    with patch.object(tool, "_reocr_job", AsyncMock()) as ro:
        resp, _ = await _call(server.api_admin_reocr, _request(db, body=body), admin=True)
    assert resp.status_code == 400 and not ro.called


# ── candidates and status: counts only ───────────────────────────────


async def test_candidates_count_cowork_facts_only_for_that_patient(db):
    await _seed(db)
    resp, data = await _call(
        server.api_admin_reread_candidates,
        _request(db, query={"patient_slug": ERIKA_UUID}),
        admin=True,
    )
    assert resp.status_code == 200
    docs = data["documents"]
    assert [(d["document_id"], d["category"], d["cowork_unverified"]) for d in docs] == [
        (501, "labs", 2),
        (502, "report", 1),
    ]
    assert set(docs[0]) == {"document_id", "filename", "category", "cowork_unverified"}
    raw = resp.body.decode()
    assert str(VALUE) not in raw and DAY not in raw and "11.5.2027" not in raw


async def test_status_never_contains_fact_values(db):
    await _seed(db)
    tool._reocr_jobs[ERIKA_UUID] = {
        "state": "done", "kind": "reocr", "dry_run": False, "task": object(),
        "started_at": "2027-06-01T10:00:00+00:00",
        "documents": [{"document_id": 501, "filename": "20270511_Synth_501.pdf",
                       "category": "labs", "reasons": ["cowork_ocr"], "unverified_facts": 2}],
        "report": {"dry_run": False, "documents": [{
            "document_id": 501, "filename": "20270511_Synth_501.pdf", "result": "review",
            "lost": [f"WBC {VALUE} missing", f"PLT {VALUE} re-dated {DAY}->2027-05-12"],
            "backup": "_reocr-backup-x.md", "facts_machine": 3, "seconds": 12.5,
            "stored": {"confirmed": [["WBC", "hash", DAY, 1]]},
            "evidence": f"WBC {VALUE}",
        }]},
        "verification": {"coded": 10, "unverified": 2, "unverified_pct": 20.0,
                         "reasons": {"cowork_ocr": 2}, "note": f"WBC {VALUE}"},
        "index": {"indexed": 1, "detail": [f"{VALUE}"]},
    }  # fmt: skip
    resp, data = await _call(
        server.api_admin_reocr_status,
        _request(db, query={"patient_slug": ERIKA_UUID}),
        admin=True,
    )
    assert resp.status_code == 200
    raw = resp.body.decode()
    assert str(VALUE) not in raw and DAY not in raw and "task" not in data
    [r] = data["results"]
    assert r["lost_count"] == 2 and r["facts_machine"] == 3 and r["seconds"] == 12.5
    assert "lost" not in r and "stored" not in r and "evidence" not in r
    assert data["verification"] == {
        "coded": 10, "unverified": 2, "unverified_pct": 20.0, "reasons": {"cowork_ocr": 2}
    }  # fmt: skip
    assert data["index"] == {"indexed": 1}


async def test_status_after_restart_reads_the_persisted_reread_job(db):
    await _seed(db)
    row = await db.insert_curated_run(patient_id=ERIKA_UUID, dry_run=True, trigger="reread")
    report = {"dry_run": True, "documents": [{
        "document_id": 501, "result": "previewed", "confirmed": 1, "date_unconfirmed": 1,
        "unconfirmed": 0, "candidates": 2, "seconds": 40.2, "engine": "qwen-local",
    }]}  # fmt: skip
    await db.finish_curated_run(row, patient_id=ERIKA_UUID, status="ok", message="",
                                report=report)  # fmt: skip
    resp, data = await _call(
        server.api_admin_reocr_status,
        _request(db, query={"patient_slug": ERIKA_UUID}),
        admin=True,
    )
    assert data["state"] == "ok" and data["kind"] == "reread" and data["dry_run"] is True
    assert data["results"] == [report["documents"][0]]
    # another patient sees nothing of it
    resp, data = await _call(
        server.api_admin_reocr_status,
        _request(db, query={"patient_slug": TEST_PATIENT_UUID}),
        admin=True,
    )
    assert data["state"] == "none" and data["results"] == []


async def test_mcp_tools_still_answer_through_the_shared_core(db):
    await _seed(db)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    with patch.object(tool, "_reread_job", AsyncMock()):
        out = json.loads(await tool.curated_reread_verify(ctx, document_ids=[501, 601]))
        await tool._reocr_jobs[ERIKA_UUID]["task"]
    # the MCP tool keeps its contract: foreign ids are ignored, not an error
    assert [d["document_id"] for d in out["documents"]] == [501]
    status = json.loads(await tool.curated_reocr_status(ctx))
    assert status["kind"] == "reread"
