"""Onboarding by e-mail via ``welcome@`` (#950 phase 2, Flow B).

A mail to the welcome address (``WELCOME_ADDRESSES``: only
``welcome@docs.oncofiles.com``, maintainer decision) that carries no patient
address token comes through the same Brevo route and durable queue as phase 1
(``/inbound/brevo`` → ``inbound_queue.enqueue``). At enqueue, :func:`admit`
decides, in this order:

1. **Loop mail** (``inbound_email.loop_reason``): ignored silently.
2. **Auth verdict**: the same fail-closed rule as phase 1. On the Brevo route
   the verdict is DKIM only (#966): the mail first waits in the claimed
   bucket and :func:`admit` runs only after the queue worker verified its
   DKIM signature (``inbound_dkim``). A fail is never answered; it is listed
   in the admin zone (reason ``welcome_auth_failed``).
3. **Existing caregiver** (maintainer decision): a sender whose canonical
   address is the caregiver of patients that are not trials. Exactly ONE such
   patient: the mail is handed to phase 1 as if it had been sent to that
   patient's personal address (its token; the core's ``sender_refusal`` rule,
   filing through ``dashboard_upload.handle_upload``, the receipt; no gate,
   an existing caregiver keeps working). More than one: nothing is filed, no
   reply, listed in the admin zone as ``caregiver_multiple_patients``.
4. **Gmail only** (the person later proves the address with Google sign-in).
   Any other domain is listed in the admin zone only (``welcome_not_gmail``).
5. **No attachments**: nothing to read, dropped (``welcome_no_files``).
6. **Onboarding gate** (#852, ``onboarding_gate.onboarding_allowed``; admins
   pass): ``closed``, or ``invite`` for a sender not invited, gets at most one
   reply per sender per ``NOT_OPEN_REPLY_INTERVAL_S`` (24 h): the files are
   not kept, the person can try the demo and sign in with Google once their
   account opens (no waitlist wording, aligned with the landing). ``open``
   continues.
7. **Trial patient**, under ``_ADMIT_LOCK`` (no two coroutines create one):
   the sender's existing trial (``trial_accounts.sender_hash``, one per
   canonical sender) or a new one, if the global ``MAX_NEW_TRIALS_PER_DAY``
   allows (counted in the DB). An ended trial (``trial_ends_at`` passed)
   takes no new mail: the reply says to sign in and connect Google Drive
   (one per sender per ``inbound_email.REPLY_INTERVAL_S``). Per-sender caps
   (``MAX_MAILS_PER_SENDER_DAY``, ``MAX_FILES_PER_SENDER_DAY``) are counted
   from the DB (the trial's queued mails and holding documents of the last
   24 h), so they survive a deploy; while the trial is in holding mode the
   core applies the same caps on top of the phase-1 caps to every mail of the
   patient, both routes (:func:`trial_mails_over_cap` + an in-memory count,
   :func:`trial_files_today` = every document of 24 h). Over a cap the mail is
   counted, no reply.
   A new trial has no name from the mail (``TRIAL_DISPLAY_NAME``), a random
   slug, ``caregiver_email`` = the sender's canonical form, the default
   patient type, and in the same transaction a ``patient_consents`` row of
   kind ``by_sending`` (terms version, time, keyed sender hash) and its
   ``trial_accounts`` row (``trial_ends_at`` = now + ``TRIAL_DAYS``). The
   holding store key must be set (``holding_store.configured``) or nothing is
   admitted (fail closed).

The admitted mail is then queued for the trial patient's phase 1 address
token, so the worker runs the ordinary core: the sender rule passes, the
attachments are screened, and filing goes to the holding store
(``holding_store.holding_mode``; its byte and disk limits stay hard whatever
the file caps allow). The welcome reply (:func:`welcome_text`) replaces the
receipt: counts, the consent-by-sending sentence with the terms link, the
sign-in link and the personal document address as text; no names, file
names, tokens or signed links; one per sender per 10 min and always
``Auto-Submitted: auto-replied``.

Logs: counts, reason codes and ``patient_id[:4]`` only, never an address.
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

# ONE constant to narrow or widen: the addresses that start an onboarding.
WELCOME_ADDRESSES = frozenset({f"welcome@{inbound_email.INBOUND_DOMAIN}"})
SIGN_IN_URL = "https://oncofiles.com/dashboard"
DEMO_URL = "https://oncofiles.com/demo"
TRIAL_DAYS = 30
TRIAL_SOURCE = "welcome_email"
TRIAL_DISPLAY_NAME = "Moje dokumenty"

MAX_MAILS_PER_SENDER_DAY = 20
MAX_FILES_PER_SENDER_DAY = 100
MAX_NEW_TRIALS_PER_DAY = 30
NOT_OPEN_REPLY_INTERVAL_S = 24 * 3600

_ADMIT_LOCK = asyncio.Lock()


class _State:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.not_open_reply: dict[str, float] = {}  # sender hash -> last not-open reply
        self.ended_reply: dict[str, float] = {}  # sender hash -> last trial-ended reply


_state = _State()


def reset_state() -> None:
    """Tests only."""
    _state.reset()


def _count(reason: str) -> None:
    inbound_email._state.counters[reason] += 1


def _day_ago() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_welcome_address(addr: str) -> bool:
    """True for a welcome address (case-insensitive, ``+tag`` ignored)."""
    local, at, domain = parseaddr(addr or "")[1].strip().lower().rpartition("@")
    return bool(at) and f"{local.split('+', 1)[0]}@{domain.rstrip('.')}" in WELCOME_ADDRESSES


def is_welcome(msg: inbound_email.InboundMessage) -> bool:
    """True when one recipient is a welcome address (``+tag`` ignored)."""
    return any(is_welcome_address(addr) for addr in msg.recipients)


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


# ── per-sender caps (from the DB; one trial per sender) ─────────────────


async def trial_mails_over_cap(db, patient_id: str, *, including_this: bool) -> bool:
    """The trial's queued mails of the last 24 h against ``MAX_MAILS_PER_SENDER_DAY``.

    ``including_this``: the mail being checked is already a queued row.
    """
    n = await db.count_inbound_jobs_since(patient_id=patient_id, since=_day_ago())
    return n > MAX_MAILS_PER_SENDER_DAY if including_this else n >= MAX_MAILS_PER_SENDER_DAY


async def trial_files_today(db, patient_id: str) -> int:
    """Every document the trial patient got in the last 24 h, on any route and
    in any store (the per-sender file cap)."""
    return await db.count_documents_since(patient_id=patient_id, since=_day_ago())


# ── replies ─────────────────────────────────────────────────────────────


def _bilingual(subject: str, sk: list[str], en: list[str]) -> tuple[str, str]:
    return subject, "\n".join(sk) + "\n\n---\n\n" + "\n".join(en) + "\n\nOncofiles\n"


def not_open_text() -> tuple[str, str]:
    """(subject, body) of the closed-gate reply: no name, no file name, no
    waitlist wording (the landing's: try the demo, sign in with Google)."""
    return _bilingual(
        "Oncofiles: účty otvárame postupne / opening step by step",
        [
            "Dobrý deň,",
            "",
            "ďakujeme za záujem. Nové účty otvárame postupne, preto sme poslané súbory "
            "zatiaľ neuložili.",
            "Vyskúšajte si ukážku s vymyslenou pacientkou: " + DEMO_URL,
            "Keď sa váš účet otvorí, prihláste sa cez Google: " + SIGN_IN_URL,
        ],
        [
            "Hello,",
            "",
            "thank you for your interest. We are opening new accounts step by step, so the "
            "files you sent were not kept for now.",
            "Try the demo with an invented patient: " + DEMO_URL,
            "Once your account opens, sign in with Google: " + SIGN_IN_URL,
        ],
    )


def trial_ended_text() -> tuple[str, str]:
    """(subject, body) for mail to an ended trial: sign in, connect Drive."""
    return _bilingual(
        "Oncofiles: skúšobná doba skončila / trial ended",
        [
            "Dobrý deň,",
            "",
            "skúšobná doba sa skončila, preto sme poslané súbory neuložili. Prihláste sa "
            "cez Google a pripojte vlastný Google Drive, potom môžete dokumenty posielať "
            "znova: " + SIGN_IN_URL,
        ],
        [
            "Hello,",
            "",
            "the trial has ended, so the files you sent were not kept. Sign in with Google "
            "and connect your own Google Drive, then you can send documents again: " + SIGN_IN_URL,
        ],
    )


def welcome_text(accepted: int, skipped, address: str) -> tuple[str, str]:
    """(subject, body) of the welcome reply: counts and fixed links only.

    Never a name, a file name, a token or a signed link (#834). ``address`` is
    the patient's personal document address (phase 1), as plain text.
    """
    sk = ["Dobrý deň,", "", f"prijali sme {accepted} dokument(ov) a čítame ich."]
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
    return _bilingual("Oncofiles: dokumenty prijaté / documents received", sk, en)


async def _send(to: str, text: tuple[str, str], tag: str) -> None:
    from oncofiles import mailer

    subject, body = text
    sent = await mailer.send_mail(
        [to], subject, body, tags=[tag], headers={"Auto-Submitted": "auto-replied"}
    )
    _count(f"{tag}_sent" if sent else f"{tag}_failed")


def _reply_once(book: dict, key: str, interval: float, to: str, text, tag: str) -> None:
    """At most one such reply per sender per ``interval``, sent in the background."""
    now = time.time()
    last = book.get(key)
    if last is not None and now - last < interval:
        _count(f"{tag}_throttled")
        return
    book[key] = now
    task = asyncio.create_task(_send(to, text, tag))
    inbound_email._BACKGROUND.add(task)
    task.add_done_callback(inbound_email._BACKGROUND.discard)


# ── admission ───────────────────────────────────────────────────────────


def _listed(sender: str, reason: str, files: int) -> None:
    """Listed in the admin zone only (keyed hash prefix, code, counts), never answered."""
    inbound_email._record_unmatched(sender, "", reason, files)
    _count(reason)
    logger.info("inbound: welcome not admitted reason=%s files=%d", reason, files)


async def _caregiver_patients(db, sender: str) -> list[str]:
    """Ids of the non-trial patients whose caregiver is ``sender`` (canonical).

    A converted trial (#950 phase 3: its held files are in Drive) counts as an
    ordinary patient: its welcome@ mail goes to its own address (phase 1)."""
    from oncofiles.persistent_oauth import _email_matches_caregiver

    out = []
    for patient in await db.list_patients(active_only=False):
        if not _email_matches_caregiver(sender, getattr(patient, "caregiver_email", None)):
            continue
        trial = await db.get_trial(patient_id=patient.patient_id)
        if trial is None or trial.get("converted_at"):
            out.append(patient.patient_id)
    return out


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


async def _token_of(db, pid: str) -> str | None:
    address = await inbound_email.ensure_address(db, pid)
    return inbound_email.token_from_address(address["address"])


async def admit(db, msg: inbound_email.InboundMessage) -> tuple[str, str, bool] | None:
    """(patient id, its address token, welcome?) for an admitted welcome@ mail,
    else None (dropped, listed or answered; see the module docstring for the
    order). ``welcome`` is False when the mail is handed to an existing
    caregiver's patient (phase 1 receipt), True for a trial (welcome reply)."""
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
    # An existing caregiver (maintainer decision): handed to phase 1, unchanged rules.
    caring = await _caregiver_patients(db, sender)
    if len(caring) > 1:
        _listed(sender, "caregiver_multiple_patients", files)
        return None
    if len(caring) == 1:
        pid = caring[0]
        if await inbound_email.sender_refusal(db, pid, msg) is not None:
            _listed(sender, "welcome_auth_failed", files)  # cannot pass phase 1 either
            return None
        token = await _token_of(db, pid)
        if token is None:
            return None
        _count("welcome_to_existing_patient")
        logger.info("inbound: welcome handed to patient %s (phase 1)", pid[:4])
        return pid, token, False
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
    as_sent = (msg.sender or "").strip().lower()
    if not any(
        onboarding_gate.onboarding_allowed(a, is_admin=_is_admin(a)) for a in (sender, as_sent)
    ):
        mode = onboarding_gate.onboarding_mode()
        _count(f"welcome_gate_{mode}")
        logger.info("inbound: welcome refused reason=gate mode=%s", mode)
        _reply_once(
            _state.not_open_reply,
            key,
            NOT_OPEN_REPLY_INTERVAL_S,
            as_sent,
            not_open_text(),
            "welcome_not_open",
        )
        return None
    async with _ADMIT_LOCK:
        trial = await db.get_trial_by_sender_hash(key)
        if trial is not None:
            pid = trial["patient_id"]
            if not holding_store.trial_active(trial):
                _count("welcome_trial_ended")
                logger.info("inbound: welcome refused reason=trial_ended patient %s", pid[:4])
                _reply_once(
                    _state.ended_reply,
                    key,
                    inbound_email.REPLY_INTERVAL_S,
                    as_sent,
                    trial_ended_text(),
                    "welcome_trial_ended",
                )
                return None
            if await trial_mails_over_cap(db, pid, including_this=False) or (
                await trial_files_today(db, pid) + files > MAX_FILES_PER_SENDER_DAY
            ):
                _count("welcome_sender_capped")
                logger.info("inbound: welcome dropped reason=welcome_sender_capped")
                return None
        else:
            if files > MAX_FILES_PER_SENDER_DAY:
                _count("welcome_sender_capped")
                return None
            if await db.count_trials_since(_day_ago()) >= MAX_NEW_TRIALS_PER_DAY:
                _count("welcome_trial_cap")
                logger.info("inbound: welcome dropped reason=welcome_trial_cap")
                return None
            pid = await _create_trial(db, sender, key)
        token = await _token_of(db, pid)
    if token is None:  # cannot happen: our own address always carries a token
        return None
    _count("welcome_admitted")
    logger.info("inbound: welcome admitted patient %s files=%d", pid[:4], files)
    return pid, token, True
