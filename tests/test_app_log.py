"""Persistent rotating app log (#706): configured from env, off by default."""

import logging
import time
from logging.handlers import TimedRotatingFileHandler

import pytest

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


# ── ReDoS hardening and redact-before-truncate (automated reviews of #756) ──

LIMIT = app_log.MAX_REDACT_CHARS
HEAD_N = LIMIT - app_log._TAIL_CHARS  # the head cut (12 KB)


def _words(n: int) -> str:
    return "word " * n


def test_pathological_input_redacts_fast_and_is_truncated():
    evil = "a" * 500_000 + "@" + "a." * 250_000  # about 1 MB, no TLD
    t0 = time.perf_counter()
    out = app_log.redact(evil)
    assert time.perf_counter() - t0 < 0.5
    assert len(out) <= LIMIT + 64
    assert "…[truncated " in out


def test_every_pattern_is_fast_on_its_own_worst_case():
    cases = [
        "x" * 30_000 + "@",
        "_a" * 15_000,
        "a." * 15_000,
        "Bearer " + "A" * 30_000,
        "token=" * 5_000,
        "onco_" + "-" * 30_000,
        "1//" * 10_000,
    ]
    for case in cases:
        t0 = time.perf_counter()
        app_log._redact_all(case)  # untruncated: the patterns alone must be linear
        assert time.perf_counter() - t0 < 0.5, case[:20]


@pytest.mark.parametrize(
    "line",
    [
        "RESEND_API_KEY=SyntheticSecretValue",
        "auth_token=SyntheticSecretValue",
        "oauth_token=SyntheticSecretValue",
        "client_secret: SyntheticSecretValue",
        '{"refresh_token": "SyntheticSecretValue"}',
        "password=SyntheticSecretValue",
    ],
)
def test_prefixed_and_plain_secret_keys_are_masked(line):
    out = app_log.redact(f"config {line} loaded")
    assert "SyntheticSecretValue" not in out and "[redacted]" in out


def test_non_secret_counters_stay():
    assert app_log.redact("ids max_tokens=12 fine") == "ids max_tokens=12 fine"


def test_normal_emails_and_tokens_still_masked():
    for email in ("caregiver.synthetic@example.test", "a+b_c%d@mail.sub.example.co"):
        assert app_log.redact(f"login {email} ok") == "login [email] ok"
    assert app_log.redact("Bearer SyntheticBearerValueXyz") == "Bearer [redacted]"


def test_email_after_a_long_underscore_run_is_masked():
    out = app_log.redact("_" * 100 + "caregiver.synthetic@example.test")
    assert "caregiver" not in out and "example.test" not in out and "[email]" in out


def test_google_refresh_token_after_a_slash_is_masked():
    out = app_log.redact("path /1//SyntheticRefreshTokenValueAbcdef end")
    assert "SyntheticRefreshTokenValue" not in out and "[token]" in out


def test_truncation_keeps_head_and_tail_and_short_text_whole():
    text = "HEADSTART " + _words(5_000) + "TAILEND"
    out = app_log.redact(text)
    assert out.startswith("HEADSTART") and out.endswith("TAILEND")
    assert len(out) < LIMIT + 64 and "…[truncated " in out
    assert app_log.redact("short") == "short"


def test_truncation_marker_counts_dropped_chars():
    text = _words(4_000)
    assert f"…[truncated {len(text) - LIMIT} chars]" in app_log.redact(text)


