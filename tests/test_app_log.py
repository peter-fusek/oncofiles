"""Persistent rotating app log (#706): configured from env, off by default."""

import logging
from logging.handlers import TimedRotatingFileHandler

from oncofiles import app_log

FMT = "%(levelname)s %(message)s"


def test_off_without_app_log_dir(monkeypatch):
    monkeypatch.delenv("APP_LOG_DIR", raising=False)
    assert app_log.file_handler_from_env(FMT, logging.INFO) is None
    monkeypatch.setenv("APP_LOG_DIR", "  ")
    assert app_log.file_handler_from_env(FMT, logging.INFO) is None


def test_daily_rotation_seven_days_in_the_configured_dir(monkeypatch, tmp_path):
    target = tmp_path / "data" / "logs"
    monkeypatch.setenv("APP_LOG_DIR", str(target))
    for k in ("APP_LOG_RETENTION_DAYS", "APP_LOG_MAX_MB"):
        monkeypatch.delenv(k, raising=False)
    h = app_log.file_handler_from_env(FMT, logging.INFO)
    try:
        assert isinstance(h, TimedRotatingFileHandler)
        assert target.is_dir()
        assert h.baseFilename == str(target / "oncofiles.log")
        assert h.when == "MIDNIGHT" and h.utc and h.backupCount == 7
        assert h.max_bytes == 50 * 1024 * 1024
        assert h.level == logging.INFO
    finally:
        h.close()


def test_retention_and_cap_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("APP_LOG_RETENTION_DAYS", "3")
    monkeypatch.setenv("APP_LOG_MAX_MB", "1")
    h = app_log.file_handler_from_env(FMT, logging.DEBUG)
    try:
        assert h.backupCount == 3 and h.max_bytes == 1024 * 1024
    finally:
        h.close()


def test_size_cap_drops_lines_after_one_marker(tmp_path):
    h = app_log.CappedDailyFileHandler(
        str(tmp_path / "oncofiles.log"), backup_days=7, max_bytes=200
    )
    h.setFormatter(logging.Formatter(FMT))
    log = logging.getLogger("test_app_log.cap")
    log.propagate = False
    log.addHandler(h)
    try:
        for i in range(50):
            log.warning("synthetic line %03d with some padding", i)
    finally:
        log.removeHandler(h)
        h.close()
    text = (tmp_path / "oncofiles.log").read_text()
    assert text.count("log size cap reached") == 1
    assert len(text.encode()) < 200 + 200  # cap plus at most one line and the marker
    assert "synthetic line 049" not in text


def test_unusable_dir_does_not_raise(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("APP_LOG_DIR", str(blocker / "logs"))  # parent is a file
    assert app_log.file_handler_from_env(FMT, logging.INFO) is None


def test_setup_logging_attaches_the_handler(monkeypatch, tmp_path):
    from oncofiles import server

    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: None)  # keep pytest's handlers
    root = logging.getLogger()
    before = list(root.handlers)
    server._setup_logging()
    added = [h for h in root.handlers if h not in before]
    try:
        assert len(added) == 1 and isinstance(added[0], app_log.CappedDailyFileHandler)
    finally:
        for h in added:
            root.removeHandler(h)
            h.close()
