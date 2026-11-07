"""drive_ops: reads survive a stale connection; writes are never retried (2026-11-07 SSLError)."""

import ssl
from unittest.mock import MagicMock

import pytest

from oncofiles.curated import drive_ops


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(drive_ops.time, "sleep", lambda s: None)


def _gdrive():
    g = MagicMock()
    g._guard_write = MagicMock()
    return g


def test_read_retries_on_ssl_error_with_fresh_connection():
    g = _gdrive()
    execute = g._service.files.return_value.get.return_value.execute
    execute.side_effect = [ssl.SSLError("bad record mac"), {"id": "f", "name": "a.pdf"}]
    assert drive_ops.get(g, "f")["id"] == "f"
    g._rebuild_service.assert_called_once()


def test_read_gives_up_after_three_attempts():
    g = _gdrive()
    g._service.files.return_value.get.return_value.execute.side_effect = ssl.SSLError("x")
    with pytest.raises(ssl.SSLError):
        drive_ops.get(g, "f")
    assert g._rebuild_service.call_count == 2


def test_non_connection_errors_are_not_retried():
    g = _gdrive()
    g._service.files.return_value.get.return_value.execute.side_effect = ValueError("boom")
    with pytest.raises(ValueError):
        drive_ops.get(g, "f")
    g._rebuild_service.assert_not_called()


def test_list_children_retries_page():
    g = _gdrive()
    g._service.files.return_value.list.return_value.execute.side_effect = [
        ssl.SSLError("x"),
        {"files": [{"id": "1", "name": "a", "mimeType": "application/pdf"}]},
    ]
    items = drive_ops.list_children(g, "folder")
    assert [i.name for i in items] == ["a"]


def test_writes_are_never_retried():
    g = _gdrive()
    g._service.files.return_value.list.return_value.execute.return_value = {"files": []}
    g._service.files.return_value.create.return_value.execute.side_effect = ssl.SSLError("x")
    with pytest.raises(ssl.SSLError):
        drive_ops.create_text(g, "folder", "a.md", "text")
    assert g._service.files.return_value.create.return_value.execute.call_count == 1


def test_driveops_renews_connection_only_after_a_pause(monkeypatch):
    g = _gdrive()
    g._service.files.return_value.list.return_value.execute.return_value = {"files": []}
    clock = [1000.0]
    monkeypatch.setattr(drive_ops.time, "monotonic", lambda: clock[0])
    ops = drive_ops.DriveOps(g)
    ops.list_children("f")
    g._rebuild_service.assert_not_called()
    clock[0] += drive_ops.STALE_AFTER_S + 1  # e.g. minutes of local-LLM work
    ops.list_children("f")
    g._rebuild_service.assert_called_once()
