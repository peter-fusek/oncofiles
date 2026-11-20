"""The user-facing document lifecycle, the activity log and the admin-only schedules.

* ``document_stage``: one source for "named" and "ready". A curated row (a
  mirror of a filed Drive file) is checked against the README naming contract,
  any other row against the legacy underscore standard. Before, every row used
  the legacy check, so a curated patient showed "named 0 / complete 0".
* ``_build_document_matrix`` / ``GET /api/documents`` / ``/status`` use it.
* ``GET /api/activity``: counts-only events of the caller's patient.
* ``GET /api/schedules``: admin only.

Synthetic patients, e-mails (example.test), filenames and values only; every
date is invented and lies in 2027 or later.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import dashboard_activity, document_stage, server
from oncofiles.database import Database
from oncofiles.tools.hygiene import _build_document_matrix, _is_extractable
from tests.helpers import PRIMARY_TEST_UUID, make_doc

A_UUID = PRIMARY_TEST_UUID
B_UUID = "00000000-0000-4000-8000-0000000009b1"
A_EMAIL = "stage-carer-a@example.test"
B_EMAIL = "stage-carer-b@example.test"
NOBODY_EMAIL = "stage-stranger@example.test"

# README contract (hyphens) vs the legacy underscore standard. The test
# patient's context name is "Jana Testova" (conftest), compact "JanaTestova".
CONTRACT_NAME = "20270412-JanaTestova-SynthClinic-Labs-Unknown-SynthHospital-BloodPanel.pdf"
LEGACY_NAME = "20270412_JanaTestova_SynthClinic_Labs_BloodPanel.pdf"
RAW_NAME = "scan synthetic 0042.pdf"


def _curated_meta(**front) -> str:
    """``structured_metadata`` as ``tools.curated.index_patient`` writes it: the
    kept sidecar front-matter keys sit at the top level, next to ``curated``."""
    return json.dumps(
        {
            "source": "curated",
            "folder": "02-labs",
            "curated": {"sidecar_id": "synth-sidecar", "index_version": 1},
            **front,
        }
    )


async def _curated_doc(
    db: Database,
    n: int,
    *,
    pid: str = A_UUID,
    filename: str = CONTRACT_NAME,
    pages: bool = True,
    mime_type: str = "application/pdf",
    **front,
):
    doc = await db.insert_document(
        make_doc(
            file_id=f"gdrive:synth-drive-{n}",
            filename=filename,
            original_filename=filename,
            document_date=date(2027, 4, 12),
            institution=None,  # unknown institution is a legitimate outcome (#854)
            gdrive_id=f"synth-drive-{n}",
            mime_type=mime_type,
        ),
        patient_id=pid,
    )
    await db.update_structured_metadata(doc.id, _curated_meta(**front))
    if pages:
        await db.save_ocr_page(doc.id, 1, "synthetic page text", "curated-sidecar")
    return doc


async def _legacy_doc(db: Database, n: int, *, pid: str = A_UUID, pages: bool = False, **fields):
    defaults = {
        "file_id": f"file_synth_{n}",
        "filename": LEGACY_NAME,
        "document_date": date(2027, 5, 3),
    }
    doc = await db.insert_document(make_doc(**{**defaults, **fields}), patient_id=pid)
    if pages:
        await db.save_ocr_page(doc.id, 1, "synthetic page text", "synthetic-ocr")
    return doc


async def _set(db: Database, doc_id: int, **columns) -> None:
    sets = ", ".join(f"{k} = ?" for k in columns)
    await db.db.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*columns.values(), doc_id))
    await db.db.commit()


def _row(result: dict, doc_id: int) -> dict:
    return next(r for r in result["documents"] if r["id"] == doc_id)


# ── document_stage: pure functions ────────────────────────────────────────


def test_is_curated_row_needs_prefix_and_curated_metadata():
    assert document_stage.is_curated_row("gdrive:synth-1", _curated_meta())
    assert document_stage.is_curated_row("gdrive:synth-1", json.loads(_curated_meta()))
    assert document_stage.is_curated_row("gdrive:synth-1", {"source": "curated"})
    # the indexer's own row between its insert and its commit marker
    assert document_stage.is_curated_row("gdrive:synth-1", None)
    # a Drive-only row of the legacy sync path shares the prefix (#777)
    assert not document_stage.is_curated_row("gdrive:synth-1", '{"source": "drive_sync"}')
    # the metadata alone never makes a row curated
    assert not document_stage.is_curated_row("file_synth_1", _curated_meta())
    assert not document_stage.is_curated_row(None, _curated_meta())


def test_is_named_uses_the_contract_for_a_curated_row(request_patient):
    curated = {"file_id": "gdrive:synth-1", "structured_metadata": _curated_meta()}
    assert document_stage.is_named(CONTRACT_NAME, patient_id=request_patient, **curated)
    # a curated row is NOT measured by the legacy standard
    assert not document_stage.is_named(LEGACY_NAME, patient_id=request_patient, **curated)
    assert not document_stage.is_named(RAW_NAME, patient_id=request_patient, **curated)
    assert not document_stage.is_named("", patient_id=request_patient, **curated)


def test_is_named_keeps_the_legacy_check_for_other_rows(request_patient):
    legacy = {"file_id": "file_synth_1", "structured_metadata": None}
    assert document_stage.is_named(LEGACY_NAME, patient_id=request_patient, **legacy)
    # a contract-style name on a legacy row is still judged by the legacy check
    assert not document_stage.is_named(CONTRACT_NAME, patient_id=request_patient, **legacy)
    assert not document_stage.is_named(RAW_NAME, patient_id=request_patient, **legacy)
    # a Drive-only legacy row (gdrive: prefix, no curated metadata) too
    drive_only = {"file_id": "gdrive:synth-9", "structured_metadata": '{"source": "drive_sync"}'}
    assert document_stage.is_named(LEGACY_NAME, patient_id=request_patient, **drive_only)
    assert not document_stage.is_named(CONTRACT_NAME, patient_id=request_patient, **drive_only)


def test_is_extracted_reuses_the_non_extractable_rule():
    assert document_stage.is_extracted(has_text=True, extractable=True)
    assert not document_stage.is_extracted(has_text=False, extractable=True)
    # a spreadsheet or a text note has no "text read" step
    assert document_stage.is_extracted(has_text=False, extractable=False)
    assert _is_extractable("application/pdf") and _is_extractable("image/jpeg")
    assert _is_extractable(None) and _is_extractable("")
    assert not _is_extractable("text/markdown")
    assert not _is_extractable("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"file_id": "gdrive:s", "structured_metadata": _curated_meta(review_needed=True)},
         "review_needed"),
        ({"file_id": "gdrive:s", "structured_metadata": _curated_meta(review_needed="true")},
         "review_needed"),
        ({"file_id": "gdrive:s", "structured_metadata": _curated_meta(review_needed=False)}, None),
        # review_needed is a curated sidecar key: never read from a legacy row
        ({"file_id": "file_s", "structured_metadata": '{"review_needed": true}'}, None),
        ({"sync_state": "deleted_remote"}, "missing_in_drive"),
        ({"outside_folder": True}, "outside_folder"),
        ({"has_text": True, "has_date": False}, "missing_date"),
        # no text read yet: the missing date is not a problem a person must solve
        ({"has_text": False, "has_date": False}, None),
        ({"has_text": True, "has_date": True, "sync_state": "synced"}, None),
        ({}, None),
    ],
)  # fmt: skip
def test_attention_reason_each_code(kwargs, expected):
    assert document_stage.attention_reason(**kwargs) == expected


def test_attention_reason_first_match_wins():
    everything = {
        "file_id": "gdrive:s",
        "structured_metadata": _curated_meta(review_needed=True),
        "sync_state": "deleted_remote",
        "outside_folder": True,
        "has_text": True,
        "has_date": False,
    }
    order = []
    remaining = dict(everything)
    for drop in ("structured_metadata", "sync_state", "outside_folder", "has_text"):
        order.append(document_stage.attention_reason(**remaining))
        remaining.pop(drop)
    assert tuple(order) == document_stage.ATTENTION_REASONS
    assert document_stage.attention_reason(**remaining) is None


@pytest.mark.parametrize(
    ("extracted", "named", "filed", "attention", "stage", "ready"),
    [
        (True, True, True, None, "ready", True),
        (False, True, True, None, "waiting_text", False),
        (False, False, False, None, "waiting_text", False),
        (True, False, True, None, "waiting_name", False),
        (True, False, False, None, "waiting_name", False),
        (True, True, False, None, "waiting_filing", False),
        # attention wins over every step, also over "all done"
        (True, True, True, "review_needed", "attention", False),
        (False, False, False, "missing_in_drive", "attention", False),
    ],
)
def test_stage_of_order(extracted, named, filed, attention, stage, ready):
    out = document_stage.stage_of(
        extracted=extracted, named=named, filed=filed, attention=attention
    )
    assert out == {
        "steps": {"extracted": extracted, "named": named, "filed": filed},
        "ready": ready,
        "stage": stage,
    }
    assert out["stage"] in document_stage.STAGES


# ── _build_document_matrix ────────────────────────────────────────────────


async def test_curated_row_with_contract_name_is_named_and_ready(db):
    """The bug: "Named 0 / Complete 0" for a curated patient."""
    doc = await _curated_doc(db, 1)
    result = await _build_document_matrix(db, patient_id=A_UUID)
    row = _row(result, doc.id)

    assert row["curated"] is True
    assert row["is_standard_name"] is True
    assert row["steps"] == {"extracted": True, "named": True, "filed": True}
    assert row["ready"] is True and row["stage"] == "ready"
    assert row["attention_reason"] is None
    # no AI summary and no institution: not required for "ready" …
    assert row["has_ai"] is False and row["has_institution"] is False
    # … while the legacy definition is unchanged and still says no
    assert row["fully_complete"] is False

    summary = result["summary"]
    assert summary["standard_named"] == summary["named"] == 1
    assert summary["ready"] == 1 and summary["fully_complete"] == 0


async def test_row_keeps_every_old_key_and_gains_the_new_ones(db):
    doc = await _curated_doc(db, 1)
    row = _row(await _build_document_matrix(db, patient_id=A_UUID), doc.id)
    old = {
        "id", "filename", "category", "date", "institution", "gdrive_id", "has_ocr", "has_ai",
        "has_metadata", "has_date", "has_institution", "is_synced", "is_standard_name",
        "fully_complete",
    }  # fmt: skip
    new = {"uploaded_at", "steps", "ready", "stage", "attention_reason", "curated"}
    assert set(row) == old | new
    # uploaded_at is documents.created_at as an explicit UTC ISO string
    parsed = datetime.strptime(row["uploaded_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - parsed).total_seconds()) < 300


async def test_legacy_row_keeps_the_legacy_naming_check(db):
    named = await _legacy_doc(db, 1, gdrive_id="synth-legacy-1", pages=True)
    hyphens = await _legacy_doc(
        db, 2, filename=CONTRACT_NAME, gdrive_id="synth-legacy-2", pages=True
    )
    result = await _build_document_matrix(db, patient_id=A_UUID)

    row = _row(result, named.id)
    assert row["curated"] is False and row["is_standard_name"] is True
    assert row["ready"] is True  # read, named, filed; no AI summary needed

    row = _row(result, hyphens.id)
    assert row["curated"] is False and row["is_standard_name"] is False
    assert row["stage"] == "waiting_name" and row["ready"] is False


async def test_waiting_stages_follow_the_step_order(db):
    no_text = await _curated_doc(db, 1, pages=False)
    no_name = await _curated_doc(db, 2, filename=RAW_NAME)
    not_filed = await _legacy_doc(db, 3, pages=True)  # no gdrive_id
    text_note = await _curated_doc(
        db, 4, pages=False, mime_type="text/markdown",
        filename=CONTRACT_NAME.replace(".pdf", ".md"),
    )  # fmt: skip
    result = await _build_document_matrix(db, patient_id=A_UUID)

    assert _row(result, no_text.id)["stage"] == "waiting_text"
    assert _row(result, no_text.id)["steps"]["extracted"] is False
    assert _row(result, no_name.id)["stage"] == "waiting_name"
    assert _row(result, not_filed.id)["stage"] == "waiting_filing"
    # a file type without a text step does not wait for text
    row = _row(result, text_note.id)
    assert row["has_ocr"] is False and row["steps"]["extracted"] is True
    assert row["ready"] is True


async def test_each_attention_reason_in_the_matrix(db):
    review = await _curated_doc(db, 1, review_needed=True)
    gone = await _curated_doc(db, 2)
    await _set(db, gone.id, sync_state="deleted_remote")
    outside = await _legacy_doc(db, 3, gdrive_id="synth-legacy-3", pages=True)
    await _set(db, outside.id, gdrive_parent_outside_root=1)
    undated = await _legacy_doc(db, 4, gdrive_id="synth-legacy-4", pages=True, document_date=None)
    undated_unread = await _legacy_doc(db, 5, gdrive_id="synth-legacy-5", document_date=None)

    result = await _build_document_matrix(db, patient_id=A_UUID)
    expected = {
        review.id: "review_needed",
        gone.id: "missing_in_drive",
        outside.id: "outside_folder",
        undated.id: "missing_date",
    }
    for doc_id, reason in expected.items():
        row = _row(result, doc_id)
        assert row["attention_reason"] == reason
        assert row["stage"] == "attention" and row["ready"] is False
    # all three steps are done for the reviewed one: attention still wins
    assert _row(result, review.id)["steps"] == {"extracted": True, "named": True, "filed": True}
    row = _row(result, undated_unread.id)
    assert row["attention_reason"] is None and row["stage"] == "waiting_text"


async def _mixed_patient(db: Database) -> dict[str, int]:
    """Two ready, three in progress, two needing attention."""
    ids = {
        "ready_curated": (await _curated_doc(db, 1)).id,
        "ready_legacy": (await _legacy_doc(db, 2, gdrive_id="synth-legacy-2", pages=True)).id,
        "waiting_text": (await _curated_doc(db, 3, pages=False)).id,
        "waiting_name": (await _curated_doc(db, 4, filename=RAW_NAME)).id,
        "waiting_filing": (await _legacy_doc(db, 5, pages=True)).id,
        "review": (await _curated_doc(db, 6, review_needed=True)).id,
        "gone": (await _curated_doc(db, 7)).id,
    }
    await _set(db, ids["gone"], sync_state="deleted_remote")
    return ids


async def test_summary_counts_add_up(db):
    await _mixed_patient(db)
    summary = (await _build_document_matrix(db, patient_id=A_UUID))["summary"]

    assert summary["total"] == 7
    assert summary["ready"] == 2
    assert summary["in_progress"] == 3
    assert summary["attention"] == 2
    assert summary["ready"] + summary["in_progress"] + summary["attention"] == summary["total"]
    # steps are counted whatever the stage (the two attention rows did them all)
    assert summary["extracted"] == 6
    assert summary["named"] == summary["standard_named"] == 6
    assert summary["filed"] == summary["synced"] == 6
    assert summary["with_ocr"] == 6
    assert summary["fully_complete"] == 0  # legacy definition: nobody has an AI summary


async def test_summary_of_an_empty_patient(db):
    summary = (await _build_document_matrix(db, patient_id=A_UUID))["summary"]
    for key in ("ready", "in_progress", "attention", "extracted", "named", "filed"):
        assert summary[key] == 0


@pytest.mark.parametrize(
    ("filter_param", "names"),
    [
        ("ready", {"ready_curated", "ready_legacy"}),
        ("in_progress", {"waiting_text", "waiting_name", "waiting_filing"}),
        ("attention", {"review", "gone"}),
    ],
)
async def test_stage_filters(db, filter_param, names):
    ids = await _mixed_patient(db)
    result = await _build_document_matrix(db, filter_param=filter_param, patient_id=A_UUID)
    assert {r["id"] for r in result["documents"]} == {ids[n] for n in names}
    assert result["matched"] == len(names)
    assert result["summary"]["total"] == 7  # the summary is never filtered


async def test_existing_filters_keep_working(db):
    ids = await _mixed_patient(db)
    not_renamed = await _build_document_matrix(db, filter_param="not_renamed", patient_id=A_UUID)
    assert {r["id"] for r in not_renamed["documents"]} == {ids["waiting_name"]}
    not_synced = await _build_document_matrix(db, filter_param="not_synced", patient_id=A_UUID)
    assert {r["id"] for r in not_synced["documents"]} == {ids["waiting_filing"]}
    incomplete = await _build_document_matrix(db, filter_param="incomplete", patient_id=A_UUID)
    assert incomplete["matched"] == 7
    everything = await _build_document_matrix(db, filter_param="all", patient_id=A_UUID)
    assert everything["matched"] == 7


async def test_filenames_are_cut_for_mcp_and_whole_on_request(db):
    assert len(CONTRACT_NAME) > 60
    doc = await _curated_doc(db, 1)
    cut = _row(await _build_document_matrix(db, patient_id=A_UUID), doc.id)
    whole = _row(await _build_document_matrix(db, patient_id=A_UUID, truncate_names=False), doc.id)
    assert cut["filename"] == CONTRACT_NAME[:60]
    assert whole["filename"] == CONTRACT_NAME
    # the cut is display only: the naming check always sees the whole name
    assert cut["is_standard_name"] is True and whole["is_standard_name"] is True


async def test_mcp_tool_output_only_grew(db):
    """get_document_status_matrix: 60-character names, the new keys present."""
    from oncofiles.tools.hygiene import get_document_status_matrix

    doc = await _curated_doc(db, 1)
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    result = json.loads(await get_document_status_matrix(ctx, filter="ready"))
    row = _row(result, doc.id)
    assert row["filename"] == CONTRACT_NAME[:60]
    assert row["ready"] is True and row["stage"] == "ready"
    assert result["summary"]["ready"] == 1


# ── HTTP routes ───────────────────────────────────────────────────────────


@pytest.fixture
async def two_patients(db: Database) -> Database:
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


def _request(db: Database, *, auth: str = "Bearer session:synthetic", **query) -> MagicMock:
    request = MagicMock()
    request.query_params = {k: str(v) for k, v in query.items()}
    request.path_params = {}
    request.headers = {"authorization": auth}
    request.app.state.fastmcp_server._lifespan_result = {
        "db": db,
        "files": None,
        "job_tracker": {},
    }
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


def _body(resp) -> dict:
    return json.loads(resp.body)


async def test_api_documents_returns_whole_filenames_and_stages(two_patients):
    db = two_patients
    doc = await _curated_doc(db, 1)
    with _caller(A_EMAIL):
        resp = await server.api_documents(_request(db))
    assert resp.status_code == 200
    body = _body(resp)
    row = _row(body, doc.id)
    assert row["filename"] == CONTRACT_NAME
    assert row["ready"] is True and row["stage"] == "ready" and row["curated"] is True
    assert body["summary"]["ready"] == 1 and body["summary"]["standard_named"] == 1


async def test_api_documents_stage_filter(two_patients):
    db = two_patients
    ids = await _mixed_patient(db)
    with _caller(A_EMAIL):
        resp = await server.api_documents(_request(db, filter="attention"))
    assert {r["id"] for r in _body(resp)["documents"]} == {ids["review"], ids["gone"]}


async def test_status_counts_curated_names(two_patients):
    db = two_patients
    await _curated_doc(db, 1)
    await _curated_doc(db, 2, filename=RAW_NAME)
    await _legacy_doc(db, 3)
    await _legacy_doc(db, 4, filename=CONTRACT_NAME)  # contract name on a legacy row: no
    with _caller(A_EMAIL):
        resp = await server.status(_request(db))
    assert resp.status_code == 200
    health = _body(resp)["document_health"]
    assert health["total"] == 4
    assert health["standard_named"] == 2
    assert set(health) == {
        "total", "with_ai", "with_metadata", "with_date", "with_institution", "synced",
        "standard_named",
    }  # fmt: skip


# ── GET /api/activity ─────────────────────────────────────────────────────


async def _curated_run(
    db: Database,
    pid: str,
    *,
    trigger: str = "auto",
    dry_run: bool = False,
    status: str | None = "ok",
    report: dict | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> int:
    row_id = await db.insert_curated_run(patient_id=pid, dry_run=dry_run, trigger=trigger)
    if status is not None:
        await db.finish_curated_run(
            row_id,
            patient_id=pid,
            status=status,
            message="synthetic message that must never leave the server",
            report=report if report is not None else {},
        )
    times = {"started_at": started_at, "finished_at": finished_at}
    for column, value in times.items():
        if value is not None:
            await db.db.execute(
                f"UPDATE curated_runs SET {column} = ? WHERE id = ?", (value, row_id)
            )
    await db.db.commit()
    return row_id


def _report(
    filed: int = 0,
    review: int = 0,
    skipped: int = 0,
    not_processed: int = 0,
) -> dict:
    docs = (
        [{"source": f"synthetic-filed-{i}.pdf", "result": "filed"} for i in range(filed)]
        + [
            {
                "source": f"synthetic-review-{i}.pdf",
                "result": "review_needed",
                "reasons": ["synthetic reason text"],
            }
            for i in range(review)
        ]
        + [{"source": f"synthetic-skip-{i}.pdf", "result": "skipped"} for i in range(skipped)]
    )
    return {
        "status": "ok",
        "message": "synthetic report message",
        "documents": docs,
        "not_processed": [f"synthetic-left-{i}.pdf" for i in range(not_processed)],
        "actions": [{"action": "move", "path": "synthetic/path.pdf"}],
    }


async def _sync(
    db: Database,
    pid: str,
    *,
    trigger: str = "scheduled",
    status: str = "completed",
    started_at: str,
    duration_s: float | None = 12.34,
    new: int = 0,
    from_errors: int = 0,
    to_errors: int = 0,
    error_message: str | None = None,
) -> None:
    await db.db.execute(
        "INSERT INTO sync_history (started_at, finished_at, sync_trigger, status, duration_s, "
        "from_gdrive_new, from_gdrive_errors, to_gdrive_errors, error_message, stats_json, "
        "patient_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            started_at, started_at, trigger, status, duration_s, new, from_errors, to_errors,
            error_message, '{"files": ["synthetic-name.pdf"]}', pid,
        ),
    )  # fmt: skip
    await db.db.commit()


def _at(
    day: str,
    *,
    hour: int = 1,
    minute: int = 0,
    second: int = 0,
) -> str:
    """An invented UTC timestamp as the tables store it."""
    return f"{day}T{hour:02d}:{minute:02d}:{second:02d}Z"


# one run of the shape test: filed + review + skipped documents, some left over
SHAPE_RUN = {
    "filed": 3,
    "review": 2,
    "skipped": 1,
    "not_processed": 4,
}
NO_COUNTS = dict.fromkeys(dashboard_activity.CURATED_COUNT_KEYS, 0)


async def _activity(db: Database, email: str | None = A_EMAIL, *, admin=False, **query) -> dict:
    with _caller(email, admin=admin):
        resp = await server.api_activity(_request(db, **query))
    assert resp.status_code == 200
    return _body(resp)


async def test_activity_event_shape_and_counts(two_patients):
    db = two_patients
    await _curated_run(
        db, A_UUID, trigger="nightly",
        report=_report(**SHAPE_RUN),
        started_at=_at("2027-06-01"),
        finished_at=_at("2027-06-01", minute=2, second=30),
    )  # fmt: skip
    await _sync(
        db, A_UUID, started_at=_at("2027-05-30", hour=8),
        new=5,
        from_errors=1,
        to_errors=2,
        error_message="synthetic error text that must never leave the server",
    )  # fmt: skip

    body = await _activity(db)
    assert set(body) == {"events", "pending"}
    assert body["pending"] == {"inbox": None}
    assert body["events"] == [
        {
            "at": "2027-06-01T01:00:00Z",
            "kind": "curated_run",
            "status": "ok",
            "trigger": "nightly",
            "duration_s": 150.0,
            "counts": {
                "documents": 6,
                "filed": SHAPE_RUN["filed"],
                "review": SHAPE_RUN["review"],
                "not_processed": SHAPE_RUN["not_processed"],
            },
        },
        {
            "at": _at("2027-05-30", hour=8),
            "kind": "drive_sync",
            "status": "ok",
            "trigger": "scheduled",
            "duration_s": 12.3,
            "counts": {"new": 5, "errors": 3},
        },
    ]


async def test_activity_carries_codes_and_counts_only(two_patients):
    """The exact key set, and no text from a report, a message or an error."""
    db = two_patients
    await _curated_run(
        db, A_UUID, report=_report(**dict.fromkeys(("filed", "review", "not_processed"), 1)),
        started_at="2027-06-02T01:00:00Z", finished_at="2027-06-02T01:00:40Z",
    )  # fmt: skip
    await _curated_run(db, A_UUID, status="aborted", started_at="2027-06-03T01:00:00Z")
    await _sync(
        db, A_UUID, status="failed", started_at="2027-06-04T01:00:00Z",
        error_message="synthetic error text",
    )  # fmt: skip
    with _caller(A_EMAIL):
        resp = await server.api_activity(_request(db))
    body = _body(resp)

    assert len(body["events"]) == 3
    for event in body["events"]:
        assert set(event) == dashboard_activity.EVENT_KEYS
        assert event["kind"] in ("curated_run", "drive_sync")
        assert event["status"] in dashboard_activity.STATUSES
        count_keys = (
            dashboard_activity.CURATED_COUNT_KEYS
            if event["kind"] == "curated_run"
            else dashboard_activity.SYNC_COUNT_KEYS
        )
        assert set(event["counts"]) == count_keys
        assert all(type(v) is int for v in event["counts"].values())
        assert event["duration_s"] is None or isinstance(event["duration_s"], int | float)
        assert isinstance(event["trigger"], str)
    raw = resp.body.decode()
    for leaked in ("synthetic", ".pdf", "message", "reason", "error_message", "stats", "path"):
        assert leaked not in raw


async def test_activity_excludes_dry_runs_and_non_run_jobs(two_patients):
    db = two_patients
    await _curated_run(db, A_UUID, dry_run=True, report=_report(filed=9),
                       started_at="2027-06-05T01:00:00Z")  # fmt: skip
    for n, trigger in enumerate(("reocr", "reread", "clinical", "scorecard", "lab")):
        await _curated_run(db, A_UUID, trigger=trigger, report=_report(filed=9),
                           started_at=f"2027-06-06T0{n}:00:00Z")  # fmt: skip
    await _curated_run(db, A_UUID, trigger="manual", report=_report(filed=2),
                       started_at="2027-06-04T01:00:00Z")  # fmt: skip

    events = (await _activity(db))["events"]
    # The re-OCR job is listed as a text re-read (its own event kind, own
    # counts); every other non-run job and the dry run stay out.
    assert [e["kind"] for e in events] == ["text_reread", "curated_run"]
    runs = [e for e in events if e["kind"] == "curated_run"]
    assert [(e["trigger"], e["counts"]["filed"]) for e in runs] == [("manual", 2)]


async def test_activity_lists_a_real_curated_run_once(two_patients):
    """A real run also writes a sync_history row (trigger "curated"): one event."""
    db = two_patients
    await _curated_run(db, A_UUID, report=_report(filed=1), started_at="2027-06-07T01:00:00Z")
    await _sync(db, A_UUID, trigger="curated", started_at="2027-06-07T01:00:00Z")
    events = (await _activity(db))["events"]
    assert [e["kind"] for e in events] == ["curated_run"]


@pytest.mark.parametrize(
    ("stored", "shown"),
    [
        ("ok", "ok"),
        ("aborted", "failed"),
        ("failed", "failed"),
        ("interrupted", "interrupted"),
        ("something-new", "failed"),
    ],
)
async def test_activity_normalises_curated_status(two_patients, stored, shown):
    db = two_patients
    await _curated_run(db, A_UUID, status=stored, started_at="2027-06-08T01:00:00Z")
    assert (await _activity(db))["events"][0]["status"] == shown


async def test_activity_running_run_has_no_duration_and_zero_counts(two_patients):
    db = two_patients
    await _curated_run(db, A_UUID, status=None, started_at="2027-06-09T01:00:00Z")
    event = (await _activity(db))["events"][0]
    assert event["status"] == "running" and event["duration_s"] is None
    assert event["counts"] == NO_COUNTS


async def test_activity_survives_an_unreadable_report(two_patients):
    db = two_patients
    row_id = await _curated_run(db, A_UUID, started_at="2027-06-10T01:00:00Z")
    odd = await _curated_run(
        db, A_UUID, started_at="2027-06-11T01:00:00Z",
        report={"documents": ["not-an-object", {"result": "filed"}], "not_processed": "x"},
    )  # fmt: skip
    await db.db.execute(
        "UPDATE curated_runs SET report_json = ? WHERE id = ?", ("{not json", row_id)
    )
    await db.db.commit()
    events = (await _activity(db))["events"]
    assert [e["counts"] for e in events] == [
        {**NO_COUNTS, "documents": 2, "filed": 1},
        NO_COUNTS,
    ]
    assert odd > row_id


@pytest.mark.parametrize(
    ("stored", "shown"),
    [("completed", "ok"), ("failed", "failed"), ("running", "running"),
     ("timeout", "interrupted")],
)  # fmt: skip
async def test_activity_normalises_sync_status(two_patients, stored, shown):
    db = two_patients
    await _sync(db, A_UUID, status=stored, started_at="2027-06-12T01:00:00Z", duration_s=None)
    event = (await _activity(db))["events"][0]
    assert event["status"] == shown and event["duration_s"] is None


async def test_activity_never_echoes_an_unexpected_trigger(two_patients):
    db = two_patients
    await _sync(db, A_UUID, trigger="Synthetic Free Text!", started_at="2027-06-13T01:00:00Z")
    assert (await _activity(db))["events"][0]["trigger"] == "other"


async def test_activity_is_newest_first_and_limited(two_patients):
    db = two_patients
    for day in range(1, 8):
        await _curated_run(db, A_UUID, started_at=f"2027-07-{day:02d}T01:00:00Z")
        await _sync(db, A_UUID, started_at=f"2027-07-{day:02d}T13:00:00Z")

    events = (await _activity(db))["events"]
    assert len(events) == 14
    assert [e["at"] for e in events] == sorted((e["at"] for e in events), reverse=True)
    assert events[0] == {**events[0], "at": "2027-07-07T13:00:00Z", "kind": "drive_sync"}

    assert len((await _activity(db, limit=5))["events"]) == 5
    assert [e["at"] for e in (await _activity(db, limit=3))["events"]] == [
        "2027-07-07T13:00:00Z",
        "2027-07-07T01:00:00Z",
        "2027-07-06T13:00:00Z",
    ]
    # bad or out-of-range limits fall back / clamp, never fail
    assert len((await _activity(db, limit="abc"))["events"]) == 14
    assert len((await _activity(db, limit=0))["events"]) == 1
    assert len((await _activity(db, limit=-4))["events"]) == 1


async def test_activity_limit_is_capped_at_100(two_patients):
    db = two_patients
    await db.db.executemany(
        "INSERT INTO sync_history (started_at, sync_trigger, status, patient_id) "
        "VALUES (?, 'scheduled', 'completed', ?)",
        [(f"2027-08-01T{n // 60:02d}:{n % 60:02d}:00Z", A_UUID) for n in range(130)],
    )
    await db.db.commit()
    assert len((await _activity(db))["events"]) == 30  # default
    assert len((await _activity(db, limit=5000))["events"]) == 100


async def test_activity_only_the_callers_patient(two_patients):
    db = two_patients
    await _curated_run(db, A_UUID, trigger="auto", report=_report(filed=1),
                       started_at="2027-09-01T01:00:00Z")  # fmt: skip
    await _sync(db, A_UUID, started_at=_at("2027-09-02"), new=1)
    await _curated_run(db, B_UUID, trigger="nightly", report=_report(filed=7),
                       started_at="2027-09-03T01:00:00Z")  # fmt: skip
    await _sync(db, B_UUID, trigger="manual", started_at="2027-09-04T01:00:00Z", new=8)

    own = await _activity(db, A_EMAIL)
    assert [(e["kind"], e["trigger"]) for e in own["events"]] == [
        ("drive_sync", "scheduled"),
        ("curated_run", "auto"),
    ]
    other = await _activity(db, B_EMAIL)
    assert [(e["kind"], e["trigger"]) for e in other["events"]] == [
        ("drive_sync", "manual"),
        ("curated_run", "nightly"),
    ]
    # by slug and by UUID, own patient
    assert await _activity(db, A_EMAIL, patient_id="stage-a") == own
    assert await _activity(db, A_EMAIL, patient_id=A_UUID) == own


@pytest.mark.parametrize("requested", ["stage-b", B_UUID, "stage-nobody", "no-such-patient"])
async def test_activity_foreign_or_unknown_patient_never_shows_it(two_patients, requested):
    """Like /api/documents: a foreign and an unknown patient answer the same —
    the caller's own patient, or nothing for a caller without one (#867, #590)."""
    db = two_patients
    await _curated_run(db, A_UUID, trigger="auto", started_at="2027-09-05T01:00:00Z")
    await _curated_run(db, B_UUID, trigger="nightly", report=_report(filed=7),
                       started_at="2027-09-06T01:00:00Z")  # fmt: skip
    own = await _activity(db, A_EMAIL)

    assert await _activity(db, A_EMAIL, patient_id=requested) == own
    assert [e["trigger"] for e in own["events"]] == ["auto"]
    # a signed-in stranger: the sentinel matches no row
    assert await _activity(db, NOBODY_EMAIL, patient_id=requested) == {
        "events": [],
        "pending": {"inbox": None},
    }


