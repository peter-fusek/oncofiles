"""Self-service bootstrap of a curated patient folder (#853).

A Drive folder becomes *curated* when its root holds ``README.md`` (the
contract) and ``_index.json``. Until #853 every such folder was written by
hand and an admin enabled processing. This module writes the contract for a
new patient from a template chosen by patient type, so the patient goes down
the curated path (local OCR, classification, filing) without an admin.

Rules (same as the pipeline's Drive writes, ``curated/drive_ops.py``):

* every write happens inside ``gdrive.contract_writes()`` and is read back;
  nothing is retried, nothing is deleted, nothing is moved;
* a write-ahead journal comes first: ``_actions.jsonl`` is created with one
  ``planned`` line (``seq``, action, path) per write before any other write,
  and gets a ``done`` line for every verified write. No canary is used, so
  nothing is trashed (the #734 deletion allowlist does not grow);
* ``README.md`` is written LAST. A bootstrap that stops half way leaves a
  folder without a README, which is not curated, so nothing processes it;
  its ``_actions.jsonl`` records what was written, and a second attempt on
  the same folder is refused (``state_files_present``): nothing is cleaned up;
* only into a folder that is not My Drive itself, not another patient's folder
  (nor inside one), not curated, not inside or above a curated folder, has NO
  subfolders and none of the state files. Loose files in it stay where they
  are: the pipeline treats files in the folder root as inbox candidates and
  files them on its first real run (after its preview), never during setup.

Templates: ``templates/README_<patient_type>.md``. The folder set is the
template's README §2 table, so the contract and the folders can never
disagree. The §3 vocabularies start empty (every first document from a new
institution or doctor goes to review, #854).
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from string import Template

from oncofiles import curated_folder
from oncofiles.curated import drive_ops
from oncofiles.curated.contract import (
    ROOT_STATE_FILES,
    Contract,
    ContractError,
    nfc,
    parse_contract,
)
from oncofiles.curated.drive_ops import CANARY_NAME, FOLDER_MIME, DriveOps
from oncofiles.curated.naming import ascii_fold

logger = logging.getLogger(__name__)

TEMPLATE_DIR = Path(__file__).parent / "templates"
PATIENT_TYPES = ("oncology", "general")
BY = "oncofiles bootstrap (#853)"
MAX_DISPLAY_CHARS = 80
MAX_TOKEN_CHARS = 40
MAX_FOLDER_NAME_CHARS = 100
# A folder with more loose files than this is refused: the per-patient
# document cap (#548) would cut the first import short anyway.
MAX_LOOSE_FILES = 500

# Characters that would break the README's markdown or its parsed lines
# (``**Patient …:** Name (`Token`)``, the §2 table) are dropped from a name.
_UNSAFE_NAME_CHARS = re.compile(r"[`*|()\[\]{}<>#\\_~$]")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BootstrapRefusedError(Exception):
    """The folder or the patient does not qualify — nothing was written."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


class BootstrapFailedError(Exception):
    """A write could not be confirmed — the bootstrap stopped, nothing is retried."""

    def __init__(self, step: str, written: int):
        super().__init__(f"bootstrap stopped at {step} after {written} verified write(s)")
        self.step = step
        self.written = written


