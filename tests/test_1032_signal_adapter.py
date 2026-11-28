"""Chat over Signal (#1032): bindings, the adapter and its locks.

Synthetic data only: invented numbers (+1000000000x), invented names and
addresses. The network guard stays on: the websocket test runs a real server
on 127.0.0.1, sends go through ``httpx.MockTransport`` or a fake client.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest

from oncofiles import data_rights
from oncofiles.channels import signal as sig
from oncofiles.channels import signal_bindings as sb
from oncofiles.chat import core as chat_core
from oncofiles.chat import prompts
from oncofiles.models import Patient

PHONE_A = "+10000000001"
PHONE_B = "+10000000002"
PHONE_X = "+10000000009"
# Signal account UUIDs (ACI), the real identity; invented
UUIDS = {
    PHONE_A: str(uuid.uuid5(uuid.NAMESPACE_URL, "sender-alpha")),
    PHONE_B: str(uuid.uuid5(uuid.NAMESPACE_URL, "sender-beta")),
    PHONE_X: str(uuid.uuid5(uuid.NAMESPACE_URL, "sender-stranger")),
}
ADDR = {phone: "uuid:" + aci for phone, aci in UUIDS.items()}
_KEEP = object()
CARE_A = "carer.alpha@example.test"
CARE_B = "carer.beta@example.test"
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _signal_env(monkeypatch):
    monkeypatch.setenv("SIGNAL_SECRET", "synthetic-signal-secret-for-tests")
    for name in (sig.ENABLED_ENV, sig.API_URL_ENV, sig.NUMBER_ENV):
        monkeypatch.delenv(name, raising=False)
    sig._counters.clear()


async def _patient(db, name: str, email: str) -> str:
    pid = str(uuid.uuid4())
    await db.insert_patient(
        Patient(patient_id=pid, slug=f"sig-{pid[:6]}", display_name=name, caregiver_email=email)
    )
    return pid


@pytest.fixture
async def family(db):
    """Caregiver A with two patients, caregiver B with one."""
    return {
        "first": await _patient(db, "Alpha One", CARE_A),
        "second": await _patient(db, "Alpha Two", CARE_A),
        "beta": await _patient(db, "Beta One", CARE_B),
    }


class FakeClient:
    """Fake signal-cli: records sends; ``identity`` answers from ``keys``
    (sender → Identity, None, or an exception to raise), else a stable
    trusted fingerprint per sender."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.keys: dict = {}
        self.identity_calls = 0

    async def send(self, recipient: str, text: str) -> bool:
        self.sent.append((recipient, text))
        return True

    async def identity(self, sender: str):
        self.identity_calls += 1
        if sender in self.keys:
            value = self.keys[sender]
            if isinstance(value, Exception):
                raise value
            return value
        return sig.Identity("fp " + sender, "TRUSTED_UNVERIFIED")


IDENT = {"identity_hash": "pinned-hash-for-tests", "identity_trust": "TRUSTED_UNVERIFIED"}


def _env(text: str, sender: str = PHONE_A, aci=_KEEP, **msg) -> dict:
    env = {"sourceNumber": sender, "dataMessage": {"message": text, **msg}}
    aci = UUIDS.get(sender) if aci is _KEEP else aci
    if aci is not None:
        env["sourceUuid"] = aci
    return {"envelope": env, "account": "+10000000000"}


@pytest.fixture
def fake_core(monkeypatch):
    calls: list[dict] = []
    reply = {"value": None}

    async def answer_text(db, *, patient_id, principal, messages, lang):
        calls.append(
            {"patient_id": patient_id, "principal": principal, "messages": messages, "lang": lang}
        )
        if reply["value"] is not None:
            return reply["value"]
        return {
            "text": "Answer for the patient.\n\n" + prompts.DISCLAIMER[lang],
            "citations": [],
            "statuses": [],
            "done": {"model": "local-test"},
            "error": None,
        }

    monkeypatch.setattr(chat_core, "answer_text", answer_text)
    stored: list[dict] = []

    async def no_history(db, pid, conv):
        return [m for m in stored if m["pid"] == pid and m["conv"] == conv]

    async def store(db, pid, conv, role, text, **kw):
        stored.append({"pid": pid, "conv": conv, "role": role, "content": text, **kw})

    monkeypatch.setattr(sig, "load_history", no_history)
    monkeypatch.setattr(sig, "store_turn", store)
    return {"calls": calls, "reply": reply, "stored": stored}


async def _bind(db, client, pid, email, phone=PHONE_A) -> str:
    code = (await sb.create_link_code(db, patient_id=pid, caregiver_email=email))["code"]
    return await sig.handle_envelope(db, client, _env(code, phone))


