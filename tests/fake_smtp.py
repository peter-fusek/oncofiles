"""A fake ``smtplib.SMTP`` for mailer tests (#834). No socket is opened.

``install(monkeypatch)`` replaces ``smtplib.SMTP`` and ``smtplib.SMTP_SSL`` and
returns the list that every created fake is appended to. Each fake records the
calls made on it and the messages it was given.
"""

from __future__ import annotations

import smtplib

# Synthetic config only (no real host, account or password).
SMTP_ENV = {
    "SMTP_HOST": "smtp.relay.invalid",
    "SMTP_PORT": "587",
    "SMTP_USER": "relay-user-synthetic",
    "SMTP_PASSWORD": "SyntheticSmtpSecret",
    "MAIL_FROM": "Oncofiles <noreply@oncofiles.com>",
    "MAIL_REPLY_TO": "hello@oncofiles.com",
    "NOTIFY_TO_EMAIL": "admin@oncofiles.com",
}

MAIL_ENV_NAMES = (*SMTP_ENV, "SMTP_SSL")


class FakeSMTP:
    instances: list[FakeSMTP]
    fail_with: BaseException | None = None
    ssl = False

    def __init__(self, host, port, timeout=None, context=None, **_kw):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.calls: list[str] = []
        self.login_args: tuple[str, str] | None = None
        self.sent: list[tuple[object, list[str]]] = []
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def ehlo(self, *a, **k):
        self.calls.append("ehlo")

    def starttls(self, *a, **k):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append("login")
        self.login_args = (user, password)

    def send_message(self, msg, from_addr=None, to_addrs=None, **_kw):
        if type(self).fail_with is not None:
            raise type(self).fail_with
        self.calls.append("send_message")
        self.sent.append((msg, list(to_addrs or [])))


def install(monkeypatch, *, env: bool = True, fail_with: BaseException | None = None):
    """Patch smtplib with fakes; optionally set the synthetic SMTP env."""
    instances: list[FakeSMTP] = []

    class _Plain(FakeSMTP):
        pass

    class _Ssl(FakeSMTP):
        ssl = True

    for cls in (_Plain, _Ssl):
        cls.instances = instances
        cls.fail_with = fail_with
    monkeypatch.setattr(smtplib, "SMTP", _Plain)
    monkeypatch.setattr(smtplib, "SMTP_SSL", _Ssl)
    for name in MAIL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    if env:
        for name, value in SMTP_ENV.items():
            monkeypatch.setenv(name, value)
    return instances