# (English, Slovak) — shown by the wizard; no ids, no names, no e-mails.
REFUSAL_MESSAGES: dict[str, tuple[str, str]] = {
    "missing_dob": (
        "The patient's date of birth is missing. It is needed so that a birth date is "
        "never filed as a document date. Add it to the patient first.",
        "Chýba dátum narodenia pacienta. Je potrebný, aby sa dátum narodenia nikdy "
        "nepoužil ako dátum dokumentu. Najprv ho doplňte.",
    ),
    "invalid_patient_type": (
        "Unknown patient type; use oncology or general.",
        "Neznámy typ pacienta; použite onkologický alebo všeobecné zdravie.",
    ),
    "invalid_name": (
        "The patient's name must contain letters.",
        "Meno pacienta musí obsahovať písmená.",
    ),
    "drive_root": (
        "My Drive itself cannot be used. Open or create a folder for this patient.",
        "Samotný My Drive sa použiť nedá. Otvorte alebo vytvorte priečinok pre tohto pacienta.",
    ),
    "not_a_folder": ("This is not a Drive folder.", "Toto nie je priečinok v Drive."),
    "not_writable": (
        "This Google account cannot add files to this folder (it is shared read-only).",
        "Tento Google účet nemôže do priečinka pridávať súbory (je zdieľaný len na čítanie).",
    ),
    "already_curated": (
        "This folder already has its own README contract.",
        "Tento priečinok už má vlastný kontrakt README.",
    ),
    "curated_overlap": (
        "This folder lies inside, or contains, a folder with its own README contract. "
        "Use an empty folder of its own for this patient.",
        "Tento priečinok leží v priečinku s vlastným kontraktom README alebo taký obsahuje. "
        "Použite pre pacienta samostatný prázdny priečinok.",
    ),
    "other_patient_folder": (
        "This folder is, or lies inside, another patient's folder.",
        "Tento priečinok je priečinkom iného pacienta alebo leží v ňom.",
    ),
    "has_subfolders": (
        "This folder has subfolders. Oncofiles sets up its folders only in a folder without "
        "subfolders: create a new folder (files can be moved into it later).",
        "Tento priečinok má podpriečinky. Oncofiles si svoje priečinky vytvorí iba v priečinku "
        "bez podpriečinkov: vytvorte nový priečinok (súbory doň môžete presunúť neskôr).",
    ),
    "state_files_present": (
        "This folder already holds Oncofiles state files (README.md, _index.json, "
        "_changelog.md, _actions.jsonl or a leftover _healthcheck.tmp). Use a new folder.",
        "Tento priečinok už obsahuje stavové súbory Oncofiles (README.md, _index.json, "
        "_changelog.md, _actions.jsonl alebo zvyšný _healthcheck.tmp). Použite nový priečinok.",
    ),
    "too_many_files": (
        f"This folder holds more than {MAX_LOOSE_FILES} files. Start with a new folder and "
        "add documents to its inbox in batches.",
        f"Priečinok obsahuje viac ako {MAX_LOOSE_FILES} súborov. Začnite s novým priečinkom "
        "a dokumenty pridávajte do inboxu postupne.",
    ),
    "invalid_dob": (
        "The date of birth must be a real date (YYYY-MM-DD), not in the future and not "
        "before 1900.",
        "Dátum narodenia musí byť skutočný dátum (RRRR-MM-DD), nie v budúcnosti a nie pred "
        "rokom 1900.",
    ),
    "invalid_folder_name": (
        "The new folder needs a name.",
        "Nový priečinok potrebuje názov.",
    ),
    "check_failed": (
        "Could not check this folder right now. Try again in a moment.",
        "Tento priečinok sa teraz nepodarilo skontrolovať. Skúste to o chvíľu znova.",
    ),
}

FAILED_MESSAGE = (
    "Setting up the folder stopped part-way; nothing was deleted or moved. The folder is not "
    "connected. Create a new empty folder and try again, or contact the maintainer.",
    "Nastavenie priečinka sa zastavilo v polovici; nič sa nezmazalo ani nepresunulo. Priečinok "
    "nie je pripojený. Vytvorte nový prázdny priečinok a skúste to znova, alebo kontaktujte "
    "správcu.",
)


# ── the plan: template → README text, validated before anything is written ──


@dataclass(frozen=True)
class Plan:
    patient_type: str
    display: str
    token: str
    dob: date
    created: date
    run_id: str
    readme: str
    contract: Contract

    @property
    def folders(self) -> list[str]:
        """The folder set, in README §2 order."""
        return list(self.contract.folders)


def clean_display_name(raw: str | None) -> str:
    """A patient name safe to put into the README (one line, no markdown syntax)."""
    text = unicodedata.normalize("NFC", raw or "")
    text = _CONTROL.sub(" ", text)
    text = _UNSAFE_NAME_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -.,;:/")
    return text[:MAX_DISPLAY_CHARS].strip()


def patient_token(display: str) -> str:
    """``Jana Nováková`` → ``JanaNovakova``: letters only (the contract's token rule)."""
    words = re.split(r"[^A-Za-z]+", ascii_fold(display))
    token = "".join(w[:1].upper() + w[1:] for w in words if w)
    return token[:MAX_TOKEN_CHARS]