# ── link codes ────────────────────────────────────────────────────────────


async def test_code_shape_and_single_use(db, family):
    out = await sb.create_link_code(db, patient_id=family["first"], caregiver_email=CARE_A.upper())
    code = out["code"]
    assert len(code) == 6 and all(ch in sb.CODE_ALPHABET for ch in code)
    assert sb.looks_like_code(code)
    async with db.db.execute("SELECT code_hash FROM signal_link_codes") as cur:
        rows = [r[0] for r in await cur.fetchall()]
    assert code not in rows and len(rows) == 1  # only the keyed hash
    first = await sb.bind_from_message(db, ADDR[PHONE_A], code.lower(), **IDENT)
    assert first.status == "bound" and first.patient_id == family["first"]
    replay = await sb.bind_from_message(db, ADDR[PHONE_B], code, **IDENT)
    assert replay.status == "refused" and replay.reason == "used_code"


async def test_wrong_and_expired_codes(db, family):
    assert (
        await sb.bind_from_message(db, ADDR[PHONE_A], "ZZ9ZZ9", **IDENT)
    ).reason == "unknown_code"
    out = await sb.create_link_code(db, patient_id=family["first"], caregiver_email=CARE_A)
    past = (datetime.now(UTC) - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.db.execute("UPDATE signal_link_codes SET expires_at = ?", (past,))
    await db.db.commit()
    res = await sb.bind_from_message(db, ADDR[PHONE_A], out["code"], **IDENT)
    assert res.status == "refused" and res.reason == "expired_code"
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None


async def test_only_the_patients_caregiver_gets_a_code(db, family):
    with pytest.raises(sb.LinkCodeRefusedError) as exc:
        await sb.create_link_code(db, patient_id=family["beta"], caregiver_email=CARE_A)
    assert exc.value.code == "not_caregiver"
    with pytest.raises(sb.LinkCodeRefusedError) as exc:
        await sb.create_link_code(db, patient_id=str(uuid.uuid4()), caregiver_email=CARE_A)
    assert exc.value.code == "not_found"


async def test_open_codes_are_capped(db, family):
    for _ in range(sb.MAX_OPEN_CODES_PER_PATIENT):
        await sb.create_link_code(db, patient_id=family["first"], caregiver_email=CARE_A)
    with pytest.raises(sb.LinkCodeRefusedError) as exc:
        await sb.create_link_code(db, patient_id=family["first"], caregiver_email=CARE_A)
    assert exc.value.code == "too_many_codes"


async def test_no_key_no_code(db, family, monkeypatch):
    monkeypatch.delenv("SIGNAL_SECRET", raising=False)
    monkeypatch.delenv("HOLDING_STORE_SECRET", raising=False)
    with pytest.raises(sb.LinkCodeRefusedError) as exc:
        await sb.create_link_code(db, patient_id=family["first"], caregiver_email=CARE_A)
    assert exc.value.code == "not_configured"


async def test_number_never_stored_in_clear(db, family):
    client = FakeClient()
    assert await _bind(db, client, family["first"], CARE_A) == "bound"
    async with db.db.execute("SELECT * FROM signal_bindings") as cur:
        row = dict(await cur.fetchone())
    for value in row.values():
        if isinstance(value, str):
            assert PHONE_A not in value and PHONE_A[1:] not in value
        if isinstance(value, bytes):
            assert PHONE_A.encode() not in value
        if isinstance(value, str):
            assert UUIDS[PHONE_A] not in value
    assert row["sender_uuid_hash"] and row["phone_hash"]
    assert sb.open_address(row["reply_enc"], row["patient_id"], row["row_uuid"]) == ADDR[PHONE_A]
    # the ciphertext is bound to its row
    assert sb.open_address(row["reply_enc"], family["second"], row["row_uuid"]) is None


async def test_revoke(db, family):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) == family["first"]
    assert await sb.revoke_binding(db, patient_id=family["first"]) == 1
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None
    listed = await sb.list_bindings(db, patient_id=family["first"])
    assert listed[0]["active"] is False and listed[0]["revoked_at"]
    assert "phone" not in json.dumps(listed)


async def test_binding_ends_when_the_caregiver_changes(db, family):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    await db.db.execute(
        "UPDATE patients SET caregiver_email = ? WHERE patient_id = ?", (CARE_B, family["first"])
    )
    await db.db.commit()
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None


# ── unknown sender ────────────────────────────────────────────────────────