async def test_activity_foreign_and_unknown_are_byte_identical(two_patients):
    db = two_patients
    await _curated_run(db, A_UUID, started_at="2027-09-07T01:00:00Z")
    await _curated_run(db, B_UUID, started_at="2027-09-08T01:00:00Z")
    responses = []
    for requested in ("stage-b", "stage-nobody"):
        with _caller(A_EMAIL):
            responses.append(await server.api_activity(_request(db, patient_id=requested)))
    assert responses[0].status_code == responses[1].status_code == 200
    assert responses[0].body == responses[1].body


async def test_activity_caller_without_a_patient_gets_an_empty_list(two_patients):
    db = two_patients
    await _curated_run(db, A_UUID, started_at="2027-09-09T01:00:00Z")
    assert await _activity(db, NOBODY_EMAIL) == {"events": [], "pending": {"inbox": None}}


async def test_activity_admin_and_operator_may_name_a_patient(two_patients):
    db = two_patients
    await _curated_run(db, B_UUID, trigger="nightly", started_at="2027-09-10T01:00:00Z")
    admin = await _activity(db, NOBODY_EMAIL, admin=True, patient_id="stage-b")
    assert [e["trigger"] for e in admin["events"]] == ["nightly"]
    with _caller(None, bearer=True):
        resp = await server.api_activity(_request(db, auth="Bearer op", patient_id=B_UUID))
    assert _body(resp) == admin


