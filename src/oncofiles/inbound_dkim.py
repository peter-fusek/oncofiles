"""Sender verification by DKIM on the raw message (#950, Brevo inbound route).

Production finding: Brevo's inbound payload carries NO
``Authentication-Results`` header. It does carry the sender's own
``DKIM-Signature`` and ``ARC-*`` headers (set by the sender's side, so not
trustworthy on their own) and an item key ``EMLDownloadToken``. So the verdict
is our own and DKIM is the ONLY source of it: the raw message is downloaded
and its DKIM signatures are verified here with ``dkimpy``. (Any
Authentication-Results header in a Brevo payload was written by the sender,
so it never feeds a verdict.)

A mail passes ONLY when all of this holds:

* exactly one ``From`` header in the parsed message (read from dkimpy's own
  header list, i.e. the headers it hashed) naming exactly one address, equal
  (case-insensitive) to the Brevo payload's ``From`` (``from_repeated`` /
  ``from_mismatch``);
* a ``DKIM-Signature`` that dkimpy verifies, whose ``d=`` (as dkimpy parsed
  and verified it) is aligned with the From domain: ``d`` equals the From
  domain or is a parent of it, never a child or a sibling, and never a public
  suffix (checked against the Public Suffix List bundled in
  ``publicsuffixlist``, no network fetch);
* replay guards (security review): that signature's ``h=`` covers ``From``,
  ``To`` and ``Date`` (``dkim_h_incomplete``); the one ``To`` (and the one
  ``Cc``, when signed) names the inbound address the mail was delivered to
  (``recipient_not_signed``); the one ``Date`` lies within
  ``DATE_MAX_AGE_S`` in the past and ``DATE_MAX_SKEW_S`` in the future
  (``stale_date``); an ``x=`` expiry, when present, has not passed
  (``dkim_expired``). A genuine signed mail of the caregiver to someone else,
  or an old one, is therefore not accepted when re-sent to our address.

Parser safety (security review): every signature's tags are read with
dkimpy's own ``dkim.util.parse_tag_value`` on the exact bytes dkimpy verifies.
A duplicate tag or a tag name that is not all lower-case (``D=``) refuses the
signature (``dkim_malformed``) before anything else, and after a pass the
``d=`` / ``s=`` dkimpy verified must equal the pre-checked ones (else
``dkim_malformed``); alignment is decided on the domain dkimpy verified.

Infra rules (#950 infra review):

* at most ``MAX_SIGNATURES`` signatures are tried per mail, the rest are
  ignored and counted (``dkim_signatures_ignored``);
* only signatures aligned with the From are looked up in DNS;
* ``l=`` never counts (``dkim_l_tag``);
* only ``rsa-sha256`` and ``ed25519-sha256``; ``rsa-sha1`` =
  ``dkim_weak_alg``, an RSA key under ``MIN_RSA_BITS`` = ``dkim_weak_key``;
* each key lookup waits at most ``DNS_TIMEOUT_S`` and all lookups of one mail
  together at most ``DNS_BUDGET_S``; a timeout, a server failure or an
  exhausted budget is TEMPORARY (``TemporaryVerifyError("dns_failed")``);
  NXDOMAIN / no record is a permanent ``dkim_invalid``;
* the raw EML is capped at ``EML_MAX_BYTES`` (base64 overhead over
  ``MAX_BYTES_PER_MAIL`` plus headroom); over it = ``eml_too_large``.

The EML lives in memory only: never written to disk, never logged. Logs carry
reason codes, ``d=`` domains and selector names only (sanitized), never
content or addresses. dkimpy gets a silent logger of its own.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import getaddresses, parsedate_to_datetime

from oncofiles import inbound_email

logger = logging.getLogger(__name__)

# dkimpy logs signature fields at debug level; it never gets our handlers.
_dkim_logger = logging.getLogger("oncofiles.inbound_dkim.dkimpy")
_dkim_logger.addHandler(logging.NullHandler())
_dkim_logger.propagate = False
_dkim_logger.setLevel(logging.CRITICAL + 1)

DNS_TIMEOUT_S = 5.0
DNS_BUDGET_S = 8.0
MAX_SIGNATURES = 5
MIN_RSA_BITS = 1024
ACCEPTED_ALGORITHMS = frozenset({b"rsa-sha256", b"ed25519-sha256"})
WEAK_ALGORITHMS = frozenset({b"rsa-sha1"})
# A mail of MAX_BYTES_PER_MAIL attachments is about 4/3 larger as base64 in
# the EML, plus headers and bodies (infra decision: 1.4 x + 1 MiB).
EML_MAX_BYTES = int(1.4 * inbound_email.MAX_BYTES_PER_MAIL) + 1024 * 1024
EML_TIMEOUT_S = 60.0
# Replay guards: the signed Date must be this fresh.
DATE_MAX_AGE_S = 7 * 24 * 3600
DATE_MAX_SKEW_S = 24 * 3600
REQUIRED_SIGNED_HEADERS = (b"from", b"to", b"date")

# The raw message. NOT documented by Brevo (checked 2026-11-21): the inbound
# parsing guide https://developers.brevo.com/docs/inbound-parse-webhooks and
# the API index https://developers.brevo.com/llms.txt list no EML endpoint and
# never name ``EMLDownloadToken``. The only documented inbound download is
# ``GET https://api.brevo.com/v3/inbound/attachments/{downloadToken}`` with the
# ``api-key`` header (https://developers.brevo.com/reference/get-inbound-email-attachment,
# "Retrieve inbound attachment with download token"), and the production
# payload's item key ``EMLDownloadToken`` is a token of that same kind, so it
# is fetched from that endpoint. To be confirmed on the first live mail: a 404
# here on every mail (``eml_unavailable`` in the admin overview) means Brevo
# serves the EML elsewhere.
BREVO_EML_URL = inbound_email.BREVO_ATTACHMENT_URL
BREVO_EML_TOKEN_KEY = "EMLDownloadToken"

# Reason codes of a refused verdict (counted in
# ``inbound_email._state.auth_refusals``, admin overview ``auth_refusals``).
DKIM_REFUSAL_REASONS = (
    "eml_unavailable",
    "eml_too_large",
    "from_repeated",
    "from_mismatch",
    "dkim_none",
    "dkim_not_aligned",
    "dkim_malformed",
    "dkim_l_tag",
    "dkim_weak_alg",
    "dkim_weak_key",
    "dkim_expired",
    "dkim_invalid",
    "dkim_h_incomplete",
    "recipient_not_signed",
    "stale_date",
)
# Among aligned signatures that all failed, the most telling code wins (a
# replay guard means the signature itself was good).
_FAILURE_ORDER = (
    "recipient_not_signed",
    "stale_date",
    "dkim_h_incomplete",
    "dkim_expired",
    "dkim_malformed",
    "dkim_invalid",
    "dkim_weak_key",
    "dkim_weak_alg",
    "dkim_l_tag",
)
_DOMAIN_RE = re.compile(rb"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_SELECTOR_RE = re.compile(rb"^[A-Za-z0-9._-]{1,128}$")


class TemporaryVerifyError(Exception):
    """The verdict may differ later (a DNS timeout / server failure or an
    exhausted DNS budget, a Brevo 5xx / 429 / auth error, a transport error,
    ``BREVO_API_KEY`` unset)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Verdict:
    ok: bool
    reason: str | None = None  # a member of DKIM_REFUSAL_REASONS when not ok
    domain: str = ""  # the d= of the passing signature
    selector: str = ""
    tried: list[tuple[str, str, str]] = field(default_factory=list)  # (d, s, outcome)
    ignored: int = 0  # signatures over MAX_SIGNATURES