async def test_unknown_sender_gets_one_neutral_reply_a_day(db, family, fake_core):
    client = FakeClient()
    assert await sig.handle_envelope(db, client, _env("Ako sa mám?", PHONE_X)) == "neutral_reply"
    assert await sig.handle_envelope(db, client, _env("Ešte raz?", PHONE_X)) == "unknown_sender"
    assert client.sent == [(ADDR[PHONE_X], sig.TEXTS["neutral"]["sk"])]
    assert fake_core["calls"] == []
    # a day later: one more
    old = (datetime.now(UTC) - timedelta(days=1, seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.db.execute("UPDATE signal_sender_limits SET neutral_reply_at = ?", (old,))
    await db.db.commit()
    assert await sig.handle_envelope(db, client, _env("Ahoj", PHONE_X)) == "neutral_reply"


async def test_wrong_code_gets_a_neutral_line_and_attempts_are_capped(db, family, fake_core):
    client = FakeClient()
    for _ in range(sb.MAX_FAILED_CODES_PER_HOUR):
        assert await sig.handle_envelope(db, client, _env("ZZ9ZZ9", PHONE_X)) == "code_refused"
    assert all(t == sig.TEXTS["code_invalid"]["sk"] for _, t in client.sent)
    out = await sig.handle_envelope(db, client, _env("ZZ9ZZ8", PHONE_X))
    assert out == "code_attempts_exhausted"
    assert len(client.sent) == sb.MAX_FAILED_CODES_PER_HOUR


async def test_group_and_receipt_messages_are_ignored(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    group = _env("Hello group", groupInfo={"groupId": "x"})
    assert await sig.handle_envelope(db, client, group) == "ignored"
    receipt = {"envelope": {"sourceNumber": PHONE_A, "receiptMessage": {"isRead": True}}}
    assert await sig.handle_envelope(db, client, receipt) == "ignored"
    assert client.sent == [] and fake_core["calls"] == []


# ── bound sender ──────────────────────────────────────────────────────────


async def test_bound_sender_gets_an_answer_for_the_bound_patient(db, family, fake_core):
    client = FakeClient()
    assert await _bind(db, client, family["first"], CARE_A) == "bound"
    assert "Alpha One" in client.sent[-1][1]
    client.sent.clear()
    out = await sig.handle_envelope(db, client, _env("Aký bol posledný hemoglobín?"))
    assert out == "answered:1"
    call = fake_core["calls"][-1]
    assert call["patient_id"] == family["first"]
    assert call["lang"] == "sk"
    assert call["principal"].startswith("signal:") and PHONE_A[1:] not in call["principal"]
    assert call["messages"][-1] == {"role": "user", "content": "Aký bol posledný hemoglobín?"}
    assert client.sent[0][0] == ADDR[PHONE_A]
    assert prompts.DISCLAIMER["sk"] in client.sent[0][1]
    roles = [m["role"] for m in fake_core["stored"]]
    assert roles == ["user", "assistant"]


async def test_follow_up_carries_recent_turns_and_english(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    await sig.handle_envelope(db, client, _env("What was the last result please?"))
    assert fake_core["calls"][-1]["lang"] == "en"
    await sig.handle_envelope(db, client, _env("And the one before?"))
    msgs = fake_core["calls"][-1]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


async def test_busy_gives_a_short_retry_line_and_stores_nothing(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    fake_core["reply"]["value"] = {
        "text": "",
        "citations": [],
        "statuses": [],
        "done": None,
        "error": {"code": "model_busy", "message": "x"},
    }
    out = await sig.handle_envelope(db, client, _env("Otázka?"))
    assert out == "error:model_busy"
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["busy"]["sk"])]
    assert fake_core["stored"] == []


async def test_attachments_are_not_supported(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    out = await sig.handle_envelope(db, client, _env("", attachments=[{"id": "a"}]))
    assert out == "attachments"
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["attachments"]["sk"])]
    assert fake_core["calls"] == []


async def test_rate_limit_is_persisted(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)  # one message used
    for _ in range(sb.MAX_MESSAGES_PER_HOUR - 1):
        assert (await sig.handle_envelope(db, client, _env("Otázka?"))).startswith("answered")
    client.sent.clear()
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "rate_limited_notice"
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "rate_limited"
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["too_many"]["sk"])]
    async with db.db.execute("SELECT window_messages FROM signal_sender_limits") as cur:
        assert (await cur.fetchone())[0] == sb.MAX_MESSAGES_PER_HOUR + 2


# ── cross-patient ─────────────────────────────────────────────────────────