async def test_activity_requires_sign_in(two_patients):
    with _caller(None):
        resp = await server.api_activity(_request(two_patients, auth=""))
    assert resp.status_code == 401


async def test_activity_pending_inbox_from_the_in_memory_probe(two_patients):
    from oncofiles.tools import curated as curated_tools

    db = two_patients
    assert (await _activity(db, A_EMAIL))["pending"] == {"inbox": None}

    curated_tools._stats(A_UUID).pending_inbox_count = 3
    curated_tools._stats(B_UUID).pending_inbox_count = 11
    assert (await _activity(db, A_EMAIL))["pending"] == {"inbox": 3}
    assert (await _activity(db, B_EMAIL))["pending"] == {"inbox": 11}
    # never another patient's counter, whatever is named
    assert (await _activity(db, A_EMAIL, patient_id="stage-b"))["pending"] == {"inbox": 3}
    assert (await _activity(db, NOBODY_EMAIL, patient_id="stage-b"))["pending"] == {"inbox": None}

    curated_tools._stats(A_UUID).pending_inbox_count = 0
    assert (await _activity(db, A_EMAIL))["pending"] == {"inbox": 0}


async def test_activity_makes_no_drive_or_model_call_and_awaits_in_sequence(two_patients):
    """Two DB reads, one after the other; nothing else is touched."""
    db = two_patients
    await _curated_run(db, A_UUID, started_at="2027-09-11T01:00:00Z")
    active = 0
    order: list[str] = []

    def _tracked(name, real):
        async def wrapper(*args, **kwargs):
            nonlocal active
            assert active == 0, f"{name} overlaps another DB read"
            active += 1
            try:
                order.append(name)
                return await real(*args, **kwargs)
            finally:
                active -= 1

        return wrapper

    clients = AsyncMock(side_effect=AssertionError("no Drive client on this route"))
    with (
        patch.object(db, "list_curated_run_activity",
                     _tracked("runs", db.list_curated_run_activity)),
        patch.object(db, "list_reocr_run_activity",
                     _tracked("rereads", db.list_reocr_run_activity)),
        patch.object(db, "get_sync_history", _tracked("syncs", db.get_sync_history)),
        patch.object(server, "_create_patient_clients", clients),
    ):  # fmt: skip
        await _activity(db, A_EMAIL)
    assert order == ["runs", "rereads", "syncs"]
    clients.assert_not_called()