def _now() -> float:
    """Wall clock for the Date / x= checks (tests move it)."""
    return time.time()


_LOG_SAFE = re.compile(r"[^a-z0-9._-]")


def _safe(value: str) -> str:
    """A d= / s= value as logged: hostname characters only, at most 64."""
    return _LOG_SAFE.sub("?", value.lower())[:64]


def _domain(addr: str) -> str:
    return addr.rpartition("@")[2].strip().rstrip(".").lower()


_psl = None


def org_domain(domain: str) -> str | None:
    """The organisational domain (registrable domain per the bundled Public
    Suffix List), or None for a public suffix / an empty or invalid name."""
    global _psl
    domain = (domain or "").strip().rstrip(".").lower()
    if not domain or "." not in domain:
        return None
    if _psl is None:
        from publicsuffixlist import PublicSuffixList

        _psl = PublicSuffixList()  # the list bundled with the package; no fetch
    try:
        return _psl.privatesuffix(domain)
    except Exception:  # noqa: BLE001 - an odd name never aligns
        return None


def aligned(from_domain: str, d: str) -> bool:
    """``d`` equals the From domain, or is a parent of it (the From ends with
    ``"." + d``); never a child or a sibling of it (security review of #966:
    organisational-domain alignment would let any sibling subdomain sign).
    ``d`` must never be a public suffix (the bundled Public Suffix List is used
    only for that)."""
    from_domain = (from_domain or "").strip().rstrip(".").lower()
    d = (d or "").strip().rstrip(".").lower()
    if not from_domain or not d or org_domain(d) is None:
        return False
    return from_domain == d or from_domain.endswith("." + d)