async def test_naming_another_patient_never_changes_the_patient(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    for text in ("Aké má výsledky Beta One?", f"patient_id {family['beta']}", "pacient 7"):
        await sig.handle_envelope(db, client, _env(text))
    assert {c["patient_id"] for c in fake_core["calls"]} == {family["first"]}
    assert all("Beta One" not in t for _, t in client.sent)


async def test_another_caregivers_code_is_refused_after_binding(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    code_b = (await sb.create_link_code(db, patient_id=family["beta"], caregiver_email=CARE_B))[
        "code"
    ]
    assert await sig.handle_envelope(db, client, _env(code_b)) == "code_refused"
    rows = await sb.bound_patients(db, ADDR[PHONE_A])
    assert [r["patient_id"] for r in rows] == [family["first"]]
    # the refusal did not burn B's code: B's own phone can still use it
    assert await sig.handle_envelope(db, client, _env(code_b, PHONE_B)) == "bound"
    await sig.handle_envelope(db, client, _env("Otázka?"))
    assert fake_core["calls"][-1]["patient_id"] == family["first"]


async def test_switch_lists_only_the_caregivers_patients(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    await _bind(db, client, family["second"], CARE_A)
    await _bind(db, client, family["beta"], CARE_B, phone=PHONE_B)
    client.sent.clear()
    assert await sig.handle_envelope(db, client, _env("Pacient")) == "patients_listed"
    listing = client.sent[-1][1]
    assert "Alpha One" in listing and "Alpha Two" in listing and "Beta One" not in listing
    assert "2. Alpha Two (aktívny)" in listing  # the newest binding is active
    assert await sig.handle_envelope(db, client, _env("pacient 1")) == "switched"
    await sig.handle_envelope(db, client, _env("Otázka?"))
    assert fake_core["calls"][-1]["patient_id"] == family["first"]
    assert await sig.handle_envelope(db, client, _env("patient 3")) == "patients_listed"
    assert sig.TEXTS["no_such_patient"]["en"] in client.sent[-1][1] or (
        sig.TEXTS["no_such_patient"]["sk"] in client.sent[-1][1]
    )


# ── formatting ────────────────────────────────────────────────────────────


def test_chunking_on_paragraphs_keeps_the_disclaimer():
    disclaimer = prompts.DISCLAIMER["sk"]
    paras = [f"Odsek {i}. " + "slovo " * 100 for i in range(10)]
    parts = sig.chunk_text("\n\n".join(paras) + "\n\n" + disclaimer, "sk")
    assert len(parts) == sig.MAX_PARTS
    assert all(len(p) <= sig.CHUNK_CHARS for p in parts)
    assert parts[-1].endswith(disclaimer)
    short = sig.chunk_text("Krátko.\n\n" + disclaimer, "sk")
    assert short == ["Krátko.\n\n" + disclaimer]


async def test_sources_as_short_lines(db, family):
    from oncofiles.models import Document

    doc = await db.insert_document(
        Document(
            file_id="sig-test-1",
            filename="20270115_AlphaOne_Clinic_Labs_BloodCount.pdf",
            original_filename="x.pdf",
            document_date=None,
            mime_type="application/pdf",
            size_bytes=10,
        ),
        patient_id=family["first"],
    )
    result = {
        "text": "Hodnota bola 12.\n\n" + prompts.DISCLAIMER["sk"],
        "citations": [
            {"document_id": doc.id, "page": 2, "label": "HGB", "date": "2027-01-15"},
            {"document_id": 999999, "page": None, "label": "X", "date": "2027-01-16"},
        ],
    }
    parts = await sig.format_answer(db, family["first"], result, "sk")
    text = parts[0]
    assert "Zdroje:" in text
    assert "• 20270115_AlphaOne_Clinic_Labs_BloodCount, 2027-01-15, s. 2" in text
    assert "• dokument #999999, 2027-01-16" in text
    assert text.endswith(prompts.DISCLAIMER["sk"])
    # a document of another patient is never named
    other = await sig.source_lines(db, family["beta"], result["citations"][:1], "sk")
    assert "AlphaOne" not in other[0]


def test_lang_and_switch_parsing():
    assert sig.detect_lang("Aký bol hemoglobín?") == "sk"
    assert sig.detect_lang("What is my latest hemoglobin?") == "en"
    assert sig.detect_lang("hemoglobin") == "sk"
    assert sig.parse_switch("Pacient 2") == (True, 2)
    assert sig.parse_switch("patient") == (True, None)
    assert sig.parse_switch("pacient Novak") == (False, None)
    assert not sb.looks_like_code("Ahojte")  # a plain word is no code


# ── logs ──────────────────────────────────────────────────────────────────


async def test_logs_carry_no_text_names_or_numbers(db, family, fake_core, caplog):
    caplog.set_level(logging.DEBUG)
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    await sig.handle_envelope(db, client, _env("Aký bol posledný hemoglobín?"))
    await sig.handle_envelope(db, client, _env("ZZ9ZZ9", PHONE_X))
    await sig.handle_envelope(db, client, _env("Ahoj", PHONE_X))
    text = caplog.text
    bare_a, bare_x = PHONE_A.lstrip("+"), PHONE_X.lstrip("+")
    wrong_code = "ZZ9ZZ9"
    secrets_ = (PHONE_A, bare_a, bare_x, UUIDS[PHONE_A], "hemoglobín", "Alpha One", CARE_A)
    for secret in (*secrets_, wrong_code):
        assert secret not in text


# ── settings, host rule, loop ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://signal-cli:8080",
        "http://10.1.2.3",
        "http://host.docker.internal:8080",
        "https://signal.internal",
    ],
)
def test_private_api_urls_pass(url):
    assert sig.api_url_refusal(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://signal.example.com",
        "http://8.8.8.8",
        "ftp://127.0.0.1",
        "http://user:pw@127.0.0.1",
        "",
        "http://[::ffff:1.1.1.1]",
    ],
)
def test_public_api_urls_are_refused(url):
    assert sig.api_url_refusal(url) is not None
    with pytest.raises(ValueError):
        sig.SignalClient(url, "+10000000000")


