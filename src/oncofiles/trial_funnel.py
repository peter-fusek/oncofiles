"""Welcome@ trials: the "ready" e-mail and the time-to-value funnel (#950 phase 3).

"Ready" e-mail
--------------
When a trial patient's first batch of held documents has been read (at least
one ``done`` and nothing left ``pending`` / ``reading`` for the holding
reader), ONE e-mail goes to the trial's address (its caregiver e-mail, the
address that sent the documents): the number of documents ready and the
dashboard link. Never a name, a file name, a token or a signed link (#834).
``Auto-Submitted: auto-generated`` (RFC 3834: it answers no mail).

* Once per trial: ``trial_accounts.ready_mail_sent_at`` is claimed with a
  conditional UPDATE before the send (two runs never both send); a send the
  relay refused clears the claim so a later run tries again, at most once per
  ``READY_RETRY_S`` per trial.
* Rate-limited: at most ``READY_MAILS_PER_HOUR`` ready mails in all per hour
  (in memory, per process); the rest wait for the next run.
* Sent by the holding reader right after a run that read something, and by
  the ``holding_move`` lifespan loop (catch-up, #999).

Funnel
------
:func:`funnel` turns ``db.trial_funnel_rows()`` into counts and medians for the
admin zone: ``received → first_reply → signed_in → first_doc_ready →
first_mcp_call → drive_connected`` (+ ``converted``). Per step: how many
trials reached it, the median seconds from ``received`` and from the previous
step (over the trials that have both). Instants only, never an id, an
address, a name or a file.

Logs: counts, codes and ``patient_id[:4]`` only.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from statistics import median

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://oncofiles.com/dashboard"
READY_MAILS_PER_HOUR = 20
READY_RETRY_S = 3600

FUNNEL_STEPS = (
    "received",
    "first_reply",
    "signed_in",
    "first_doc_ready",
    "first_mcp_call",
    "drive_connected",
)

_sent_times: list[float] = []  # monotonic times of ready mails in the last hour
_last_failed: dict[str, float] = {}  # patient id -> monotonic time of a refused send


def reset_state() -> None:
    """Tests only."""
    _sent_times.clear()
    _last_failed.clear()


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def ready_text(done: int) -> tuple[str, str]:
    """(subject, body) of the ready e-mail: one count and the dashboard link."""
    sk = [
        "Dobrý deň,",
        "",
        f"prečítali sme dokumenty, ktoré ste poslali. Pripravené: {done}.",
        "",
        "Prihláste sa cez Google adresou, z ktorej ste ich poslali: " + DASHBOARD_URL,
    ]
    en = [
        "Hello,",
        "",
        f"we have read the documents you sent. Ready: {done}.",
        "",
        "Sign in with Google with the address you sent them from: " + DASHBOARD_URL,
    ]
    body = "\n".join(sk) + "\n\n---\n\n" + "\n".join(en) + "\n\nOncofiles\n"
    return "Oncofiles: dokumenty sú pripravené / documents are ready", body


def _recipient(patient) -> str:
    from oncofiles.holding_sql import first_recipient

    return first_recipient(getattr(patient, "caregiver_email", "") or "")


def _rate_ok(now: float) -> bool:
    while _sent_times and now - _sent_times[0] > 3600:
        _sent_times.pop(0)
    return len(_sent_times) < READY_MAILS_PER_HOUR


async def send_due_ready_mails(db) -> int:
    """Send the ready e-mail to every trial that is due; returns how many sent."""
    from oncofiles import mailer

    sent_count = 0
    for due in await db.trials_awaiting_ready_mail():
        pid = due["patient_id"]
        now = time.monotonic()
        failed = _last_failed.get(pid)
        if failed is not None and now - failed < READY_RETRY_S:
            continue
        if not _rate_ok(now):
            logger.info("trial: ready mails rate-limited, the rest wait")
            break
        patient = await db.get_patient(pid)
        to = _recipient(patient) if patient is not None else ""
        if not to:
            continue
        at = _now_iso()
        if not await db.note_trial_event("ready_mail_sent_at", patient_id=pid, at=at):
            continue  # claimed by another run
        subject, body = ready_text(int(due["done"]))
        ok = await mailer.send_mail(
            [to],
            subject,
            body,
            tags=["trial_ready"],
            headers={"Auto-Submitted": "auto-generated"},
        )
        if not ok:
            await db.clear_trial_event("ready_mail_sent_at", patient_id=pid, at=at)
            _last_failed[pid] = now
            logger.info("trial: ready mail for patient %s not sent, retried later", pid[:4])
            continue
        _sent_times.append(now)
        _last_failed.pop(pid, None)
        sent_count += 1
        logger.info("trial: ready mail sent for patient %s documents=%d", pid[:4], due["done"])
    return sent_count


async def note_sign_in(db, email: str) -> None:
    """First dashboard sign-in with a trial's sending address (never raises)."""
    try:
        from oncofiles.email_identity import canonical_email
        from oncofiles.secrets_keys import caller_email_hash

        key = caller_email_hash(canonical_email(email or ""))
        if not key:
            return
        trial = await db.get_trial_by_sender_hash(key)
        if trial is not None:
            await db.note_trial_event("signed_in_at", patient_id=trial["patient_id"], at=_now_iso())
    except Exception as exc:  # noqa: BLE001 - telemetry never blocks a sign-in
        logger.warning("trial: sign-in not noted (%s)", type(exc).__name__)


async def note_event(db, column: str, patient_id: str) -> None:
    """Set one once-only funnel instant of a trial patient (never raises; a
    patient without a trial is a no-op)."""
    try:
        await db.note_trial_event(column, patient_id=patient_id, at=_now_iso())
    except Exception as exc:  # noqa: BLE001 - telemetry never blocks the caller
        logger.warning("trial: %s not noted (%s)", column, type(exc).__name__)


def _parse(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _median_s(pairs: list[tuple[datetime, datetime]]) -> int | None:
    deltas = [max(0.0, (b - a).total_seconds()) for a, b in pairs]
    return int(median(deltas)) if deltas else None


def funnel(rows: list[dict]) -> dict:
    """Counts and medians of the trial funnel (instants only in, numbers out)."""
    parsed = [{step: _parse(r.get(f"{step}_at")) for step in FUNNEL_STEPS} for r in rows]
    steps = []
    for i, step in enumerate(FUNNEL_STEPS):
        reached = [p for p in parsed if p[step] is not None]
        entry: dict = {"step": step, "count": len(reached)}
        if i:
            prev = FUNNEL_STEPS[i - 1]
            entry["median_s_from_received"] = _median_s(
                [(p["received"], p[step]) for p in reached if p["received"] is not None]
            )
            entry["median_s_from_previous"] = _median_s(
                [(p[prev], p[step]) for p in reached if p[prev] is not None]
            )
        steps.append(entry)
    return {
        "trials": len(rows),
        "steps": steps,
        "converted": sum(1 for r in rows if r.get("converted_at")),
        "ready_mail_sent": sum(1 for r in rows if r.get("ready_mail_sent_at")),
    }
