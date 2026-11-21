"""Inbound go-live diagnostics (#950): authserv-id candidates and auth refusal codes.

Synthetic values only (invented domains under ``.test`` / ``.example``).
"""

from __future__ import annotations

import json
import logging

import pytest

from oncofiles import inbound_email

AUTHSERV = "mx.relay-synthetic.example"
SENDER = "carer.synthetic@mail-synthetic.test"
SUBJECT = "Synthetic subject 2027 do not log"


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    inbound_email.reset_state()
    monkeypatch.delenv(inbound_email.BREVO_API_KEY_ENV, raising=False)
    yield
    inbound_email.reset_state()


def ar(authserv: str = AUTHSERV, dmarc: str = "pass", domain: str = "mail-synthetic.test") -> str:
    return f"{authserv}; spf=pass smtp.mailfrom={domain}; dmarc={dmarc} header.from={domain}"


def item(headers, sender: str = SENDER) -> dict:
    return {
        "MessageId": "<synthetic-950-diag@example.test>",
        "From": {"Name": "Synthetic Carer", "Address": sender},
        "To": [{"Name": None, "Address": f"x-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}"}],
        "Recipients": [],
        "Subject": SUBJECT,
        "Headers": headers,
        "Attachments": [],
    }


def overview() -> dict:
    return inbound_email.unmatched_overview()


_BUCKET_OF = dict(zip(("one", "two", "more"), inbound_email.LINES_BUCKETS, strict=True))


def hist(**counts) -> dict:
    """A lines-per-item histogram, e.g. ``hist(two=n)``; unnamed buckets are zero."""
    out = dict.fromkeys(inbound_email.LINES_BUCKETS, 0)
    for name, n in counts.items():
        out[_BUCKET_OF[name]] = n
    return out


# ── authserv-id candidates ───────────────────────────────────────────────


def test_candidates_collected_hostname_only(caplog):
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    payload = {
        "items": [
            item({"Authentication-Results": [ar(), ar("other.example")]}),
            item({"Authentication-Results": ar("MX.Relay-Synthetic.Example")}),
            item({"Authentication-Results": ["bad host!; dmarc=pass", "; dmarc=pass"]}),
            item({"Subject": SUBJECT}),  # no A-R header at all
        ]
    }
    inbound_email.parse_brevo_payload(payload)
    cand = overview()["authserv_candidates"]
    hosts = {h["host"]: h for h in cand["hosts"]}
    assert set(hosts) == {AUTHSERV, "other.example"}
    assert hosts[AUTHSERV]["items"] == 2
    assert hosts[AUTHSERV]["lines_per_item"] == hist(one=2)
    assert hosts["other.example"]["items"] == 1
    assert set(hosts[AUTHSERV]) == {"host", "items", "lines_per_item", "last_at"}
    assert cand["hosts"][0]["host"] == AUTHSERV  # most seen first
    assert cand["invalid"] == 2
    assert cand["items_without_header"] == 1
    assert cand["items_seen"] == 4
    assert cand["evicted"] == 0
    # one INFO line per item, hostnames and counts only
    lines = [r.getMessage() for r in caplog.records if "authserv-id candidates" in r.getMessage()]
    assert lines == [
        f"inbound: authserv-id candidates: {AUTHSERV}(lines=1), other.example(lines=1)",
        f"inbound: authserv-id candidates: {AUTHSERV}(lines=1)",
        "inbound: authserv-id candidates: none; invalid=2",
        "inbound: authserv-id candidates: none (no Authentication-Results)",
    ]
    text = caplog.text + json.dumps(overview())
    content = ("dmarc=", "header.from", "spf=", "bad host", "smtp.mailfrom", "mail-synthetic")
    for secret in (SENDER, SUBJECT, *content):
        assert secret not in text


def test_two_lines_with_the_same_id_show_lines_2(caplog):
    """The provider's own line plus a planted copy: lines=2, histogram key "2"."""
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    payload = {
        "items": [
            item({"Authentication-Results": [ar(), ar(dmarc="fail"), ar("other.test")]}),
            item({"Authentication-Results": ar()}),
            item({"Authentication-Results": [ar(), ar(), ar()]}),
        ]
    }
    inbound_email.parse_brevo_payload(payload)
    lines = [r.getMessage() for r in caplog.records if "authserv-id candidates" in r.getMessage()]
    first, _, last = lines
    assert first == f"inbound: authserv-id candidates: {AUTHSERV}(lines=2), other.test(lines=1)"
    assert last == f"inbound: authserv-id candidates: {AUTHSERV}(lines=3)"
    hosts = {h["host"]: h for h in overview()["authserv_candidates"]["hosts"]}
    assert hosts[AUTHSERV]["items"] == 3
    trusted = hosts[AUTHSERV]["lines_per_item"]
    assert list(trusted) == list(inbound_email.LINES_BUCKETS)
    assert set(trusted.values()) == {1}  # one item each with one, two, three lines
    assert trusted["2"] == 1
    assert hosts["other.test"]["lines_per_item"] == hist(one=1)


def test_candidates_collected_whether_or_not_env_is_set(monkeypatch):
    monkeypatch.delenv(inbound_email.BREVO_AUTHSERV_ENV, raising=False)
    inbound_email.parse_brevo_payload({"items": [item({"Authentication-Results": ar()})]})
    monkeypatch.setenv(inbound_email.BREVO_AUTHSERV_ENV, AUTHSERV)
    inbound_email.parse_brevo_payload({"items": [item({"Authentication-Results": ar()})]})
    [entry] = overview()["authserv_candidates"]["hosts"]
    assert entry["host"] == AUTHSERV and entry["items"] == 2