def test_off_by_default_and_names_what_is_missing(monkeypatch):
    assert sig.start_reason() is not None
    monkeypatch.setenv(sig.ENABLED_ENV, "true")
    reason = sig.start_reason()
    assert sig.API_URL_ENV in reason and sig.NUMBER_ENV in reason
    monkeypatch.setenv(sig.API_URL_ENV, "https://signal.example.com")
    monkeypatch.setenv(sig.NUMBER_ENV, "+10000000000")
    assert "not a private host" in sig.start_reason()
    monkeypatch.setenv(sig.API_URL_ENV, "http://signal-cli:8080")
    assert sig.start_reason() is None
    monkeypatch.delenv("SIGNAL_SECRET")
    monkeypatch.delenv("HOLDING_STORE_SECRET", raising=False)
    assert "SIGNAL_SECRET" in sig.start_reason()
    assert "+10000000000" not in sig.start_reason()


def test_lifespan_starts_the_loop_only_when_configured():
    src = (ROOT / "src/oncofiles/server.py").read_text()
    block = src[src.index("_signal_off = signal_channel.start_reason()") :][:700]
    assert "if _signal_off is None:" in block
    assert block.index("if _signal_off is None:") < block.index("ensure_background_loop(")
    assert 'local_only_task("signal_adapter", signal_channel.signal_loop)' in block
    assert sig.JOB_ID == "signal_adapter"


async def test_loop_receives_handles_and_survives_errors(db, family, fake_core):
    class LoopClient(FakeClient):
        def __init__(self, batches):
            super().__init__()
            self.batches = batches

        async def receive(self):
            batch = self.batches.pop(0) if self.batches else None
            if batch is None:
                await asyncio.sleep(3600)
                return
            if batch == "boom":
                raise ConnectionError("down")
            for item in batch:
                yield item

    client = LoopClient(["boom", [{"not": "an envelope"}, _env("Ahoj", PHONE_X)]])
    old = sig.RECONNECT_MIN_S
    sig.RECONNECT_MIN_S = 0.01
    try:
        task = asyncio.create_task(sig.signal_loop(db, client, grace_s=0))
        for _ in range(200):
            if client.sent:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        sig.RECONNECT_MIN_S = old
    assert client.sent == [(ADDR[PHONE_X], sig.TEXTS["neutral"]["sk"])]
    counts = sig.counters()
    assert counts["receive_failed"] == 1 and counts["ignored"] == 1
    assert counts["neutral_reply"] == 1


async def test_client_on_loopback_websocket_and_send():
    from websockets.asyncio.server import serve

    paths = []

    async def handler(ws):
        paths.append(ws.request.path)
        await ws.send("not json")
        await ws.send(json.dumps(_env("Ahoj", PHONE_X)))
        await ws.close()

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        posted = []

        def respond(request: httpx.Request) -> httpx.Response:
            posted.append((request.url.path, json.loads(request.content)))
            return httpx.Response(201, json={"timestamp": "1"})

        client = sig.SignalClient(
            f"http://127.0.0.1:{port}", "+10000000000", transport=httpx.MockTransport(respond)
        )
        got = [m async for m in client.receive()]
        assert await client.send(PHONE_X, "hello") is True
        hidden = str(uuid.UUID(int=5))
        assert await client.send("uuid:" + hidden, "hi") is True
    assert paths == ["/v1/receive/%2B10000000000"]
    assert len(got) == 1 and got[0]["envelope"]["sourceNumber"] == PHONE_X
    assert posted[0] == (
        "/v2/send",
        {"message": "hello", "number": "+10000000000", "recipients": [PHONE_X]},
    )
    second_body = posted[-1][1]
    assert second_body["recipients"] == [hidden]


