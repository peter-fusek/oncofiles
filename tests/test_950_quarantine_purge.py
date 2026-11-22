# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950 quarantine purge: encrypted files go after the 30-day trash.

Maintainer decision 2026-11-22 (RUNBOOK lifecycle): rejected / expired items
lose ONLY their encrypted files ``TRASH_DAYS`` after ``decided_at``; the row
stays with state ``purged``. Approved copies are removed only with
``QUARANTINE_PURGE_APPROVED=true`` (default off), when every file was filed
and the approval is older than ``QUARANTINE_PURGE_APPROVED_AFTER_DAYS``.
Pending items (a restored item is pending) are never touched.

Synthetic only: invented addresses and bytes; timestamps relative to now.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from oncofiles import inbound_quarantine, server
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures used by name
    PID,
    STRANGER,
    _call,
    _dash_request,
    _env,
    drive,
    idb,
    mails,
    pdf,
)
from tests.test_950_quarantine import (  # noqa: F401 - autouse fixture + helpers
    _quarantine_env,
    ago,
    quarantine_one,
    rows,
)

ROOT = Path(server.__file__).parent.parent.parent


@pytest.fixture(autouse=True)
def _purge_env(monkeypatch):
    monkeypatch.delenv("QUARANTINE_PURGE_APPROVED", raising=False)
    monkeypatch.delenv("QUARANTINE_PURGE_APPROVED_AFTER_DAYS", raising=False)
    inbound_quarantine._purge_last.clear()
    yield
    inbound_quarantine._purge_last.clear()


def item_dir(tmp_path: Path, item_uuid: str) -> Path:
    return tmp_path / "quarantine" / item_uuid


async def new_item(db, n: int = 1, files: int = 1) -> dict:
    await quarantine_one(
        db,
        sender=f"sender{n}purge@example.test",
        files=[
            (f"Synth{n}x{i}.pdf", pdf(900 + 10 * n + i), "application/pdf") for i in range(files)
        ],
    )
    [item] = await rows(db, "SELECT * FROM inbound_quarantine ORDER BY id DESC LIMIT 1")
    return item


