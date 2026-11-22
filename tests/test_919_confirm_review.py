"""#919: the caregiver confirms a filed curated document in review "as is".

The dashboard action writes ONE front-matter line, the #900 override
``review_override: "confirmed in dashboard (<date>)"``, into the document's
sidecar (never an original), inside ``contract_writes()`` through drive_ops,
then re-indexes that document so its row turns ready.

Synthetic data only: invented names, institutions and dates in 2027+.
"""

from __future__ import annotations

import ast
import json
import re
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import JSONResponse

from oncofiles import document_stage, server
from oncofiles.curated import indexer
from oncofiles.curated.confirm_review import (
    CONFIRMABLE_CODES,
    AlreadyResolvedError,
    ConfirmRefusedError,
    override_value,
    plan,
)
from oncofiles.curated.runner import review_override
from oncofiles.models import ActivityLogQuery
from oncofiles.tools import curated as tool
from tests.helpers import PRIMARY_TEST_UUID, make_doc
from tests.test_curated_runner import LABS, REPORT, FakeDrive

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
FILED = "20270305-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"
TODAY = date(2027, 6, 1)


def _sidecar(reasons: list[str], *, review_needed: str = "true", extra: str = "") -> str:
    return (
        "---\n"
        f'source_file: "{FILED}"\n'
        'patient: "Jana Novakova"\n'
        'doc_date: "2027-03-05"\n'
        'institution: "NOU"\n'
        'doc_type: "Labs"\n'
        f"review_needed: {review_needed}\n"
        f"review_reasons: {json.dumps(reasons)}\n"
        f"{extra}"
        'summary: "Krvny obraz, synteticky."\n'
        'ocr_engine: "qwen-local"\n'
        "---\n\n<!-- page 1 -->\nHemoglobin 121 g/l\n<!-- page 2 -->\nLeukocyty 6.1\n"
    )


SOFT = [
    "no valid clinical date found in the text",
    "model confidence 0.41 below the threshold",
]


# ── pure: the planned sidecar ─────────────────────────────────────────


def test_plan_adds_exactly_one_front_matter_line_and_keeps_every_other_byte():
    old = _sidecar(SOFT)
    new, codes = plan(old, TODAY)
    assert codes == ["no_date", "low_confidence"]
    old_lines, new_lines = old.split("\n"), new.split("\n")
    assert len(new_lines) == len(old_lines) + 1
    added = [ln for ln in new_lines if ln not in old_lines]
    assert added == ['review_override: "confirmed in dashboard (2027-06-01)"']
    # the line is appended at the end of the front matter; the body is byte for byte
    old_front, old_body = old.split("\n---", 1)
    new_front, new_body = new.split("\n---", 1)
    assert new_body == old_body
    assert new_front == old_front + '\nreview_override: "confirmed in dashboard (2027-06-01)"'


def test_written_value_is_the_one_runner_and_indexer_recognise():
    new, _ = plan(_sidecar(SOFT), TODAY)
    assert review_override(new) == override_value(TODAY)
    rec = indexer.IndexRecord(
        gdrive_id="g1",
        filename=FILED,
        folder=LABS,
        category=indexer.category_for("Labs"),
        mime_type="application/pdf",
        size_bytes=1,
        modified="2027-08-14T09:21:00Z",
    )
    front, pages = indexer.parse_sidecar(new)
    indexer._apply_sidecar(rec, front, pages, new)
    assert rec.review_override is True
    meta = {"source": "curated", "review_needed": True, "curated": {"review_override": True}}
    assert document_stage.attention_reason(file_id="gdrive:g1", structured_metadata=meta) is None


def test_override_value_carries_no_person():
    value = override_value(TODAY)
    assert value == "confirmed in dashboard (2027-06-01)"
    assert "@" not in value


def test_allowlist_is_exactly_the_soft_codes():
    assert (
        frozenset(
            {
                "no_date",
                "low_confidence",
                "rotated_region",
                "orientation_unsure",
                "chart_page",
                "other",
            }
        )
        == CONFIRMABLE_CODES
    )


@pytest.mark.parametrize(
    "reason",
    [
        "document names another person (Peter Novak)",
        "date 1960-01-02 is the patient's birth date",
        "looks like several documents",
        "doc_type 'Xyz' not in contract",
        "institution unknown",
    ],
)
def test_hard_codes_are_refused(reason):
    with pytest.raises(ConfirmRefusedError) as exc:
        plan(_sidecar([SOFT[0], reason]), TODAY)
    assert exc.value.code == "hard_check"