async def test_send_failure_is_reported_not_raised():
    def refuse(request):
        return httpx.Response(500)

    client = sig.SignalClient(
        "http://127.0.0.1:1", "+10000000000", transport=httpx.MockTransport(refuse)
    )
    assert await client.send(PHONE_X, "x") is False


# ── registry, schema ──────────────────────────────────────────────────────


def test_registry_classification():
    bindings = data_rights.rule_for("signal_bindings")
    codes = data_rights.rule_for("signal_link_codes")
    assert bindings.export and not codes.export
    cols = [
        "id", "patient_id", "caregiver_email_hash", "sender_uuid_hash", "phone_hash", "row_uuid",
        "reply_enc",
        "conversation_uuid", "conversation_at", "selected_at", "active", "created_at",
        "revoked_at",
    ]  # fmt: skip
    exported = data_rights.exported_columns(bindings, cols)
    hidden_cols = {"caregiver_email_hash", "sender_uuid_hash", "phone_hash", "reply_enc"}
    assert not hidden_cols & set(exported)
    assert "signal_sender_limits" in data_rights.NOT_PATIENT_DATA
    names = [r.table for r in data_rights.REGISTRY]
    assert names.index("signal_bindings") < names.index("patients")


async def test_migration_creates_the_tables(db):
    async with db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'signal_%'"
    ) as cur:
        names = {r[0] for r in await cur.fetchall()}
    assert names == {"signal_bindings", "signal_link_codes", "signal_sender_limits"}
    sql = (ROOT / "migrations/108_signal_bindings.sql").read_text()
    assert "DELETE" not in sql.upper().replace("DELETED", "")


def test_no_delete_in_the_signal_code():
    for name in ("signal.py", "signal_bindings.py"):
        src = (ROOT / "src/oncofiles/channels" / name).read_text()
        assert not re.search(r"DELETE\s+FROM", src, re.I)


# ── identity: the account UUID, never the number (security review) ───────


async def test_recycled_number_with_another_uuid_gets_nothing(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    newcomer = str(uuid.uuid5(uuid.NAMESPACE_URL, "next-owner-of-the-number"))
    out = await sig.handle_envelope(db, client, _env("Aké sú výsledky?", PHONE_A, aci=newcomer))
    assert out == "neutral_reply"
    assert fake_core["calls"] == []
    assert client.sent == [("uuid:" + newcomer, sig.TEXTS["neutral"]["sk"])]
    # the real owner (same number, original UUID) still works
    assert (await sig.handle_envelope(db, client, _env("Otázka?"))).startswith("answered")
    assert fake_core["calls"][-1]["patient_id"] == family["first"]


async def test_message_without_uuid_is_ignored(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    assert await sig.handle_envelope(db, client, _env("Otázka?", aci=None)) == "no_uuid"
    code = (await sb.create_link_code(db, patient_id=family["second"], caregiver_email=CARE_A))[
        "code"
    ]
    assert await sig.handle_envelope(db, client, _env(code, PHONE_X, aci=None)) == "no_uuid"
    assert client.sent == [] and fake_core["calls"] == []
    async with db.db.execute("SELECT COUNT(*) FROM signal_sender_limits") as cur:
        assert (await cur.fetchone())[0] == 1  # only the bound sender's own row


async def test_identity_change_suspends_the_binding(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    await _bind(db, client, family["second"], CARE_A)
    client.sent.clear()
    event = {
        "envelope": {"sourceNumber": PHONE_A, "sourceUuid": UUIDS[PHONE_A]},
        "exception": {"type": "UntrustedIdentityException", "message": "Untrusted identity"},
    }
    assert await sig.handle_envelope(db, client, event) == "identity_suspended"
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["reconnect"]["sk"])]
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None
    # a second report sends nothing more
    assert await sig.handle_envelope(db, client, event) == "identity_change"
    # no answers until a new code
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "neutral_reply"
    assert fake_core["calls"] == []
    listed = await sb.list_bindings(db, patient_id=family["first"])
    assert listed[0]["suspended_at"] and not listed[0]["active"]
    assert await _bind(db, client, family["first"], CARE_A) == "bound"
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) == family["first"]