def signature_tags(value: bytes) -> dict[bytes, bytes] | None:
    """The tags of one DKIM-Signature value, parsed by dkimpy itself, or None
    (fail closed) on a parse error, a duplicate tag or a tag name that is not
    all lower-case (``D=``, ``Bh=``): RFC 6376 tag names are case-sensitive
    and every defined one is lower-case, so a variant could only ever serve
    to make two parsers disagree."""
    from dkim.util import parse_tag_value

    try:
        tags = parse_tag_value(value)
    except Exception:  # noqa: BLE001 - DuplicateTag, InvalidTagSpec, ...
        return None
    if any(k != k.lower() for k in tags):
        return None
    return tags


def payload_prefilter(values: list[str], sender: str) -> str | None:
    """Refusal code from the payload's own ``DKIM-Signature`` header values
    (which can only REFUSE, never pass): ``dkim_none`` without any,
    ``dkim_not_aligned`` when none has a ``d=`` aligned with the From domain.
    None = worth downloading and verifying."""
    if not values:
        return "dkim_none"
    from_domain = _domain(sender)
    for value in values:
        tags = signature_tags(value.encode("utf-8", "replace"))
        d = (tags or {}).get(b"d", b"").strip()
        if d and _DOMAIN_RE.match(d) and aligned(from_domain, d.decode("ascii").lower()):
            return None
    return "dkim_not_aligned"


def _dns_txt(name: str, timeout: float = DNS_TIMEOUT_S) -> bytes | None:
    """TXT record of ``name`` (bytes), None when there is none.

    Raises ``TemporaryVerifyError("dns_failed")`` on a timeout, a server
    failure or a missing resolver configuration. Tests replace this function.
    """
    import dns.exception
    import dns.resolver

    try:
        answer = dns.resolver.resolve(
            name,
            "TXT",
            raise_on_no_answer=False,
            lifetime=max(0.1, min(float(timeout), DNS_TIMEOUT_S)),
            search=False,
        )
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return None
    except (
        dns.exception.Timeout,
        dns.resolver.NoNameservers,
        dns.resolver.NoResolverConfiguration,
        dns.resolver.LifetimeTimeout,
    ) as exc:
        raise TemporaryVerifyError("dns_failed") from exc
    except dns.exception.DNSException:
        return None
    if answer.rrset is None:
        return None
    for rdata in answer.rrset:
        return b"".join(rdata.strings)
    return None


def _unfold(value: bytes) -> str:
    text = value.decode("utf-8", "replace")
    return re.sub(r"\r?\n[ \t]+", " ", text).strip()


def _single(headers: list[tuple[bytes, str]], name: bytes) -> list[str]:
    return [v for n, v in headers if n == name]


def _replay_refusal(
    headers: list[tuple[bytes, str]],
    signed: tuple[bytes, ...],
    recipient_ok: Callable[[str], bool],
) -> str | None:
    """The replay guards on the message headers dkimpy hashed (see the module
    docstring); ``signed`` is the verified signature's ``h=`` list."""
    if any(name not in signed for name in REQUIRED_SIGNED_HEADERS):
        return "dkim_h_incomplete"
    to = _single(headers, b"to")
    cc = _single(headers, b"cc") if b"cc" in signed else []
    if len(to) != 1 or len(cc) > 1:
        return "recipient_not_signed"  # an added copy could be the unsigned one
    if not any(recipient_ok(addr) for _n, addr in getaddresses(to + cc) if addr):
        return "recipient_not_signed"
    dates = _single(headers, b"date")
    if len(dates) != 1:
        return "stale_date"
    try:
        sent = parsedate_to_datetime(dates[0]).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return "stale_date"
    now = _now()
    if sent < now - DATE_MAX_AGE_S or sent > now + DATE_MAX_SKEW_S:
        return "stale_date"
    return None


