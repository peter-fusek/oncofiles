# ruff: noqa: F811 - the #950 fixtures are imported and used as test arguments
"""#950 / #966: sender verification by DKIM on the raw message (Brevo route).

Brevo's inbound payload carries no Authentication-Results header, so the
queue worker downloads the raw EML (``EMLDownloadToken``) and verifies its
DKIM signatures (``inbound_dkim``). A mail claiming the caregiver's From (or
any welcome@ mail) waits in the small ``claimed`` bucket
(``inbound_claimed_jobs``) until then.

Synthetic only: keys are generated in the test, messages are invented (dates
in 2027, the verifier's clock is moved there), DNS is a dict, the EML download
is a dict. No network.
"""

from __future__ import annotations

import base64
import inspect
import logging
import random
from email.utils import parsedate_to_datetime

import dkim
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from oncofiles import inbound_dkim, inbound_email, inbound_queue, inbound_welcome, server
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
SENT = "Mon, 15 Mar 2027 10:00:00 +0000"
SENT_TS = parsedate_to_datetime(SENT).timestamp()
TO_ADDR = "synthcarer-abcdefghjk@docs.oncofiles.com"
DAY = 24 * 3600
WELCOME = sorted(inbound_welcome.WELCOME_ADDRESSES)[0]


def to_ok(address: str) -> bool:
    return address.strip().lower() == TO_ADDR


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The verifier's wall clock sits one hour after the synthetic Date."""
    state = {"now": SENT_TS + 3600}
    monkeypatch.setattr(inbound_dkim, "_now", lambda: state["now"])
    return state


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
    sender: str = CARER,
    *,
    to: str = TO_ADDR,
    body: bytes = BODY_MARKER,
    date: str = SENT,
    from_headers: list[str] | None = None,
    extra: str = "",
) -> bytes:
    froms = from_headers if from_headers is not None else [f"Synthetic Carer <{sender}>"]
    head = "".join(f"From: {f}\r\n" for f in froms)
    head += (
        f"To: {to}\r\n"
        f"Subject: {SUBJECT}\r\n"
        f"Date: {date}\r\n"
        "Message-ID: <dkim-synthetic-2027@example.test>\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/plain\r\n"
    ) + extra
    return head.encode() + b"\r\n" + body + b"\r\n"


SIGNED = [b"from", b"to", b"subject", b"date", b"message-id"]


def sign(
    raw: bytes,
    pem: bytes,
    *,
    domain: str = DOMAIN,
    selector: str = "s2027",
    alg: bytes = b"rsa-sha256",
    length: bool = False,
    headers: list[bytes] | None = None,
) -> bytes:
    sig = dkim.sign(
        raw,
        selector.encode(),
        domain.encode(),
        pem,
        signature_algorithm=alg,
        include_headers=headers or SIGNED,
        length=length,
    )
    return sig + raw


def with_tag(signed: bytes, tag: str) -> bytes:
    """Insert ``tag;`` right after ``DKIM-Signature: v=1;`` (the signature is
    then broken, but the pre-checks under test run before any verification)."""
    head = b"DKIM-Signature: v=1;"
    assert signed.startswith(head)
    return head + b" " + tag.encode() + b";" + signed[len(head) :]


@pytest.fixture
def dns(monkeypatch):
    """TXT records by name; ``calls`` lists (name, timeout) of each lookup."""
    records: dict[str, object] = {}
    calls: list[tuple[str, float]] = []

    def _txt(name, timeout=inbound_dkim.DNS_TIMEOUT_S):
        assert timeout <= inbound_dkim.DNS_TIMEOUT_S
        calls.append((name, timeout))
        value = records.get(name.rstrip("."))
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(inbound_dkim, "_dns_txt", _txt)
    return records, calls


def key_name(selector: str = "s2027", domain: str = DOMAIN) -> str:
    return f"{selector}._domainkey.{domain}"


def verify(raw: bytes, sender: str = CARER, **kw):
    return inbound_dkim.verify_raw(raw, sender, to_ok, **kw)


# ── verify_raw: the rules ───────────────────────────────────────────────