def test_identity_change_flag_forms():
    base = {"sourceUuid": UUIDS[PHONE_A]}
    flagged = {"envelope": {**base, "untrustedIdentity": True}}
    assert sig.parse_envelope(flagged).kind == "identity_change"
    err = {"envelope": base, "error": "org.example.ProtocolUntrustedIdentityException"}
    assert sig.parse_envelope(err).kind == "identity_change"
    assert sig.parse_envelope({"envelope": base}).kind == "ignored"


async def test_idle_binding_expires(db, family, fake_core, monkeypatch):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    monkeypatch.setenv(sb.IDLE_DAYS_ENV, "30")
    old = (datetime.now(UTC) - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.db.execute("UPDATE signal_bindings SET last_seen_at = ?, created_at = ?", (old, old))
    await db.db.commit()
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None
    listed = await sb.list_bindings(db, patient_id=family["first"])
    assert listed[0]["expired_at"] and not listed[0]["active"]
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "neutral_reply"
    assert fake_core["calls"] == []


async def test_recent_use_keeps_a_binding_alive(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    days = sb.DEFAULT_IDLE_DAYS - 1
    old = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await db.db.execute("UPDATE signal_bindings SET last_seen_at = ?", (old,))
    await db.db.commit()
    assert (await sig.handle_envelope(db, client, _env("Otázka?"))).startswith("answered")
    async with db.db.execute("SELECT last_seen_at FROM signal_bindings") as cur:
        assert (await cur.fetchone())[0] > old


def test_idle_days_setting(monkeypatch):
    assert sb.idle_days() == sb.DEFAULT_IDLE_DAYS
    monkeypatch.setenv(sb.IDLE_DAYS_ENV, "7")
    assert sb.idle_days() == 7
    monkeypatch.setenv(sb.IDLE_DAYS_ENV, "nonsense")
    assert sb.idle_days() == sb.DEFAULT_IDLE_DAYS


# ── identity key pinned at bind time, checked before every answer ────────


@pytest.fixture(autouse=True)
def _clear_identity_cache():
    sig._identity_ok.clear()
    yield
    sig._identity_ok.clear()


async def test_same_fingerprint_answers(db, family, fake_core):
    client = FakeClient()
    assert await _bind(db, client, family["first"], CARE_A) == "bound"
    async with db.db.execute("SELECT identity_hash, identity_trust FROM signal_bindings") as cur:
        pinned, trust = await cur.fetchone()
    assert pinned == sb.identity_hash("fp " + ADDR[PHONE_A]) and trust == "TRUSTED_UNVERIFIED"
    assert "fp " not in pinned  # only the keyed hash is stored
    assert (await sig.handle_envelope(db, client, _env("Otázka?"))).startswith("answered")


async def test_changed_fingerprint_suspends_and_answers_nothing(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    client.keys[ADDR[PHONE_A]] = sig.Identity("a brand new key", "TRUSTED_UNVERIFIED")
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "identity_suspended"
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["reconnect"]["sk"])]
    assert fake_core["calls"] == []
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None
    # a new code reconnects with the new key pinned
    assert await _bind(db, client, family["first"], CARE_A) == "bound"
    assert (await sig.handle_envelope(db, client, _env("Otázka?"))).startswith("answered")


async def test_untrusted_answers_nothing_and_does_not_suspend(db, family, fake_core):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    client.keys[ADDR[PHONE_A]] = sig.Identity("fp " + ADDR[PHONE_A], "UNTRUSTED")
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "identity_unconfirmed"
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["unavailable"]["sk"])]
    assert fake_core["calls"] == []
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) == family["first"]


@pytest.mark.parametrize("failure", [None, TimeoutError("slow"), RuntimeError("odd")])
async def test_failed_identity_check_answers_nothing(db, family, fake_core, failure):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    client.sent.clear()
    client.keys[ADDR[PHONE_A]] = failure
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "identity_unconfirmed"
    assert await sig.handle_envelope(db, client, _env("Pacient")) == "identity_unconfirmed"
    # the "temporarily unavailable" line at most once an hour
    assert client.sent == [(ADDR[PHONE_A], sig.TEXTS["unavailable"]["sk"])]
    assert fake_core["calls"] == []
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) == family["first"]  # not suspended


async def test_no_confirmed_identity_no_binding(db, family, fake_core):
    client = FakeClient()
    client.keys[ADDR[PHONE_A]] = None
    code = (await sb.create_link_code(db, patient_id=family["first"], caregiver_email=CARE_A))[
        "code"
    ]
    assert await sig.handle_envelope(db, client, _env(code)) == "identity_unconfirmed"
    assert await sb.binding_for_sender(db, ADDR[PHONE_A]) is None
    client.keys[ADDR[PHONE_A]] = sig.Identity("some key", "UNTRUSTED")
    assert await sig.handle_envelope(db, client, _env(code)) == "identity_unconfirmed"
    del client.keys[ADDR[PHONE_A]]
    assert await sig.handle_envelope(db, client, _env(code)) == "bound"  # code not consumed