def test_already_overridden_or_not_in_review_is_resolved():
    with pytest.raises(AlreadyResolvedError):
        plan(_sidecar(SOFT, extra='review_override: "checked by hand"\n'), TODAY)
    with pytest.raises(AlreadyResolvedError):
        plan(_sidecar([], review_needed="false"), TODAY)


def test_blocked_or_inactive_override_is_refused():
    blocked = 'review_override: "x"\nreview_override_status: "hard_checks_failed"\n'
    with pytest.raises(ConfirmRefusedError) as exc:
        plan(_sidecar(SOFT, extra=blocked), TODAY)
    assert exc.value.code == "override_blocked"
    with pytest.raises(ConfirmRefusedError) as exc:
        plan(_sidecar(SOFT, extra="review_override: false\n"), TODAY)
    assert exc.value.code == "override_present"
    with pytest.raises(ConfirmRefusedError) as exc:
        plan("no front matter here\n", TODAY)
    assert exc.value.code == "no_front_matter"


# ── confirm_review_for: the fake Drive end to end ─────────────────────


def _drive(reasons=SOFT):
    d = FakeDrive()
    src = d.add(FILED, d.folder[LABS], b"%PDF fake original", modified="2027-08-14T09:21:00Z")
    side = d.add(FILED + ".md", d.folder[LABS], _sidecar(reasons).encode(),
                 modified="2027-08-14T09:21:07Z")  # fmt: skip
    note = d.add("20270310-JanaNovakova-NOU-Report-Note.md", d.folder[REPORT],
                 b"---\nreview_needed: true\nreview_reasons: []\n---\nPoznamka",
                 modified="2027-08-19T13:42:00Z")  # fmt: skip
    return d, src, side, note


@pytest.fixture
def drive_env():
    holder = {}

    @contextmanager
    def _env(reasons=SOFT):
        d, src, side, note = _drive(reasons)
        gdrive = MagicMock()
        gdrive.dedicated_copy.return_value = MagicMock()
        with (
            patch(
                "oncofiles.server._create_patient_clients",
                AsyncMock(return_value=(gdrive, None, None, d.root)),
            ),
            patch("oncofiles.curated.drive_ops.DriveOps", lambda _own: d),
        ):
            holder.update(d=d, src=src, side=side, note=note)
            yield holder

    return _env


async def _doc(db, gdrive_id):
    return await db.get_document_by_gdrive_id(gdrive_id, patient_id=PRIMARY_TEST_UUID)


def _reason(doc):
    return document_stage.describe(
        filename=doc.filename, file_id=doc.file_id, structured_metadata=doc.structured_metadata,
        patient_id=PRIMARY_TEST_UUID, has_text=True, extractable=True, gdrive_id=doc.gdrive_id,
    )["attention_reason"]  # fmt: skip


async def _activity(db):
    return await db.search_activity_log(ActivityLogQuery(patient_id=PRIMARY_TEST_UUID, limit=50))


async def test_confirm_writes_only_the_sidecar_and_the_row_turns_ready(db, drive_env):
    with drive_env() as env:
        d = env["d"]
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        doc = await _doc(db, env["src"])
        assert _reason(doc) == "review_needed"
        original = d.items[env["src"]]["content"]
        old_side = d.items[env["side"]]["content"].decode()
        d.writes.clear()

        status, body = await tool.confirm_review_for(
            db, PRIMARY_TEST_UUID, doc.id, caller_hash="hash-abc"
        )
        assert status == 200 and body["result"] == "confirmed"
        assert body["codes"] == ["no_date", "low_confidence"]
        # exactly one write, to the .md sidecar; the original is untouched
        assert d.writes == [("update", FILED + ".md")]
        assert d.items[env["src"]]["content"] == original
        new_side = d.items[env["side"]]["content"].decode()
        assert new_side == plan(old_side, date.fromisoformat(new_side.split("(")[1][:10]))[0]
        assert re.search(r'^review_override: "confirmed in dashboard \(\d{4}-\d\d-\d\d\)"$',
                         new_side, re.M)  # fmt: skip
        # re-indexed: the row is no longer in review
        doc = await _doc(db, env["src"])
        assert _reason(doc) is None
        rows = [r for r in await _activity(db) if r.tool_name == "curated_confirm_review"]
        assert len(rows) == 1
        assert json.loads(rows[0].input_summary) == {
            "document_id": doc.id, "caller_email_hash": "hash-abc"}  # fmt: skip
        assert json.loads(rows[0].output_summary) == {
            "result": "confirmed", "codes_set_aside": ["no_date", "low_confidence"]}  # fmt: skip
        assert "@" not in rows[0].input_summary + rows[0].output_summary
        assert FILED not in rows[0].input_summary + rows[0].output_summary

        # idempotent: a second confirm writes nothing and says so
        d.writes.clear()
        status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        assert status == 200 and body == {"result": "already_resolved", "already_resolved": True}
        assert d.writes == []


