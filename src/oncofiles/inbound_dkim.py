"""Sender verification by DKIM on the raw message (#950, Brevo inbound route).

Production finding: Brevo's inbound payload carries NO
``Authentication-Results`` header. It does carry the sender's own
``DKIM-Signature`` and ``ARC-*`` headers (set by the sender's side, so not
trustworthy on their own) and an item key ``EMLDownloadToken``. So the verdict
is our own: the raw message is downloaded and its DKIM signatures are verified
here with ``dkimpy``.

A mail passes ONLY when
* its RFC 5322 ``From`` occurs exactly once (``from_repeated`` otherwise) and
  names exactly one address, equal (case-insensitive) to the Brevo payload's
  ``From`` address (``from_mismatch`` otherwise);
* at least one ``DKIM-Signature`` verifies AND is DMARC-aligned with that From
  domain: ``d=`` equals the From domain, or the From domain is a subdomain of
  ``d=`` (relaxed alignment); never the other way round.

Infra rules (#950 infra review):
* at most ``MAX_SIGNATURES`` (5) signatures are tried per mail, the rest are
  ignored and counted (``dkim_signatures_ignored``): a DoS guard;
* a signature with the ``l=`` body-length tag never counts (``dkim_l_tag``):
  anything could be appended after the signed length;
* only ``rsa-sha256`` and ``ed25519-sha256``; ``rsa-sha1`` = ``dkim_weak_alg``,
  an RSA key under ``MIN_RSA_BITS`` (1024) = ``dkim_weak_key``;
* DNS key lookups time out after ``DNS_TIMEOUT_S`` (5 s); a DNS timeout or
  server failure is TEMPORARY (``TemporaryVerifyError("dns_failed")``, the job
  is retried with the backoff); NXDOMAIN / no record is a permanent
  ``dkim_invalid``;
* the raw EML is capped at ``EML_MAX_BYTES`` = ``inbound_email.MAX_BYTES_PER_MAIL``.

Only signatures whose ``d=`` is aligned with the From domain are looked up in
DNS at all (an unaligned one could never pass), so a forged mail with five
unaligned signatures costs no lookup.

The EML lives in memory only: never written to disk, never logged. Logs carry
reason codes, ``d=`` domains and selector names only, never content or
addresses. dkimpy gets a silent logger of its own.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from email.parser import BytesHeaderParser
from email.policy import compat32
from email.utils import getaddresses

from oncofiles import inbound_email

logger = logging.getLogger(__name__)

# dkimpy logs signature fields at debug level; it never gets our handlers.
_dkim_logger = logging.getLogger("oncofiles.inbound_dkim.dkimpy")
_dkim_logger.addHandler(logging.NullHandler())
_dkim_logger.propagate = False
_dkim_logger.setLevel(logging.CRITICAL + 1)

DNS_TIMEOUT_S = 5.0
MAX_SIGNATURES = 5
MIN_RSA_BITS = 1024
ACCEPTED_ALGORITHMS = frozenset({b"rsa-sha256", b"ed25519-sha256"})
WEAK_ALGORITHMS = frozenset({b"rsa-sha1"})
EML_MAX_BYTES = inbound_email.MAX_BYTES_PER_MAIL
EML_TIMEOUT_S = 60.0

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

# Reason codes of a refused verdict, in the order of the checks (counted in
# ``inbound_email._state.auth_refusals``, admin overview ``auth_refusals``).
DKIM_REFUSAL_REASONS = (
    "eml_unavailable",
    "from_repeated",
    "from_mismatch",
    "dkim_none",
    "dkim_not_aligned",
    "dkim_l_tag",
    "dkim_weak_alg",
    "dkim_weak_key",
    "dkim_invalid",
)
# Among aligned signatures that all failed, the most telling code wins.
_FAILURE_ORDER = ("dkim_invalid", "dkim_weak_key", "dkim_weak_alg", "dkim_l_tag")


class TemporaryVerifyError(Exception):
    """The verdict may differ later (a DNS timeout / server failure, a Brevo
    5xx / 429 / auth error, a transport error, ``BREVO_API_KEY`` unset)."""

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


_LOG_SAFE = re.compile(r"[^a-z0-9._-]")


def _safe(value: str) -> str:
    """A d= / s= value as logged: hostname characters only, at most 64."""
    return _LOG_SAFE.sub("?", value.lower())[:64]


def _domain(addr: str) -> str:
    return addr.rpartition("@")[2].strip().rstrip(".").lower()


def aligned(from_domain: str, d: str) -> bool:
    """Relaxed DMARC alignment: ``d`` equals the From domain or is a parent of
    it; never a child of it. A ``d`` without a dot (a bare TLD) never aligns."""
    from_domain = from_domain.strip().rstrip(".").lower()
    d = d.strip().rstrip(".").lower()
    if not from_domain or not d or "." not in d:
        return False
    return from_domain == d or from_domain.endswith("." + d)


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
            lifetime=min(float(timeout), DNS_TIMEOUT_S),
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


_TAG_SPLIT = re.compile(rb"\s*;\s*")


def _tags(value: bytes) -> dict[bytes, bytes]:
    """Lenient tag=value parse of a DKIM-Signature (only to read a=, d=, s=, l=)."""
    out: dict[bytes, bytes] = {}
    for part in _TAG_SPLIT.split(re.sub(rb"\r?\n[ \t]+", b" ", value).strip()):
        name, sep, val = part.partition(b"=")
        if sep:
            out.setdefault(name.strip().lower(), re.sub(rb"\s+", b"", val))
    return out


def _from_check(raw: bytes, payload_from: str) -> tuple[str | None, str]:
    """(refusal code or None, From domain) of the EML's own From header."""
    try:
        headers = BytesHeaderParser(policy=compat32).parsebytes(raw)
        values = headers.get_all("From") or []
    except Exception:  # noqa: BLE001 - an unreadable header block
        return "from_mismatch", ""
    if len(values) != 1:
        return ("from_repeated" if len(values) > 1 else "from_mismatch"), ""
    addrs = [a for _n, a in getaddresses([str(values[0])]) if a]
    if len(addrs) != 1:
        return ("from_repeated" if len(addrs) > 1 else "from_mismatch"), ""
    addr = addrs[0].strip().lower()
    if not payload_from or addr != payload_from.strip().lower():
        return "from_mismatch", ""
    return None, _domain(addr)


