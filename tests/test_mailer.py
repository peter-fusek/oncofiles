"""Outgoing e-mail over SMTP (#834): mailer, call sites, waitlist flags, landing.

smtplib is replaced by ``tests/fake_smtp.py``; no socket is opened (and the
network guard in conftest would fail the test if one were).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import smtplib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from oncofiles import mailer
from tests import fake_smtp

SUBSCRIBER = "waitlist-person@example.com"
LANDING = (Path(__file__).parent.parent / "src" / "oncofiles" / "landing.html").read_text(
    encoding="utf-8"
)


def _log_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _assert_no_secrets_in_logs(caplog) -> None:
    text = _log_text(caplog)
    assert "@" not in text, text
    assert fake_smtp.SMTP_ENV["SMTP_PASSWORD"] not in text


# ── mailer.send_mail ─────────────────────────────────────────────────────


async def test_send_mail_starttls_login_and_headers(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    instances = fake_smtp.install(monkeypatch)

    ok = await mailer.send_mail(
        [SUBSCRIBER], "Hello there", "plain body", html="<p>html body</p>", tags=["test_kind"]
    )

    assert ok is True
    assert len(instances) == 1
    smtp = instances[0]
    assert not smtp.ssl
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.relay.invalid", 587, 10.0)
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "send_message", "quit"]
    assert smtp.login_args == ("relay-user-synthetic", "SyntheticSmtpSecret")
    msg, rcpt = smtp.sent[0]
    assert rcpt == [SUBSCRIBER]
    assert msg["From"] == "Oncofiles <noreply@oncofiles.com>"
    assert msg["To"] == SUBSCRIBER
    assert msg["Subject"] == "Hello there"
    assert msg["Reply-To"] == "hello@oncofiles.com"
    assert msg["Message-ID"].endswith("@oncofiles.com>")
    assert msg["X-Mailin-Tag"] == "test_kind"
    assert msg.get_body(("plain",)).get_content().strip() == "plain body"
    assert msg.get_body(("html",)).get_content().strip() == "<p>html body</p>"
    # Logged: kind, count, hash. Never an address, the subject or the body.
    text = _log_text(caplog)
    assert "Mail sent (kind=test_kind" in text and "recipients=1" in text
    assert "Hello there" not in text and "plain body" not in text
    _assert_no_secrets_in_logs(caplog)


async def test_send_mail_implicit_tls_on_465(monkeypatch):
    instances = fake_smtp.install(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "465")

    assert await mailer.send_mail([SUBSCRIBER], "s", "t") is True
    assert instances[0].ssl
    assert "starttls" not in instances[0].calls


async def test_send_mail_skips_login_without_user(monkeypatch):
    instances = fake_smtp.install(monkeypatch)
    monkeypatch.delenv("SMTP_USER")

    assert await mailer.send_mail([SUBSCRIBER], "s", "t") is True
    assert "login" not in instances[0].calls
    assert "starttls" in instances[0].calls


@pytest.mark.parametrize("missing", ["SMTP_HOST", "MAIL_FROM"])
async def test_missing_config_returns_false_without_connecting(monkeypatch, caplog, missing):
    caplog.set_level(logging.WARNING)
    instances = fake_smtp.install(monkeypatch)
    monkeypatch.delenv(missing)

    assert mailer.is_configured() is False
    assert await mailer.send_mail([SUBSCRIBER], "s", "t", tags=["k"]) is False
    assert instances == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and missing in warnings[0].getMessage()
    _assert_no_secrets_in_logs(caplog)


async def test_bad_port_disables_mail(monkeypatch):
    instances = fake_smtp.install(monkeypatch)
    monkeypatch.setenv("SMTP_PORT", "five-eight-seven")
    assert await mailer.send_mail([SUBSCRIBER], "s", "t") is False
    assert instances == []


async def test_smtp_failure_returns_false_and_logs_code_only(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    fake_smtp.install(
        monkeypatch,
        fail_with=smtplib.SMTPRecipientsRefused(
            {SUBSCRIBER: (550, b"unknown " + SUBSCRIBER.encode())}
        ),
    )
    assert await mailer.send_mail([SUBSCRIBER], "s", "t") is False
    assert "SMTPRecipientsRefused" in _log_text(caplog)
    _assert_no_secrets_in_logs(caplog)


async def test_smtp_auth_error_logs_numeric_code(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    fake_smtp.install(
        monkeypatch, fail_with=smtplib.SMTPAuthenticationError(535, b"bad SyntheticSmtpSecret")
    )
    assert await mailer.send_mail([SUBSCRIBER], "s", "t") is False
    assert "SMTPAuthenticationError:535" in _log_text(caplog)
    _assert_no_secrets_in_logs(caplog)


async def test_header_injection_is_neutralised(monkeypatch):
    instances = fake_smtp.install(monkeypatch)

    assert await mailer.send_mail(["a@example.com\r\nBcc: b@example.com"], "s", "t") is False
    assert instances == []

    assert await mailer.send_mail([SUBSCRIBER], "line1\r\nBcc: b@example.com", "t") is True
    msg, rcpt = instances[0].sent[0]
    assert rcpt == [SUBSCRIBER]
    assert msg["Bcc"] is None
    assert "\n" not in msg["Subject"]


def test_admin_recipients_splits_list(monkeypatch):
    monkeypatch.setenv("NOTIFY_TO_EMAIL", "a@example.com, b@example.com,")
    assert mailer.admin_recipients() == ["a@example.com", "b@example.com"]
    monkeypatch.delenv("NOTIFY_TO_EMAIL")
    assert mailer.admin_recipients() == []


def test_mailer_is_stdlib_smtp_and_resend_is_gone():
    """One SMTP path; no HTTP mail API left anywhere in the package (#834)."""
    src = Path(mailer.__file__).parent
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "api.resend.com" not in text, path
        assert "RESEND_API_KEY" not in text or path.name == "app_log.py", path
        assert "NOTIFY_FROM_EMAIL" not in text, path
    mailer_src = Path(mailer.__file__).read_text(encoding="utf-8")
    assert "import smtplib" in mailer_src and "httpx" not in mailer_src


# ── Call sites ───────────────────────────────────────────────────────────


async def test_welcome_mail_is_bilingual_and_link_clean(monkeypatch, caplog):
    from oncofiles.server import _send_subscriber_welcome

    caplog.set_level(logging.DEBUG)
    instances = fake_smtp.install(monkeypatch)

    assert await _send_subscriber_welcome(SUBSCRIBER, "landing") is True
    msg, rcpt = instances[0].sent[0]
    assert rcpt == [SUBSCRIBER]
    assert msg["Reply-To"] == "hello@oncofiles.com"  # unsubscribe = reply
    text = msg.get_body(("plain",)).get_content()
    html = msg.get_body(("html",)).get_content()
    for body in (text, html):
        assert "Ďakujeme" in body and "Thank you" in body
        assert "odpovedzte na tento e-mail" in body and "reply to this e-mail" in body
    # No signed links, no tracking parameters of ours.
    assert "?" not in " ".join(re.findall(r"https?://\S+", text + html))
    assert "utm_" not in text + html and "<img" not in html
    _assert_no_secrets_in_logs(caplog)


async def test_lead_mail_carries_address_in_body_only(monkeypatch, caplog):
    from oncofiles.server import _notify_new_subscriber

    caplog.set_level(logging.DEBUG)
    instances = fake_smtp.install(monkeypatch)

    await _notify_new_subscriber(SUBSCRIBER, "landing")
    msg, rcpt = instances[0].sent[0]
    assert rcpt == ["admin@oncofiles.com"]
    assert msg["Subject"].startswith("[Lead]") and SUBSCRIBER not in msg["Subject"]
    assert SUBSCRIBER in msg.get_content()
    _assert_no_secrets_in_logs(caplog)


async def test_lead_mail_without_notify_to_is_skipped(monkeypatch, caplog):
    from oncofiles.server import _notify_new_subscriber

    caplog.set_level(logging.WARNING)
    instances = fake_smtp.install(monkeypatch)
    monkeypatch.delenv("NOTIFY_TO_EMAIL")

    await _notify_new_subscriber(SUBSCRIBER, "landing")
    assert instances == []
    assert "NOTIFY_TO_EMAIL" in _log_text(caplog)
    _assert_no_secrets_in_logs(caplog)


async def test_pipeline_alert_sends_once_per_day(monkeypatch):
    from oncofiles import server

    instances = fake_smtp.install(monkeypatch)
    monkeypatch.setattr(server, "_pipeline_alert_sent", {})

    for _ in range(2):
        await server._notify_admin_pipeline_alert(
            condition="sync_stale", subject="[Oncofiles] stalled", body="detail"
        )
    assert len(instances) == 1
    msg, rcpt = instances[0].sent[0]
    assert rcpt == ["admin@oncofiles.com"] and msg["Subject"] == "[Oncofiles] stalled"


async def test_pipeline_alert_unconfigured_is_skipped_and_not_marked(monkeypatch):
    from oncofiles import server

    instances = fake_smtp.install(monkeypatch, env=False)
    sent: dict[str, str] = {}
    monkeypatch.setattr(server, "_pipeline_alert_sent", sent)

    await server._notify_admin_pipeline_alert(condition="sync_stale", subject="s", body="b")
    assert instances == [] and sent == {}


# ── Waitlist endpoint flags ──────────────────────────────────────────────


@pytest.fixture
def _clear_rate_limits():
    from oncofiles.server import _newsletter_rate

    _newsletter_rate.clear()
    yield
    _newsletter_rate.clear()


def _subscribe_request(db, email: str) -> MagicMock:
    request = MagicMock()
    request.client = MagicMock()
    request.client.host = "127.0.0.1"

    async def _json():
        return {"email": email, "source": "landing"}

    request.json = _json
    request.app = MagicMock()
    request.app.state.fastmcp_server._lifespan_result = {"db": db}
    return request


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0.01)


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_subscribe_reports_email_queued_and_sends_both_mails(db, monkeypatch):
    from oncofiles import server

    instances = fake_smtp.install(monkeypatch)

    resp = await server.api_newsletter_subscribe(_subscribe_request(db, SUBSCRIBER))
    assert json.loads(resp.body) == {"ok": True, "email_queued": True}
    await _drain()
    recipients = sorted(r for i in instances for _m, rs in i.sent for r in rs)
    assert recipients == ["admin@oncofiles.com", SUBSCRIBER]

    # Already on the list: same answer (no membership oracle), nothing sent.
    resp2 = await server.api_newsletter_subscribe(_subscribe_request(db, SUBSCRIBER))
    assert json.loads(resp2.body) == {"ok": True, "email_queued": True}
    await _drain()
    assert len(instances) == 2