async def set_cols(db, item_id: int, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    await db.db.execute(
        f"UPDATE inbound_quarantine SET {sets} WHERE id = ?", (*cols.values(), item_id)
    )
    await db.db.commit()


async def rejected(db, n: int = 1, days: int = 31, files: int = 1) -> dict:
    item = await new_item(db, n, files)
    await inbound_quarantine.reject(db, PID, item["id"], decided_by="h", via="dashboard")
    await set_cols(db, item["id"], decided_at=ago(days=days))
    return item


async def approved(db, n: int = 1, days: int = 8) -> dict:
    item = await new_item(db, n)
    out = await inbound_quarantine.approve(db, PID, item["id"], decided_by="h", via="dashboard")
    assert out["result"] == "approved"
    await set_cols(db, item["id"], decided_at=ago(days=days))
    return item


async def row(db, item_id: int) -> dict:
    [r] = await rows(db, "SELECT * FROM inbound_quarantine WHERE id = ?", item_id)
    return r


# ── rejected / expired after the trash ───────────────────────────────────


async def test_rejected_after_the_trash_loses_only_its_files(idb, drive, mails, tmp_path):
    item = await rejected(idb, files=2)
    folder = item_dir(tmp_path, item["item_uuid"])
    sizes = sum(p.stat().st_size for p in folder.iterdir())
    out = await inbound_quarantine.run_maintenance(idb)
    assert out["purge"]["purged_items"] == 1 and out["purge"]["files_removed"] == 2
    assert out["purge"]["purged_bytes"] == sizes
    assert not folder.exists()
    assert (tmp_path / "quarantine").is_dir()  # the root stays
    after = await row(idb, item["id"])
    assert after["state"] == "purged" and after["files_purged_at"]
    # the row and its encrypted blobs stay
    assert after["sender_enc"] == item["sender_enc"] and after["files_enc"] == item["files_enc"]
    assert after["file_count"] == 2 and after["total_bytes"] == item["total_bytes"]
    # a purged item is neither restorable, nor approvable, nor in the trash list
    assert (await inbound_quarantine.restore(idb, PID, item["id"], decided_by="h"))[
        "result"
    ] == "not_restorable"
    assert (await inbound_quarantine.approve(idb, PID, item["id"], decided_by="h", via="x"))[
        "result"
    ] == "not_pending"
    view = await inbound_quarantine.dashboard_view(idb, PID, caregiver=True)
    assert view["trash"] == [] and view["items"] == []
    assert drive.files == []


async def test_expired_after_the_trash_is_purged(idb, drive, mails, tmp_path):
    item = await new_item(idb)
    await set_cols(idb, item["id"], received_at=ago(days=70))
    await inbound_quarantine.run_maintenance(idb)  # expires it, decided_at = now
    assert (await row(idb, item["id"]))["state"] == "expired"
    assert item_dir(tmp_path, item["item_uuid"]).is_dir()  # inside the trash: kept
    await set_cols(idb, item["id"], decided_at=ago(days=31))
    await inbound_quarantine.run_maintenance(idb)
    assert (await row(idb, item["id"]))["state"] == "purged"
    assert not item_dir(tmp_path, item["item_uuid"]).exists()


async def test_rejected_inside_the_trash_is_kept(idb, drive, mails, tmp_path):
    item = await rejected(idb, days=29)
    out = await inbound_quarantine.purge_due(idb)
    assert out == {}
    assert (await row(idb, item["id"]))["state"] == "rejected"
    assert inbound_quarantine.read_file(PID, item["item_uuid"], 0).startswith(b"%PDF")


async def test_rejected_and_restored_within_the_window_is_never_purged(idb, drive, mails, tmp_path):
    item = await rejected(idb, days=29)
    assert (await inbound_quarantine.restore(idb, PID, item["id"], decided_by="h"))[
        "result"
    ] == "restored"
    # even long after: a restored item is pending until decided again
    await inbound_quarantine.run_maintenance(idb)
    assert (await row(idb, item["id"]))["state"] == "pending"
    assert inbound_quarantine.read_file(PID, item["item_uuid"], 0).startswith(b"%PDF")


async def test_pending_is_never_purged_whatever_its_timestamps(idb, drive, mails, monkeypatch):
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED", "true")
    item = await new_item(idb)
    # a pending row with an old decided_at (restore clears it; forced here)
    await set_cols(idb, item["id"], decided_at=ago(days=400), filed_count=1)
    assert await inbound_quarantine.purge_due(idb) == {}
    assert (await row(idb, item["id"]))["state"] == "pending"
    assert inbound_quarantine.read_file(PID, item["item_uuid"], 0).startswith(b"%PDF")


async def test_second_run_is_idempotent(idb, drive, mails):
    await rejected(idb)
    first = await inbound_quarantine.purge_due(idb)
    assert first["purged_items"] == 1
    assert await inbound_quarantine.purge_due(idb) == {}


# ── approved copies: separate switch, default off ────────────────────────


async def test_approved_is_never_purged_with_the_switch_off(idb, drive, mails, tmp_path):
    assert inbound_quarantine.purge_approved_enabled() is False
    item = await approved(idb, days=365)
    assert await inbound_quarantine.purge_due(idb) == {}
    after = await row(idb, item["id"])
    assert after["state"] == "approved" and after["files_purged_at"] is None
    assert item_dir(tmp_path, item["item_uuid"]).is_dir()


async def test_approved_with_the_switch_on_after_seven_days(
    idb, drive, mails, monkeypatch, tmp_path
):
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED", "true")
    old = await approved(idb, 1, days=8)
    fresh = await approved(idb, 2, days=6)
    out = await inbound_quarantine.purge_due(idb)
    assert out["approved_purged_items"] == 1 and "purged_items" not in out
    gone = await row(idb, old["id"])
    assert gone["state"] == "approved" and gone["files_purged_at"]
    assert not item_dir(tmp_path, old["item_uuid"]).exists()
    kept = await row(idb, fresh["id"])
    assert kept["files_purged_at"] is None and item_dir(tmp_path, fresh["item_uuid"]).is_dir()
    # approving the purged item again never reads a missing file
    again = await inbound_quarantine.approve(idb, PID, old["id"], decided_by="h", via="x")
    assert again == {"result": "already"}
    assert len(drive.files) == 2  # what was filed stays filed


async def test_approved_with_a_file_not_filed_is_kept(idb, drive, mails, monkeypatch, tmp_path):
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED", "true")
    item = await approved(idb, days=30)
    await set_cols(idb, item["id"], filed_count=0)  # e.g. a duplicate skip
    assert await inbound_quarantine.purge_due(idb) == {}
    assert item_dir(tmp_path, item["item_uuid"]).is_dir()


def test_approved_after_days_env(monkeypatch):
    assert inbound_quarantine.purge_approved_after_days() == 7
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED_AFTER_DAYS", "14")
    assert inbound_quarantine.purge_approved_after_days() == 14
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED_AFTER_DAYS", "nonsense")
    assert inbound_quarantine.purge_approved_after_days() == 7
    monkeypatch.setenv("QUARANTINE_PURGE_APPROVED_AFTER_DAYS", "0")
    assert inbound_quarantine.purge_approved_after_days() == 1


# ── path safety, missing files, limits ───────────────────────────────────


async def test_a_missing_file_is_tolerated_and_counted(idb, drive, mails, tmp_path):
    item = await rejected(idb, files=2)
    os.remove(item_dir(tmp_path, item["item_uuid"]) / "1.bin")
    out = await inbound_quarantine.purge_due(idb)
    assert out["files_missing"] == 1 and out["files_removed"] == 1
    assert out["purged_items"] == 1
    assert not item_dir(tmp_path, item["item_uuid"]).exists()


async def test_a_missing_directory_still_marks_purged(idb, drive, mails, tmp_path):
    item = await rejected(idb)
    folder = item_dir(tmp_path, item["item_uuid"])
    os.remove(folder / "0.bin")
    os.rmdir(folder)
    out = await inbound_quarantine.purge_due(idb)
    assert out["files_missing"] == 1 and out["purged_items"] == 1
    assert (await row(idb, item["id"]))["state"] == "purged"


async def test_a_path_outside_the_root_is_refused(idb, drive, mails, tmp_path):
    victim_dir = tmp_path / "elsewhere"
    victim_dir.mkdir()
    (victim_dir / "0.bin").write_bytes(b"not ours")
    traversal = await rejected(idb, 1)
    await set_cols(idb, traversal["id"], item_uuid="../elsewhere")
    noncanonical = await rejected(idb, 2)
    await set_cols(idb, noncanonical["id"], item_uuid=noncanonical["item_uuid"].upper())
    # a symlinked item directory pointing outside the root
    linked = await rejected(idb, 3)
    folder = item_dir(tmp_path, linked["item_uuid"])
    os.remove(folder / "0.bin")
    os.rmdir(folder)
    folder.symlink_to(victim_dir, target_is_directory=True)
    out = await inbound_quarantine.purge_due(idb)
    assert out["purge_refused"] == 3 and "purged_items" not in out
    assert (victim_dir / "0.bin").read_bytes() == b"not ours"
    for it in (traversal, noncanonical, linked):
        assert (await row(idb, it["id"]))["state"] == "rejected"
    # the untouched real directory of the upper-cased row is still there
    assert item_dir(tmp_path, noncanonical["item_uuid"]).is_dir()


async def test_a_leftover_we_did_not_name_keeps_the_directory(idb, drive, mails, tmp_path):
    item = await rejected(idb)
    folder = item_dir(tmp_path, item["item_uuid"])
    (folder / "0.tmp").write_bytes(b"crashed write")
    out = await inbound_quarantine.purge_due(idb)
    assert out["dirs_kept"] == 1 and out["purged_items"] == 1
    assert not (folder / "0.bin").exists() and (folder / "0.tmp").exists()


async def test_at_most_n_items_per_run_committed_per_item(idb, drive, mails):
    for n in range(3):
        await rejected(idb, n)
    first = await inbound_quarantine.purge_due(idb, limit=2)
    assert first["purged_items"] == 2
    assert len(await rows(idb, "SELECT id FROM inbound_quarantine WHERE state = 'purged'")) == 2
    second = await inbound_quarantine.purge_due(idb, limit=2)
    assert second["purged_items"] == 1
    assert inbound_quarantine.PURGE_MAX_ITEMS == 200


async def test_purge_log_is_counts_only(idb, drive, mails, caplog):
    caplog.set_level(logging.DEBUG)
    item = await rejected(idb)
    await inbound_quarantine.run_maintenance(idb)
    lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == "oncofiles.inbound_quarantine" and "purge" in r.getMessage()
    ]
    assert lines and all(line.startswith("quarantine: purge {") for line in lines)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("oncofiles"))
    hidden = [item["item_uuid"], STRANGER, PID, "purge@example.test", ".pdf"]
    assert not [h for h in hidden if h in text]


