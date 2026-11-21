"""Onboarding by e-mail via ``welcome@`` (#950 phase 2, Flow B).

A mail to ``welcome@`` (``WELCOME_LOCAL`` at one of ``WELCOME_DOMAINS``) that
carries no patient address token comes through the same Brevo route and
durable queue as phase 1 (``/inbound/brevo`` → ``inbound_queue.enqueue``).
At enqueue, :func:`admit` decides, in this order:

1. **Loop mail** (``inbound_email.loop_reason``): ignored silently.
2. **Auth verdict**: the same fail-closed rule as phase 1 (the trusted
   ``Authentication-Results`` line must say ``dmarc=pass`` aligned with the
   From domain, ``inbound_email._brevo_auth_pass``). A fail is never answered;
   it is listed in the admin zone (reason ``welcome_auth_failed``).
3. **Gmail only** (maintainer decision 2026-11-21: the person later proves the
   address with Google sign-in). Any other domain is listed in the admin zone
   only (``welcome_not_gmail``), nothing else for now.
4. **No attachments**: nothing to read, dropped (``welcome_no_files``).
5. **Per-sender caps** (``MAX_MAILS_PER_SENDER_DAY``,
   ``MAX_FILES_PER_SENDER_DAY``): over them the mail is counted and dropped,
   no reply.
6. **Onboarding gate** (#852, ``onboarding_gate.onboarding_allowed``; admins
   pass): ``closed``, or ``invite`` for a sender not invited, gets at most one
   reply per sender per ``WAITLIST_REPLY_INTERVAL_S`` (24 h) with the waitlist
   link; the files are not kept. ``open`` continues.
7. **Trial patient**, under ``_ADMIT_LOCK`` (no two coroutines create one):
   the sender's existing trial (``trial_accounts.sender_hash``, one per
   sender) or a new one. A sender who already is the caregiver of a patient
   that is not a trial is listed only (``welcome_existing_caregiver``): no
   second account. A new trial needs room under the global
   ``MAX_NEW_TRIALS_PER_DAY`` (counted in the DB, survives restarts). It is
   created with no name from the mail (``TRIAL_DISPLAY_NAME``), a random
   slug, ``caregiver_email`` = the sender's canonical form, the default patient
   type, and in the same transaction a ``patient_consents`` row of kind
   ``by_sending`` (terms version, time, keyed sender hash) and its
   ``trial_accounts`` row (``trial_ends_at`` = now + ``TRIAL_DAYS``). The
   holding store key must be set (``holding_store.configured``) or nothing is
   admitted (fail closed).

The admitted mail is then queued for the trial patient's phase 1 address
token, so the worker runs the ordinary core: the sender rule passes
(caregiver = the sender, auth passed), attachments are screened, and filing
goes to the holding store (``holding_store.holding_mode``). The welcome reply
(:func:`welcome_text`) replaces the receipt: counts, the consent-by-sending
sentence with the terms link, the sign-in link and the personal document
address as text; no names, file names, tokens or signed links; one per
sender per 10 min (``inbound_email.REPLY_INTERVAL_S``) and always
``Auto-Submitted: auto-replied``.

Counters live in memory (lost on restart; the trial cap is in the DB). Logs:
counts, reason codes and ``patient_id[:4]`` only, never an address.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from oncofiles import inbound_email

logger = logging.getLogger(__name__)

WELCOME_LOCAL = "welcome"
WELCOME_DOMAINS = frozenset({"oncofiles.com", inbound_email.INBOUND_DOMAIN})
SIGN_IN_URL = "https://oncofiles.com/dashboard"
WAITLIST_URL = "https://oncofiles.com/#join"
TRIAL_DAYS = 30
TRIAL_SOURCE = "welcome_email"
TRIAL_DISPLAY_NAME = "Moje dokumenty"

MAX_MAILS_PER_SENDER_DAY = 10
MAX_FILES_PER_SENDER_DAY = 40
MAX_NEW_TRIALS_PER_DAY = 20
WAITLIST_REPLY_INTERVAL_S = 24 * 3600

_ADMIT_LOCK = asyncio.Lock()


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.mails: dict[str, list[float]] = {}  # sender hash -> admitted mail times
        self.files: dict[str, list[tuple[float, int]]] = {}  # sender hash -> (time, files)
        self.waitlist_reply: dict[str, float] = {}  # sender hash -> last waitlist reply


_state = _State()


def reset_state() -> None:
    """Tests only."""
    _state.reset()


def _count(reason: str) -> None:
    inbound_email._state.counters[reason] += 1


def is_welcome(msg: inbound_email.InboundMessage) -> bool:
    """True when one recipient is ``welcome@`` at one of our domains."""
    for addr in msg.recipients:
        local, at, domain = parseaddr(addr or "")[1].strip().rpartition("@")
        if not at:
            continue
        if (
            local.split("+", 1)[0].lower() == WELCOME_LOCAL
            and domain.lower().rstrip(".") in WELCOME_DOMAINS
        ):
            return True
    return False


def _sender(msg: inbound_email.InboundMessage) -> str:
    """The sender's canonical identity (security review of #950 phase 2):
    for Gmail lowercase, ``+tag`` dropped, dots removed, googlemail.com =
    gmail.com (``email_identity.canonical_email``), so the variants of one
    mailbox are one sender for the trial, every cap and the caregiver match."""
    from oncofiles.email_identity import canonical_email

    return canonical_email(msg.sender)


def is_gmail(sender: str) -> bool:
    from oncofiles.email_identity import is_gmail as _is_gmail

    return _is_gmail(sender)


def _sender_key(sender: str) -> str | None:
    from oncofiles.secrets_keys import caller_email_hash

    return caller_email_hash(sender)


def _is_admin(sender: str) -> bool:
    from oncofiles import config

    return sender in {str(e).strip().lower() for e in config.DASHBOARD_ADMIN_EMAILS}


def _within_day(times: list, now: float) -> list:
    return [t for t in times if now - (t[0] if isinstance(t, tuple) else t) < 86400]


def _over_sender_caps(key: str, files: int, now: float) -> bool:
    mails = _within_day(_state.mails.get(key, []), now)
    sent = _within_day(_state.files.get(key, []), now)
    _state.mails[key], _state.files[key] = mails, sent
    if len(mails) >= MAX_MAILS_PER_SENDER_DAY:
        return True
    if sum(n for _t, n in sent) + files > MAX_FILES_PER_SENDER_DAY:
        return True
    mails.append(now)
    sent.append((now, files))
    return False


# ── replies ─────────────────────────────────────────────────────────────


def waitlist_text() -> tuple[str, str]:
    """(subject, body) of the closed-gate reply: no name, no file name."""
    subject = "Oncofiles: zatiaľ cez poradovník / waitlist first"
    sk = [
        "Dobrý deň,",
        "",
        "ďakujeme za záujem. Nové účty zatiaľ otvárame postupne, preto sme poslané "
        "súbory neuložili. Zapíšte sa do poradovníka a ozveme sa vám:",
        WAITLIST_URL,
    ]
    en = [
        "Hello,",
        "",
        "thank you for your interest. We are opening new accounts step by step, so the "
        "files you sent were not kept. Join the waitlist and we will let you know:",
        WAITLIST_URL,
    ]
    return subject, "\n".join(sk) + "\n\n---\n\n" + "\n".join(en) + "\n\nOncofiles\n"


def welcome_text(accepted: int, skipped, address: str) -> tuple[str, str]:
    """(subject, body) of the welcome reply: counts and fixed links only.

    Never a name, a file name, a token or a signed link (#834). ``address`` is
    the patient's personal document address (phase 1), as plain text.
    """
    subject = "Oncofiles: dokumenty prijaté / documents received"
    sk = [
        "Dobrý deň,",
        "",
        f"prijali sme {accepted} dokument(ov) a čítame ich.",
    ]
    en = ["Hello,", "", f"we received {accepted} document(s) and are reading them."]
    if skipped:
        sk.append(f"Preskočené: {sum(skipped.values())}")
        en.append(f"Skipped: {sum(skipped.values())}")
        for code, n in sorted(skipped.items()):
            s_sk, s_en = inbound_email.REASON_TEXT.get(code, (code, code))
            sk.append(f"  - {s_sk} ({code}): {n}")
            en.append(f"  - {s_en} ({code}): {n}")
    sk += [
        "",
        "Poslaním dokumentov ste súhlasili s podmienkami používania a so spracovaním "
        "týchto údajov: " + inbound_email.TERMS_URL,
        "",
        "Prihláste sa cez Google touto adresou a uvidíte ich: " + SIGN_IN_URL,
        "",
        "Ďalšie dokumenty posielajte na svoju osobnú adresu: " + address,
        "",
        f"Dokumenty u nás počkajú najviac {TRIAL_DAYS} dní, kým si pripojíte vlastný Google Drive.",
    ]
    en += [
        "",
        "By sending them you agreed to the terms of service and to the processing of "
        "this data: " + inbound_email.TERMS_URL,
        "",
        "Sign in with Google with this address to see them: " + SIGN_IN_URL,
        "",
        "Send further documents to your personal address: " + address,
        "",
        f"The documents wait with us for at most {TRIAL_DAYS} days, until you connect "
        "your own Google Drive.",
    ]
    return subject, "\n".join(sk) + "\n\n---\n\n" + "\n".join(en) + "\n\nOncofiles\n"


async def _send_waitlist(to: str) -> None:
    from oncofiles import mailer

    subject, body = waitlist_text()
    sent = await mailer.send_mail(
        [to],
        subject,
        body,
        tags=["welcome_waitlist"],
        headers={"Auto-Submitted": "auto-replied"},
    )
    _count("welcome_waitlist_sent" if sent else "welcome_waitlist_failed")


def _waitlist_reply(key: str, sender: str, now: float) -> None:
    """At most one waitlist reply per sender per 24 h, sent in the background."""
    last = _state.waitlist_reply.get(key)
    if last is not None and now - last < WAITLIST_REPLY_INTERVAL_S:
        _count("welcome_waitlist_throttled")
        return
    _state.waitlist_reply[key] = now
    task = asyncio.create_task(_send_waitlist(sender))
    inbound_email._BACKGROUND.add(task)
    task.add_done_callback(inbound_email._BACKGROUND.discard)


# ── admission ───────────────────────────────────────────────────────────


def _listed(sender: str, reason: str, files: int) -> None:
    """Listed in the admin zone only (keyed hash, counts), never answered."""
    inbound_email._record_unmatched(sender, "", reason, files)
    _count(reason)
    logger.info("inbound: welcome not admitted reason=%s files=%d", reason, files)


async def _existing_caregiver(db, sender: str) -> bool:
    from oncofiles.persistent_oauth import _email_matches_caregiver

    for patient in await db.list_patients(active_only=False):
        if (
            _email_matches_caregiver(sender, getattr(patient, "caregiver_email", None))
            and await db.get_trial(patient_id=patient.patient_id) is None
        ):
            return True
    return False


async def _create_trial(db, sender: str, key: str) -> str:
    import uuid

    from oncofiles import patient_consent, patient_context
    from oncofiles.models import Patient

    slug = ""
    for _ in range(10):
        candidate = f"trial-{secrets.token_hex(4)}"
        if await db.get_patient_by_slug(candidate) is None:
            slug = candidate
            break
    if not slug:
        raise RuntimeError("no free trial slug")
    ends = datetime.now(UTC) + timedelta(days=TRIAL_DAYS)
    created = await db.insert_patient(
        Patient(
            patient_id=str(uuid.uuid4()),
            slug=slug,
            display_name=TRIAL_DISPLAY_NAME,
            caregiver_email=sender,
            preferred_lang="sk",
        ),
        consent_version=patient_consent.CONSENT_VERSION,
        consent_caller_hash=key,
        consent_by_admin=False,
        consent_kind=patient_consent.CONSENT_KIND_BY_SENDING,
        trial={
            "sender_hash": key,
            "source": TRIAL_SOURCE,
            "trial_ends_at": ends.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )
    pid = created.patient_id
    try:  # the default patient type; the name shown is the generic one
        await patient_context.ensure_loaded(db.db, pid)
        ctx = patient_context.update_context({"name": TRIAL_DISPLAY_NAME}, patient_id=pid)
        await patient_context.save_to_db(db.db, ctx, patient_id=pid)
    except Exception as exc:  # the patient exists; the context is best-effort
        logger.warning("inbound: trial context not saved (%s)", type(exc).__name__)
    _count("welcome_trial_created")
    logger.info("inbound: welcome trial created patient %s", pid[:4])
    return pid


async def admit(db, msg: inbound_email.InboundMessage) -> tuple[str, str] | None:
    """(trial patient id, its address token) for an admitted welcome@ mail,
    else None (dropped, listed or answered with the waitlist; see the module
    docstring for the order)."""
    from oncofiles import holding_store, onboarding_gate

    loop = inbound_email.loop_reason(msg)
    if loop:
        _count(f"welcome_ignored_{loop}")
        logger.info("inbound: welcome ignored reason=%s", loop)
        return None
    sender = _sender(msg)
    files = len(msg.attachments)
    if not msg.auth_pass:
        _listed(sender, "welcome_auth_failed", files)
        return None
    if not is_gmail(sender):
        _listed(sender, "welcome_not_gmail", files)
        return None
    if not files:
        _count("welcome_no_files")
        logger.info("inbound: welcome dropped reason=welcome_no_files")
        return None
    key = _sender_key(sender)
    if not key or not holding_store.configured():
        _count("welcome_unconfigured")
        logger.warning("inbound: welcome refused (sender hash key or HOLDING_STORE_SECRET unset)")
        return None
    now = time.time()
    if _over_sender_caps(key, files, now):
        _count("welcome_sender_capped")
        logger.info("inbound: welcome dropped reason=welcome_sender_capped")
        return None
    as_sent = (msg.sender or "").strip().lower()
    if not any(
        onboarding_gate.onboarding_allowed(a, is_admin=_is_admin(a)) for a in (sender, as_sent)
    ):
        mode = onboarding_gate.onboarding_mode()
        _count(f"welcome_gate_{mode}")
        logger.info("inbound: welcome refused reason=gate mode=%s", mode)
        _waitlist_reply(key, as_sent, now)  # to the address that wrote
        return None
    async with _ADMIT_LOCK:
        trial = await db.get_trial_by_sender_hash(key)
        if trial is not None:
            pid = trial["patient_id"]
        else:
            if await _existing_caregiver(db, sender):
                _listed(sender, "welcome_existing_caregiver", files)
                return None
            since = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            if await db.count_trials_since(since) >= MAX_NEW_TRIALS_PER_DAY:
                _count("welcome_trial_cap")
                logger.info("inbound: welcome dropped reason=welcome_trial_cap")
                return None
            pid = await _create_trial(db, sender, key)
        address = await inbound_email.ensure_address(db, pid)
    token = inbound_email.token_from_address(address["address"])
    if token is None:  # cannot happen: our own address always carries a token
        return None
    _count("welcome_admitted")
    logger.info("inbound: welcome admitted patient %s files=%d", pid[:4], files)
    return pid, token
