# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""Inbound go-live diagnostics (#950): field names of the Brevo payload, no values.

Synthetic values only (invented domains under ``.test`` / ``.example``).
"""

from __future__ import annotations

import json
import logging

import pytest

from oncofiles import inbound_email
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures
    _env,
    _request,
    address_of,
    brevo,
    brevo_item,
    brevo_request,
    drive,
    idb,
)

SENDER = "carer.synthetic@mail-synthetic.test"
SECRET_VALUES = (
    "Synthetic subject 2027 do not log",
    "v=1; a=rsa-sha256; d=mail-synthetic.test; b=SYNTHETICSIG2027",
    "i=1; a=rsa-sha256; cv=none; d=relay-synthetic.example",
    "pass (relay-synthetic.example: domain of mail-synthetic.test)",
    "4.2",
    "synthetic-uuid-2027-0001",
    SENDER,
)


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    inbound_email.reset_state()
    monkeypatch.delenv(inbound_email.BREVO_API_KEY_ENV, raising=False)
    yield
    inbound_email.reset_state()


def item(headers, **extra) -> dict:
    out = {
        "Uuid": ["synthetic-uuid-2027-0001"],
        "MessageId": "<synthetic-950-names@example.test>",
        "From": {"Name": "Synthetic Carer", "Address": SENDER},
        "To": [{"Name": None, "Address": f"x-{'2' * 10}@{inbound_email.INBOUND_DOMAIN}"}],
        "Subject": "Synthetic subject 2027 do not log",
        "Headers": headers,
        "Attachments": [],
    }
    out.update(extra)
    return out


OBJECT_HEADERS = {
    "DKIM-Signature": "v=1; a=rsa-sha256; d=mail-synthetic.test; b=SYNTHETICSIG2027",
    "ARC-Seal": "i=1; a=rsa-sha256; cv=none; d=relay-synthetic.example",
    "Received-SPF": "pass (relay-synthetic.example: domain of mail-synthetic.test)",
    "Subject": "Synthetic subject 2027 do not log",
    "X-Mailin-Client": "synthetic-client-2027",
}
LIST_HEADERS = [
    {"Name": "DKIM-Signature", "Value": OBJECT_HEADERS["DKIM-Signature"]},
    {"Name": "ARC-Seal", "Value": OBJECT_HEADERS["ARC-Seal"]},
    {"name": "Received-SPF", "value": OBJECT_HEADERS["Received-SPF"]},
    {"Subject": "Synthetic subject 2027 do not log"},
    {"Name": "X-Mailin-Client", "Value": "synthetic-client-2027"},
]


def overview() -> dict:
    return inbound_email.unmatched_overview()


def names(entries) -> dict:
    return {e["name"]: e["items"] for e in entries}


@pytest.mark.parametrize("headers", [OBJECT_HEADERS, LIST_HEADERS], ids=["object", "list"])
def test_header_names_collected_without_values_both_shapes(headers, caplog):
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    inbound_email.parse_brevo_payload({"items": [item(headers), item(headers)]})
    ov = overview()
    assert names(ov["header_names"]) == {
        "DKIM-Signature": 2,
        "ARC-Seal": 2,
        "Received-SPF": 2,
        "Subject": 2,
        "X-Mailin-Client": 2,
    }
    assert names(ov["item_keys"]) == {
        k: 2 for k in ("Uuid", "MessageId", "From", "To", "Subject", "Headers", "Attachments")
    }
    assert names(ov["payload_keys"]) == {"items": 1}
    assert ov["field_names_stats"]["items_seen"] == 2
    assert ov["field_names_stats"]["payloads_seen"] == 1
    lines = [r.getMessage() for r in caplog.records if "inbound: header names" in r.getMessage()]
    assert (
        lines
        == [
            "inbound: header names: DKIM-Signature, ARC-Seal, Received-SPF, Subject, "
            "X-Mailin-Client; item keys: Uuid, MessageId, From, To, Subject, Headers, Attachments"
        ]
        * 2
    )
    dumped = json.dumps(ov) + caplog.text
    for value in SECRET_VALUES:
        assert value not in dumped


def test_sorted_by_items():
    inbound_email.parse_brevo_payload(
        {"items": [item({"A-One": "x"}), item({"A-One": "x", "B-Two": "y"})]}
    )
    inbound_email.parse_brevo_payload({"items": [item({"B-Two": "y", "C-Three": "z"})]})
    ov = overview()
    assert ov["header_names"] == [
        {"name": "A-One", "items": 2},
        {"name": "B-Two", "items": 2},
        {"name": "C-Three", "items": 1},
    ]


def test_invalid_names_counted_never_kept(caplog):
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    headers = {
        "Good-Name": "1",
        "bad name with spaces": "2",
        "bad:colon": "3",
        "x" * 65: "4",
        "": "5",
        "Ünicode": "6",
    }
    inbound_email.parse_brevo_payload({"items": [item(headers, **{"bad key!": "v"})]})
    ov = overview()
    assert names(ov["header_names"]) == {"Good-Name": 1}
    assert "bad key!" not in names(ov["item_keys"])
    assert ov["field_names_stats"]["invalid_names"] == 6
    assert "bad name" not in caplog.text and "colon" not in caplog.text


def test_per_item_cap_and_truncation():
    headers = {f"X-Synthetic-{i}": "v" for i in range(80)}
    inbound_email.parse_brevo_payload({"items": [item(headers)]})
    ov = overview()
    assert len(ov["header_names"]) == inbound_email.FIELD_NAMES_PER_ITEM_MAX == 50
    assert ov["field_names_stats"]["truncated_names"] == 30


def test_duplicate_names_in_one_item_counted_once():
    headers = [{"Name": "Received", "Value": "a"}, {"Name": "Received", "Value": "b"}]
    inbound_email.parse_brevo_payload({"items": [item(headers)]})
    assert names(overview()["header_names"]) == {"Received": 1}


def test_distinct_cap_evicts_least_seen():
    for _ in range(3):
        inbound_email.parse_brevo_payload({"items": [item({"Common-Name": "v"})]})
    for batch in range(5):
        headers = {f"X-Oneoff-{batch}-{i}": "v" for i in range(50)}
        inbound_email.parse_brevo_payload({"items": [item(headers)]})
    ov = overview()
    assert len(ov["header_names"]) == inbound_email.FIELD_NAMES_MAX == 200
    assert ov["header_names"][0] == {"name": "Common-Name", "items": 3}
    assert ov["field_names_stats"]["evicted"] == 251 - 200


def test_auth_evidence_flags(caplog):
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    rich = item(
        {
            "ARC-Authentication-Results": "i=1; relay-synthetic.example; dmarc=pass",
            "arc-seal": "i=1",
            "ARC-Message-Signature": "i=1",
            "Received-SPF": "pass",
            "DKIM-Signature": "v=1",
            "Authentication-Results": "relay-synthetic.example; dmarc=pass",
            "X-Brevo-Synthetic": "1",
            "X-Sib-Id": "2",
            "x-mailin-client": "3",
        },
        SpamScore=4.2,
        SPFResult="pass",
        DkimSignatureValid=True,
        Dmarc="pass",
    )
    bare = item({"Subject": "Synthetic subject 2027 do not log"})
    inbound_email.parse_brevo_payload({"items": [rich, bare]})
    ev = overview()["auth_evidence"]
    assert set(ev) == set(inbound_email.AUTH_EVIDENCE_FLAGS)
    assert all(ev[f] == 1 for f in inbound_email.AUTH_EVIDENCE_FLAGS), ev
    lines = [r.getMessage() for r in caplog.records if "inbound: auth evidence" in r.getMessage()]
    assert lines[1] == "inbound: auth evidence: none"
    assert lines[0].startswith("inbound: auth evidence: arc_authentication_results, arc_seal")
    assert "4.2" not in caplog.text and "dmarc=pass" not in caplog.text


def test_auth_evidence_looks_beyond_the_name_cap():
    headers = {f"X-Filler-{i}": "v" for i in range(60)}
    headers["DKIM-Signature"] = "v=1"
    inbound_email.parse_brevo_payload({"items": [item(headers)]})
    ov = overview()
    assert "DKIM-Signature" not in names(ov["header_names"])
    assert ov["auth_evidence"]["dkim_signature"] == 1


def test_spam_substring_flags():
    flags = inbound_email._auth_evidence([], ["Spam"])
    assert flags["key_spam"] and not flags["key_spam_score"]
    flags = inbound_email._auth_evidence([], ["spamscore"])
    assert flags["key_spam"] and flags["key_spam_score"]


def test_no_items_nothing_collected(caplog):
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    inbound_email.parse_brevo_payload({"items": []})
    assert "header names" not in caplog.text
    assert overview()["field_names_stats"]["payloads_seen"] == 0


def test_non_dict_items_and_headers_tolerated():
    inbound_email.parse_brevo_payload(
        {"items": ["junk", item("not headers"), item([1, "x", None])]}
    )
    ov = overview()
    assert ov["field_names_stats"]["items_seen"] == 2
    assert ov["header_names"] == []


# ── through the Brevo route and the admin endpoint ───────────────────────


async def test_route_collects_after_token_check_and_api_has_no_values(
    idb, drive, brevo, monkeypatch, caplog
):
    from unittest.mock import MagicMock

    from starlette.exceptions import HTTPException

    from oncofiles import inbound_queue, server

    monkeypatch.setattr(inbound_queue, "kick", lambda db: None)
    caplog.set_level(logging.INFO, logger=inbound_email.logger.name)
    addr = await address_of(idb)
    payload = {"items": [brevo_item(addr, headers={"DKIM-Signature": SECRET_VALUES[1]})]}

    with pytest.raises(HTTPException):
        await server.inbound_brevo_webhook(brevo_request(idb, payload, "wrong"))
    assert overview()["field_names_stats"]["items_seen"] == 0  # nothing before the token

    resp = await server.inbound_brevo_webhook(brevo_request(idb, payload))
    assert resp.status_code == 202

    monkeypatch.setattr(server, "_require_admin", lambda request: None)
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": idb}
    req = _request(idb, b"", {})
    req.scope["app"] = app
    body = json.loads((await server.api_inbound_unmatched(req)).body)
    assert {"name": "DKIM-Signature", "items": 1} in body["header_names"]
    assert {"name": "SpamScore", "items": 1} in body["item_keys"]
    assert body["auth_evidence"]["dkim_signature"] == 1
    assert body["auth_evidence"]["authentication_results"] == 1
    assert body["auth_evidence"]["key_spam_score"] == 1
    dumped = json.dumps(body) + caplog.text
    for value in (SECRET_VALUES[1], "never stored", addr, "Synthetic Carer"):
        assert value not in dumped