@pytest.mark.parametrize("tail_words", [3_000, 60_000])  # below and above the raw cap
def test_bearer_token_straddling_the_head_cut_leaves_no_fragment(tail_words):
    token = "QzSyntheticStraddleTokenQz" * 3
    prefix = "auth header Bearer "
    filler = "w " * ((HEAD_N - len(prefix) - len(token) // 2) // 2)
    text = filler + prefix + token + " " + _words(tail_words)
    start = len(filler) + len(prefix)
    assert start < HEAD_N < start + len(token)  # really straddles the cut
    out = app_log.redact(text)
    assert "Qz" not in out and "Straddle" not in out and "Synthetic" not in out


def test_unknown_token_straddling_a_cut_is_masked():
    blob = "Zx" * 40  # not a known token shape: only the cut-boundary mask applies
    text = "w " * ((HEAD_N - len(blob) // 2) // 2) + blob + " " + _words(LIMIT)
    out = app_log.redact(text)
    assert "Zx" not in out and "[cut]" in out


def test_email_straddling_the_tail_cut_leaves_no_fragment():
    email = "caregiver.straddle@example.test"
    tail_len = app_log._TAIL_CHARS
    after = _words(tail_len)[: tail_len - len(email) // 2]
    text = _words(4_000) + email + after
    pos = text.index(email)
    assert pos < len(text) - tail_len < pos + len(email)
    out = app_log.redact(text)
    assert "straddle" not in out and "example" not in out


RAW = app_log.MAX_RAW_CHARS
RAW_TAIL = app_log._RAW_TAIL_CHARS


def _straddle(cut_at: int, total: int, secret: str) -> str:
    """Space-separated filler of length ``total`` with ``secret`` across ``cut_at``."""
    pos = cut_at - len(secret) // 2
    text = ("w " * (total // 2 + 1))[:total]
    return text[: pos - 1] + " " + secret + " " + text[pos + len(secret) + 1 :]


@pytest.mark.parametrize("where", ["raw_head", "raw_tail", "head", "tail"])
@pytest.mark.parametrize(
    "secret", ["Bearer QzSyntheticStraddleTokenQzQzSyntheticStraddleQz", "Zx" * 40]
)
def test_a_secret_straddling_any_cut_leaves_no_fragment(where, secret):
    total = RAW + RAW_TAIL  # over the raw cap: every cut exists
    cut_at = {
        "raw_head": RAW - RAW_TAIL,
        "raw_tail": total - RAW_TAIL,
        "head": HEAD_N,
        "tail": total - app_log._TAIL_CHARS,
    }[where]
    text = _straddle(cut_at, total, secret)
    assert len(text) == total and secret in text
    out = app_log.redact(text)
    for frag in ("Qz", "Straddle", "Synthetic", "Zx"):
        assert frag not in out, (where, frag)


def test_raw_cap_bounds_the_work_and_reports_dropped_chars():
    text = _words(RAW)  # five times the cap
    t0 = time.perf_counter()
    out = app_log.redact(text)
    assert time.perf_counter() - t0 < 0.5
    assert f"…[truncated {len(text) - LIMIT} chars]" in out and len(out) < LIMIT + 64


# ── a cut between a context prefix and its value (final review of #756) ──

PREFIXES = ["Bearer ", "password: ", '"refresh_token": "']
VALUE = "QzSyntheticValueQz" * 2


def _filler(total: int, newlines: bool) -> str:
    line = ("w " * 39 + "\n") if newlines else "w " * 40
    return (line * (total // len(line) + 1))[:total]


def _with_split_at(cut: int, total: int, prefix: str, newlines: bool) -> str:
    """Filler with ``prefix`` ending exactly at ``cut`` and VALUE starting there."""
    base = _filler(total, newlines)
    text = base[: cut - len(prefix) - 1] + " " + prefix + VALUE + " " + base[cut + len(VALUE) + 1 :]
    assert text[cut - len(prefix) : cut] == prefix and text[cut : cut + len(VALUE)] == VALUE
    return text


@pytest.mark.parametrize("newlines", [True, False])
@pytest.mark.parametrize("prefix", PREFIXES)
def test_helpers_never_show_a_value_without_its_prefix(prefix, newlines):
    total = 40_000
    cut = 20_000
    text = _with_split_at(cut, total, prefix, newlines)
    head = app_log._head(text, cut)  # head-side cut right after the prefix
    tail = app_log._tail(text, total - cut)  # tail-side cut right before the value
    assert prefix.strip() not in head[-app_log._CUT_LINE_CHARS :]
    assert "Qz" not in tail and "Synthetic" not in tail
    assert head.endswith(app_log._CUT) and tail.startswith(app_log._CUT)


@pytest.mark.parametrize("newlines", [True, False])
@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("where", ["raw_head", "raw_tail", "head", "tail"])
def test_end_to_end_split_prefix_and_value_leaves_no_fragment(where, prefix, newlines):
    total = RAW + RAW_TAIL if where.startswith("raw") else LIMIT * 4
    cut = {
        "raw_head": RAW - RAW_TAIL,
        "raw_tail": total - RAW_TAIL,
        "head": HEAD_N,
        "tail": total - app_log._TAIL_CHARS,
    }[where]
    out = app_log.redact(_with_split_at(cut, total, prefix, newlines))
    assert "Qz" not in out and "Synthetic" not in out, where