async def test_sidecar_changed_meanwhile_is_already_resolved_and_writes_nothing(db, drive_env):
    with drive_env() as env:
        d = env["d"]
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        doc = await _doc(db, env["src"])
        # a person cleared the flag in Drive after the last index
        d.items[env["side"]]["content"] = _sidecar([], review_needed="false").encode()
        d.writes.clear()
        status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        assert status == 200 and body["already_resolved"] is True
        assert d.writes == []


async def test_hard_code_is_refused_before_drive(db, drive_env):
    with drive_env([SOFT[0], "document names another person (Peter Novak)"]) as env:
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        doc = await _doc(db, env["src"])
        env["d"].writes.clear()
        status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        assert status == 409 and body["error"] == "hard_check"
        assert body["codes"] == ["other_person"]
        assert env["d"].writes == []


async def test_text_source_note_has_no_sidecar(db, drive_env):
    with drive_env() as env:
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        note = await _doc(db, env["note"])
        assert _reason(note) == "review_needed"
        env["d"].writes.clear()
        status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, note.id)
        assert (status, body) == (409, {"error": "no_sidecar"})
        assert env["d"].writes == []


async def test_busy_patient_is_refused(db, drive_env):
    with drive_env() as env:
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        doc = await _doc(db, env["src"])
        env["d"].writes.clear()
        async with tool.patient_lock(PRIMARY_TEST_UUID):
            status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        assert (status, body) == (409, {"error": "busy"})
        tool._runs[PRIMARY_TEST_UUID] = {"state": "running"}
        try:
            status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        finally:
            tool._runs.pop(PRIMARY_TEST_UUID, None)
        assert (status, body) == (409, {"error": "busy"})
        assert env["d"].writes == []


async def test_drive_failure_fails_closed(db, drive_env):
    with drive_env() as env:
        d = env["d"]
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        doc = await _doc(db, env["src"])
        d.fail_on.add("update_text")
        status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        assert (status, body) == (502, {"error": "drive_failed"})
        doc = await _doc(db, env["src"])
        assert _reason(doc) == "review_needed"  # nothing re-indexed
        rows = [r for r in await _activity(db) if r.tool_name == "curated_confirm_review"]
        assert [json.loads(r.output_summary)["result"] for r in rows] == ["drive_failed"]
        assert not tool.run_active()  # the slot is released


async def test_legacy_or_unknown_document_is_refused(db):
    legacy = await db.insert_document(
        make_doc(file_id="legacy-919-1", filename="20270402_Legacy_Labs.pdf"),
        patient_id=PRIMARY_TEST_UUID,
    )
    status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, legacy.id)
    assert (status, body) == (409, {"error": "not_curated"})
    status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, 987654)
    assert (status, body) == (404, {"error": "not_found"})


async def test_filed_document_not_in_review_is_refused(db, drive_env):
    with drive_env() as env:
        env["d"].items[env["side"]]["content"] = _sidecar([], review_needed="false").encode()
        await tool.index_patient(db, PRIMARY_TEST_UUID)
        doc = await _doc(db, env["src"])
        status, body = await tool.confirm_review_for(db, PRIMARY_TEST_UUID, doc.id)
        assert (status, body) == (409, {"error": "not_in_review"})


# ── the route: ACL, write refusal, origin ─────────────────────────────

A_UUID = PRIMARY_TEST_UUID
B_UUID = "00000000-0000-4000-8000-0000000009b1"
A_EMAIL = "carer-a919@example.test"
B_EMAIL = "carer-b919@example.test"


@pytest.fixture
async def acl_db(db):
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("cr919-a", A_EMAIL, A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "cr919-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.commit()
    return db


def _request(db, body, *, origin=None):
    request = MagicMock()
    request.query_params = {}
    request.path_params = {}
    request.headers = {"authorization": "Bearer session:synthetic", "host": "oncofiles.test"}
    if origin:
        request.headers["origin"] = origin
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    request.json = AsyncMock(return_value=body)
    return request


@contextmanager
def _caller(email, *, admin=False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=email),
        patch.object(server, "_check_bearer", return_value=unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


@pytest.fixture
def confirm_spy():
    spy = AsyncMock(return_value=(200, {"result": "confirmed", "codes": []}))
    with patch.object(tool, "confirm_review_for", spy):
        yield spy


async def test_route_foreign_and_unknown_patient_are_the_same_not_found(acl_db, confirm_spy):
    with _caller(A_EMAIL):
        foreign = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "cr919-b", "document_id": 1}))  # fmt: skip
        unknown = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "no-such-919", "document_id": 1}))  # fmt: skip
    assert foreign.status_code == unknown.status_code == 404
    assert foreign.body == unknown.body == b'{"error":"not_found"}'
    confirm_spy.assert_not_called()