def _parse_dob(value) -> date | None:
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value
    raw = str(value or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def render_readme(patient_type: str, display: str, token: str, dob: date, created: date) -> str:
    template = Template((TEMPLATE_DIR / f"README_{patient_type}.md").read_text(encoding="utf-8"))
    return template.substitute(
        display=display,
        token=token,
        dob=dob.strftime("%d.%m.%Y"),
        created=created.isoformat(),
    )


def make_plan(
    *,
    patient_type: str | None,
    display_name: str | None,
    date_of_birth,
    today: date | None = None,
    run_id: str | None = None,
) -> Plan:
    """Render and validate the contract for one patient. Raises ``BootstrapRefusedError``."""
    today = today or datetime.now(UTC).date()
    ptype = (patient_type or "oncology").strip().lower()
    if ptype not in PATIENT_TYPES:
        raise BootstrapRefusedError("invalid_patient_type")
    dob = _parse_dob(date_of_birth)
    if dob is None or dob > today or dob.year < 1900:
        raise BootstrapRefusedError("missing_dob")
    display = clean_display_name(display_name)
    token = patient_token(display)
    if not display or not token:
        raise BootstrapRefusedError("invalid_name")
    readme = render_readme(ptype, display, token, dob, today)
    try:
        contract = parse_contract(readme)
    except ContractError as exc:  # a template bug, never user input: fail loudly
        raise RuntimeError(f"bootstrap template {ptype!r} does not parse: {exc}") from exc
    if contract.patient_token != token or dob.strftime("%Y%m%d") not in contract.birth_dates:
        raise RuntimeError(f"bootstrap template {ptype!r}: token or birth date not parsed back")
    return Plan(
        patient_type=ptype,
        display=display,
        token=token,
        dob=dob,
        created=today,
        run_id=run_id or f"bootstrap-{today:%Y%m%d}-{uuid.uuid4().hex[:8]}",
        readme=readme,
        contract=contract,
    )


# ── Drive access ─────────────────────────────────────────────────────────


class BootstrapOps(DriveOps):
    """``DriveOps`` plus the reads the target check needs and ``create_folder``.

    Tests substitute a fake with the same methods.
    """

    def get_meta(self, file_id: str) -> dict:
        self._fresh()
        return drive_ops._read(
            self.gdrive,
            lambda: (
                self.gdrive._service.files()
                .get(
                    fileId=file_id,
                    fields="id,name,mimeType,parents,trashed,capabilities(canAddChildren)",
                    supportsAllDrives=True,
                )
                .execute()
            ),
        )

    def my_drive_root_id(self) -> str:
        return self.get_meta("root").get("id") or "root"

    def ancestors(self, folder_id: str) -> list[str]:
        self._fresh()
        return curated_folder._ancestors(self.gdrive, folder_id)

    def curated_reason(self, folder_id: str) -> str | None:
        self._fresh()
        return curated_folder.curated_reason(self.gdrive, folder_id, use_cache=False)

    def create_folder(self, parent_id: str, name: str) -> str:
        self._fresh()
        return drive_ops.create_folder(self.gdrive, parent_id, name)


# ── target check (read-only) ─────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    folder_id: str
    name: str
    loose_files: int


def check_target(ops, folder_id: str, *, other_patient_folders: set[str]) -> Target:
    """Refuse (``BootstrapRefusedError``) unless ``folder_id`` may receive a contract.

    Read-only, fails closed: a Drive error is ``check_failed``, never a pass.
    """
    if not folder_id or folder_id == "root":
        raise BootstrapRefusedError("drive_root")
    try:
        meta = ops.get_meta(folder_id)
        root_id = ops.my_drive_root_id()
    except Exception as exc:
        logger.warning("bootstrap: folder check failed (%s)", type(exc).__name__)
        raise BootstrapRefusedError("check_failed") from exc
    if meta.get("trashed") or meta.get("mimeType") != FOLDER_MIME:
        raise BootstrapRefusedError("not_a_folder")
    if folder_id == root_id:
        raise BootstrapRefusedError("drive_root")
    if (meta.get("capabilities") or {}).get("canAddChildren") is False:
        raise BootstrapRefusedError("not_writable")

    others = {root_id if f == "root" else f for f in other_patient_folders if f}
    if folder_id in others:
        raise BootstrapRefusedError("other_patient_folder")
    try:
        ancestors = ops.ancestors(folder_id)
    except Exception as exc:
        logger.warning("bootstrap: ancestry check failed (%s)", type(exc).__name__)
        raise BootstrapRefusedError("check_failed") from exc
    if others & set(ancestors):
        raise BootstrapRefusedError("other_patient_folder")

    reason = ops.curated_reason(folder_id)
    if reason == "is a curated folder":
        raise BootstrapRefusedError("already_curated")
    if reason and "detection failed" in reason:
        raise BootstrapRefusedError("check_failed")
    if reason:
        raise BootstrapRefusedError("curated_overlap")

    try:
        children = ops.list_children(folder_id)
    except Exception as exc:
        logger.warning("bootstrap: listing failed (%s)", type(exc).__name__)
        raise BootstrapRefusedError("check_failed") from exc
    if any(i.is_folder for i in children):
        raise BootstrapRefusedError("has_subfolders")
    names = {nfc(i.name) for i in children}
    if names & {*ROOT_STATE_FILES, CANARY_NAME}:
        raise BootstrapRefusedError("state_files_present")
    if len(children) > MAX_LOOSE_FILES:
        raise BootstrapRefusedError("too_many_files")
    return Target(folder_id=folder_id, name=meta.get("name") or "", loose_files=len(children))


# ── the writes ───────────────────────────────────────────────────────────


def _changelog(plan: Plan, now: datetime) -> str:
    return (
        "# Changelog\n\n"
        f"## {now:%Y-%m-%d %H:%M} UTC — folder set up by Oncofiles ({plan.run_id})\n"
        f"- README.md contract from the `{plan.patient_type}` template, standard version "
        f"{plan.contract.version}; vocabularies empty.\n"
        f"- Created {len(plan.folders)} folders (README §2), `_index.json`, `_actions.jsonl` "
        "and this file. README.md was written last.\n"
        "- Files that were already in the folder root were left where they are; the pipeline "
        "files them on its first run.\n"
    )


def _journal_line(plan: Plan, ts: str, entry: dict, status: str) -> str:
    return (
        json.dumps(
            {"ts": ts, "run_id": plan.run_id, **entry, "status": status, "by": BY},
            ensure_ascii=False,
        )
        + "\n"
    )


def write_contract(ops, folder_id: str, plan: Plan, *, now: datetime | None = None) -> dict:
    """Journal, folders, state files, README last. Caller holds ``contract_writes()``.

    The write-ahead journal is ``_actions.jsonl`` itself: its first content is
    one ``planned`` line per write (with ``seq``), written and read back before
    anything else; after each phase the verified writes get a ``done`` line.
    No canary is used, so nothing is ever trashed. Raises
    ``BootstrapFailedError`` on the first failed write; nothing is retried.
    """
    now = now or datetime.now(UTC)
    ts = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    planned: list[dict] = [{"action": "bootstrap_create_folder", "path": n} for n in plan.folders]
    planned += [
        {"action": "bootstrap_create_file", "path": n}
        for n in ("_actions.jsonl", "_changelog.md", "_index.json", "README.md")
    ]
    for seq, entry in enumerate(planned, start=1):
        entry["seq"] = seq
    by_path = {e["path"]: e for e in planned}
    journal = "".join(_journal_line(plan, ts, e, "planned") for e in planned)

    def done(*paths: str) -> None:
        nonlocal journal
        journal += "".join(_journal_line(plan, ts, by_path[p], "done") for p in paths)
        ops.update_text(journal_id, journal, "application/x-ndjson")

    step, written = "journal", 0
    try:
        journal_id = ops.create_text(folder_id, "_actions.jsonl", journal, "application/x-ndjson")
        written += 1
        step = "folders"
        for name in plan.folders:
            ops.create_folder(folder_id, name)
            written += 1
        done(*plan.folders, "_actions.jsonl")
        step = "state files"
        ops.create_text(folder_id, "_changelog.md", _changelog(plan, now), "text/markdown")
        written += 1
        ops.create_text(folder_id, "_index.json", "[]\n", "application/json")
        written += 1
        done("_changelog.md", "_index.json")
        step = "README.md"
        ops.create_text(folder_id, "README.md", plan.readme, "text/markdown")
        written += 1
    except Exception as exc:
        logger.warning(
            "bootstrap %s: stopped at %s after %d verified write(s) (%s)",
            plan.run_id,
            step,
            written,
            type(exc).__name__,
        )
        raise BootstrapFailedError(step, written) from exc
    journal_complete = True
    try:
        done("README.md")
    except Exception as exc:  # README is written and verified: the folder is curated
        journal_complete = False
        logger.warning(
            "bootstrap %s: README written, its journal line not (%s)",
            plan.run_id,
            type(exc).__name__,
        )
    return {
        "folders_created": len(plan.folders),
        "files_created": 4,
        "journal_complete": journal_complete,
    }


def bootstrap_folder(
    gdrive,
    folder_id: str,
    plan: Plan,
    *,
    other_patient_folders: set[str],
    ops=None,
) -> dict:
    """Check the target, write the contract, confirm the folder is now curated.

    Blocking (Drive calls): run via ``asyncio.to_thread``. The writes run in
    this thread inside ``gdrive.contract_writes()``.
    """
    ops = ops if ops is not None else BootstrapOps(gdrive)
    target = check_target(ops, folder_id, other_patient_folders=other_patient_folders)
    with gdrive.contract_writes():
        written = write_contract(ops, folder_id, plan)
    curated_folder.clear_cache(folder_id)
    if ops.curated_reason(folder_id) != "is a curated folder":
        raise BootstrapFailedError(
            "verification", written["folders_created"] + written["files_created"]
        )
    logger.info(
        "bootstrap %s: %s contract written (%d folders, %d loose file(s) left in place)",
        plan.run_id,
        plan.patient_type,
        written["folders_created"],
        target.loose_files,
    )
    return {
        **written,
        "loose_files": target.loose_files,
        "run_id": plan.run_id,
        "patient_type": plan.patient_type,
        "folder_name": target.name,
    }


def clean_folder_name(raw: str | None) -> str:
    text = _CONTROL.sub(" ", unicodedata.normalize("NFC", raw or ""))
    text = re.sub(r"\s+", " ", text.replace("/", " ")).strip()
    return text[:MAX_FOLDER_NAME_CHARS].strip()


def check_new_folder_parent(ops, parent_id: str, *, other_patient_folders: set[str]) -> str:
    """Refuse a parent the new patient folder must not be created in. Returns its real id.

    Runs BEFORE the new folder exists, so a refusal leaves nothing behind: the
    parent must not be (or lie inside) another patient's folder, and must not
    be or lie inside a curated folder. My Drive itself is a valid parent (the
    new folder is not My Drive). Read-only, fails closed.
    """
    try:
        root_id = ops.my_drive_root_id()
        parent = root_id if parent_id in ("", "root") else parent_id
        meta = ops.get_meta(parent)
        ancestors = [] if parent == root_id else ops.ancestors(parent)
    except Exception as exc:
        logger.warning("bootstrap: parent check failed (%s)", type(exc).__name__)
        raise BootstrapRefusedError("check_failed") from exc
    if meta.get("trashed") or meta.get("mimeType") != FOLDER_MIME:
        raise BootstrapRefusedError("not_a_folder")
    if (meta.get("capabilities") or {}).get("canAddChildren") is False:
        raise BootstrapRefusedError("not_writable")
    others = {root_id if f == "root" else f for f in other_patient_folders if f}
    if parent in others or others & set(ancestors):
        raise BootstrapRefusedError("other_patient_folder")
    if parent != root_id:
        reason = ops.curated_reason(parent)
        if reason and "detection failed" in reason:
            raise BootstrapRefusedError("check_failed")
        if reason == "is a curated folder" or (reason or "").startswith("lies inside"):
            raise BootstrapRefusedError("curated_overlap")
    return parent


def create_patient_root(
    gdrive,
    name: str,
    parent_id: str,
    ops=None,
    *,
    other_patient_folders: set[str] | None = None,
) -> str:
    """Create the new, empty patient folder inside ``parent_id`` (verified, no retry).

    The parent is checked first (``check_new_folder_parent``), so a refused
    parent leaves no empty folder behind. A failed create leaves at most this
    one empty folder (nothing is deleted).
    """
    folder_name = clean_folder_name(name)
    if not folder_name:
        raise BootstrapRefusedError("invalid_folder_name")
    ops = ops if ops is not None else BootstrapOps(gdrive)
    parent = check_new_folder_parent(
        ops, parent_id, other_patient_folders=set(other_patient_folders or ())
    )
    try:
        return ops.create_folder(parent, folder_name)
    except Exception as exc:
        logger.warning("bootstrap: patient folder create failed (%s)", type(exc).__name__)
        raise BootstrapFailedError("patient folder", 0) from exc


# ── async glue for the dashboard endpoint ────────────────────────────────


def parse_new_dob(raw, *, today: date | None = None) -> date:
    """A DOB typed in the wizard: real ISO date, not in the future, not before 1900."""
    today = today or datetime.now(UTC).date()
    raw_s = raw.strip() if isinstance(raw, str) else ""
    dob = _parse_dob(raw_s) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_s) else None
    if dob is None or dob > today or dob.year < 1900:
        raise BootstrapRefusedError("invalid_dob")
    return dob