def test_list_shape_headers_are_read():
    headers = [
        {"Name": "Authentication-Results", "Value": ar()},
        {"Name": "Authentication-Results", "Value": ar("second.example")},
    ]
    inbound_email.parse_brevo_payload({"items": [item(headers)]})
    hosts = {h["host"] for h in overview()["authserv_candidates"]["hosts"]}
    assert hosts == {AUTHSERV, "second.example"}


def test_same_host_twice_in_one_item_counts_one_item():
    inbound_email.parse_brevo_payload({"items": [item({"Authentication-Results": [ar(), ar()]})]})
    [entry] = overview()["authserv_candidates"]["hosts"]
    assert entry["items"] == 1
    assert entry["lines_per_item"]["2"] == 1


def test_hostname_regex_bounds():
    long_ok = "a" * 253
    too_long = "a" * 254
    lines = [f"{long_ok}; x", f"{too_long}; x", "host_underscore.example; x", "ex ample; x"]
    inbound_email.parse_brevo_payload({"items": [item({"Authentication-Results": lines})]})
    cand = overview()["authserv_candidates"]
    assert [h["host"] for h in cand["hosts"]] == [long_ok]
    assert cand["invalid"] == 3


def test_candidates_capped_at_twenty_and_frequent_host_survives():
    # the real id on many mails first
    for _ in range(3):
        inbound_email.parse_brevo_payload({"items": [item({"Authentication-Results": ar()})]})
    # then a flood of distinct planted one-off values
    planted = [ar(f"planted{i}.example") for i in range(40)]
    inbound_email.parse_brevo_payload({"items": [item({"Authentication-Results": planted})]})
    cand = overview()["authserv_candidates"]
    assert len(cand["hosts"]) == inbound_email.AUTHSERV_CANDIDATES_MAX == 20
    assert cand["hosts"][0]["host"] == AUTHSERV
    assert cand["hosts"][0]["items"] == 3
    assert cand["evicted"] == 40 - 19


def test_no_payload_items_no_log(caplog):
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    inbound_email.parse_brevo_payload({"items": []})
    assert "authserv-id candidates" not in caplog.text
    assert overview()["authserv_candidates"]["items_seen"] == 0


# ── auth refusal reason codes ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("env", "headers", "sender", "reason"),
    [
        (None, {"Authentication-Results": ar()}, SENDER, "authserv_unset"),
        (
            AUTHSERV,
            {"Authentication-Results": ar(), "From": [SENDER, SENDER]},
            SENDER,
            "single_header_repeated",
        ),
        (
            AUTHSERV,
            {"Authentication-Results": ar(), "Return-Path": ["<a@x.test>", "<b@x.test>"]},
            SENDER,
            "single_header_repeated",
        ),
        (
            AUTHSERV,
            {"Authentication-Results": ar(), "From": "Other <someone@elsewhere.test>"},
            SENDER,
            "from_header_mismatch",
        ),
        (AUTHSERV, {"Authentication-Results": ar("other.example")}, SENDER, "no_trusted_line"),
        (AUTHSERV, {}, SENDER, "no_trusted_line"),
        # a forged lower line carrying the trusted id next to the real one
        (
            AUTHSERV,
            {"Authentication-Results": [ar(), ar()]},
            SENDER,
            "multiple_trusted_lines",
        ),
        (AUTHSERV, {"Authentication-Results": ar(dmarc="fail")}, SENDER, "dmarc_not_pass"),
        (
            AUTHSERV,
            {"Authentication-Results": f"{AUTHSERV}; spf=pass; dmarc=pass"},
            SENDER,
            "header_from_missing",
        ),
        (
            AUTHSERV,
            {"Authentication-Results": ar(domain="elsewhere.test")},
            SENDER,
            "header_from_mismatch",
        ),
    ],
)
def test_each_refusal_reason(monkeypatch, env, headers, sender, reason):
    if env is None:
        monkeypatch.delenv(inbound_email.BREVO_AUTHSERV_ENV, raising=False)
    else:
        monkeypatch.setenv(inbound_email.BREVO_AUTHSERV_ENV, env)
    parsed = inbound_email._brevo_headers(headers)
    assert inbound_email._brevo_auth_refusal(parsed, sender) == reason
    assert inbound_email.parse_brevo_item(item(headers, sender)).auth_pass is False
    refusals = overview()["auth_refusals"]
    assert refusals[reason] == 1
    assert sum(refusals.values()) == 1


def test_pass_records_no_refusal(monkeypatch):
    monkeypatch.setenv(inbound_email.BREVO_AUTHSERV_ENV, AUTHSERV)
    headers = {"Authentication-Results": ar(), "From": f"Synthetic <{SENDER}>"}
    assert inbound_email._brevo_auth_refusal(inbound_email._brevo_headers(headers), SENDER) is None
    assert inbound_email.parse_brevo_item(item(headers)).auth_pass is True
    assert set(overview()["auth_refusals"].values()) == {0}


def test_auth_refusals_lists_every_code():
    assert list(overview()["auth_refusals"]) == list(inbound_email.AUTH_REFUSAL_REASONS)
    assert len(inbound_email.AUTH_REFUSAL_REASONS) == 8


def test_bool_contract_unchanged(monkeypatch):
    """``_brevo_auth_pass`` stays a plain bool of the same verdict."""
    monkeypatch.setenv(inbound_email.BREVO_AUTHSERV_ENV, AUTHSERV)
    ok = inbound_email._brevo_auth_pass(
        inbound_email._brevo_headers({"Authentication-Results": ar()}), SENDER
    )
    bad = inbound_email._brevo_auth_pass({}, SENDER)
    assert ok is True and bad is False