async def test_route_admin_who_is_not_the_caregiver_is_read_only(acl_db, confirm_spy):
    with _caller("admin919@example.test", admin=True):
        resp = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "cr919-b", "document_id": 1}))  # fmt: skip
    assert resp.status_code == 403 and json.loads(resp.body)["error"] == "read_only"
    confirm_spy.assert_not_called()


async def test_route_admin_who_is_the_caregiver_may_confirm(acl_db, confirm_spy):
    with _caller(B_EMAIL, admin=True), patch("oncofiles.config.MCP_BEARER_TOKEN", "bearer-919"):
        resp = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "cr919-b", "document_id": 7}))  # fmt: skip
    assert resp.status_code == 200
    args, kwargs = confirm_spy.call_args
    assert args[1:] == (B_UUID, 7)
    assert kwargs["caller_hash"] and B_EMAIL not in kwargs["caller_hash"]


async def test_route_caregiver_reaches_the_action(acl_db, confirm_spy):
    with _caller(A_EMAIL):
        resp = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "cr919-a", "document_id": "12"}))  # fmt: skip
    assert resp.status_code == 200
    assert confirm_spy.call_args.args[1:] == (A_UUID, 12)


@pytest.mark.parametrize("bad", [None, True, "x", 0, -3, 1.5, [1]])
async def test_route_bad_document_id_is_400(acl_db, confirm_spy, bad):
    with _caller(A_EMAIL):
        resp = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "cr919-a", "document_id": bad}))  # fmt: skip
    assert resp.status_code == 400
    confirm_spy.assert_not_called()


async def test_route_cross_origin_is_refused(acl_db, confirm_spy):
    with _caller(A_EMAIL):
        resp = await server.api_curated_confirm_review(
            _request(acl_db, {"patient_id": "cr919-a", "document_id": 1},
                     origin="https://evil.example.test"))  # fmt: skip
    assert resp.status_code == 403
    confirm_spy.assert_not_called()


# ── locks ─────────────────────────────────────────────────────────────


def test_confirm_module_writes_only_through_update_text():
    """The only Drive write of the action is ``update_text`` on the sidecar."""
    tree = ast.parse((SRC / "curated" / "confirm_review.py").read_text(encoding="utf-8"))
    attrs = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    writes = {"create_text", "move_rename", "upload_original", "create_folder", "trash_canary",
              "upload", "update", "trash_file", "rename_file", "move_file"}  # fmt: skip
    assert attrs & writes == set()
    assert "update_text" in attrs and "contract_writes" not in attrs


def test_update_text_refuses_an_original_so_the_action_can_never_write_one():
    from tests.test_curated_originals_invariant import FakeGDrive

    g = FakeGDrive()
    pdf = g._service.add(FILED, "folder1", b"%PDF original")
    with (
        patch(
            "oncofiles.curated.drive_ops.download",
            lambda gd, fid: gd._service.items[fid]["content"],
        ),  # fmt: skip
        pytest.raises(Exception, match="not a text state file"),
    ):
        from oncofiles.curated.drive_ops import DriveOps

        DriveOps(g).update_text(pdf, _sidecar(SOFT))
    assert g._service.items[pdf]["content"] == b"%PDF original"


def test_dashboard_wiring():
    js = (SRC / "dashboard.js").read_text(encoding="utf-8")
    blocked = re.search(r"var DEMO_BLOCKED_ACTIONS = \{(.*?)\};", js, re.S).group(1)
    assert "confirmReviewAsIs: 1" in blocked
    codes = re.search(r"var CONFIRMABLE_REVIEW_CODES = \[(.*?)\];", js).group(1)
    assert set(re.findall(r"'([a-z_]+)'", codes)) == set(CONFIRMABLE_CODES)
    block = js[js.index("var CONFIRMABLE_REVIEW_CODES") : js.index("function renderInboxDetail")]
    assert "innerHTML" not in block and "confirm(" not in block.replace("confirmReview", "")
    assert "alert(" not in block
    assert "'Potvrdiť tak, ako je', 'Confirm as is'" in block
    assert "data-action', 'confirmReviewAsIs|" in block
    assert "classList.add('armed')" in block
    assert "/api/curated/confirm-review" in block