async def store_dob_if_missing(db, patient_id: str, raw) -> bool:
    """Store the wizard's DOB in the patient context, only when it has none.

    Validated first (``invalid_dob`` refuses, nothing stored). An existing DOB
    is never overwritten here; returns True when one was stored.
    """
    from oncofiles import patient_context

    dob = parse_new_dob(raw)
    await patient_context.ensure_loaded(db.db, patient_id)
    ctx = patient_context.get_context(patient_id) or {}
    if ctx.get("date_of_birth"):
        return False
    new_ctx = patient_context.update_context(
        {"date_of_birth": dob.isoformat()}, patient_id=patient_id
    )
    await patient_context.save_to_db(db.db, new_ctx, patient_id=patient_id)
    return True


async def plan_for_patient(db, patient_id: str) -> Plan:
    """Build the plan from the patient row and context (type, DOB). Read-only."""
    from oncofiles import patient_context

    patient = await db.get_patient(patient_id)
    if patient is None:
        raise BootstrapRefusedError("invalid_name")
    await patient_context.ensure_loaded(db.db, patient_id)
    ctx = patient_context.get_context(patient_id) or {}
    return make_plan(
        patient_type=ctx.get("patient_type") or "oncology",
        display_name=patient.display_name or ctx.get("name") or "",
        date_of_birth=ctx.get("date_of_birth") or None,
    )