async def test_confirmed_check_is_cached_then_expires(db, family, fake_core, monkeypatch):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    calls_after_bind = client.identity_calls
    await sig.handle_envelope(db, client, _env("Otázka?"))
    await sig.handle_envelope(db, client, _env("Ďalšia otázka?"))
    assert client.identity_calls == calls_after_bind + 1  # second answer from the cache
    clock = {"now": sig.time.monotonic() + sig.IDENTITY_CACHE_S + 1}
    monkeypatch.setattr(sig.time, "monotonic", lambda: clock["now"])
    await sig.handle_envelope(db, client, _env("Tretia otázka?"))
    assert client.identity_calls == calls_after_bind + 2


async def test_cache_never_hides_a_change_after_expiry(db, family, fake_core, monkeypatch):
    client = FakeClient()
    await _bind(db, client, family["first"], CARE_A)
    await sig.handle_envelope(db, client, _env("Otázka?"))
    client.keys[ADDR[PHONE_A]] = sig.Identity("another key", "TRUSTED_UNVERIFIED")
    clock = {"now": sig.time.monotonic() + sig.IDENTITY_CACHE_S + 1}
    monkeypatch.setattr(sig.time, "monotonic", lambda: clock["now"])
    assert await sig.handle_envelope(db, client, _env("Otázka?")) == "identity_suspended"


def test_identity_parsing_is_strict():
    me = UUIDS[PHONE_A]
    good = {"uuid": me, "fingerprint": "ab cd", "status": "TRUSTED_VERIFIED"}
    assert sig.identity_from([good], ADDR[PHONE_A]) == sig.Identity("ab cd", "TRUSTED_VERIFIED")
    other = {**good, "uuid": UUIDS[PHONE_B]}
    assert sig.identity_from([other], ADDR[PHONE_A]) is None  # not this sender
    assert sig.identity_from([good, good], ADDR[PHONE_A]) is None  # ambiguous
    assert sig.identity_from({"identities": [good]}, ADDR[PHONE_A]) is None  # odd shape
    assert sig.identity_from([{**good, "status": "MAYBE"}], ADDR[PHONE_A]) is None
    assert sig.identity_from([{**good, "fingerprint": ""}], ADDR[PHONE_A]) is None
    safety = {"uuid": me, "safety_number": "x y", "status": "UNTRUSTED"}
    assert sig.identity_from([safety], ADDR[PHONE_A]) == sig.Identity("x y", "UNTRUSTED")


async def test_client_identity_over_http():
    me = UUIDS[PHONE_A]
    answers = []

    def respond(request: httpx.Request) -> httpx.Response:
        answers.append(request.url.raw_path.decode())
        mode = answers_mode["value"]
        if mode == "ok":
            body = [{"uuid": me, "fingerprint": "ab", "status": "TRUSTED_UNVERIFIED"}]
            return httpx.Response(200, json=body)
        if mode == "error":
            return httpx.Response(500, json={"error": "x"})
        if mode == "odd":
            return httpx.Response(200, text="not json")
        raise httpx.ReadTimeout("slow", request=request)

    answers_mode = {"value": "ok"}
    client = sig.SignalClient(
        "http://127.0.0.1:1", "+10000000000", transport=httpx.MockTransport(respond)
    )
    assert await client.identity(ADDR[PHONE_A]) == sig.Identity("ab", "TRUSTED_UNVERIFIED")
    for mode in ("error", "odd", "timeout"):
        answers_mode["value"] = mode
        assert await client.identity(ADDR[PHONE_A]) is None
    bot_number = quote("+10000000000", safe="")
    assert answers[0] == "/v1/identities/" + bot_number


def test_probe_summary_has_no_values():
    me = UUIDS[PHONE_A]
    data = [{"uuid": me, "fingerprint": "ab cd", "status": "TRUSTED_UNVERIFIED", "number": PHONE_A}]
    out = sig.probe_summary(200, data)
    text = json.dumps(out)
    assert me not in text and "ab cd" not in text and PHONE_A not in text
    assert out["entries"] == 1 and out["with_uuid"] == 1 and out["expected_shape"] is True
    assert out["statuses"] == ["TRUSTED_UNVERIFIED"]
    assert sig.probe_summary(None, None)["expected_shape"] is False
