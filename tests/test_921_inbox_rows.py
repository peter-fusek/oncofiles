"""Inbox files show in the dashboard table right away (#921 follow-up).

The curated probe keeps an in-memory snapshot of each patient's inbox
(``tools.curated.inbox_snapshot``); ``GET /api/documents`` turns it into
``inbox: true`` rows (``dashboard_inbox``) without any Drive call, and the
upload route adds its file at once. Synthetic names and ids only.
"""

from __future__ import annotations

import inspect
import re
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from oncofiles import dashboard_inbox, dashboard_upload, document_stage, server
from oncofiles.curated import inbox_upload
from oncofiles.curated.runner import InboxEntry
from oncofiles.tools import curated as curated_tool
from oncofiles.tools.hygiene import _build_document_matrix
from tests.test_dashboard_document_stages import (
    A_EMAIL,
    A_UUID,
    B_EMAIL,
    B_UUID,
    NOBODY_EMAIL,
    _body,
    _caller,
    _curated_doc,
    _request,
)

JS = (Path(server.__file__).parent / "dashboard.js").read_text(encoding="utf-8")
PDF = b"%PDF-1.7\nsynthetic\n"


@pytest.fixture
async def patients(db):
    """Patient A (caregiver A_EMAIL) and patient B (caregiver B_EMAIL)."""
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("stage-a", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "stage-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.commit()
    return db


@pytest.fixture(autouse=True)
def _clean_snapshots():
    curated_tool._inbox_snapshots.clear()
    yield
    curated_tool._inbox_snapshots.clear()


def _probe(signature: str, entries: list[InboxEntry] | None):
    return SimpleNamespace(signature=signature, inbox=entries)


def _entry(n: int, *, review: bool = False) -> InboxEntry:
    return InboxEntry(
        id=f"synthInbox{n:04d}",
        name=f"synthetic scan {n}.pdf",
        size=1000 + n,
        mime="application/pdf",
        modified=f"2031-09-2{n}T06:47:13Z",
        review=review,
    )


def _snapshot(pid: str, *entries: InboxEntry, signature: str = "sig-1") -> None:
    curated_tool.record_inbox_snapshot(pid, _probe(signature, list(entries)))


# ── the in-memory snapshot ────────────────────────────────────────────────


def test_snapshot_keeps_what_the_probe_saw():
    _snapshot(A_UUID, _entry(1), _entry(2, review=True))
    got = {e["id"]: e for e in curated_tool.inbox_snapshot(A_UUID)}
    assert set(got) == {"synthInbox0001", "synthInbox0002"}
    assert got["synthInbox0002"]["review"] is True
    assert got["synthInbox0001"] == {
        "id": "synthInbox0001",
        "name": "synthetic scan 1.pdf",
        "size": 1001,
        "mime": "application/pdf",
        "modified": "2031-09-21T06:47:13Z",
        "review": False,
    }
    assert curated_tool.inbox_snapshot(B_UUID) == []
    assert curated_tool.inbox_snapshot(None) == []


def test_idle_probe_confirms_and_a_new_listing_replaces(monkeypatch):
    _snapshot(A_UUID, _entry(1))
    old = curated_tool._inbox_snapshots[A_UUID].taken_at
    monkeypatch.setattr(time, "monotonic", lambda: old + 100)
    curated_tool.record_inbox_snapshot(A_UUID, _probe("sig-1", None))  # idle repeat
    assert curated_tool._inbox_snapshots[A_UUID].taken_at == old + 100
    assert len(curated_tool.inbox_snapshot(A_UUID)) == 1
    # an idle answer about ANOTHER tree confirms nothing
    curated_tool.record_inbox_snapshot(A_UUID, _probe("sig-other", None))
    assert curated_tool._inbox_snapshots[A_UUID].taken_at == old + 100
    _snapshot(A_UUID, signature="sig-2")  # the file was filed: empty inbox
    assert curated_tool.inbox_snapshot(A_UUID) == []


def test_stale_snapshot_counts_as_unknown(monkeypatch):
    _snapshot(A_UUID, _entry(1))
    taken = curated_tool._inbox_snapshots[A_UUID].taken_at
    monkeypatch.setattr(
        time, "monotonic", lambda: taken + curated_tool.INBOX_SNAPSHOT_MAX_AGE_S + 1
    )
    assert curated_tool.inbox_snapshot(A_UUID) == []


def test_upload_note_shows_before_the_next_probe():
    curated_tool.note_inbox_upload(
        A_UUID, drive_id="synthUp0001", name="new scan.pdf", size=42, mime="application/pdf"
    )
    (e,) = curated_tool.inbox_snapshot(A_UUID)
    assert (e["id"], e["name"], e["size"], e["review"]) == (
        "synthUp0001",
        "new scan.pdf",
        42,
        False,
    )
    assert e["modified"].endswith("Z")
    _snapshot(A_UUID, _entry(1), signature="sig-after-upload")  # the probe takes over
    assert [x["id"] for x in curated_tool.inbox_snapshot(A_UUID)] == ["synthInbox0001"]
    curated_tool.note_inbox_upload(A_UUID, drive_id="", name="x", size=1, mime="x")  # no id
    assert len(curated_tool.inbox_snapshot(A_UUID)) == 1


async def test_probe_patient_records_the_snapshot(monkeypatch):
    """The one probe entry point keeps the snapshot (the loop calls nothing else)."""
    import oncofiles.curated.runner as runner_mod

    gdrive = SimpleNamespace(dedicated_copy=lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(
        server, "_create_patient_clients", AsyncMock(return_value=(gdrive, None, None, "root1"))
    )
    monkeypatch.setattr(curated_tool, "reviewed_text_sources", AsyncMock(return_value=frozenset()))
    monkeypatch.setattr(runner_mod, "probe", lambda *a, **k: _probe("sig-9", [_entry(3)]))
    await curated_tool.probe_patient(None, A_UUID, None)
    assert [e["id"] for e in curated_tool.inbox_snapshot(A_UUID)] == ["synthInbox0003"]


# ── rows and the summary ──────────────────────────────────────────────────


def test_inbox_row_shape_and_stage():
    row = dashboard_inbox.row(curated_tool._inbox_entry(_entry(1)))
    assert row["id"] == "inbox:synthInbox0001" and row["inbox"] is True
    assert row["stage"] == document_stage.STAGE_IN_INBOX == "in_inbox"
    assert row["stage"] in document_stage.STAGES
    assert row["ready"] is False and row["attention_reason"] is None
    assert row["steps"] == {"extracted": False, "named": False, "filed": False}
    assert row["uploaded_at"] == "2031-09-21T06:47:13Z" and row["category"] is None
    review = dashboard_inbox.row(curated_tool._inbox_entry(_entry(2, review=True)))
    assert review["stage"] == "attention" and review["attention_reason"] == "review_needed"


async def test_matrix_counts_inbox_rows_and_keeps_the_invariant(patients):
    db = patients
    await _curated_doc(db, 1)  # ready
    entries = [curated_tool._inbox_entry(e) for e in (_entry(1), _entry(2, review=True))]
    out = await _build_document_matrix(
        db, patient_id=A_UUID, truncate_names=False, inbox_items=entries
    )
    s = out["summary"]
    assert s["inbox"] == len(entries) and s["total"] == len(entries) + 1
    assert s["ready"] == s["in_progress"] == s["attention"] == 1
    assert s["ready"] + s["in_progress"] + s["attention"] == s["total"]
    inbox = [r for r in out["documents"] if r.get("inbox")]
    assert {r["stage"] for r in inbox} == {"in_inbox", "attention"}
    for flt, want in (
        ("in_progress", {"in_inbox"}),
        ("attention", {"attention"}),
        ("ready", set()),
    ):
        got = await _build_document_matrix(
            db, filter_param=flt, patient_id=A_UUID, inbox_items=entries
        )
        assert {r["stage"] for r in got["documents"] if r.get("inbox")} == want, flt


async def test_a_filed_file_never_shows_twice(patients):
    """The run moved the file (same Drive id) and the indexer mirrored it before
    the next probe refreshed the snapshot: the document row wins."""
    db = patients
    await _curated_doc(db, 7)  # gdrive id synth-drive-7
    entries = [{"id": "synth-drive-7", "name": "scan.pdf", "modified": "2031-09-21T06:47:13Z"}]
    out = await _build_document_matrix(db, patient_id=A_UUID, inbox_items=entries)
    assert out["summary"]["inbox"] == 0 and out["summary"]["total"] == 1
    assert not any(r.get("inbox") for r in out["documents"])


async def test_the_mcp_matrix_has_no_inbox_rows(patients):
    _snapshot(A_UUID, _entry(1))
    out = await _build_document_matrix(patients, patient_id=A_UUID)
    assert out["summary"]["inbox"] == 0 and out["summary"]["total"] == 0


# ── GET /api/documents: ACL and no Drive call ─────────────────────────────


@pytest.fixture
def no_drive(monkeypatch):
    boom = AsyncMock(side_effect=AssertionError("Drive reached from the request path"))
    monkeypatch.setattr(server, "_create_patient_clients", boom)
    monkeypatch.setattr(curated_tool, "probe_patient", boom)
    return boom


async def test_route_shows_the_callers_own_inbox_without_drive(patients, no_drive):
    _snapshot(A_UUID, _entry(1))
    with _caller(A_EMAIL):
        body = _body(await server.api_documents(_request(patients)))
    (row,) = body["documents"]
    assert row["id"] == "inbox:synthInbox0001" and row["stage"] == "in_inbox"
    assert row["filename"] == "synthetic scan 1.pdf"
    s = body["summary"]
    assert s["total"] == s["in_progress"] == s["inbox"] == 1
    assert s["ready"] + s["in_progress"] + s["attention"] == s["total"]
    no_drive.assert_not_called()


@pytest.mark.parametrize("email", [B_EMAIL, NOBODY_EMAIL])
async def test_another_patients_inbox_never_leaks(patients, no_drive, email):
    _snapshot(A_UUID, _entry(1), _entry(2, review=True))
    _snapshot(B_UUID, _entry(5), signature="sig-b")
    for query in ({}, {"patient_id": A_UUID}, {"patient_id": "stage-a"}):
        with _caller(email):
            body = _body(await server.api_documents(_request(patients, **query)))
        names = {r["filename"] for r in body["documents"]}
        assert "synthetic scan 1.pdf" not in names and "synthetic scan 2.pdf" not in names
        if email == B_EMAIL:
            assert names == {"synthetic scan 5.pdf"}
        else:
            assert names == set() and body["summary"]["total"] == 0
    no_drive.assert_not_called()


def test_health_never_carries_the_inbox():
    src = inspect.getsource(server.health)
    assert "inbox_snapshot" not in src and "_inbox_snapshots" not in src


def test_the_snapshot_is_never_logged():
    for fn in (
        curated_tool.record_inbox_snapshot,
        curated_tool.note_inbox_upload,
        curated_tool.inbox_snapshot,
    ):
        assert "logger." not in inspect.getsource(fn)


# ── the upload adds its file at once ──────────────────────────────────────


def _upload(name: str, data: bytes):
    import io

    from starlette.datastructures import UploadFile

    return UploadFile(io.BytesIO(data), filename=name, size=len(data))


def _clients(monkeypatch, reason):
    monkeypatch.setattr(
        server,
        "_create_patient_clients",
        AsyncMock(return_value=(object(), None, None, "rootFolder0001")),
    )
    monkeypatch.setattr("oncofiles.curated_folder.curated_reason", lambda g, f, use_cache: reason)


async def test_curated_upload_shows_in_the_table_at_once(db, monkeypatch):
    _clients(monkeypatch, "is a curated folder")
    monkeypatch.setattr(
        inbox_upload,
        "upload_to_inbox",
        lambda g, root, files: [
            {"status": "uploaded", "name": files[0].name, "id": "synthNew0001"}
        ],
    )
    out = await dashboard_upload.handle_upload(db, A_UUID, [_upload("scan 9.pdf", PDF)])
    assert out.status == 200
    assert "id" not in out.payload["files"][0]  # the response is unchanged
    (e,) = curated_tool.inbox_snapshot(A_UUID)
    assert (e["id"], e["name"], e["size"], e["mime"]) == (
        "synthNew0001",
        "scan 9.pdf",
        len(PDF),
        "application/pdf",
    )
    assert curated_tool.inbox_snapshot(B_UUID) == []


async def test_drive_only_upload_adds_no_inbox_row(db, monkeypatch):
    _clients(monkeypatch, None)
    monkeypatch.setattr(
        "oncofiles.tools.documents.store_drive_only_upload",
        AsyncMock(return_value={"status": "uploaded", "name": "scan.pdf"}),
    )
    out = await dashboard_upload.handle_upload(db, A_UUID, [_upload("scan.pdf", PDF)])
    assert out.payload["path"] == "drive_only"
    assert curated_tool.inbox_snapshot(A_UUID) == []


def test_upload_to_inbox_returns_the_drive_id():
    src = inspect.getsource(inbox_upload.upload_to_inbox)
    assert '{"status": "uploaded", "name": name, "id": file_id}' in src


# ── dashboard.js ──────────────────────────────────────────────────────────


def _fn(name: str) -> str:
    start = re.search(rf"\n    (?:async )?function {re.escape(name)}\(", JS)
    assert start, name
    return JS[start.start() : JS.find("\n    }\n", start.start())]


def test_in_inbox_is_a_bilingual_in_progress_state():
    assert "in_inbox: ['Čaká na spracovanie', 'Waiting to be processed']" in JS
    assert "in_inbox: 1" in JS  # STAGE_RANK
    # stageKind: anything but ready / attention is the in-progress shape
    assert "d.stage === 'ready' ? 'ready' : d.stage === 'attention' ? 'attention' : 'progress'" in (
        _fn("stageKind")
    )


def test_an_inbox_row_expands_without_a_detail_fetch():
    opener = _fn("openDocDetail")
    assert opener.index("if (doc.inbox) { renderInboxDetail(doc, detailTd); return; }") < (
        opener.index("apiFetch('/api/doc-detail/'")
    )
    detail = _fn("renderInboxDetail")
    assert "apiFetch" not in detail and "fetch(" not in detail
    assert "setI18n(p, 'Súbor je v priečinku pre nové súbory" in detail


def test_demo_upload_rows_have_the_inbox_shape():
    sim = _fn("simulateDemoUpload")
    assert "id: 'inbox:demo-upload-' + demoUploadSeq, inbox: true" in sim
    assert "stage: 'in_inbox'" in sim
    assert "demoUploadDetail" not in JS