def pipeline_summary() -> dict:
    """Which pipeline processes a bootstrapped folder, and when (for the wizard)."""
    from oncofiles.curated import vocabulary
    from oncofiles.tools.curated import continuous_settings, nightly_settings

    cont, poll_s, settle_s, chunk = continuous_settings()
    nightly, hour, _ = nightly_settings()
    return {
        "kind": "curated",
        "processing_enabled": bool(nightly),
        "continuous": bool(nightly and cont),
        "poll_s": poll_s,
        "settle_s": settle_s,
        "docs_per_run": chunk,
        "nightly_hour": hour,
        "first_run": "preview",
        # #865: CURATED_AUTO_VOCAB, so the wizard says truthfully whether a new
        # institution or doctor is added to the README or the document waits.
        "auto_vocab": vocabulary.enabled(),
    }


def refusal_payload(exc: BootstrapRefusedError) -> dict:
    en, sk = REFUSAL_MESSAGES.get(exc.reason, REFUSAL_MESSAGES["check_failed"])
    return {"error": en, "error_sk": sk, "code": "bootstrap_refused", "reason": exc.reason}


def failure_payload(exc: BootstrapFailedError) -> dict:
    return {
        "error": FAILED_MESSAGE[0],
        "error_sk": FAILED_MESSAGE[1],
        "code": "bootstrap_failed",
        "step": exc.step,
    }


__all__ = [
    "BootstrapFailedError",
    "BootstrapOps",
    "BootstrapRefusedError",
    "Plan",
    "bootstrap_folder",
    "check_target",
    "create_patient_root",
    "make_plan",
    "patient_token",
    "plan_for_patient",
    "render_readme",
    "write_contract",
]
