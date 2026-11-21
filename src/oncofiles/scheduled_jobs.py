"""The admin "Scheduled server jobs" panel, built from the RUNNING scheduler.

``GET /api/schedules`` used to return a hardcoded catalogue, so the dashboard
listed legacy jobs (5-minute Drive sync, Gmail, Calendar, sweeps) as "not run
yet" on a server where they were never registered, and did not show the
curated loop at all. This module builds the list from what the process really
runs:

* ``jobs``: every job of the APScheduler registered in ``scheduler_singleton``
  (id, trigger, next run), merged with the ``job_tracker`` telemetry
  (``running`` / ``last_ok`` / ``last_error`` / duration). A one-off job that
  already fired is no longer in the scheduler; it stays visible from the
  tracker with ``registered: false``.
* ``inactive``: known job ids that this server does not run, each with the
  switch that keeps it off (``SYNC_ENABLED``, ``AI_NIGHTLY_ONLY``, ...). The
  dashboard shows them in a collapsed group, so "why is there no Gmail sync?"
  still has an answer without cluttering the list of real jobs.
* ``curated_loop``: the curated loop is an asyncio task, not an APScheduler
  job, so it is a pseudo-entry with its settings and AGGREGATE probe counters
  (no patient ids, names or per-patient rows).

Pure functions over in-memory state: no DB call, nothing here is read by
``/health``. Error texts are not returned (an exception message can carry
anything); only the time and the exception class. The message stays in the
server log.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# Human descriptions by job id: (group, name sk, name en, desc sk, desc en).
# A lookup only — whether a job is shown depends on the running scheduler.
CATALOGUE: dict[str, tuple[str, str, str, str, str]] = {
    "nightly_ai_pipeline": (
        "pipeline",
        "Nočný AI pipeline",
        "Nightly AI pipeline",
        "Import z Drive a AI spracovanie nových dokumentov (limit na pacienta a deň)",
        "Drive import and AI processing of new documents (per-patient daily cap)",
    ),
    "weekly_discovery_pipeline": (
        "pipeline",
        "Týždenné hľadanie súvislostí",
        "Weekly discovery",
        "Rozdelenie zlúčených PDF a zoskupenie viacsúborových dokumentov",
        "Split multi-document PDFs and group multi-file documents",
    ),
    "nightly_ai_oneoff": (
        "pipeline",
        "Jednorazový AI pipeline",
        "One-off AI pipeline",
        "Overovací beh nočného pipeline krátko po štarte (AI_RUN_ON_STARTUP)",
        "Validation run of the nightly pipeline shortly after start (AI_RUN_ON_STARTUP)",
    ),
    "gdrive_sync": (
        "sync",
        "Synchronizácia Drive",
        "Drive sync",
        "Obojsmerná synchronizácia: import, export do priečinkov, premenovanie",
        "Two-way sync: import, export to category folders, rename",
    ),
    "enhancement_sweep": (
        "pipeline",
        "Dokončenie spracovania",
        "Enhancement sweep",
        "Opakované OCR, AI súhrn a štítky pre zaseknuté dokumenty (max 5)",
        "Retry OCR, AI summary and tags for stuck documents (max 5)",
    ),
    "metadata_extraction": (
        "pipeline",
        "Extrakcia metadát",
        "Metadata extraction",
        "Doplnenie chýbajúcich štruktúrovaných metadát (max 5)",
        "Backfill missing structured metadata (max 5)",
    ),
    "document_groups_backfill": (
        "pipeline",
        "Skupiny dokumentov",
        "Document groups",
        "Doplnenie skupín rozdelených a zlúčených dokumentov",
        "Backfill groups of split and consolidated documents",
    ),
    "category_validation": (
        "housekeeping",
        "Kontrola kategórií",
        "Category validation",
        "Oprava kategórie podľa AI metadát",
        "Correct document categories from AI metadata",
    ),
    "tier_transition": (
        "housekeeping",
        "Prechod taríf",
        "Tier transition",
        "Po 30 dňoch presunie pacientov z onboarding tarify na bežnú",
        "Move patients from the onboarding tier to free after 30 days",
    ),
    "oauth_token_cleanup": (
        "infra",
        "Čistenie OAuth tokenov",
        "OAuth cleanup",
        "Odstránenie expirovaných a zrušených OAuth tokenov",
        "Remove expired or revoked OAuth tokens",
    ),
    "prompt_log_cleanup": (
        "infra",
        "Čistenie logu AI volaní",
        "Prompt log cleanup",
        "Zmazanie telemetrie AI volaní staršej ako 90 dní",
        "Delete AI call telemetry older than 90 days",
    ),
    "dashboard_access_log_retention": (
        "infra",
        "Retencia logu prístupov",
        "Access log retention",
        "Zmazanie záznamov prístupov do dashboardu starších ako 90 dní",
        "Delete dashboard access log rows older than 90 days",
    ),
    "inbound_queue": (
        "infra",
        "Fronta prijatej pošty",
        "Inbound mail queue",
        "Spracuje dokumenty poslané e-mailom, ktoré čakajú vo fronte (aj po reštarte)",
        "Process documents sent by e-mail that wait in the queue (also after a restart)",
    ),
    "holding_extract": (
        "pipeline",
        "Čítanie dokumentov skúšobného účtu",
        "Trial document reading",
        "Lokálnym modelom prečíta dokumenty poslané na welcome@, ktoré ešte čakajú",
        "Read documents sent to welcome@ that still wait, with the local model",
    ),
    "holding_move": (
        "pipeline",
        "Presun dokumentov skúšobného účtu do Drive",
        "Trial documents to Drive",
        "Po pripojení Drive skopíruje dokumenty skúšobného účtu do priečinka na nové "
        "súbory a pošle e-mail, keď sú dokumenty pripravené",
        "After Drive is connected, copy trial documents into the inbox folder and send "
        "the e-mail when documents are ready",
    ),
    "pipeline_integrity": (
        "pipeline",
        "Integrita pipeline",
        "Pipeline integrity",
        "Nájde dokumenty zaseknuté v nedokončenom stave a spustí ich znova",
        "Find documents stuck half-processed and re-run them",
    ),
    "sync_watchdog": (
        "infra",
        "Strážca synchronizácie",
        "Sync watchdog",
        "Upozorní, keď sa pacient so zdieľaným priečinkom dlho nesynchronizoval",
        "Alert when a patient with a connected folder has not synced for too long",
    ),
    "weekly_analytics": (
        "infra",
        "Týždenná analytika",
        "Weekly analytics",
        "Súhrn týždenných štatistík používania",
        "Aggregate weekly usage statistics",
    ),
    "startup_catchup": (
        "sync",
        "Import po štarte",
        "Startup catch-up",
        "Jednorazový import z Drive po štarte servera",
        "One-off Drive import after the server starts",
    ),
    "gmail_sync": (
        "sync",
        "Synchronizácia Gmailu",
        "Gmail sync",
        "Import medicínskych e-mailov a ich klasifikácia",
        "Import and classify medical e-mails",
    ),
    "gmail_startup_sync": (
        "sync",
        "Gmail po štarte",
        "Gmail startup sync",
        "Úvodný import e-mailov po štarte servera",
        "Initial e-mail import after the server starts",
    ),
    "calendar_sync": (
        "sync",
        "Synchronizácia kalendára",
        "Calendar sync",
        "Import udalostí kalendára a označenie lekárskych termínov",
        "Import calendar events and tag medical appointments",
    ),
    "calendar_startup_sync": (
        "sync",
        "Kalendár po štarte",
        "Calendar startup sync",
        "Úvodný import kalendára po štarte servera",
        "Initial calendar import after the server starts",
    ),
    "memory_check": (
        "infra",
        "Kontrola pamäte",
        "Memory monitor",
        "Kontrola RSS pamäte, uvoľnenie alebo riadený reštart pri prahu",
        "Check RSS memory, reclaim or restart gracefully at the threshold",
    ),
}

# Jobs that exist only in legacy mode (AI_NIGHTLY_ONLY=false) and vice versa,
# mirroring the registration branches in server._start_sync_scheduler.
_LEGACY_ONLY = {
    "gdrive_sync",
    "enhancement_sweep",
    "metadata_extraction",
    "document_groups_backfill",
    "category_validation",
}
_NIGHTLY_ONLY = {"nightly_ai_pipeline", "weekly_discovery_pipeline"}


def describe(job_id: str) -> dict:
    """Group and bilingual texts for a job id; an unknown id shows the id."""
    entry = CATALOGUE.get(job_id)
    if entry is None:
        return {
            "group": "other",
            "name": {"sk": job_id, "en": job_id},
            "desc": {"sk": "", "en": ""},
            "known": False,
        }
    group, name_sk, name_en, desc_sk, desc_en = entry
    return {
        "group": group,
        "name": {"sk": name_sk, "en": name_en},
        "desc": {"sk": desc_sk, "en": desc_en},
        "known": True,
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def trigger_info(trigger: Any) -> dict:
    """Structured, display-neutral trigger description.

    ``kind`` is interval / cron / date / other. ``cadence`` tells the browser
    how to phrase it: a daily or weekly cron is rendered from the job's
    ``next_run_at`` in the viewer's local time, so the server never has to
    know the browser's zone. ``text`` is APScheduler's own string, the
    fallback for anything else.
    """
    text = str(trigger)
    cls = type(trigger).__name__
    if cls == "IntervalTrigger":
        interval = getattr(trigger, "interval", None)
        seconds = int(interval.total_seconds()) if interval is not None else None
        return {"kind": "interval", "seconds": seconds, "text": text}
    if cls == "DateTrigger":
        return {"kind": "date", "text": text}
    if cls == "CronTrigger":
        fields = {
            f.name: str(f)
            for f in getattr(trigger, "fields", [])
            if not getattr(f, "is_default", False)
        }
        hour, minute = fields.get("hour", ""), fields.get("minute", "")
        simple_time = hour.isdigit() and minute.isdigit()
        others = set(fields) - {"hour", "minute", "second", "day_of_week"}
        dow = fields.get("day_of_week")
        if simple_time and not others and not dow:
            cadence = "daily"
        elif simple_time and not others and dow and dow.isalpha():
            cadence = "weekly"
        else:
            cadence = "custom"
        return {"kind": "cron", "cadence": cadence, "text": text}
    return {"kind": "other", "text": text}


def _telemetry(entry: dict | None) -> dict:
    entry = entry or {}
    return {
        "running": bool(entry.get("running", False)),
        "last_ok": entry.get("last_ok"),
        "last_error": entry.get("last_error"),
        # Only the exception class: a message can carry ids or file names.
        "last_error_type": entry.get("last_error_type"),
        "last_duration_s": entry.get("last_duration_s"),
        "last_missed": entry.get("last_missed"),
    }


def inactive_reason(job_id: str, flags: dict) -> dict:
    """Why a known job id is not registered on this server.

    ``flags`` holds the switches ``server._start_sync_scheduler`` branches on.
    The ``switch`` text names the env variable and its current value.
    """
    if not flags.get("scheduler_running"):
        if not flags.get("sync_enabled", True):
            return {"reason": "sync_disabled", "switch": "SYNC_ENABLED=false"}
        return {"reason": "scheduler_not_running", "switch": None}
    nightly = flags.get("ai_nightly_only", True)
    gmail_cal = flags.get("gmail_calendar_enabled", True)
    if job_id in _LEGACY_ONLY and nightly:
        return {"reason": "ai_nightly_only", "switch": "AI_NIGHTLY_ONLY=true"}
    if job_id in _NIGHTLY_ONLY and not nightly:
        return {"reason": "legacy_mode", "switch": "AI_NIGHTLY_ONLY=false"}
    if job_id == "nightly_ai_oneoff":
        if not nightly:
            return {"reason": "legacy_mode", "switch": "AI_NIGHTLY_ONLY=false"}
        return {"reason": "run_on_startup_off", "switch": "AI_RUN_ON_STARTUP=false"}
    if job_id in ("gmail_sync", "calendar_sync", "gmail_startup_sync", "calendar_startup_sync"):
        if not gmail_cal:
            return {"reason": "gmail_calendar_disabled", "switch": "GMAIL_CALENDAR_ENABLED=false"}
        if job_id.endswith("_startup_sync") and nightly:
            return {"reason": "ai_nightly_only", "switch": "AI_NIGHTLY_ONLY=true"}
    if job_id == "pipeline_integrity" and not flags.get("integrity_check", False):
        return {"reason": "integrity_check_off", "switch": "ENABLE_INTEGRITY_CHECK=false"}
    return {"reason": "not_registered", "switch": None}


def build_jobs(scheduler: Any, tracker: dict | None, flags: dict) -> dict:
    """``jobs`` (registered + finished one-offs) and ``inactive`` (known, not run)."""
    tracker = tracker or {}
    jobs: list[dict] = []
    seen: set[str] = set()
    registered: list[Any] = []
    if scheduler is not None and flags.get("scheduler_running"):
        try:
            registered = list(scheduler.get_jobs())
        except Exception:
            registered = []
    for job in registered:
        job_id = str(job.id)
        seen.add(job_id)
        jobs.append(
            {
                "id": job_id,
                **describe(job_id),
                "registered": True,
                "trigger": trigger_info(job.trigger),
                "next_run_at": _iso(getattr(job, "next_run_time", None)),
                **_telemetry(tracker.get(job_id)),
            }
        )
    # A DateTrigger job leaves the scheduler once it fired; its run is still
    # in the tracker and is worth showing ("ran once at ...").
    for job_id in sorted(tracker):
        if job_id in seen:
            continue
        seen.add(job_id)
        jobs.append(
            {
                "id": str(job_id),
                **describe(str(job_id)),
                "registered": False,
                "trigger": None,
                "next_run_at": None,
                **_telemetry(tracker.get(job_id)),
            }
        )
    jobs.sort(key=lambda j: (j["next_run_at"] is None, j["next_run_at"] or "", j["id"]))
    inactive = [
        {"id": job_id, **describe(job_id), **inactive_reason(job_id, flags)}
        for job_id in CATALOGUE
        if job_id not in seen
    ]
    return {"jobs": jobs, "inactive": inactive}


def _max(values: list[str | None]) -> str | None:
    present = [v for v in values if v]
    return max(present) if present else None


def curated_loop_entry(
    *,
    running: bool,
    nightly_enabled: bool,
    nightly_hour: int | None,
    google_enabled: bool,
    continuous: tuple[bool, int, int, int],
    stats: dict,
) -> dict:
    """Pseudo-entry for the curated loop: settings + aggregate counters only.

    ``stats`` is ``tools.curated.probe_stats()`` (keyed by patient id); only
    sums, maxima and the number of patients leave this function.
    """
    cont, poll_s, settle_s, chunk = continuous
    rows = [r for r in (stats or {}).values() if isinstance(r, dict)]
    reason = None
    switch = None
    if not running:
        if not nightly_enabled:
            reason, switch = "curated_disabled", "CURATED_NIGHTLY_ENABLED=false"
        elif not google_enabled:
            reason, switch = "google_disabled", "GOOGLE_INTEGRATION_ENABLED=false"
        else:
            reason = "not_running"
    return {
        "id": "curated_loop",
        "group": "curated",
        "name": {"sk": "Kurátorská slučka", "en": "Curated loop"},
        "desc": {
            "sk": "Sleduje kurátorské priečinky pacientov a spracuje nové súbory lokálnym modelom",
            "en": "Watches curated patient folders and processes new files with the local model",
        },
        "running": bool(running),
        "reason": reason,
        "switch": switch,
        "continuous": bool(cont),
        "poll_s": poll_s,
        "settle_s": settle_s,
        "chunk_docs": chunk,
        "nightly_hour": nightly_hour,  # Europe/Bratislava; None = every hour
        "patients_probed": len(rows),
        "last_probe_at": _max([r.get("last_probe_at") for r in rows]),
        "probes_total": sum(int(r.get("probes_total") or 0) for r in rows),
        "last_auto_run_at": _max([r.get("last_auto_run_at") for r in rows]),
        "auto_runs_total": sum(int(r.get("auto_runs_total") or 0) for r in rows),
        "runs_blocked_total": sum(sum((r.get("runs_blocked") or {}).values()) for r in rows),
        "pending_inbox_total": sum(int(r.get("pending_inbox_count") or 0) for r in rows),
    }