def verify_raw(
    raw: bytes,
    payload_from: str,
    recipient_ok: Callable[[str], bool],
    *,
    budget_s: float = DNS_BUDGET_S,
) -> Verdict:
    """The DKIM verdict of one raw message (blocking: DNS; run it in a thread).

    ``recipient_ok(address)`` says whether an address is the inbound address
    this mail was delivered to. Raises ``TemporaryVerifyError("dns_failed")``
    when no signature passed and a key lookup of an aligned one hit a DNS
    timeout / server failure or the mail's DNS budget ran out.
    """
    import dkim

    try:
        signer = dkim.DKIM(raw, logger=_dkim_logger, minkey=MIN_RSA_BITS, timeout=DNS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - dkimpy could not parse the message
        return Verdict(False, "dkim_invalid")
    # The headers exactly as dkimpy parsed (and hashes) them.
    headers = [(bytes(n).lower(), _unfold(bytes(v))) for n, v in signer.headers]
    froms = _single(headers, b"from")
    if len(froms) != 1:
        return Verdict(False, "from_repeated" if froms else "from_mismatch")
    addrs = [a for _n, a in getaddresses(froms) if a]
    if len(addrs) != 1:
        return Verdict(False, "from_repeated" if addrs else "from_mismatch")
    if not payload_from or addrs[0].strip().lower() != payload_from.strip().lower():
        return Verdict(False, "from_mismatch")
    from_domain = _domain(addrs[0])
    sigs = [bytes(v) for n, v in signer.headers if bytes(n).lower() == b"dkim-signature"]
    if not sigs:
        return Verdict(False, "dkim_none")
    verdict = Verdict(False, None, ignored=max(len(sigs) - MAX_SIGNATURES, 0))
    if verdict.ignored:
        inbound_email._state.counters["dkim_signatures_ignored"] += verdict.ignored
    failures: set[str] = set()
    dns_failed = False
    deadline = time.monotonic() + budget_s

    for idx, value in enumerate(sigs[:MAX_SIGNATURES]):
        tags = signature_tags(value)
        d_raw = (tags or {}).get(b"d", b"").strip()
        s_raw = (tags or {}).get(b"s", b"").strip()
        if tags is None or not _DOMAIN_RE.match(d_raw) or not _SELECTOR_RE.match(s_raw):
            verdict.tried.append(("", "", "dkim_malformed"))
            failures.add("dkim_malformed")
            continue
        d, s = d_raw.decode("ascii").lower(), s_raw.decode("ascii").lower()
        if not aligned(from_domain, d):
            verdict.tried.append((d, s, "not_aligned"))
            continue
        alg = tags.get(b"a", b"").strip().lower()
        signed = tuple(h.strip().lower() for h in tags.get(b"h", b"").split(b":"))
        expiry = tags.get(b"x", b"").strip()
        if b"l" in tags:
            outcome = "dkim_l_tag"
        elif alg in WEAK_ALGORITHMS:
            outcome = "dkim_weak_alg"
        elif alg not in ACCEPTED_ALGORITHMS:
            outcome = "dkim_invalid"
        elif expiry and (not expiry.isdigit() or int(expiry) < _now()):
            outcome = "dkim_expired" if expiry.isdigit() else "dkim_malformed"
        elif any(name not in signed for name in REQUIRED_SIGNED_HEADERS):
            outcome = "dkim_h_incomplete"  # never worth a lookup
        elif deadline - time.monotonic() <= 0:
            outcome = "dns_failed"  # the mail's DNS budget is used up
        else:
            outcome = _verify_one(signer, idx, d_raw, s_raw, from_domain, deadline)
            if outcome == "pass":
                outcome = _replay_refusal(headers, signed, recipient_ok) or "pass"
        verdict.tried.append((d, s, outcome))
        if outcome == "pass":
            verdict.ok, verdict.reason, verdict.domain, verdict.selector = True, None, d, s
            return verdict
        if outcome == "dns_failed":
            dns_failed = True
        else:
            failures.add(outcome)
    if dns_failed:
        raise TemporaryVerifyError("dns_failed")
    verdict.reason = next((c for c in _FAILURE_ORDER if c in failures), "dkim_not_aligned")
    return verdict


def _verify_one(signer, idx: int, d_raw: bytes, s_raw: bytes, from_domain: str, deadline) -> str:
    """dkimpy's verification of signature ``idx``; ``pass`` or a code."""
    import dkim

    failed_dns = {"hit": False}

    def dnsfunc(name, timeout=DNS_TIMEOUT_S):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failed_dns["hit"] = True
            return None
        try:
            return _dns_txt(
                name.decode("ascii", "replace") if isinstance(name, bytes) else name,
                min(float(timeout), DNS_TIMEOUT_S, remaining),
            )
        except TemporaryVerifyError:
            failed_dns["hit"] = True
            return None

    try:
        ok = bool(signer.verify(idx=idx, dnsfunc=dnsfunc))
        outcome = "pass" if ok else "dkim_invalid"
        if ok and signer.ktag == b"rsa" and (signer.keysize or 0) < MIN_RSA_BITS:
            outcome = "dkim_weak_key"
    except dkim.KeyFormatError as exc:
        outcome = "dkim_weak_key" if "too small" in str(exc) else "dkim_invalid"
    except Exception:  # noqa: BLE001 - any malformed signature / key
        outcome = "dkim_invalid"
    if failed_dns["hit"] and outcome != "pass":
        return "dns_failed"
    if outcome == "pass":
        # Parser differential guard: what dkimpy verified must be what we
        # pre-checked, and alignment is decided on dkimpy's own domain.
        verified_d = bytes(getattr(signer, "domain", b"") or b"")
        verified_s = bytes(getattr(signer, "selector", b"") or b"")
        if verified_d != d_raw or verified_s != s_raw:
            return "dkim_malformed"
        if not aligned(from_domain, verified_d.decode("ascii", "replace").lower()):
            return "dkim_malformed"
    return outcome


async def fetch_eml(token: str, api_key: str) -> bytes | str:
    """The raw EML in memory, or a permanent refusal code: ``eml_unavailable``
    (404, other 4xx, bad token) or ``eml_too_large`` (over ``EML_MAX_BYTES``).

    Pinned host (``api.brevo.com``, the same URL template as the attachment
    fetch), ``api-key`` header, ``EML_TIMEOUT_S``, no redirects, streamed with
    the cap. Raises ``TemporaryVerifyError("fetch_failed")`` on a 5xx, 401 /
    403 / 408 / 425 / 429, a timeout or a transport error.
    """
    import httpx

    if not inbound_email._DOWNLOAD_TOKEN_RE.match(token or ""):
        return "eml_unavailable"
    url = BREVO_EML_URL.format(token=token)
    try:
        async with (
            httpx.AsyncClient(timeout=EML_TIMEOUT_S, follow_redirects=False) as client,
            client.stream("GET", url, headers={"api-key": api_key}) as resp,
        ):
            if resp.status_code != 200:
                logger.info("inbound: brevo eml fetch status=%d", resp.status_code)
                if (
                    resp.status_code >= 500
                    or resp.status_code in inbound_email._BREVO_TEMPORARY_STATUS
                ):
                    raise TemporaryVerifyError("fetch_failed")
                return "eml_unavailable"
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > EML_MAX_BYTES:
                logger.info("inbound: brevo eml over the cap (declared)")
                return "eml_too_large"
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > EML_MAX_BYTES:
                    logger.info("inbound: brevo eml over the cap")
                    return "eml_too_large"
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.TransportError as exc:
        raise TemporaryVerifyError("fetch_failed") from exc


async def verify_message(
    eml_token: str,
    payload_from: str,
    api_key: str,
    recipient_ok: Callable[[str], bool],
) -> Verdict:
    """Download the EML and verify it. Raises ``TemporaryVerifyError``.

    Network only (download + DNS in a worker thread); no DB call, so the
    queue runs it outside ``PROCESS_LOCK``.
    """
    import asyncio

    if not api_key:
        raise TemporaryVerifyError("no_api_key")
    raw = await fetch_eml(eml_token, api_key) if eml_token else "eml_unavailable"
    if isinstance(raw, str):
        return Verdict(False, raw)
    try:
        verdict = await asyncio.to_thread(verify_raw, raw, payload_from, recipient_ok)
    finally:
        del raw
    logger.info(
        "inbound: dkim verdict=%s reason=%s signatures=%s ignored=%d",
        "pass" if verdict.ok else "fail",
        verdict.reason or "-",
        ",".join(f"{_safe(d) or '-'}/{_safe(s) or '-'}:{o}" for d, s, o in verdict.tried) or "-",
        verdict.ignored,
    )
    return verdict