def test_activity_route_uses_the_single_acl_path_and_passes_no_row_through():
    """#867: the patient comes from `_get_dashboard_patient_id` only; the events
    come from `dashboard_activity`, which reads named columns."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(server.api_activity))
    called = {
        n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name | ast.Attribute)
    }
    assert {"_check_dashboard_auth", "_get_dashboard_patient_id", "merge"} <= called
    assert not called & {
        "_email_matches_caregiver", "resolve_patient_id", "get_curated_run",
        "get_last_curated_run", "list_curated_runs", "_create_patient_clients",
    }  # fmt: skip
    builders = inspect.getsource(dashboard_activity)
    for column in ("message", "error_message", "stats_json", "report"):
        assert f'"{column}"' not in builders


async def test_list_curated_run_activity_is_patient_scoped(two_patients):
    db = two_patients
    await _curated_run(db, A_UUID, report=_report(filed=1), started_at="2027-09-12T01:00:00Z")
    await _curated_run(db, B_UUID, report=_report(filed=2), started_at="2027-09-13T01:00:00Z")
    rows = await db.list_curated_run_activity(patient_id=A_UUID)
    assert [r["filed"] for r in rows] == [1]
    assert set(rows[0]) == {
        "id", "trigger", "status", "started_at", "finished_at", "documents", "filed", "review",
        "not_processed",
    }  # fmt: skip
    assert await db.list_curated_run_activity(patient_id="") == []
    with pytest.raises(TypeError):
        await db.list_curated_run_activity(A_UUID)  # patient_id is keyword-only


async def test_reocr_jobs_show_as_text_reread_with_counts_only(two_patients):
    """Prod click-through (#917): a re-OCR batch that rewrote documents left no
    trace in "Čo sa dialo". Real re-OCR jobs are now an event of their own."""
    db = two_patients
    report = {
        "documents": [
            {"document_id": 1, "result": "rewritten", "lost_codes": {"HGB": 1}},
            {"document_id": 2, "result": "rewritten"},
            {"document_id": 3, "result": "review", "review_reasons": ["missing_dates"]},
        ]
    }
    await _curated_run(
        db, A_UUID, trigger="reocr", report=report,
        started_at="2027-09-14T10:00:00Z", finished_at="2027-09-14T10:40:00Z",
    )  # fmt: skip
    await _curated_run(db, A_UUID, trigger="reocr", dry_run=True, report=report,
                       started_at="2027-09-15T10:00:00Z")  # fmt: skip
    await _curated_run(db, B_UUID, trigger="reocr", report=report,
                       started_at="2027-09-16T10:00:00Z")  # fmt: skip
    rows = await db.list_reocr_run_activity(patient_id=A_UUID)
    assert [(r["rewritten"], r["kept"]) for r in rows] == [(2, 1)]
    with pytest.raises(TypeError):
        await db.list_reocr_run_activity(A_UUID)
    # a re-OCR is still not a folder run
    assert await db.list_curated_run_activity(patient_id=A_UUID) == []

    body = await _activity(db, A_EMAIL)
    rereads = [e for e in body["events"] if e["kind"] == dashboard_activity.KIND_TEXT_REREAD]
    assert len(rereads) == 1
    assert set(rereads[0]) == dashboard_activity.EVENT_KEYS
    assert set(rereads[0]["counts"]) == dashboard_activity.REREAD_COUNT_KEYS
    counts = rereads[0]["counts"]
    assert (counts["rewritten"], counts["kept"]) == (rows[0]["rewritten"], rows[0]["kept"])
    assert rereads[0]["duration_s"] == 2400.0
    text = json.dumps(body)
    for leak in ("HGB", "missing_dates", "lost_codes", "review_reasons"):
        assert leak not in text


def test_activity_merge_puts_unreadable_times_last():
    runs = [
        {"started_at": "not a time", "status": "ok", "trigger": "auto"},
        {"started_at": "2027-10-01 05:06:07", "status": "ok", "trigger": "auto"},
    ]
    syncs = [{"started_at": "2027-10-02T05:06:07+02:00", "status": "completed"}]
    events = dashboard_activity.merge(runs, syncs, limit=10)
    assert [e["at"] for e in events] == ["2027-10-02T03:06:07Z", "2027-10-01T05:06:07Z", None]


# ── GET /api/schedules: admin only ────────────────────────────────────────


async def test_schedules_refuses_a_non_admin_session(two_patients):
    db = two_patients
    stats = AsyncMock(side_effect=AssertionError("no system-wide stats for a non-admin"))
    with _caller(A_EMAIL), patch.object(db, "get_prompt_stats", stats):
        resp = await server.api_schedules(_request(db))
    assert resp.status_code == 403
    assert _body(resp) == {"error": "admin only"}
    stats.assert_not_called()


async def test_schedules_requires_sign_in(two_patients):
    with _caller(None):
        resp = await server.api_schedules(_request(two_patients, auth=""))
    assert resp.status_code == 401


@pytest.mark.parametrize("bearer", [False, True])
async def test_schedules_serves_admin_and_operator(two_patients, bearer):
    with _caller(None if bearer else NOBODY_EMAIL, bearer=bearer, admin=not bearer):
        resp = await server.api_schedules(_request(two_patients))
    assert resp.status_code == 200
    body = _body(resp)
    assert set(body) == {"scheduler", "curated_loop", "jobs", "inactive", "prompt_stats"}
    # No scheduler runs in the test process: no invented jobs, every known
    # job is listed as not active, with a reason.
    assert body["jobs"] == []
    assert body["scheduler"]["running"] is False
    assert body["inactive"] and all(j["reason"] for j in body["inactive"])