@pytest.mark.usefixtures("_clear_rate_limits")
async def test_subscribe_without_mail_config_says_not_queued(db, monkeypatch, caplog):
    from oncofiles import server

    caplog.set_level(logging.INFO)  # aiosqlite DEBUG echoes SQL parameters
    instances = fake_smtp.install(monkeypatch, env=False)

    resp = await server.api_newsletter_subscribe(_subscribe_request(db, SUBSCRIBER))
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"ok": True, "email_queued": False}
    await _drain()
    assert instances == []
    assert SUBSCRIBER not in _log_text(caplog)


# ── Landing text ─────────────────────────────────────────────────────────


def test_landing_no_longer_has_the_waitlist_form():
    """Live operation (2026-11-21): the waitlist form left the landing. The
    route stays (old cached pages, see the subscribe tests above), but the
    landing never posts to it and never promises a confirmation e-mail."""
    assert "/api/newsletter/subscribe" not in LANDING
    assert "<form" not in LANDING
    assert "check your e-mail" not in LANDING.lower()
    assert "on the list" not in LANDING.lower()


async def test_extra_headers_are_added(monkeypatch):
    """#950: an automatic reply carries Auto-Submitted (RFC 3834)."""
    instances = fake_smtp.install(monkeypatch)
    ok = await mailer.send_mail(
        [SUBSCRIBER], "Receipt", "body", headers={"Auto-Submitted": "auto-replied"}
    )
    assert ok is True
    msg, _rcpt = instances[0].sent[0]
    assert msg["Auto-Submitted"] == "auto-replied"


@pytest.mark.parametrize(
    "headers",
    [
        {"From": "other@example.test"},
        {"Message-ID": "<x@example.test>"},
        {"Bad Name": "x"},
        {"X-Ok\r\nBcc": "x"},
        {"X-Ok": 1},
    ],
)
async def test_extra_headers_cannot_replace_or_inject(monkeypatch, headers):
    instances = fake_smtp.install(monkeypatch)
    assert await mailer.send_mail([SUBSCRIBER], "s", "b", headers=headers) is False
    assert instances == []


async def test_extra_header_values_are_one_line(monkeypatch):
    instances = fake_smtp.install(monkeypatch)
    await mailer.send_mail([SUBSCRIBER], "s", "b", headers={"X-Note": "a\r\nBcc: x"})
    msg, _rcpt = instances[0].sent[0]
    assert msg["Bcc"] is None and "\n" not in msg["X-Note"]
