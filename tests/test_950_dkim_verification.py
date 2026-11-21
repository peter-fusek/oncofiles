# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950: sender verification by DKIM on the raw message (Brevo route).

Brevo's inbound payload carries no Authentication-Results header, so the
queue worker downloads the raw EML (``EMLDownloadToken``) and verifies its
DKIM signatures (``inbound_dkim``). A mail claiming the caregiver's From waits
in the small ``claimed`` bucket (``inbound_claimed_jobs``) until then.

Synthetic only: keys are generated in the test, messages are invented (dates
in 2027), DNS is a dict, the EML download is a dict. No network.
"""

from __future__ import annotations

import base64
import logging
import random

import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from oncofiles import inbound_dkim, inbound_email, inbound_queue, inbound_welcome, server
from oncofiles.database._inbound_jobs import InboundQueueFullError
from tests.test_950_inbound_address import (  # noqa: F401 - fixtures
    CARER,
    PID,
    STRANGER,
    SUBJECT,
    _env,
    address_of,
    brevo_request,
    drive,
    idb,
    mails,
    pdf,
)

DOMAIN = CARER.rpartition("@")[2]
BODY_MARKER = b"Synthetic body text 2027, never logged"

# ── keys, signing, DNS ──────────────────────────────────────────────────


def _rsa_pem_and_record(key) -> tuple[bytes, bytes]:
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return pem, b"v=DKIM1; k=rsa; p=" + base64.b64encode(der)


@pytest.fixture(scope="module")
def rsa_key():
    return _rsa_pem_and_record(rsa.generate_private_key(public_exponent=65537, key_size=2048))


@pytest.fixture(scope="module")
def other_rsa_key():
    return _rsa_pem_and_record(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def _is_probable_prime(n: int, rng: random.Random) -> bool:
    if n < 4:
        return n in (2, 3)
    if n % 2 == 0:
        return False
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(24):
        x = pow(rng.randrange(2, n - 1), d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int, rng: random.Random) -> int:
    while True:
        top_two = 3 << (bits - 2)  # both top bits set: the product keeps its length
        c = rng.getrandbits(bits) | top_two | 1
        if _is_probable_prime(c, rng):
            return c


@pytest.fixture(scope="module")
def weak_rsa_key():
    """A 512-bit RSA key (cryptography refuses to generate one, so it is built)."""
    rng = random.Random(950)
    e = 65537
    while True:
        p, q = _prime(256, rng), _prime(256, rng)
        phi = (p - 1) * (q - 1)
        if p != q and phi % e and (p * q).bit_length() == 512:
            break
    d = pow(e, -1, phi)
    numbers = rsa.RSAPrivateNumbers(
        p=p,
        q=q,
        d=d,
        dmp1=rsa.rsa_crt_dmp1(d, p),
        dmq1=rsa.rsa_crt_dmq1(d, q),
        iqmp=rsa.rsa_crt_iqmp(p, q),
        public_numbers=rsa.RSAPublicNumbers(e, p * q),
    )
    return _rsa_pem_and_record(numbers.private_key(unsafe_skip_rsa_key_validation=True))


def raw_mail(
    sender: str = CARER, *, body: bytes = BODY_MARKER, from_headers: list[str] | None = None
) -> bytes:
    froms = from_headers if from_headers is not None else [f"Synthetic Carer <{sender}>"]
    head = "".join(f"From: {f}\r\n" for f in froms)
    head += (
        "To: inbox@docs.oncofiles.com\r\n"
        f"Subject: {SUBJECT}\r\n"
        "Date: Mon, 15 Mar 2027 10:00:00 +0000\r\n"
        "Message-ID: <dkim-synthetic-2027@example.test>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain\r\n"
    )
    return head.encode() + b"\r\n" + body + b"\r\n"


def sign(
    raw: bytes,
    pem: bytes,
    *,
    domain: str = DOMAIN,
    selector: str = "s2027",
    alg: bytes = b"rsa-sha256",
    length: bool = False,
) -> bytes:
    sig = dkim.sign(
        raw,
        selector.encode(),
        domain.encode(),
        pem,
        signature_algorithm=alg,
        include_headers=[b"from", b"to", b"subject", b"date", b"message-id"],
        length=length,
    )
    return sig + raw


@pytest.fixture
def dns(monkeypatch):
    """TXT records by name; ``calls`` lists the names looked up."""
    records: dict[str, object] = {}
    calls: list[str] = []

    def _txt(name, timeout=inbound_dkim.DNS_TIMEOUT_S):
        assert timeout <= inbound_dkim.DNS_TIMEOUT_S
        calls.append(name)
        value = records.get(name.rstrip("."))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(inbound_dkim, "_dns_txt", _txt)
    return records, calls


def key_name(selector: str = "s2027", domain: str = DOMAIN) -> str:
    return f"{selector}._domainkey.{domain}"


# ── verify_raw: the rules ───────────────────────────────────────────────


def test_valid_aligned_signature_passes(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    verdict = inbound_dkim.verify_raw(sign(raw_mail(), pem), CARER)
    assert verdict.ok and verdict.reason is None
    assert (verdict.domain, verdict.selector) == (DOMAIN, "s2027")


def test_payload_from_comparison_is_case_insensitive(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    assert inbound_dkim.verify_raw(sign(raw_mail(), pem), CARER.upper()).ok


def test_parent_domain_signature_is_aligned(dns, rsa_key):
    pem, record = rsa_key
    sender = "carer@mail.synthetic-clinic.test"
    dns[0][key_name(domain="synthetic-clinic.test")] = record
    signed = sign(raw_mail(sender), pem, domain="synthetic-clinic.test")
    assert inbound_dkim.verify_raw(signed, sender).ok


def test_child_domain_signature_is_not_aligned(dns, rsa_key):
    pem, record = rsa_key
    sender = "carer@synthetic-clinic.test"
    dns[0][key_name(domain="mail.synthetic-clinic.test")] = record
    signed = sign(raw_mail(sender), pem, domain="mail.synthetic-clinic.test")
    verdict = inbound_dkim.verify_raw(signed, sender)
    assert not verdict.ok and verdict.reason == "dkim_not_aligned"
    assert dns[1] == []  # an unaligned signature is never looked up


@pytest.mark.parametrize(
    ("from_domain", "d", "expect"),
    [
        ("gmail.com", "gmail.com", True),
        ("Mail.Example.TEST", "example.test", True),
        ("example.test", "mail.example.test", False),
        ("notexample.test", "example.test", False),
        ("example.test", "test", False),
        ("example.test", "", False),
    ],
)
def test_alignment_rule(from_domain, d, expect):
    assert inbound_dkim.aligned(from_domain, d) is expect


def test_valid_but_unaligned_d_is_refused(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name(domain="synthetic-relay.test")] = record
    signed = sign(raw_mail(), pem, domain="synthetic-relay.test")
    verdict = inbound_dkim.verify_raw(signed, CARER)
    assert not verdict.ok and verdict.reason == "dkim_not_aligned"


def test_tampered_body_is_invalid(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem)
    tampered = signed.replace(BODY_MARKER, b"Changed body, forged 2027")
    verdict = inbound_dkim.verify_raw(tampered, CARER)
    assert not verdict.ok and verdict.reason == "dkim_invalid"


def test_no_signature_is_dkim_none(dns):
    verdict = inbound_dkim.verify_raw(raw_mail(), CARER)
    assert not verdict.ok and verdict.reason == "dkim_none"


def test_eml_from_other_than_payload_from_is_from_mismatch(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    verdict = inbound_dkim.verify_raw(sign(raw_mail(), pem), "someone.else@gmail.com")
    assert not verdict.ok and verdict.reason == "from_mismatch"
    assert dns[1] == []


@pytest.mark.parametrize(
    "froms",
    [
        [f"Synthetic Carer <{CARER}>", f"Synthetic Carer <{CARER}>"],
        [f"Synthetic Carer <{CARER}>, Other <other.synthetic@gmail.com>"],
    ],
    ids=["two-headers", "two-mailboxes"],
)
def test_repeated_from_is_from_repeated(dns, rsa_key, froms):
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(from_headers=froms), pem)
    verdict = inbound_dkim.verify_raw(signed, CARER)
    assert not verdict.ok and verdict.reason == "from_repeated"


def test_l_tag_never_counts(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem, length=True)
    assert b" l=" in signed or b";l=" in signed or b"l=" in signed.split(b"\r\n\r\n")[0]
    verdict = inbound_dkim.verify_raw(signed, CARER)
    assert not verdict.ok and verdict.reason == "dkim_l_tag"


def test_l_tag_with_appended_text_still_refused(dns, rsa_key):
    """The point of the rule: text appended after the signed length."""
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem, length=True) + b"Appended by a forger 2027\r\n"
    assert inbound_dkim.verify_raw(signed, CARER).reason == "dkim_l_tag"


def test_rsa_sha1_is_weak_alg(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem, alg=b"rsa-sha1")
    verdict = inbound_dkim.verify_raw(signed, CARER)
    assert not verdict.ok and verdict.reason == "dkim_weak_alg"
    assert dns[1] == []


def test_512_bit_key_is_weak_key(dns, weak_rsa_key):
    pem, record = weak_rsa_key
    dns[0][key_name()] = record
    verdict = inbound_dkim.verify_raw(sign(raw_mail(), pem), CARER)
    assert not verdict.ok and verdict.reason == "dkim_weak_key"


def test_ed25519_sha256_is_accepted(dns):
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    dns[0][key_name("ed2027")] = b"v=DKIM1; k=ed25519; p=" + base64.b64encode(pub)
    signed = sign(raw_mail(), base64.b64encode(seed), selector="ed2027", alg=b"ed25519-sha256")
    assert inbound_dkim.verify_raw(signed, CARER).ok


def test_only_the_first_five_signatures_are_tried(dns, rsa_key, other_rsa_key):
    pem, record = rsa_key
    other_pem, _ = other_rsa_key
    # the valid signature is the sixth (bottom); five broken ones sit above it
    raw = sign(raw_mail(), pem, selector="good")
    dns[0][key_name("good")] = record
    for n in range(5):
        dns[0][key_name(f"bad{n}")] = record  # key of another signer
        raw = (
            dkim.sign(
                raw,
                f"bad{n}".encode(),
                DOMAIN.encode(),
                other_pem,
                include_headers=[b"from", b"subject"],
            )
            + raw
        )
    verdict = inbound_dkim.verify_raw(raw, CARER)
    assert not verdict.ok and verdict.reason == "dkim_invalid"
    assert verdict.ignored == 1
    looked_up = dns[1]
    assert len(looked_up) == inbound_dkim.MAX_SIGNATURES
    assert not any("good" in name for name in looked_up)
    assert inbound_email._state.counters["dkim_signatures_ignored"] == 1


def test_dns_timeout_is_temporary(dns, rsa_key):
    pem, _ = rsa_key
    dns[0][key_name()] = inbound_dkim.TemporaryVerifyError("dns_failed")
    with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
        inbound_dkim.verify_raw(sign(raw_mail(), pem), CARER)
    assert exc.value.code == "dns_failed"


def test_missing_key_record_is_permanent(dns, rsa_key):
    pem, _ = rsa_key
    verdict = inbound_dkim.verify_raw(sign(raw_mail(), pem), CARER)
    assert not verdict.ok and verdict.reason == "dkim_invalid"


def test_real_dns_function_maps_timeouts_to_temporary(monkeypatch):
    import dns.exception
    import dns.resolver

    seen = {}

    def _resolve(name, rdtype, **kw):
        seen.update(kw)
        raise dns.exception.Timeout()

    monkeypatch.setattr(dns.resolver, "resolve", _resolve)
    with pytest.raises(inbound_dkim.TemporaryVerifyError):
        inbound_dkim._dns_txt("s2027._domainkey.synthetic.test", 30)
    assert seen["lifetime"] <= inbound_dkim.DNS_TIMEOUT_S <= 5

    def _nx(name, rdtype, **kw):
        raise dns.resolver.NXDOMAIN()

    monkeypatch.setattr(dns.resolver, "resolve", _nx)
    assert inbound_dkim._dns_txt("s2027._domainkey.synthetic.test") is None


# ── forged copies of a real signature ───────────────────────────────────


def test_copied_signature_with_changed_body_fails(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    real = sign(raw_mail(), pem)
    sig_header = real[: real.index(b"From:")]
    forged = sig_header + raw_mail(body=b"Forged body with other files 2027")
    assert inbound_dkim.verify_raw(forged, CARER).reason == "dkim_invalid"


def test_copied_signature_with_changed_from_fails(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    real = sign(raw_mail(), pem)
    sig_header = real[: real.index(b"From:")]
    # same domain, another mailbox: the signed From no longer matches
    other = f"forger.synthetic@{DOMAIN}"
    assert inbound_dkim.verify_raw(sig_header + raw_mail(other), other).reason == "dkim_invalid"
    # another domain: the copied d= is not aligned with it
    foreign = "carer@forged-synthetic.test"
    verdict = inbound_dkim.verify_raw(sig_header + raw_mail(foreign), foreign)
    assert verdict.reason == "dkim_not_aligned"


# ── the EML download ────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    async def aiter_bytes(self):
        for i in range(0, len(self._body), 1024):
            yield self._body[i : i + 1024]


class _Client:
    plan: object = 200
    body: bytes = b""
    seen: list = []

    def __init__(self, *a, **kw):
        _Client.seen.append(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url, headers=None):
        _Client.seen.append({"url": url, "headers": headers})
        plan, body = _Client.plan, _Client.body

        class _Ctx:
            async def __aenter__(self):
                if isinstance(plan, type) and issubclass(plan, BaseException):
                    raise plan("synthetic")
                return _Resp(plan, body)

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


@pytest.fixture
def http(monkeypatch):
    import httpx

    _Client.plan, _Client.body, _Client.seen = 200, b"", []
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return _Client


async def test_eml_fetch_is_pinned_and_capped(http):
    http.body = raw_mail()
    assert await inbound_dkim.fetch_eml("eml-tok", "synthetic-key") == raw_mail()
    client_kw, call = http.seen
    assert client_kw["follow_redirects"] is False and client_kw["timeout"]
    assert call["url"] == "https://api.brevo.com/v3/inbound/attachments/eml-tok"
    assert call["headers"] == {"api-key": "synthetic-key"}
    assert inbound_dkim.EML_MAX_BYTES == inbound_email.MAX_BYTES_PER_MAIL


async def test_oversize_eml_is_permanent(http, monkeypatch):
    monkeypatch.setattr(inbound_dkim, "EML_MAX_BYTES", 5000)
    http.body = b"x" * 5001
    assert await inbound_dkim.fetch_eml("eml-tok", "synthetic-key") is None
    verdict = await inbound_dkim.verify_message("eml-tok", CARER, "synthetic-key")
    assert verdict.reason == "eml_unavailable"


@pytest.mark.parametrize(
    ("plan", "temporary"),
    [(500, True), (503, True), (429, True), ("timeout", True), (404, False), (410, False)],
)
async def test_eml_status_codes(http, plan, temporary):
    import httpx

    http.plan = {"timeout": httpx.ReadTimeout}.get(plan, plan)
    if temporary:
        with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
            await inbound_dkim.verify_message("eml-tok", CARER, "synthetic-key")
        assert exc.value.code == "fetch_failed"
    else:
        verdict = await inbound_dkim.verify_message("eml-tok", CARER, "synthetic-key")
        assert not verdict.ok and verdict.reason == "eml_unavailable"


async def test_bad_token_shape_is_never_requested(http):
    assert await inbound_dkim.fetch_eml("../etc/passwd", "synthetic-key") is None
    assert http.seen == []


async def test_missing_api_key_is_temporary():
    with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
        await inbound_dkim.verify_message("eml-tok", CARER, "")
    assert exc.value.code == "no_api_key"


def test_eml_endpoint_source_is_cited():
    import inspect

    src = inspect.getsource(inbound_dkim)
    assert "developers.brevo.com/reference/get-inbound-email-attachment" in src
    assert "EMLDownloadToken" in src


# ── the queue: claimed bucket, worker verification ──────────────────────


@pytest.fixture
def dkim_brevo(monkeypatch, dns):
    """DKIM mode (no authserv-id), BREVO_API_KEY set; attachments and EMLs
    are served from dicts; the worker is run by hand (no kick)."""
    monkeypatch.setenv(inbound_email.BREVO_API_KEY_ENV, "synthetic-brevo-key-950")
    monkeypatch.delenv(inbound_email.BREVO_AUTHSERV_ENV, raising=False)
    files: dict[str, bytes] = {}
    emls: dict[str, object] = {}
    fetched: list[str] = []

    def _fetcher(token, api_key, limit):
        async def fetch():
            fetched.append(token)
            return files.get(token)

        return fetch

    async def _fetch_eml(token, api_key):
        fetched.append(token)
        value = emls.get(token)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(inbound_email, "_brevo_fetcher", _fetcher)
    monkeypatch.setattr(inbound_dkim, "fetch_eml", _fetch_eml)
    monkeypatch.setattr(inbound_queue, "kick", lambda db: None)
    return files, emls, fetched, dns[0]


_SEQ = iter(range(1, 10_000))


def dkim_item(to, *, sender=CARER, eml="", files=(), dkim_header=True, mid=None):
    n = next(_SEQ)
    headers = {"From": f"Synthetic Carer <{sender}>"}
    if dkim_header:
        headers["DKIM-Signature"] = f"v=1; a=rsa-sha256; d={DOMAIN}; s=s2027; bh=x; b=y"
    item = {
        "Uuid": [f"00000000-0000-4000-8000-{n:012d}"],
        "MessageId": mid or f"<dkim-{n}@example.test>",
        "From": {"Name": "Synthetic Carer", "Address": sender},
        "To": [{"Name": None, "Address": to}],
        "Recipients": [to],
        "Subject": SUBJECT,
        "RawTextBody": "never stored",
        "SpamScore": 0.5,
        "Attachments": [
            {
                "Name": name,
                "ContentType": "application/pdf",
                "ContentLength": 100,
                "ContentID": None,
                "DownloadToken": tok,
            }
            for name, tok in files
        ],
        "Headers": headers,
    }
    if eml:
        item["EMLDownloadToken"] = eml
    return item


async def _post(db, *items):
    return await server.inbound_brevo_webhook(brevo_request(db, {"items": list(items)}))


async def _rows(db, table):
    async with db.db.execute(
        f"SELECT state, reason, attempts FROM {table} ORDER BY id"  # noqa: S608 - test table name
    ) as cur:
        return [dict(r) for r in await cur.fetchall()]


async def _trusted_jobs(db):
    async with db.db.execute("SELECT trusted, state, reason FROM inbound_jobs ORDER BY id") as cur:
        return [dict(r) for r in await cur.fetchall()]


async def test_caregiver_mail_waits_in_the_claimed_bucket_then_files(
    idb, drive, mails, dkim_brevo, rsa_key
):
    files, emls, fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    files["att-1"] = pdf(1)
    emls["eml-1"] = sign(raw_mail(), pem)
    resp = await _post(idb, dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]))
    assert resp.status_code == 202
    assert await _rows(idb, "inbound_claimed_jobs") == [
        {"state": "pending", "reason": None, "attempts": 0}
    ]
    assert await _trusted_jobs(idb) == [] and fetched == []  # nothing fetched in the webhook
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["reason"] == "verified"
    [job] = await _trusted_jobs(idb)
    assert job["trusted"] == 1 and job["state"] == "done"
    assert [f[1] for f in drive.files] == [pdf(1)]


async def test_forged_caregiver_mail_is_not_filed(idb, drive, mails, dkim_brevo, rsa_key):
    files, emls, fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    files["att-1"] = pdf(1)
    emls["eml-1"] = sign(raw_mail(), pem).replace(BODY_MARKER, b"forged body 2027")
    await _post(idb, dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]))
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["reason"] == "dkim_invalid"
    [job] = await _trusted_jobs(idb)
    assert job["trusted"] == 0 and job["reason"] == "unmatched"
    assert drive.files == [] and "att-1" not in fetched
    assert inbound_email.unmatched_overview()["auth_refusals"]["dkim_invalid"] == 1


async def test_payload_without_dkim_signature_is_refused_at_once(idb, drive, dkim_brevo):
    _files, _emls, fetched, _ = dkim_brevo
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-1", dkim_header=False))
    assert await _rows(idb, "inbound_claimed_jobs") == []
    [job] = await _trusted_jobs(idb)
    assert job["trusted"] == 0
    assert inbound_email.unmatched_overview()["auth_refusals"]["dkim_none"] == 1
    await inbound_queue.run_pending(idb)
    assert fetched == []


async def test_stranger_mail_is_never_claimed_nor_downloaded(idb, drive, dkim_brevo):
    _files, _emls, fetched, _ = dkim_brevo
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, sender=STRANGER, eml="eml-1"))
    assert await _rows(idb, "inbound_claimed_jobs") == []
    assert (await _trusted_jobs(idb))[0]["trusted"] == 0
    await inbound_queue.run_pending(idb)
    assert fetched == [] and drive.files == []


async def test_eml_from_mismatch_and_repeat_reach_the_overview(idb, drive, dkim_brevo, rsa_key):
    _files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    other = f"other.synthetic@{DOMAIN}"
    emls["eml-a"] = sign(raw_mail(other), pem)
    froms = [f"Synthetic Carer <{CARER}>", f"Synthetic Carer <{CARER}>"]
    emls["eml-b"] = sign(raw_mail(from_headers=froms), pem)
    await _post(idb, dkim_item(addr, eml="eml-a"), dkim_item(addr, eml="eml-b"))
    await inbound_queue.run_pending(idb)
    reasons = [r["reason"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert reasons == ["from_mismatch", "from_repeated"]
    refusals = inbound_email.unmatched_overview()["auth_refusals"]
    assert refusals["from_mismatch"] == 1 and refusals["from_repeated"] == 1


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (inbound_dkim.TemporaryVerifyError("fetch_failed"), "fetch_failed"),  # EML 5xx / 429
        ("dns", "dns_failed"),
    ],
)
async def test_temporary_failures_go_back_to_pending(
    idb, drive, dkim_brevo, rsa_key, failure, code
):
    _files, emls, _fetched, records = dkim_brevo
    pem, _ = rsa_key
    addr = await address_of(idb)
    if failure == "dns":
        emls["eml-1"] = sign(raw_mail(), pem)
        records[key_name()] = inbound_dkim.TemporaryVerifyError("dns_failed")
    else:
        emls["eml-1"] = failure
    await _post(idb, dkim_item(addr, eml="eml-1"))
    await inbound_queue.run_pending(idb)
    assert await _rows(idb, "inbound_claimed_jobs") == [
        {"state": "pending", "reason": code, "attempts": 1}
    ]
    assert await _trusted_jobs(idb) == []


async def test_eml_404_is_permanent(idb, drive, dkim_brevo):
    _files, _emls, _fetched, _ = dkim_brevo  # the dict has no such EML: a 404
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-missing"))
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0] == {
        "state": "done",
        "reason": "eml_unavailable",
        "attempts": 1,
    }
    assert (await _trusted_jobs(idb))[0]["trusted"] == 0


async def test_temporary_failure_ends_failed_after_max_attempts(
    idb, drive, dkim_brevo, monkeypatch
):
    _files, emls, _fetched, _ = dkim_brevo
    monkeypatch.setattr(inbound_queue, "_backoff", lambda n: inbound_queue.timedelta(0))
    addr = await address_of(idb)
    emls["eml-1"] = inbound_dkim.TemporaryVerifyError("fetch_failed")
    await _post(idb, dkim_item(addr, eml="eml-1"))
    for _ in range(inbound_queue.MAX_ATTEMPTS + 1):
        await inbound_queue.run_pending(idb)
    [row] = await _rows(idb, "inbound_claimed_jobs")
    assert row["state"] == "failed" and row["reason"] == "fetch_failed"


async def test_forged_flood_cannot_block_real_caregiver_mail(
    idb, drive, mails, dkim_brevo, rsa_key, monkeypatch
):
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_JOBS_PER_TOKEN", 3)
    addr = await address_of(idb)
    forged = sign(raw_mail(), pem).replace(BODY_MARKER, b"forged body 2027")
    for n in range(3):
        emls[f"forged-{n}"] = forged
        assert (await _post(idb, dkim_item(addr, eml=f"forged-{n}"))).status_code == 202
    # the bucket is full: the real mail gets a 503, Brevo retries it later
    files["att-real"] = pdf(7)
    emls["eml-real"] = sign(raw_mail(), pem)
    real = dkim_item(addr, eml="eml-real", files=[("SynthReal2027.pdf", "att-real")])
    assert (await _post(idb, real)).status_code == 503
    assert inbound_email._state.counters["jobs_claimed_full"] == 1
    # the forged rows leave the bucket after one quick verification each
    await inbound_queue.run_pending(idb)
    assert {r["reason"] for r in await _rows(idb, "inbound_claimed_jobs")} == {"dkim_invalid"}
    # Brevo's retry of the real mail now enters, verifies and is filed
    assert (await _post(idb, real)).status_code == 202
    await inbound_queue.run_pending(idb)
    assert [f[1] for f in drive.files] == [pdf(7)]
    trusted = [j for j in await _trusted_jobs(idb) if j["trusted"]]
    assert len(trusted) == 1 and trusted[0]["state"] == "done"


async def test_claimed_cap_refuses_before_writing(idb, dkim_brevo, monkeypatch):
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_JOBS_TOTAL", 1)
    addr = await address_of(idb)
    with pytest.raises(InboundQueueFullError):
        await inbound_queue.enqueue(
            idb,
            [
                inbound_email.parse_brevo_item(dkim_item(addr, eml="eml-1")),
                inbound_email.parse_brevo_item(dkim_item(addr, eml="eml-2")),
            ],
        )
    assert await _rows(idb, "inbound_claimed_jobs") == []


async def test_brevo_retry_of_a_claimed_mail_is_a_no_op(idb, dkim_brevo):
    addr = await address_of(idb)
    item = dkim_item(addr, eml="eml-1", mid="<same-2027@example.test>")
    await _post(idb, item)
    await _post(idb, item)
    assert len(await _rows(idb, "inbound_claimed_jobs")) == 1


async def test_claimed_payload_is_encrypted(idb, dkim_brevo):
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-secret-token"))
    async with idb.db.execute("SELECT * FROM inbound_claimed_jobs") as cur:
        row = dict(await cur.fetchone())
    blob = repr(row)
    for clear in (CARER, "eml-secret-token", SUBJECT, addr):
        assert clear not in blob


async def test_welcome_mail_is_admitted_only_after_verification(
    idb, drive, mails, dkim_brevo, rsa_key, monkeypatch
):
    """A forged welcome@ mail never reaches admit with a pass (no trial, no reply)."""
    _files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    seen = []

    async def _admit(db, msg):
        seen.append(msg.auth_pass)
        return None

    monkeypatch.setattr(inbound_welcome, "admit", _admit)
    welcome = sorted(inbound_welcome.WELCOME_ADDRESSES)[0]
    emls["eml-forged"] = sign(raw_mail(), pem).replace(BODY_MARKER, b"forged 2027")
    emls["eml-real"] = sign(raw_mail(), pem)
    await _post(idb, dkim_item(welcome, eml="eml-forged"), dkim_item(welcome, eml="eml-real"))
    assert seen == []  # nothing admitted in the webhook
    assert len(await _rows(idb, "inbound_claimed_jobs")) == 2
    await inbound_queue.run_pending(idb)
    assert seen == [False, True]


async def test_logs_carry_no_eml_bytes_or_addresses(idb, drive, mails, dkim_brevo, rsa_key, caplog):
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    files["att-1"] = pdf(1)
    emls["eml-1"] = sign(raw_mail(), pem)
    emls["eml-2"] = sign(raw_mail(), pem).replace(BODY_MARKER, b"forged body 2027")
    caplog.set_level(logging.DEBUG)
    await _post(
        idb,
        dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]),
        dkim_item(addr, eml="eml-2"),
    )
    await inbound_queue.run_pending(idb)
    text = caplog.text
    assert "inbound: dkim verdict=pass" in text and f"{DOMAIN}/s2027" in text
    for leaked in (
        CARER,
        addr,
        SUBJECT,
        BODY_MARKER.decode(),
        "forged body",
        "eml-1",
        "Synthetic Carer",
    ):
        assert leaked not in text, leaked
    assert "bh=" not in text and "b=" not in text.replace("dkim_", "")


async def test_authserv_mode_keeps_the_old_path(idb, dkim_brevo, monkeypatch):
    monkeypatch.setenv(inbound_email.BREVO_AUTHSERV_ENV, "mx.synthetic.test")
    assert inbound_email.verification_mode() == "authserv"
    addr = await address_of(idb)
    msg = inbound_email.parse_brevo_item(dkim_item(addr, eml="eml-1"))
    assert msg.auth_mode == "" and msg.auth_pass is False
    await _post(idb, dkim_item(addr, eml="eml-1"))
    assert await _rows(idb, "inbound_claimed_jobs") == []


def test_dkim_codes_are_in_the_admin_overview():
    overview = inbound_email.unmatched_overview()
    assert overview["verification_mode"] in inbound_email.VERIFICATION_MODES
    for code in inbound_dkim.DKIM_REFUSAL_REASONS:
        assert code in overview["auth_refusals"]


def test_infra_limits():
    assert inbound_dkim.DNS_TIMEOUT_S <= 5
    assert inbound_dkim.MAX_SIGNATURES == 5
    assert inbound_dkim.MIN_RSA_BITS == 1024
    assert {b"rsa-sha256", b"ed25519-sha256"} == inbound_dkim.ACCEPTED_ALGORITHMS
    assert inbound_dkim.EML_MAX_BYTES == inbound_email.MAX_BYTES_PER_MAIL