def verify_raw(raw: bytes, payload_from: str) -> Verdict:
    """The DKIM verdict of one raw message (blocking: DNS; run it in a thread).

    Raises ``TemporaryVerifyError("dns_failed")`` when no signature passed and
    a key lookup of an aligned one hit a DNS timeout / server failure.
    """
    import dkim

    code, from_domain = _from_check(raw, payload_from)
    if code:
        return Verdict(False, code)
    try:
        signer = dkim.DKIM(raw, logger=_dkim_logger, minkey=MIN_RSA_BITS, timeout=DNS_TIMEOUT_S)
    except Exception:  # noqa: BLE001 - dkimpy could not parse the message
        return Verdict(False, "dkim_invalid")
    sigs = [v for k, v in signer.headers if k.lower() == b"dkim-signature"]
    if not sigs:
        return Verdict(False, "dkim_none")
    verdict = Verdict(False, None, ignored=max(len(sigs) - MAX_SIGNATURES, 0))
    if verdict.ignored:
        inbound_email._state.counters["dkim_signatures_ignored"] += verdict.ignored
    failures: set[str] = set()
    any_aligned = False
    dns_failed = False

    for idx, value in enumerate(sigs[:MAX_SIGNATURES]):
        tags = _tags(value)
        d = tags.get(b"d", b"").decode("ascii", "replace").lower()
        s = tags.get(b"s", b"").decode("ascii", "replace").lower()
        if not aligned(from_domain, d):
            verdict.tried.append((d, s, "not_aligned"))
            continue
        any_aligned = True
        alg = tags.get(b"a", b"").lower()
        if b"l" in tags:
            outcome = "dkim_l_tag"
        elif alg in WEAK_ALGORITHMS:
            outcome = "dkim_weak_alg"
        elif alg not in ACCEPTED_ALGORITHMS:
            outcome = "dkim_invalid"
        else:
            failed_dns = {"hit": False}

            def dnsfunc(name, timeout=DNS_TIMEOUT_S, _flag=failed_dns):
                try:
                    return _dns_txt(
                        name.decode("ascii", "replace") if isinstance(name, bytes) else name,
                        min(float(timeout), DNS_TIMEOUT_S),
                    )
                except TemporaryVerifyError:
                    _flag["hit"] = True
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
                dns_failed = True
                outcome = "dns_failed"
        verdict.tried.append((d, s, outcome))
        if outcome == "pass":
            verdict.ok, verdict.reason, verdict.domain, verdict.selector = True, None, d, s
            return verdict
        if outcome != "dns_failed":
            failures.add(outcome)
    if dns_failed:
        raise TemporaryVerifyError("dns_failed")
    if not any_aligned:
        verdict.reason = "dkim_not_aligned"
    else:
        verdict.reason = next((c for c in _FAILURE_ORDER if c in failures), "dkim_invalid")
    return verdict


async def fetch_eml(token: str, api_key: str) -> bytes | None:
    """The raw EML, in memory only; None = permanent (404, other 4xx, oversize).

    Pinned host (``api.brevo.com``, the same URL template as the attachment
    fetch), ``api-key`` header, ``EML_TIMEOUT_S``, no redirects, streamed with
    the ``EML_MAX_BYTES`` cap. Raises ``TemporaryVerifyError("fetch_failed")``
    on a 5xx, 401 / 403 / 408 / 425 / 429, a timeout or a transport error.
    """
    import httpx

    if not inbound_email._DOWNLOAD_TOKEN_RE.match(token or ""):
        return None
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
                return None
            declared = resp.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > EML_MAX_BYTES:
                logger.info("inbound: brevo eml over the cap (declared)")
                return None
            chunks, size = [], 0
            async for chunk in resp.aiter_bytes():
                size += len(chunk)
                if size > EML_MAX_BYTES:
                    logger.info("inbound: brevo eml over the cap")
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.TransportError as exc:
        raise TemporaryVerifyError("fetch_failed") from exc


async def verify_message(eml_token: str, payload_from: str, api_key: str) -> Verdict:
    """Download the EML and verify it. Raises ``TemporaryVerifyError``."""
    import asyncio

    if not api_key:
        raise TemporaryVerifyError("no_api_key")
    raw = await fetch_eml(eml_token, api_key) if eml_token else None
    if raw is None:
        return Verdict(False, "eml_unavailable")
    try:
        verdict = await asyncio.to_thread(verify_raw, raw, payload_from)
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
