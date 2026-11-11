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


# ── permissions and redaction (infra review of #756) ──

SECRETS = [
    "onco_SyntheticPatientTokenAbcdef",
    "Bearer SyntheticBearerValueXyzXyz",
    "refresh_token=SyntheticRefreshValueQwerty",
    "ya29.SyntheticGoogleAccessToken",
    "caregiver.synthetic@example.test",
]


def _file_handler(monkeypatch, tmp_path, subdir="logs"):
    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path / subdir))
    return app_log.file_handler_from_env(FMT, logging.INFO)


def test_dir_is_0700_and_file_0600_even_with_a_loose_umask(monkeypatch, tmp_path):
    import os
    import stat

    old = os.umask(0o002)
    try:
        h = _file_handler(monkeypatch, tmp_path)
    finally:
        os.umask(old)
    try:
        log_dir = tmp_path / "logs"
        assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((log_dir / "oncofiles.log").stat().st_mode) == 0o600
        assert (log_dir / "oncofiles.log").stat().st_uid == os.getuid()  # process uid
    finally:
        h.close()


def test_existing_loose_dir_and_file_are_tightened(monkeypatch, tmp_path):
    import os
    import stat

    log_dir = tmp_path / "logs"
    log_dir.mkdir(mode=0o755)
    (log_dir / "oncofiles.log").write_text("")
    os.chmod(log_dir / "oncofiles.log", 0o644)
    h = _file_handler(monkeypatch, tmp_path)
    try:
        assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((log_dir / "oncofiles.log").stat().st_mode) == 0o600
    finally:
        h.close()


def test_rotated_file_keeps_0600(monkeypatch, tmp_path):
    import stat

    h = _file_handler(monkeypatch, tmp_path)
    try:
        h.doRollover()
        files = sorted((tmp_path / "logs").iterdir())
        assert len(files) == 2
        assert {stat.S_IMODE(f.stat().st_mode) for f in files} == {0o600}
    finally:
        h.close()


def test_token_and_email_never_reach_the_file(monkeypatch, tmp_path):
    h = _file_handler(monkeypatch, tmp_path)
    log = logging.getLogger("test_app_log.redact")
    log.propagate = False
    log.addHandler(h)
    try:
        for s in SECRETS:
            log.warning("auth for %s failed", s)
        try:
            raise ValueError(f"bad credential {SECRETS[0]} for {SECRETS[-1]}")
        except ValueError:
            log.exception("traceback carries %s", "secrets")
        log.warning("patient 00000000-0000-4000-8000-00000000beef probed")
    finally:
        log.removeHandler(h)
        h.close()
    text = (tmp_path / "logs" / "oncofiles.log").read_text()
    for needle in ("SyntheticPatientToken", "SyntheticBearerValue", "SyntheticRefreshValue",
                   "SyntheticGoogleAccess", "caregiver.synthetic", "example.test"):  # fmt: skip
        assert needle not in text
    assert "[email]" in text and "[token]" in text and "[redacted]" in text
    assert "ValueError" in text  # the traceback is kept, only redacted
    assert "00000000-0000-4000-8000-00000000beef" in text  # ids are not secrets


def test_setup_logging_puts_the_same_filter_on_stderr_and_the_file(monkeypatch, tmp_path):
    import io

    from oncofiles import server

    monkeypatch.setenv("APP_LOG_DIR", str(tmp_path / "logs"))
    root = logging.getLogger()
    before = list(root.handlers)
    saved_filters = {h: list(h.filters) for h in before}
    stderr = logging.StreamHandler(io.StringIO())
    stderr.setFormatter(logging.Formatter(FMT))

    def fake_basic_config(**kw):  # stand-in for stderr; keeps pytest's handlers
        root.addHandler(stderr)

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)
    old_level = root.level
    server._setup_logging()
    added = [h for h in root.handlers if h not in before]
    try:
        files = [h for h in added if isinstance(h, app_log.CappedDailyFileHandler)]
        assert len(files) == 1 and stderr in added
        for h in (stderr, files[0]):
            assert any(isinstance(f, app_log.RedactFilter) for f in h.filters)
        logging.getLogger("test_app_log.setup").warning("login %s", SECRETS[-1])
        files[0].flush()
        for out in (stderr.stream.getvalue(), (tmp_path / "logs" / "oncofiles.log").read_text()):
            assert "caregiver.synthetic" not in out and "[email]" in out
    finally:
        for h in added:
            root.removeHandler(h)
            h.close()
        for h, flt in saved_filters.items():
            h.filters[:] = flt
        root.setLevel(old_level)