def test_valid_aligned_signature_passes(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    verdict = verify(sign(raw_mail(), pem))
    assert verdict.ok and verdict.reason is None
    assert (verdict.domain, verdict.selector) == (DOMAIN, "s2027")


def test_payload_from_comparison_is_case_insensitive(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    assert verify(sign(raw_mail(), pem), CARER.upper()).ok


def test_parent_domain_signature_passes(dns, rsa_key):
    pem, record = rsa_key
    sender, d = "carer@mail.synthetic-clinic.test", "synthetic-clinic.test"
    dns[0][key_name(domain=d)] = record
    assert verify(sign(raw_mail(sender), pem, domain=d), sender).ok


@pytest.mark.parametrize(
    ("sender", "d"),
    [
        ("carer@synthetic-clinic.test", "mail.synthetic-clinic.test"),  # child d
        ("carer@mail.synthetic-clinic.test", "news.synthetic-clinic.test"),  # sibling d
        ("carer@clinic.example.co.uk", "co.uk"),  # public-suffix d
    ],
    ids=["child", "sibling", "public-suffix"],
)
def test_child_sibling_and_public_suffix_d_do_not_align(dns, rsa_key, sender, d):
    pem, record = rsa_key
    dns[0][key_name(domain=d)] = record
    verdict = verify(sign(raw_mail(sender), pem, domain=d), sender)
    assert not verdict.ok and verdict.reason == "dkim_not_aligned"
    assert dns[1] == []  # never looked up


@pytest.mark.parametrize(
    ("from_domain", "d", "expect"),
    [
        ("gmail.com", "gmail.com", True),
        ("Mail.Example.TEST", "example.test", True),
        ("mail.example.co.uk", "example.co.uk", True),
        ("example.test", "mail.example.test", False),  # child
        ("a.example.test", "b.example.test", False),  # sibling
        ("notexample.test", "example.test", False),
        ("example.co.uk", "co.uk", False),  # a public suffix never aligns
        ("x.github.io", "github.io", False),  # a private-section public suffix
        ("example.test", "test", False),
        ("example.test", "", False),
    ],
)
def test_alignment_rule(from_domain, d, expect):
    assert inbound_dkim.aligned(from_domain, d) is expect


def test_public_suffix_list_is_bundled_not_fetched(monkeypatch):
    import socket

    def _no_network(*a, **kw):
        raise AssertionError("network")

    monkeypatch.setattr(inbound_dkim, "_psl", None)
    monkeypatch.setattr(socket, "create_connection", _no_network)
    assert inbound_dkim.org_domain("x.example.co.uk") == "example.co.uk"


def test_valid_but_unaligned_d_is_refused(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name(domain="synthetic-relay.test")] = record
    verdict = verify(sign(raw_mail(), pem, domain="synthetic-relay.test"))
    assert not verdict.ok and verdict.reason == "dkim_not_aligned"
    assert dns[1] == []  # an unaligned signature is never looked up


def test_tampered_body_is_invalid(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    tampered = sign(raw_mail(), pem).replace(BODY_MARKER, b"Changed body, forged 2027")
    verdict = verify(tampered)
    assert not verdict.ok and verdict.reason == "dkim_invalid"


def test_no_signature_is_dkim_none(dns):
    verdict = verify(raw_mail())
    assert not verdict.ok and verdict.reason == "dkim_none"


def test_eml_from_other_than_payload_from_is_from_mismatch(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    verdict = verify(sign(raw_mail(), pem), "someone.else@gmail.com")
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
    verdict = verify(sign(raw_mail(from_headers=froms), pem))
    assert not verdict.ok and verdict.reason == "from_repeated"


def test_from_is_read_from_dkimpys_own_header_list(dns, rsa_key):
    """The From compared is one dkimpy parsed (and hashed): a folded From works,
    and there is no second, separate parse of the header block."""
    pem, record = rsa_key
    dns[0][key_name()] = record
    folded = sign(raw_mail(from_headers=[f"Synthetic Carer\r\n <{CARER}>"]), pem)
    assert verify(folded).ok
    src = inspect.getsource(inbound_dkim)
    assert "BytesHeaderParser" not in src and "signer.headers" in src


def test_l_tag_never_counts(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem, length=True)
    assert inbound_dkim.signature_tags(signed.split(b"\r\nFrom:")[0][16:]).get(b"l")
    verdict = verify(signed)
    assert not verdict.ok and verdict.reason == "dkim_l_tag"


def test_l_tag_with_appended_text_still_refused(dns, rsa_key):
    """The point of the rule: text appended after the signed length."""
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem, length=True) + b"Appended by a forger 2027\r\n"
    assert verify(signed).reason == "dkim_l_tag"


def test_rsa_sha1_is_weak_alg(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    verdict = verify(sign(raw_mail(), pem, alg=b"rsa-sha1"))
    assert not verdict.ok and verdict.reason == "dkim_weak_alg"
    assert dns[1] == []


def test_512_bit_key_is_weak_key(dns, weak_rsa_key):
    pem, record = weak_rsa_key
    dns[0][key_name()] = record
    verdict = verify(sign(raw_mail(), pem))
    assert not verdict.ok and verdict.reason == "dkim_weak_key"


def test_ed25519_sha256_is_accepted(dns):
    key = ed25519.Ed25519PrivateKey.generate()
    seed = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    dns[0][key_name("ed2027")] = b"v=DKIM1; k=ed25519; p=" + base64.b64encode(pub)
    signed = sign(raw_mail(), base64.b64encode(seed), selector="ed2027", alg=b"ed25519-sha256")
    assert verify(signed).ok


def test_only_the_first_five_signatures_are_tried(dns, rsa_key, other_rsa_key):
    pem, record = rsa_key
    other_pem, _ = other_rsa_key
    # the valid signature is the sixth (bottom); five broken ones sit above it
    raw = sign(raw_mail(), pem, selector="good")
    dns[0][key_name("good")] = record
    for n in range(5):
        dns[0][key_name(f"bad{n}")] = record  # key of another signer
        raw = sign(raw, other_pem, selector=f"bad{n}", headers=[b"from", b"to", b"date"])
    verdict = verify(raw)
    assert not verdict.ok and verdict.reason == "dkim_invalid"
    assert verdict.ignored == 1
    looked_up = [name for name, _t in dns[1]]
    assert len(looked_up) == inbound_dkim.MAX_SIGNATURES
    assert not any("good" in name for name in looked_up)
    assert inbound_email._state.counters["dkim_signatures_ignored"] == 1


def test_dns_timeout_is_temporary(dns, rsa_key):
    pem, _ = rsa_key
    dns[0][key_name()] = inbound_dkim.TemporaryVerifyError("dns_failed")
    with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
        verify(sign(raw_mail(), pem))
    assert exc.value.code == "dns_failed"


def test_dns_budget_bounds_every_lookup(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    assert verify(sign(raw_mail(), pem), budget_s=0.5).ok
    [(_name, timeout)] = dns[1]
    assert timeout <= 0.5
    assert inbound_dkim.DNS_BUDGET_S <= 8


def test_exhausted_dns_budget_is_temporary_without_a_lookup(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
        verify(sign(raw_mail(), pem), budget_s=0)
    assert exc.value.code == "dns_failed" and dns[1] == []


def test_missing_key_record_is_permanent(dns, rsa_key):
    pem, _ = rsa_key
    verdict = verify(sign(raw_mail(), pem))
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


# ── parser differential (security review) ───────────────────────────────


def test_duplicate_d_tag_is_malformed(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    forged = with_tag(sign(raw_mail(), pem), "d=synthetic-forger.test")
    verdict = verify(forged)
    assert not verdict.ok and verdict.reason == "dkim_malformed"
    assert dns[1] == []


@pytest.mark.parametrize("tag", ["D=synthetic-forger.test", "S=other", "A=rsa-sha256", "Bh=AAAA"])
def test_case_variant_tag_is_malformed(dns, rsa_key, tag):
    pem, record = rsa_key
    dns[0][key_name()] = record
    verdict = verify(with_tag(sign(raw_mail(), pem), tag))
    assert not verdict.ok and verdict.reason == "dkim_malformed"


def test_pre_parse_disagreeing_with_dkimpy_is_refused(dns, rsa_key, monkeypatch):
    """If our pre-check ever read another d= than the one dkimpy verified,
    the signature does not count (alignment is decided on dkimpy's domain)."""
    pem, record = rsa_key
    sender, d = "carer@mail.synthetic-clinic.test", "mail.synthetic-clinic.test"
    dns[0][key_name(domain=d)] = record
    real = inbound_dkim.signature_tags

    def _disagree(value):
        tags = dict(real(value) or {})
        tags[b"d"] = b"synthetic-clinic.test"  # still aligned, but not what dkimpy sees
        return tags

    monkeypatch.setattr(inbound_dkim, "signature_tags", _disagree)
    verdict = verify(sign(raw_mail(sender), pem, domain=d), sender)
    assert not verdict.ok and verdict.reason == "dkim_malformed"


def test_tags_are_parsed_by_dkimpy_itself():
    src = inspect.getsource(inbound_dkim.signature_tags)
    assert "parse_tag_value" in src
    assert inbound_dkim.signature_tags(b"v=1; d=a.test; d=b.test") is None
    assert inbound_dkim.signature_tags(b"v=1; D=a.test") is None
    assert inbound_dkim.signature_tags(b"v=1; d=a.test")[b"d"] == b"a.test"


# ── replay guards (security review) ─────────────────────────────────────


def test_signature_without_date_in_h_is_incomplete(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    signed = sign(raw_mail(), pem, headers=[b"from", b"to", b"subject"])
    verdict = verify(signed)
    assert not verdict.ok and verdict.reason == "dkim_h_incomplete"
    assert dns[1] == []


def test_signature_without_to_in_h_is_incomplete(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    assert verify(sign(raw_mail(), pem, headers=[b"from", b"date"])).reason == "dkim_h_incomplete"


def test_mail_to_someone_else_is_not_accepted(dns, rsa_key):
    """A genuine signed mail of the caregiver to another person, re-sent."""
    pem, record = rsa_key
    dns[0][key_name()] = record
    genuine = sign(raw_mail(to="someone.synthetic@gmail.com"), pem)
    verdict = verify(genuine)
    assert not verdict.ok and verdict.reason == "recipient_not_signed"


def test_an_added_unsigned_to_header_does_not_help(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    genuine = sign(raw_mail(to="someone.synthetic@gmail.com"), pem)
    forged = f"To: {TO_ADDR}\r\n".encode() + genuine
    assert verify(forged).reason == "recipient_not_signed"


def test_recipient_in_signed_cc_is_accepted(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    raw = raw_mail(to="someone.synthetic@gmail.com", extra=f"Cc: {TO_ADDR.upper()}\r\n")
    assert verify(sign(raw, pem, headers=[*SIGNED, b"cc"])).ok


def test_recipient_in_unsigned_cc_is_not_accepted(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    raw = raw_mail(to="someone.synthetic@gmail.com", extra=f"Cc: {TO_ADDR}\r\n")
    assert verify(sign(raw, pem)).reason == "recipient_not_signed"


@pytest.mark.parametrize(
    ("offset", "ok"),
    [
        (-inbound_dkim.DATE_MAX_AGE_S + DAY, True),
        (-inbound_dkim.DATE_MAX_AGE_S - DAY, False),  # an old mail
        (inbound_dkim.DATE_MAX_SKEW_S - 3600, True),
        (inbound_dkim.DATE_MAX_SKEW_S + 3600, False),  # dated in the future
    ],
)
def test_signed_date_must_be_fresh(dns, rsa_key, clock, offset, ok):
    pem, record = rsa_key
    dns[0][key_name()] = record
    clock["now"] = SENT_TS - offset
    verdict = verify(sign(raw_mail(), pem))
    assert verdict.ok is ok
    if not ok:
        assert verdict.reason == "stale_date"


def test_unparseable_date_is_stale(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    assert verify(sign(raw_mail(date="not a date"), pem)).reason == "stale_date"


def test_expired_x_tag_is_refused(dns, rsa_key, clock):
    pem, record = rsa_key
    dns[0][key_name()] = record
    expired = with_tag(sign(raw_mail(), pem), f"x={int(clock['now']) - 60}")
    verdict = verify(expired)
    assert not verdict.ok and verdict.reason == "dkim_expired"
    assert dns[1] == []


# ── forged copies of a real signature ───────────────────────────────────


def test_copied_signature_with_changed_body_fails(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    real = sign(raw_mail(), pem)
    sig_header = real[: real.index(b"From:")]
    forged = sig_header + raw_mail(body=b"Forged body with other files 2027")
    assert verify(forged).reason == "dkim_invalid"


def test_copied_signature_with_changed_from_fails(dns, rsa_key):
    pem, record = rsa_key
    dns[0][key_name()] = record
    real = sign(raw_mail(), pem)
    sig_header = real[: real.index(b"From:")]
    other = f"forger.synthetic@{DOMAIN}"  # same domain: the signed From differs
    assert verify(sig_header + raw_mail(other), other).reason == "dkim_invalid"
    foreign = "carer@forged-synthetic.test"  # another domain: the d= does not align
    assert verify(sig_header + raw_mail(foreign), foreign).reason == "dkim_not_aligned"


# ── the payload pre-filter ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("values", "expect"),
    [
        ([], "dkim_none"),
        (["v=1; a=rsa-sha256; d=synthetic-relay.test; s=x; bh=A; b=A"], "dkim_not_aligned"),
        (["v=1; d=gmail.com; d=gmail.com; s=x"], "dkim_not_aligned"),  # malformed only
        (["v=1; a=rsa-sha256; d=gmail.com; s=x; bh=A; b=A"], None),
        (["v=1; d=relay.test; s=x", "v=1; d=gmail.com; s=y"], None),
    ],
)
def test_payload_prefilter_can_only_refuse(values, expect):
    assert inbound_dkim.payload_prefilter(values, CARER) == expect


# ── the EML download ────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    async def aiter_bytes(self):
        for i in range(0, len(self._body), 65536):
            yield self._body[i : i + 65536]


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


async def test_eml_fetch_is_pinned(http):
    http.body = raw_mail()
    assert await inbound_dkim.fetch_eml("eml-tok", "synthetic-key") == raw_mail()
    client_kw, call = http.seen
    assert client_kw["follow_redirects"] is False and client_kw["timeout"]
    assert call["url"] == "https://api.brevo.com/v3/inbound/attachments/eml-tok"
    assert call["headers"] == {"api-key": "synthetic-key"}


def test_eml_cap_leaves_room_for_base64():
    cap, per_mail = inbound_dkim.EML_MAX_BYTES, inbound_email.MAX_BYTES_PER_MAIL
    assert cap == int(1.4 * per_mail) + 1024 * 1024
    assert cap > per_mail * 4 // 3  # a mail at the per-mail cap still fits


async def test_oversize_eml_is_eml_too_large(http, monkeypatch):
    monkeypatch.setattr(inbound_dkim, "EML_MAX_BYTES", 5000)
    http.body = b"x" * 5001
    assert await inbound_dkim.fetch_eml("eml-tok", "synthetic-key") == "eml_too_large"
    verdict = await inbound_dkim.verify_message("eml-tok", CARER, "synthetic-key", to_ok)
    assert not verdict.ok and verdict.reason == "eml_too_large"


@pytest.mark.parametrize(
    ("plan", "temporary"),
    [(500, True), (503, True), (429, True), ("timeout", True), (404, False), (410, False)],
)
async def test_eml_status_codes(http, plan, temporary):
    import httpx

    http.plan = {"timeout": httpx.ReadTimeout}.get(plan, plan)
    if temporary:
        with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
            await inbound_dkim.verify_message("eml-tok", CARER, "synthetic-key", to_ok)
        assert exc.value.code == "fetch_failed"
    else:
        verdict = await inbound_dkim.verify_message("eml-tok", CARER, "synthetic-key", to_ok)
        assert not verdict.ok and verdict.reason == "eml_unavailable"


async def test_bad_token_shape_is_never_requested(http):
    assert await inbound_dkim.fetch_eml("../etc/passwd", "synthetic-key") == "eml_unavailable"
    assert http.seen == []


async def test_missing_api_key_is_temporary():
    with pytest.raises(inbound_dkim.TemporaryVerifyError) as exc:
        await inbound_dkim.verify_message("eml-tok", CARER, "", to_ok)
    assert exc.value.code == "no_api_key"


def test_eml_endpoint_source_is_cited():
    src = inspect.getsource(inbound_dkim)
    assert "developers.brevo.com/reference/get-inbound-email-attachment" in src
    assert "EMLDownloadToken" in src


# ── the queue: claimed bucket, worker verification ──────────────────────


@pytest.fixture
def dkim_brevo(monkeypatch, dns):
    """BREVO_API_KEY set; attachments and EMLs are served from dicts; the
    worker is run by hand (no kick)."""
    monkeypatch.setenv(inbound_email.BREVO_API_KEY_ENV, "synthetic-brevo-key-950")
    monkeypatch.delenv(inbound_email.BREVO_AUTHSERV_ENV, raising=False)
    monkeypatch.setattr(inbound_queue, "_backoff", lambda n: inbound_queue.timedelta(0))
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
        value = emls.get(token, "eml_unavailable")
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(inbound_email, "_brevo_fetcher", _fetcher)
    monkeypatch.setattr(inbound_dkim, "fetch_eml", _fetch_eml)
    monkeypatch.setattr(inbound_queue, "kick", lambda db: None)
    return files, emls, fetched, dns[0]


_SEQ = iter(range(1, 100_000))


def dkim_item(to, *, sender=CARER, eml="", files=(), d=None, mid=None, uuid=None):
    n = next(_SEQ)
    d = DOMAIN if d is None else d
    headers = {"From": f"Synthetic Carer <{sender}>"}
    if d:
        headers["DKIM-Signature"] = f"v=1; a=rsa-sha256; d={d}; s=s2027; bh=AAAA; b=AAAA"
    item = {
        "Uuid": [uuid or f"00000000-0000-4000-8000-{n:012d}"],
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
        f"SELECT state, reason, attempts FROM {table} ORDER BY id"  # noqa: S608 - test table
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
    emls["eml-1"] = sign(raw_mail(to=addr), pem)
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
    emls["eml-1"] = sign(raw_mail(to=addr), pem).replace(BODY_MARKER, b"forged body 2027")
    await _post(idb, dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]))
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["reason"] == "dkim_invalid"
    [job] = await _trusted_jobs(idb)
    assert job["trusted"] == 0 and job["reason"] == "unmatched"
    assert drive.files == [] and "att-1" not in fetched
    assert inbound_email.unmatched_overview()["auth_refusals"]["dkim_invalid"] == 1


async def test_replayed_mail_to_someone_else_is_not_filed(idb, drive, mails, dkim_brevo, rsa_key):
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    files["att-1"] = pdf(1)
    emls["eml-1"] = sign(raw_mail(to="someone.synthetic@gmail.com"), pem)
    await _post(idb, dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]))
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["reason"] == "recipient_not_signed"
    assert drive.files == []


@pytest.mark.parametrize(
    ("d", "code"), [("", "dkim_none"), ("synthetic-relay.test", "dkim_not_aligned")]
)
async def test_payload_without_an_aligned_signature_is_refused_at_once(
    idb, drive, dkim_brevo, d, code
):
    _files, _emls, fetched, _ = dkim_brevo
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-1", d=d))
    assert await _rows(idb, "inbound_claimed_jobs") == []
    [job] = await _trusted_jobs(idb)
    assert job["trusted"] == 0
    assert inbound_email.unmatched_overview()["auth_refusals"][code] == 1
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
    emls["eml-a"] = sign(raw_mail(other, to=addr), pem)
    froms = [f"Synthetic Carer <{CARER}>", f"Synthetic Carer <{CARER}>"]
    emls["eml-b"] = sign(raw_mail(to=addr, from_headers=froms), pem)
    await _post(idb, dkim_item(addr, eml="eml-a"), dkim_item(addr, eml="eml-b"))
    await inbound_queue.run_pending(idb)
    reasons = [r["reason"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert reasons == ["from_mismatch", "from_repeated"]
    refusals = inbound_email.unmatched_overview()["auth_refusals"]
    assert refusals["from_mismatch"] == 1 and refusals["from_repeated"] == 1


@pytest.mark.parametrize("failure", ["fetch", "dns"])
async def test_temporary_failures_go_back_to_pending(idb, drive, dkim_brevo, rsa_key, failure):
    _files, emls, _fetched, records = dkim_brevo
    pem, _ = rsa_key
    addr = await address_of(idb)
    if failure == "dns":
        emls["eml-1"] = sign(raw_mail(to=addr), pem)
        records[key_name()] = inbound_dkim.TemporaryVerifyError("dns_failed")
    else:
        # an EML server error or throttling
        emls["eml-1"] = inbound_dkim.TemporaryVerifyError("fetch_failed")
    await _post(idb, dkim_item(addr, eml="eml-1"))
    await inbound_queue.run_pending(idb, limit=1)
    code = "dns_failed" if failure == "dns" else "fetch_failed"
    assert await _rows(idb, "inbound_claimed_jobs") == [
        {"state": "pending", "reason": code, "attempts": 1}
    ]
    assert await _trusted_jobs(idb) == []


async def test_eml_404_is_permanent(idb, drive, dkim_brevo):
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-missing"))  # not in the dict: a 404
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0] == {
        "state": "done",
        "reason": "eml_unavailable",
        "attempts": 1,
    }
    assert (await _trusted_jobs(idb))[0]["trusted"] == 0


async def test_temporary_failure_ends_failed_after_max_attempts(idb, drive, dkim_brevo):
    _files, emls, _fetched, _ = dkim_brevo
    addr = await address_of(idb)
    emls["eml-1"] = inbound_dkim.TemporaryVerifyError("fetch_failed")
    await _post(idb, dkim_item(addr, eml="eml-1"))
    for _ in range(inbound_queue.MAX_ATTEMPTS + 1):
        await inbound_queue.run_pending(idb, limit=1)
    [row] = await _rows(idb, "inbound_claimed_jobs")
    assert row["state"] == "failed" and row["reason"] == "fetch_failed"


async def test_welcome_dns_failure_gets_one_retry_then_fails(
    idb, drive, dkim_brevo, rsa_key, monkeypatch
):
    _files, emls, _fetched, records = dkim_brevo
    pem, _ = rsa_key

    async def _admit(db, msg):
        return None

    monkeypatch.setattr(inbound_welcome, "admit", _admit)
    emls["eml-w"] = sign(raw_mail(to=WELCOME), pem)
    records[key_name()] = inbound_dkim.TemporaryVerifyError("dns_failed")
    await _post(idb, dkim_item(WELCOME, eml="eml-w"))
    await inbound_queue.run_pending(idb, limit=1)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["state"] == "pending"
    await inbound_queue.run_pending(idb, limit=1)
    [row] = await _rows(idb, "inbound_claimed_jobs")
    assert inbound_queue.WELCOME_DNS_RETRIES == 1
    assert row == {"state": "failed", "reason": "dns_failed", "attempts": 2}


async def test_verification_runs_outside_the_process_lock(idb, drive, dkim_brevo, monkeypatch):
    seen = []

    async def _verify(eml_token, sender, api_key, recipient_ok):
        seen.append(inbound_email.PROCESS_LOCK.locked())
        return inbound_dkim.Verdict(False, "dkim_invalid")

    monkeypatch.setattr(inbound_dkim, "verify_message", _verify)
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-1"))
    await inbound_queue.run_pending(idb)
    assert seen == [False]
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["reason"] == "dkim_invalid"


async def test_forged_flood_defers_real_mail_which_is_then_filed(
    idb, drive, mails, dkim_brevo, rsa_key
):
    """Security review: a forged flood fills the open slots, the real caregiver
    mail waits deferred (no download, no DNS), is promoted once the forged rows
    are refused, verifies and is filed. Never a 503, never lost."""
    files, emls, fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    forged = sign(raw_mail(to=addr), pem).replace(BODY_MARKER, b"forged body 2027")
    for n in range(inbound_queue.MAX_CLAIMED_PER_SENDER):
        emls[f"forged-{n}"] = forged
        assert (await _post(idb, dkim_item(addr, eml=f"forged-{n}"))).status_code == 202
    files["att-real"] = pdf(7)
    emls["eml-real"] = sign(raw_mail(to=addr), pem)
    real = dkim_item(addr, eml="eml-real", files=[("SynthReal2027.pdf", "att-real")])
    assert (await _post(idb, real)).status_code == 202
    states = [r["state"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert states == ["pending", "pending", "deferred"]
    counters = inbound_email._state.counters
    assert counters["jobs_claimed_deferred_sender"] == 1 and counters["jobs_claimed_dropped"] == 0
    assert fetched == []
    await inbound_queue.run_pending(idb)
    reasons = [r["reason"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert reasons == ["dkim_invalid", "dkim_invalid", "verified"]
    assert counters["claimed_deferred_promoted"] == 1
    assert [f[1] for f in drive.files] == [pdf(7)]
    trusted = [j for j in await _trusted_jobs(idb) if j["trusted"]]
    assert len(trusted) == 1 and trusted[0]["state"] == "done"


async def test_over_an_open_cap_the_item_is_deferred_not_dropped(idb, dkim_brevo, monkeypatch):
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_JOBS_TOTAL", 1)
    addr = await address_of(idb)
    resp = await _post(idb, dkim_item(addr, eml="eml-1"), dkim_item(addr, eml="eml-2"))
    assert resp.status_code == 202
    assert [r["state"] for r in await _rows(idb, "inbound_claimed_jobs")] == [
        "pending",
        "deferred",
    ]
    assert inbound_email._state.counters["jobs_claimed_deferred_total"] == 1


async def test_beyond_the_address_deferred_bound_the_batch_gets_503(
    idb, drive, dkim_brevo, monkeypatch
):
    """Infra review: a patient's real mail is never dropped. Beyond the
    deferred bound of an address bucket the batch answers 503 (nothing
    written) and Brevo retries; what it already holds is a no-op then."""
    monkeypatch.setattr(inbound_queue, "MAX_DEFERRED_PER_SENDER", 1)
    addr = await address_of(idb)
    held = [dkim_item(addr, eml=f"eml-{n}") for n in range(3)]
    for item in held:
        assert (await _post(idb, item)).status_code == 202
    assert [r["state"] for r in await _rows(idb, "inbound_claimed_jobs")] == [
        "pending",
        "pending",
        "deferred",
    ]
    late = dkim_item(addr, eml="eml-late")
    resp = await _post(idb, held[0], late)
    assert resp.status_code == 503 and resp.headers.get("retry-after")
    assert len(await _rows(idb, "inbound_claimed_jobs")) == 3  # nothing written
    assert inbound_email._state.counters["jobs_claimed_full"] == 1
    assert inbound_email._state.counters["jobs_claimed_dropped"] == 0
    await inbound_queue.run_pending(idb)  # the held rows are verified and leave
    assert (await _post(idb, held[0], late)).status_code == 202  # the retry
    assert len(await _rows(idb, "inbound_claimed_jobs")) == 4


async def test_beyond_the_welcome_deferred_bound_the_item_is_dropped(idb, dkim_brevo, monkeypatch):
    monkeypatch.setattr(inbound_queue, "MAX_DEFERRED_PER_SENDER", 1)
    items = [dkim_item(WELCOME, sender="synth.flood@gmail.com", eml=f"eml-{n}") for n in range(4)]
    assert (await _post(idb, *items)).status_code == 202
    states = [r["state"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert states == ["pending", "pending", "deferred"]
    counters = inbound_email._state.counters
    assert counters["jobs_claimed_dropped"] == 1
    assert counters["jobs_claimed_dropped_deferred_sender"] == 1


async def test_welcome_forgeries_of_the_caregiver_never_block_the_patient_address(
    idb, drive, mails, dkim_brevo, rsa_key
):
    """Infra review: the sender caps count per (bucket, sender). Forgeries
    carrying the caregiver's From sent to the public welcome@ fill only the
    welcome slots; the caregiver's real mail to the patient address is
    queued, verified and filed."""
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    bound = inbound_queue.MAX_CLAIMED_PER_SENDER + inbound_queue.MAX_DEFERRED_PER_SENDER
    forgeries = [dkim_item(WELCOME, eml=f"eml-forged-{n}") for n in range(bound + 1)]
    assert (await _post(idb, *forgeries)).status_code == 202
    assert inbound_email._state.counters["jobs_claimed_dropped_deferred_sender"] == 1
    addr = await address_of(idb)
    files["att-real"] = pdf(3)
    emls["eml-real"] = sign(raw_mail(to=addr), pem)
    real = dkim_item(addr, eml="eml-real", files=[("SynthReal2027.pdf", "att-real")])
    assert (await _post(idb, real)).status_code == 202
    async with idb.db.execute(
        "SELECT state FROM inbound_claimed_jobs WHERE bucket <> 'welcome'"
    ) as cur:
        assert [r[0] for r in await cur.fetchall()] == ["pending"]
    await inbound_queue.run_pending(idb)
    assert [f[1] for f in drive.files] == [pdf(3)]


async def test_a_lost_lease_after_verification_skips_the_promotion(
    idb, drive, dkim_brevo, monkeypatch
):
    """Infra review: the lease is touched under the lock before the
    promotion; if another worker took the row over meanwhile, nothing is
    queued and nothing is finished by this worker."""
    calls = {"n": 0}
    real_touch = idb.touch_inbound_claimed_job

    async def _touch(job_id, *, lease, now):
        calls["n"] += 1
        if calls["n"] >= 2:  # the touch before the promotion
            return False
        return await real_touch(job_id, lease=lease, now=now)

    async def _verify(eml_token, sender, api_key, recipient_ok):
        return inbound_dkim.Verdict(True, None, DOMAIN, "s2027")

    monkeypatch.setattr(idb, "touch_inbound_claimed_job", _touch)
    monkeypatch.setattr(inbound_dkim, "verify_message", _verify)
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-1"))
    counts = await inbound_queue.run_pending(idb, limit=1)
    assert counts == {"claimed:lease_lost": 1}
    assert await _trusted_jobs(idb) == []
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["state"] == "processing"


async def test_deferred_row_past_the_retention_ends_download_expired(idb, dkim_brevo, monkeypatch):
    from datetime import timedelta

    addr = await address_of(idb)
    items = [dkim_item(addr, eml=f"eml-{n}") for n in range(3)]
    await _post(idb, *items)
    real_now = inbound_queue._now
    later = timedelta(seconds=inbound_queue.BREVO_ATTACHMENT_RETENTION_S + 60)
    monkeypatch.setattr(inbound_queue, "_now", lambda: real_now() + later)
    await inbound_queue.run_pending(idb)
    rows = await _rows(idb, "inbound_claimed_jobs")
    assert all(r["state"] == "failed" and r["reason"] == "download_expired" for r in rows)
    assert inbound_email._state.counters["claimed_deferred_expired"] == 1


async def test_welcome_flood_leaves_the_address_pools_working(
    idb, drive, mails, dkim_brevo, rsa_key, monkeypatch
):
    """Security review: welcome@ and the address buckets are separate pools,
    open and deferred. A welcome@ flood from many Gmail senders that fills
    both welcome pools leaves address-bucket deferral and verification
    working."""
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_WELCOME_JOBS", 3)
    monkeypatch.setattr(inbound_queue, "MAX_DEFERRED_WELCOME", 3)
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_JOBS_TOTAL", 3)
    monkeypatch.setattr(inbound_queue, "MAX_DEFERRED_TOTAL", 3)
    flood = [dkim_item(WELCOME, sender=f"synth.flood{n}@{DOMAIN}", eml="eml-x") for n in range(8)]
    assert (await _post(idb, *flood)).status_code == 202
    counters = inbound_email._state.counters
    assert counters["jobs_claimed_deferred_welcome"] == 3
    assert counters["jobs_claimed_dropped_deferred_welcome"] == 2
    # the patient's address still gets its open slots, then its deferred ones
    addr = await address_of(idb)
    files["att-real"] = pdf(5)
    emls["eml-real"] = sign(raw_mail(to=addr), pem)
    real = dkim_item(addr, eml="eml-real", files=[("SynthReal2027.pdf", "att-real")])
    assert (await _post(idb, real)).status_code == 202
    async with idb.db.execute(
        "SELECT state FROM inbound_claimed_jobs WHERE bucket <> 'welcome' ORDER BY id"
    ) as cur:
        assert [r[0] for r in await cur.fetchall()] == ["pending"]
    # address-bucket deferral still has room (its own pool)
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_PER_DOMAIN", 0)  # force deferral
    second = dkim_item(addr, eml="eml-second")
    assert (await _post(idb, second)).status_code == 202
    assert counters["jobs_claimed_deferred_domain"] == 1
    monkeypatch.setattr(inbound_queue, "MAX_CLAIMED_PER_DOMAIN", 2)
    await inbound_queue.run_pending(idb)
    assert [f[1] for f in drive.files] == [pdf(5)]


async def test_three_gmail_senders_to_welcome_are_all_queued(idb, dkim_brevo):
    """Maintainer decision: no per-domain cap in the welcome bucket (every
    welcome@ sender is a Gmail user)."""
    senders = [f"synth.onboard{n}@{DOMAIN}" for n in range(3)]
    items = [dkim_item(WELCOME, sender=s, eml=f"eml-{s[:13]}") for s in senders]
    assert (await _post(idb, *items)).status_code == 202
    assert [r["state"] for r in await _rows(idb, "inbound_claimed_jobs")] == ["pending"] * 3
    counters = inbound_email._state.counters
    assert counters["jobs_claimed_deferred"] == 0 and counters["jobs_claimed_dropped"] == 0


async def test_third_open_row_of_one_welcome_sender_is_held_back(idb, dkim_brevo):
    """The per-sender cap counts the canonical sender: Gmail dots and +tag
    variants of one mailbox are one sender. The third waits deferred."""
    variants = ["synth.onboard@gmail.com", "synthonboard@gmail.com", "synth.onboard+x@gmail.com"]
    items = [dkim_item(WELCOME, sender=s, eml=f"eml-v{n}") for n, s in enumerate(variants)]
    assert (await _post(idb, *items)).status_code == 202
    states = [r["state"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert states.count("pending") == inbound_queue.MAX_CLAIMED_PER_SENDER
    assert states.count("deferred") == 1
    assert inbound_email._state.counters["jobs_claimed_deferred_sender"] == 1


async def test_domain_cap_still_applies_to_an_address_bucket(idb):
    caps = {
        "address": 50,
        "welcome": 50,
        "total": 50,
        "sender": 50,
        "domain": 2,
        "deferred_bucket": 0,
        "deferred_sender": 0,
        "deferred_total": 0,
        "deferred_welcome": 0,
    }

    def row(n, bucket):
        return {
            "job_uuid": f"00000000-0000-4000-8000-{n:012d}",
            "bucket": bucket,
            "message_id_hash": f"mid-{n}",
            "sender_hash": f"sender-{n}",
            "domain_hash": "same-domain",
            "delivery_hash": f"delivery-{n}",
            "payload_enc": "v1:synthetic",
        }

    result = await idb.insert_inbound_claimed_jobs(
        [row(n, "token-hash-a") for n in range(2)], caps=caps
    )
    assert result["inserted"] == 2
    # the third is over the domain cap and, with no deferred room, refused
    result = await idb.insert_inbound_claimed_jobs([row(2, "token-hash-a")], caps=caps)
    assert result["inserted"] == 0 and result["over_bound"] == ["token-hash-a"]
    result = await idb.insert_inbound_claimed_jobs(
        [row(n, "welcome") for n in range(10, 13)], caps=caps
    )
    assert result["inserted"] == 3 and result["dropped"] == {} and result["deferred"] == {}


async def test_brevo_retry_of_a_claimed_mail_is_a_no_op(idb, dkim_brevo):
    addr = await address_of(idb)
    item = dkim_item(addr, eml="eml-1", mid="<same-2027@example.test>")
    await _post(idb, item)
    await _post(idb, item)
    assert len(await _rows(idb, "inbound_claimed_jobs")) == 1


async def test_refused_copy_never_suppresses_the_real_delivery(
    idb, drive, mails, dkim_brevo, rsa_key
):
    """Security review: a forged copy with the real mail's Message-ID and From
    is refused first; the real delivery is still queued and verified."""
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    mid = "<shared-2027@example.test>"
    emls["eml-forged"] = sign(raw_mail(to=addr), pem).replace(BODY_MARKER, b"forged 2027")
    await _post(idb, dkim_item(addr, eml="eml-forged", mid=mid))
    await inbound_queue.run_pending(idb)
    files["att-real"] = pdf(9)
    emls["eml-real"] = sign(raw_mail(to=addr), pem)
    real = dkim_item(addr, eml="eml-real", mid=mid, files=[("SynthReal2027.pdf", "att-real")])
    await _post(idb, real)
    await inbound_queue.run_pending(idb)
    reasons = [r["reason"] for r in await _rows(idb, "inbound_claimed_jobs")]
    assert reasons == ["dkim_invalid", "verified"]
    assert [f[1] for f in drive.files] == [pdf(9)]


async def test_claimed_payload_is_encrypted(idb, dkim_brevo):
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-secret-token"))
    async with idb.db.execute("SELECT * FROM inbound_claimed_jobs") as cur:
        row = dict(await cur.fetchone())
    blob = repr(row)
    for clear in (CARER, "eml-secret-token", SUBJECT, addr, DOMAIN):
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
    emls["eml-forged"] = sign(raw_mail(to=WELCOME), pem).replace(BODY_MARKER, b"forged 2027")
    emls["eml-real"] = sign(raw_mail("synth.real@gmail.com", to=WELCOME), pem)
    await _post(
        idb,
        dkim_item(WELCOME, eml="eml-forged"),
        dkim_item(WELCOME, sender="synth.real@gmail.com", eml="eml-real"),
    )
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
    emls["eml-1"] = sign(raw_mail(to=addr), pem)
    emls["eml-2"] = sign(raw_mail(to=addr), pem).replace(BODY_MARKER, b"forged body 2027")
    caplog.set_level(logging.DEBUG)
    await _post(
        idb,
        dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]),
        dkim_item(addr, eml="eml-2"),
    )
    await inbound_queue.run_pending(idb)
    text = caplog.text
    assert "inbound: dkim verdict=pass" in text and f"{DOMAIN}/s2027" in text
    for leaked in (CARER, addr, SUBJECT, BODY_MARKER.decode(), "forged body", "eml-1"):
        assert leaked not in text, leaked
    assert "Synthetic Carer" not in text and "bh=" not in text


def test_dkim_codes_are_in_the_admin_overview():
    overview = inbound_email.unmatched_overview()
    assert overview["verification_mode"] == "dkim"
    for code in inbound_dkim.DKIM_REFUSAL_REASONS:
        assert code in overview["auth_refusals"]


def test_infra_limits():
    assert inbound_dkim.DNS_TIMEOUT_S <= 5
    assert inbound_dkim.DNS_BUDGET_S <= 8
    assert inbound_dkim.MAX_SIGNATURES == 5
    assert inbound_dkim.MIN_RSA_BITS == 1024
    assert {b"rsa-sha256", b"ed25519-sha256"} == inbound_dkim.ACCEPTED_ALGORITHMS
    assert inbound_queue.MAX_CLAIMED_PER_SENDER == 2
    assert inbound_queue.MAX_CLAIMED_PER_DOMAIN == 2


def test_address_card_says_bcc_does_not_work():
    from pathlib import Path

    html = (Path(inbound_dkim.__file__).parent / "dashboard.html").read_text()
    assert 'class="inbound-terms inbound-bcc-note"' in html
    note = html.partition('inbound-bcc-note"')[2].partition("</p>")[0]
    assert "data-sk=" in note and "data-en=" in note
    assert "Bcc" in note and "To (or Cc)" in note


# ── over-bound address rows: the rest of the batch, the 503 budget ──────


async def test_over_bound_row_never_holds_back_another_patients_mail(
    idb, drive, dkim_brevo, monkeypatch
):
    """The rest of the batch is committed before the over-bound address row
    makes it answer 503; Brevo's retry is a no-op for the committed row."""
    from tests.test_950_inbound_address import OTHER_CARER, OTHER_PID

    monkeypatch.setattr(inbound_queue, "MAX_DEFERRED_PER_SENDER", 0)
    addr = await address_of(idb)
    other_addr = await address_of(idb, OTHER_PID)
    for n in range(inbound_queue.MAX_CLAIMED_PER_SENDER):
        assert (await _post(idb, dkim_item(addr, eml=f"eml-held-{n}"))).status_code == 202
    over = dkim_item(addr, eml="eml-over")
    other = dkim_item(other_addr, sender=OTHER_CARER, eml="eml-other", d="example.test")
    resp = await _post(idb, over, other)
    assert resp.status_code == 503
    async with idb.db.execute("SELECT bucket FROM inbound_claimed_jobs ORDER BY id") as cur:
        buckets = [r[0] for r in await cur.fetchall()]
    other_bucket = inbound_email.token_hash(inbound_email.token_from_address(other_addr))
    assert buckets.count(other_bucket) == 1  # committed despite the 503
    assert (await _post(idb, over, other)).status_code == 503  # Brevo's retry
    async with idb.db.execute("SELECT COUNT(*) FROM inbound_claimed_jobs") as cur:
        assert (await cur.fetchone())[0] == len(buckets)  # the retry was a no-op


async def test_over_bound_address_rows_are_never_dropped_and_alerted_after_three(
    idb, dkim_brevo, monkeypatch
):
    """Fail closed: an over-bound address row always gets 503, at any count;
    more than three refusals of one token within the hour raise the alert."""
    monkeypatch.setattr(inbound_queue, "MAX_DEFERRED_PER_SENDER", 0)
    addr = await address_of(idb)
    for n in range(inbound_queue.MAX_CLAIMED_PER_SENDER):
        await _post(idb, dkim_item(addr, eml=f"eml-held-{n}"))
    threshold = inbound_queue.REFUSAL_ALERT_PER_TOKEN
    statuses = []
    for n in range(threshold):
        statuses.append((await _post(idb, dkim_item(addr, eml=f"eml-over-{n}"))).status_code)
    assert statuses == [503] * threshold
    none = {"claimed_address_refusals_high": 0, "bad_tokens_high": 0}
    assert inbound_email.alerts_overview() == none
    for n in range(threshold, threshold + 3):
        statuses.append((await _post(idb, dkim_item(addr, eml=f"eml-over-{n}"))).status_code)
    assert set(statuses) == {503}
    counters = inbound_email._state.counters
    assert counters["jobs_claimed_dropped"] == 0
    assert counters["jobs_claimed_full"] == len(statuses)
    expected = {"claimed_address_refusals_high": 3, "bad_tokens_high": 0}
    assert inbound_email.alerts_overview() == expected
    assert inbound_email.unmatched_overview()["alerts"] == expected
    held = len(await _rows(idb, "inbound_claimed_jobs"))
    assert held == inbound_queue.MAX_CLAIMED_PER_SENDER  # nothing over-bound written
    assert threshold == 3 and inbound_queue.REFUSAL_ALERT_WINDOW_S == 3600


async def test_alerts_are_on_readiness_never_on_health(idb, monkeypatch):
    import json
    from unittest.mock import MagicMock

    from tests.test_950_inbound_address import _request

    inbound_email.note_alert("claimed_address_refusals_high")
    monkeypatch.setattr(server, "_readiness_caller_is_operator", lambda request: True)
    app = MagicMock()
    app.state.fastmcp_server._lifespan_result = {"db": idb}
    req = _request(idb, b"", {})
    req.scope["app"] = app
    body = json.loads((await server.readiness(req)).body)
    assert body["inbound_alerts"] == {"claimed_address_refusals_high": 1, "bad_tokens_high": 0}
    assert "alerts" not in inspect.getsource(server.health)


def test_admin_panel_renders_the_alert():
    from pathlib import Path

    js = (Path(inbound_dkim.__file__).parent / "dashboard.js").read_text()
    assert "claimed_address_refusals_high" in js and "data.alerts" in js


# ── #976: the mail text on claimed rows ─────────────────────────────────


async def _claimed_payloads(db):
    async with db.db.execute(
        "SELECT bucket, job_uuid, payload_enc FROM inbound_claimed_jobs ORDER BY id"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    return [
        inbound_queue.decrypt_payload(
            r["payload_enc"],
            patient_id=inbound_queue._CLAIMED_SCOPE,
            token_hash=r["bucket"],
            job_uuid=r["job_uuid"],
        )
        for r in rows
    ]


async def _job_payloads(db):
    async with db.db.execute(
        "SELECT patient_id, token_hash, job_uuid, payload_enc FROM inbound_jobs ORDER BY id"
    ) as cur:
        rows = [dict(r) for r in await cur.fetchall()]
    return [
        inbound_queue.decrypt_payload(
            r["payload_enc"],
            patient_id=r["patient_id"],
            token_hash=r["token_hash"],
            job_uuid=r["job_uuid"],
        )
        for r in rows
    ]


async def test_claimed_row_carries_the_text_until_verified_then_a_caregiver_row_drops_it(
    idb, drive, mails, dkim_brevo, rsa_key
):
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    files["att-1"] = pdf(1)
    emls["eml-1"] = sign(raw_mail(to=addr), pem)
    await _post(idb, dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]))
    [claimed] = await _claimed_payloads(idb)
    assert claimed["body_text"] == "never stored"  # unverified: kept, encrypted
    await inbound_queue.run_pending(idb)
    [claimed] = await _claimed_payloads(idb)
    assert "body_text" not in claimed  # the row ended: dropped
    [job] = await _job_payloads(idb)
    assert "body_text" not in job  # a verified caregiver row never carries it


async def test_forged_claim_is_quarantined_with_its_text(
    idb, drive, mails, dkim_brevo, rsa_key, monkeypatch, tmp_path
):
    from oncofiles import inbound_quarantine

    monkeypatch.setenv("HOLDING_STORE_SECRET", "synthetic-holding-secret-976")
    monkeypatch.setattr("oncofiles.config.DATA_DIR", str(tmp_path))
    monkeypatch.setattr(inbound_quarantine, "_free_bytes", lambda: 10**15)
    files, emls, _fetched, records = dkim_brevo
    pem, record = rsa_key
    records[key_name()] = record
    addr = await address_of(idb)
    files["att-1"] = pdf(1)
    emls["eml-1"] = sign(raw_mail(to=addr), pem).replace(BODY_MARKER, b"forged body 2027")
    await _post(idb, dkim_item(addr, eml="eml-1", files=[("SynthLab2027.pdf", "att-1")]))
    await inbound_queue.run_pending(idb)
    async with idb.db.execute("SELECT * FROM inbound_quarantine") as cur:
        [item] = [dict(r) for r in await cur.fetchall()]
    assert item["refusal_reason"] == "auth_failed"
    assert inbound_quarantine.body_text(item) == "never stored"
    for payload in [*(await _claimed_payloads(idb)), *(await _job_payloads(idb))]:
        assert "body_text" not in payload


async def test_claimed_row_failed_before_decryption_drops_the_text(idb, dkim_brevo):
    addr = await address_of(idb)
    await _post(idb, dkim_item(addr, eml="eml-1"))
    await idb.db.execute(
        "UPDATE inbound_claimed_jobs SET attempts = ?", (inbound_queue.MAX_ATTEMPTS,)
    )
    await idb.db.commit()
    await inbound_queue.run_pending(idb)
    assert (await _rows(idb, "inbound_claimed_jobs"))[0]["reason"] == "max_attempts"
    [claimed] = await _claimed_payloads(idb)
    assert "body_text" not in claimed