# ── telemetry ────────────────────────────────────────────────────────────


async def test_admin_overview_and_readiness_report_the_last_run(idb, drive, mails, tmp_path):
    item = await rejected(idb)
    size = (item_dir(tmp_path, item["item_uuid"]) / "0.bin").stat().st_size
    before = await inbound_quarantine.admin_overview(idb)
    assert before["purged_items"] == 0 and before["purge"]["last_run"] is None
    assert before["purge"]["approved_switch"] is False
    await inbound_quarantine.run_maintenance(idb)
    resp = await _call(
        server.api_inbound_unmatched, _dash_request(idb), "admin950@example.test", admin=True
    )
    q = json.loads(resp.body)["quarantine"]
    assert q["purged_items"] == 1 and q["purged_bytes"] == size
    assert q["stored_bytes"] == 0 and q["by_state"]["purged"]["items"] == 1
    assert q["purge"]["last_run"]["purged_items"] == 1
    flat = json.dumps(q)
    assert "@" not in flat and PID not in flat and item["item_uuid"] not in flat
    src = Path(server.__file__).read_text()
    assert 'result["inbound_quarantine_purge"] = _inbound_quarantine.purge_overview()' in src


# ── schema, vocabulary, docs ─────────────────────────────────────────────


def test_purged_state_and_vocabulary():
    from oncofiles import vocab
    from oncofiles.database._inbound_quarantine import QUARANTINE_STATES

    assert "purged" in QUARANTINE_STATES
    assert "purged" in vocab.codes("inbound_quarantine_state")
    assert vocab.label("inbound_quarantine_state", "purged", "en")


def test_migration_102_is_additive():
    sql = (ROOT / "migrations" / "102_inbound_quarantine_purge.sql").read_text()
    code = "\n".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
    statements = [st.strip() for st in code.split(";") if st.strip()]
    assert statements == ["ALTER TABLE inbound_quarantine ADD COLUMN files_purged_at TEXT"]


def test_claude_md_documents_the_purge():
    text = (ROOT / "CLAUDE.md").read_text()
    assert "QUARANTINE_PURGE_APPROVED" in text and "purge_due" in text
    assert "that purge job is a separate PR" not in text
