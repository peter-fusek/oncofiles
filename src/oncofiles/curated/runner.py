"""One run of the curated-folder pipeline (#562).

    contract → preflight gates → candidates (inbox + loose root files)
      → per document: exact-duplicate quarantine | extract → classify → sidecar → file
      → state files (_actions.jsonl, _changelog.md, _index.json, daily report) → canary cleanup

Rules from the folder README that this module enforces:

* never create folders; never delete (quarantine to ``99-duplicates`` only);
* source + sidecar are one unit — written next to each other, moved together;
* every write is verified by reading it back (``drive_ops``); the run aborts on
  the first failure and never retries;
* ``review_needed`` documents stay in ``00-inbox`` with a sidecar explaining why;
* a new institution / doctor printed in the document is added to README §3 before
  the document is filed (#854, ``CURATED_AUTO_VOCAB``): one journaled, verified
  README update per document that brings new tokens;
* a maintainer override in an inbox sidecar (``review_override: "<reason>"``,
  #900) keeps that sidecar's text as it is (no re-OCR, no sidecar rewrite) and
  ignores only the vision soft reasons (rotated region, unsure orientation,
  chart page); every hard check still runs on the kept text;
* a FILED document whose sidecar says ``review_needed: true`` with no
  recognised reason (#919 item 4) is re-checked on its kept text, a few per
  run: all hard checks pass → the flag is cleared, else its reason codes are
  written; front-matter keys only, never a move;
* a per-run action budget; a run that stops and explains itself is a success.

``dry_run=True`` (the default) performs every read, extraction and
classification but no write, and returns the plan.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime

from oncofiles.curated import classify as classify_mod
from oncofiles.curated import extract as extract_mod
from oncofiles.curated import llm, llm_input, review_codes
from oncofiles.curated import normalise as normalise_mod
from oncofiles.curated import orientation as orientation_mod
from oncofiles.curated import vocabulary as vocab_mod
from oncofiles.curated.contract import (
    DUPLICATES_PREFIX,
    ROOT_STATE_FILES,
    Contract,
    ContractError,
    folder_key,
    nfc,
    parse_contract,
)
from oncofiles.curated.drive_ops import CANARY_NAME, Item, WriteVerificationError
from oncofiles.curated.naming import (
    NATIVE_EXT,
    build_filename,
    is_google_native,
    is_standard,
    with_collision_suffix,
)

logger = logging.getLogger(__name__)

CANARY = CANARY_NAME  # "_healthcheck.tmp"; drive_ops.trash_canary trashes nothing else
# The canary doubles as the run's write-ahead journal (#581): line 1 is
# "<run_id> journal-v1", then one JSON line per action, written (and verified)
# BEFORE the action. A canary left by a killed run therefore says exactly
# what that run may have changed. Older canaries carry only the run id.
# Each action line carries a ``seq``; once the action (for a pair: both
# moves) is verified, a ``{"ok": seq}`` line confirms it. A killed run whose
# every journaled action is confirmed left a consistent folder, so the next
# run recovers automatically; any unconfirmed action still needs a human.
JOURNAL_MARK = "journal-v1"
# A leftover canary younger than this may belong to a run that is still alive
# (e.g. the old container during a rolling deploy) — never touch it.
STALE_CANARY_MIN_AGE_S = 300
# A review caused only by a technical failure (model ran out of budget, bad
# JSON) is not a human decision: retry it on later runs, at most this often.
MAX_TECHNICAL_ATTEMPTS = 3
ACTIONS_PER_DOCUMENT = 3  # sidecar + source move + sidecar move
SOURCE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,5}$")
YYYY_MM_RE = re.compile(r"^\d{4}-\d{2}( \(\d+\))?$")
DUP_FOLDER_RE = re.compile(r"^(?P<base>.+?)(?: \(\d+\)| \d+)$")
# #919 item 4: filed documents held in review without a recognised reason are
# re-checked (one model classification of the kept sidecar text each, no OCR)
# at most this many per run; the rest wait for a later run.
STALE_REVIEW_MAX_PER_RUN = 5
STALE_REVIEW_CLEARED_BY = "auto (#919)"


@dataclass
class RunOptions:
    dry_run: bool = True
    max_docs: int = 20
    max_actions: int = 40
    min_text_chars: int = 100
    run_id: str = ""
    now: datetime | None = None
    # Oncology contract (the first patient's Cowork daily clean-up job): a date
    # more than two years old on a new scan is suspect → review. None = no age
    # rule (general patients).
    max_doc_age_days: int | None = None
    # Already-filed documents without a sidecar get one (sidecar only) per run.
    max_missing_sidecars: int = 20
    # Checked before each document: True stops the run cleanly (shutdown drain).
    should_stop: Callable[[], bool] | None = None
    # Text sources have no sidecar, so a review decision leaves no mark on
    # Drive: (file id, modified) pairs already sent to review by earlier runs.
    reviewed_text: frozenset = frozenset()
    # A filed document without a sidecar has nowhere on Drive to count failed
    # attempts either: {file id: {"attempts": n, "code": reason}} of earlier
    # real runs' backfill model answer failures (``backfill_attempts_from``).
    # At MAX_TECHNICAL_ATTEMPTS the model is no longer asked; it goes to review.
    backfill_attempts: dict = field(default_factory=dict)
    # #854: add printed new institutions / doctors to README §3 and file the
    # document (``CURATED_AUTO_VOCAB``, default on); False = #754 review.
    auto_vocab: bool = field(default_factory=vocab_mod.enabled)
    # #855 capacity rules, checked BEFORE a document is read by the model:
    # ``doc_cap`` = the patient's document limit (MAX_DOCUMENTS_PER_PATIENT,
    # 0 = unlimited) against max(``docs_existing``, documents filed in the
    # tree) + documents this run already sent to OCR. Over the cap the file
    # stays in the inbox untouched, listed in ``RunReport.held``.
    doc_cap: int = 0
    docs_existing: int = 0
    # Returns a reason ("daily_page_budget") when no more model OCR may start
    # now; None = go. Same effect as the cap: the file stays, listed in ``held``.
    ocr_gate: Callable[[], str | None] | None = None
    # #919: filed documents whose name fails the standard are renamed in place
    # (pair together, journaled) from their sidecar front matter.
    # ``CURATED_NORMALISE_FILED`` (default false) gates real renames; a dry run
    # always shows the plan. At most ``CURATED_NORMALISE_MAX`` renames per run.
    normalise_filed: bool = field(default_factory=normalise_mod.enabled)
    normalise_max: int = field(default_factory=normalise_mod.max_per_run)
    # #919 item 4: Drive ids of FILED documents the read model shows held in
    # review with no recognised reason (``db.curated_stale_review_ids``). The
    # run re-reads each sidecar and re-checks at most ``stale_review_max`` of
    # them (``_recheck_stale_reviews``); empty = the pass does nothing.
    stale_review_ids: frozenset = frozenset()
    stale_review_max: int = STALE_REVIEW_MAX_PER_RUN


HOLD_DOCUMENT_CAP = "document_cap"


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str = ""
    blocking: bool = True


@dataclass
class RunReport:
    run_id: str
    dry_run: bool
    status: str = "ok"  # ok | aborted
    message: str = ""
    contract_version: str = ""
    gates: list[Gate] = field(default_factory=list)
    documents: list[dict] = field(default_factory=list)
    actions: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    awaiting_review: list[str] = field(default_factory=list)
    not_processed: list[str] = field(default_factory=list)
    # #855: files left in place unread, {"source": label, "reason": code}
    # (document_cap | daily_page_budget). Never moved, never deleted.
    held: list[dict] = field(default_factory=list)
    # #855: pages the local model OCR'd in this run (cache hits excluded); set
    # by the caller that counts them (tools/curated.py), 0 here.
    model_pages: int = 0
    # filed documents this run left without a sidecar because of a bad model
    # answer (truncated / not JSON); each is in ``documents`` with its code
    backfill_skipped: int = 0
    # #855: nothing left that a later run could process (no budget leftovers,
    # nothing held except by the document cap). Marks a finished first import.
    inbox_drained: bool = False
    # #854: README vocabulary. Tokens only — the printed spelling goes to the
    # README. A dry run always fills ``planned`` and ``outcomes`` (also with
    # CURATED_AUTO_VOCAB=false, as a preview); ``added`` only by a real run.
    vocabulary: dict = field(default_factory=lambda: _vocab_report())
    # #882: ``orientation.tally`` summed over this run's vision extractions:
    # direction questions asked vs pages decided without the model (counts
    # only; a cached extraction asked nothing in this run).
    orientation: dict = field(default_factory=dict)
    # #931: ingest OCR telemetry, counts only: pages asked once more with
    # thinking off (#806 retry) and PDFs whose scanner OCR layer was ignored
    # (not born-digital, vision-read instead). A cached extraction retried
    # nothing in this run (``tools.curated.cached_extract`` clears it).
    ocr: dict = field(default_factory=lambda: {"retried_pages": 0, "text_layer_ignored": 0})
    # #919: filed documents whose name fails the standard (``_normalise_filed``).
    # Names are fine here (the patient's own run report); server logs get
    # counts and name shapes only.
    normalise: dict = field(default_factory=lambda: _normalise_report())
    # #919 item 4: filed documents whose stale review flag was re-checked
    # (``_recheck_stale_reviews``). Names only in this report; codes, never
    # reason sentences; server logs get counts only.
    stale_review: dict = field(default_factory=lambda: _stale_review_report())

    def to_dict(self) -> dict:
        return asdict(self)

    def count_orientation(self, extraction) -> None:
        """Telemetry of one extraction: the #882 orientation tally and the #931
        OCR counts."""
        orientation_mod.add_tally(self.orientation, getattr(extraction, "orientation", None))
        retried = getattr(extraction, "retried_pages", None)
        if isinstance(retried, list):
            self.ocr["retried_pages"] += len(retried)
        if getattr(extraction, "text_layer_ignored", False) is True:
            self.ocr["text_layer_ignored"] += 1


class RunAbortError(Exception):
    """Stop the run now; the message goes into the report."""


# ── tree ─────────────────────────────────────────────────────────────


@dataclass
class Tree:
    root_id: str
    root_files: list[Item]
    folders: dict[str, Item]  # NFC name -> folder item (top level)
    files: dict[str, list[Item]]  # NFC folder name -> direct child files
    subfolders: dict[str, list[Item]]  # NFC folder name -> direct child folders

    def names_in(self, folder: str) -> set[str]:
        return {nfc(i.name) for i in self.files.get(folder, [])}

    def folder_ids(self) -> frozenset[str]:
        """Every folder id of the curated tree (root, top level, one level below).

        Passed to ``move_rename(allowed_parents=…)``: a move never leaves the tree.
        """
        ids = {self.root_id, *(f.id for f in self.folders.values())}
        for subs in self.subfolders.values():
            ids.update(f.id for f in subs)
        return frozenset(ids)


def walk(drive, root_id: str) -> Tree:
    root_items = drive.list_children(root_id)
    folders = {nfc(i.name): i for i in root_items if i.is_folder}
    root_files = [i for i in root_items if not i.is_folder]
    files: dict[str, list[Item]] = {}
    subfolders: dict[str, list[Item]] = {}
    for name, folder in folders.items():
        children = drive.list_children(folder.id)
        files[name] = [c for c in children if not c.is_folder]
        subfolders[name] = [c for c in children if c.is_folder]
    return Tree(root_id, root_files, folders, files, subfolders)


def _walk_all(drive, folder_id: str, prefix: str = "") -> list[tuple[str, Item]]:
    out: list[tuple[str, Item]] = []
    for item in drive.list_children(folder_id):
        path = f"{prefix}{nfc(item.name)}"
        if item.is_folder:
            out.extend(_walk_all(drive, item.id, path + "/"))
        else:
            out.append((path, item))
    return out


def _llm_down(failure: str | None) -> bool:
    """True when the local model is unavailable (abort the run); False for a
    per-document problem such as an answer truncated by the reasoning budget.

    ``failure`` is ``llm.LocalLLMError.failure`` (or ``Extraction.llm_failure``;
    None = the error did not come from the model). Decided by kind only: anything
    but ``llm.ANSWER``, including a kind this code does not know, means down
    (fail closed), so a dead LM Studio never costs one failure per document."""
    return failure is not None and failure != llm.ANSWER


def _model_failure_code(exc: llm.LocalLLMError) -> str:
    """Reason code (never the message) for a per-document model answer failure."""
    return "model_answer_truncated" if "truncated" in str(exc) else "model_answer_not_json"


def _backfill_entry(label: str, file_id: str, code: str | None, attempts: int) -> dict:
    """Report entry of a filed document whose sidecar backfill got a bad model
    answer: ``skipped`` (retried next run) until ``MAX_TECHNICAL_ATTEMPTS``, then
    ``review``. ``file_id`` is the key the caller counts attempts under."""
    return {
        "source": label,
        "result": "review" if attempts >= MAX_TECHNICAL_ATTEMPTS else "skipped",
        "reasons": [code or "model_answer_not_json"],
        "attempts": attempts,
        "stage": "backfill",
        "file_id": file_id,
    }


def backfill_attempts_from(report: dict | None) -> dict[str, dict]:
    """``{file_id: {"attempts", "code"}}`` of the backfill model failures in a
    run report (the caller merges these across runs, keeping the highest)."""
    out: dict[str, dict] = {}
    for d in (report or {}).get("documents", []):
        if d.get("stage") == "backfill" and d.get("file_id") and d.get("attempts"):
            out[d["file_id"]] = {
                "attempts": int(d["attempts"]),
                "code": (d.get("reasons") or [None])[0],
            }
    return out


def sidecar_sources(name: str) -> list[str]:
    """Possible source names for a sidecar.

    ``X.md`` belongs to ``X``. A Google Doc shows as ``X.gdoc`` on the Drive for
    Desktop mount (where the Cowork jobs named its sidecar ``X.gdoc.md``) but
    as plain ``X`` through the Drive API — so ``X.gdoc.md`` also pairs with ``X``.
    """
    if not name.endswith(".md"):
        return []
    base = name[:-3]
    return [base, base[:-5]] if base.endswith(".gdoc") else [base]


def _is_sidecar(name: str, siblings: set[str]) -> bool:
    """``X.md`` next to ``X`` — whatever X's extension (Google Docs have none)."""
    return any(src in siblings for src in sidecar_sources(name))


# ── preflight ────────────────────────────────────────────────────────


def preflight(drive, tree: Tree, contract: Contract) -> list[Gate]:
    gates: list[Gate] = []

    names = list(tree.folders)
    dups = [
        n for n in names if (m := DUP_FOLDER_RE.match(n)) and m.group("base") in contract.folders
    ]
    gates.append(Gate("no duplicate-name folders", not dups, ", ".join(dups)))

    stray = [i.name for i in drive.list_my_drive_root() if i.is_folder and YYYY_MM_RE.match(i.name)]
    gates.append(Gate("no YYYY-MM folders at Drive root", not stray, ", ".join(stray)))

    gates.append(Gate("no cloud-only placeholders", True, "n/a — Drive API reads server copies"))

    orphans: list[str] = []
    quarantined: list[str] = []
    for folder, items in tree.files.items():
        siblings = {nfc(i.name) for i in items}
        for n in siblings:
            if n.endswith(".md") and SOURCE_EXT_RE.search(n[:-3]) and not _is_sidecar(n, siblings):
                if folder.startswith("98-logs"):
                    continue
                # The duplicates folder is the quarantine: a sidecar parked there
                # without its source was put there on purpose — report, never block.
                bucket = quarantined if folder.startswith(DUPLICATES_PREFIX) else orphans
                bucket.append(f"{folder}/{n}")
    gates.append(Gate("every sidecar has its source", not orphans, ", ".join(orphans[:10])))
    if quarantined:
        gates.append(
            Gate(
                "quarantined sidecars without source",
                False,
                ", ".join(quarantined[:10]),
                blocking=False,
            )
        )

    unknown = sorted(set(tree.folders) - set(contract.folders))
    gates.append(Gate("folder set within README §2", not unknown, ", ".join(unknown)))

    nested = [
        f"{f}/{s.name}"
        for f, subs in tree.subfolders.items()
        for s in subs
        if contract.folders.get(f) is not None  # DocumentType folders must be flat
    ]
    # Advisory, as in the Cowork jobs' six gates: report it, never process inside it.
    gates.append(
        Gate("DocumentType folders are flat", not nested, ", ".join(nested[:10]), blocking=False)
    )
    return gates


# ── sidecar ──────────────────────────────────────────────────────────


def _y(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_sidecar(
    *,
    source_name: str,
    contract: Contract,
    item: Item,
    extraction,
    cls: classify_mod.Classification | None,
    now: datetime,
    note: str = "",
    extra: dict | None = None,
) -> str:
    raw = cls.raw if cls else {}
    fields = cls.fields if cls else None
    meta = {
        "source_file": source_name,
        "patient": contract.patient_display,
        "doc_date": (f"{fields.date[:4]}-{fields.date[4:6]}-{fields.date[6:]}" if fields else None),
        "institution": fields.institution if fields else raw.get("institution"),
        "doc_type": cls.doc_type if cls else None,
        "doctor": raw.get("doctor"),
        "hospital_unit": raw.get("hospital_unit"),
        "diagnoses": raw.get("diagnoses") or [],
        "language": raw.get("language"),
        "pages": extraction.pages if extraction else None,
        "extraction": extraction.method if extraction else None,
        "ocr_engine": extraction.engine if extraction else None,
        "handwritten": bool(extraction.handwritten) if extraction else False,
        # #609: ocr_version, rotation, rotation_source, has_chart, rotated_region
        # (vision readings only; optional keys, older sidecars lack them)
        **(_vision_keys(extraction)),
        "sha256": item.sha256 or None,
        "extracted_at": now.date().isoformat(),
        "review_needed": bool(cls.review_needed) if cls else True,
        "review_reasons": (cls.reasons if cls else []) + ([note] if note else []),
        "flags": cls.flags if cls else [],
        "summary": raw.get("summary"),
        "processed_by": "oncofiles curated pipeline (#562)",
    }
    meta.update(extra or {})
    if item.link and item.mime.startswith(extract_mod.GOOGLE_NATIVE_PREFIX):
        meta["link"] = item.link
    front = "\n".join(f"{k}: {_y(v)}" for k, v in meta.items())
    body = extraction.text.strip() if extraction and extraction.text else "(no text extracted)"
    return f"---\n{front}\n---\n\n{body}\n"


def _vision_keys(extraction) -> dict:
    keys = getattr(extraction, "front_matter", None)
    return keys() if callable(keys) else {}


def _vision_review_notes(extraction) -> list[str]:
    notes = getattr(extraction, "review_notes", None)
    return notes() if callable(notes) else []


def _review_reasons(sidecar_text: str) -> list[str]:
    m = re.search(r"^review_reasons:\s*(\[.*\])\s*$", sidecar_text, re.M)
    if not m:
        return []
    try:
        value = json.loads(m.group(1))
    except ValueError:
        return []
    return [str(r) for r in value] if isinstance(value, list) else []


# #919: the built name failed the checker; the document waits for a person
# instead of being filed under a name the dashboard would never call "named".
NAME_NOT_STANDARD_NOTE = "name_not_standard: the built filename does not follow the README standard"


def _is_technical(reason: str) -> bool:
    """A failure of the machinery, not a property of the document."""
    return reason.startswith("classification failed:") or (
        reason.startswith("extraction failed:") and "local LLM" in reason
    )


# ── #900: maintainer override of a vision soft review ────────────────

OVERRIDE_KEY = "review_override"
OVERRIDE_STATUS_KEY = "review_override_status"
OVERRIDE_FAILED = "hard_checks_failed"
OVERRIDE_REASON_MAX = 300  # characters of the reason kept in the run journal
# Review reasons that come from the vision reading (#609, ``Extraction.review_notes``),
# never from the document's content: the only ones an override may set aside.
VISION_SOFT_REASONS = {
    "chart_page": re.compile(r"^chart on page "),
    "rotated_region": re.compile(r"^rotated region on page "),
    "orientation_unsure": re.compile(r"^orientation unsure on page "),
}


def _split_sidecar(text: str) -> tuple[str, str] | None:
    """(front matter, rest) of a sidecar, or None without a front-matter block.

    ``rest`` starts right after the closing ``---`` (newlines included), so
    ``f"---\\n{front}\\n---{rest}"`` is the sidecar byte for byte."""
    m = re.match(r"\A---\n(.*?)\n---(?=\n|\Z)", text, re.S)
    return (m.group(1), text[m.end() :]) if m else None


def _front_value(front: str, key: str) -> str | None:
    """A front-matter value as text: JSON (as the runner writes it) or plain."""
    m = re.search(rf"^{re.escape(key)}:[ \t]*(.*?)[ \t]*$", front, re.M)
    if not m:
        return None
    raw = m.group(1)
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw.strip("'\"")
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return None
    return str(value).strip()


def review_override(sidecar_text: str) -> str | None:
    """The maintainer's override reason in an inbox sidecar, or None (#900).

    The rule: a non-empty ``review_override: "<reason>"`` in the front matter.
    For sidecars edited before the key existed, ``review_needed: false``
    together with a non-empty ``review_note`` counts the same. An override the
    runner already found blocked by a hard check (``review_override_status:
    hard_checks_failed``) is not active until a person removes that line.
    """
    parts = _split_sidecar(sidecar_text)
    if parts is None:
        return None
    front = parts[0]
    if _front_value(front, OVERRIDE_STATUS_KEY):
        return None
    reason = _front_value(front, OVERRIDE_KEY)
    if reason and reason.lower() not in ("false", "null", "none", "no"):
        return reason
    note = _front_value(front, "review_note")
    if _front_value(front, "review_needed") == "false" and note and note.lower() != "null":
        return note
    return None


def _vision_soft_codes(reasons: list[str]) -> list[str]:
    return [code for code, rx in VISION_SOFT_REASONS.items() if any(rx.match(r) for r in reasons)]


def _set_front(front: str, key: str, value) -> str:
    line = f"{key}: {_y(value)}"
    pattern = re.compile(rf"^{re.escape(key)}:.*$", re.M)
    if pattern.search(front):
        return pattern.sub(lambda _m: line, front, count=1)
    return f"{front}\n{line}"


def _override_blocked_sidecar(sidecar_text: str, reasons: list[str]) -> str:
    """The sidecar with only its review keys changed: the body stays byte for byte."""
    front, body = _split_sidecar(sidecar_text)
    front = _set_front(front, "review_needed", True)
    front = _set_front(front, "review_reasons", reasons)
    front = _set_front(front, OVERRIDE_STATUS_KEY, OVERRIDE_FAILED)
    return f"---\n{front}\n---{body}"


@dataclass
class _Override:
    reason: str
    sidecar_text: str
    dropped: list[str]  # vision soft reason codes set aside (from the old sidecar)


def _override_extraction(sidecar_text: str) -> extract_mod.Extraction:
    """The kept sidecar text as an extraction: no file is read, no model OCR runs."""
    front, body = _split_sidecar(sidecar_text)
    text = body.strip()
    if text == "(no text extracted)":
        text = ""
    pages = _front_value(front, "pages")
    return extract_mod.Extraction(
        text,
        _front_value(front, "extraction") or "sidecar",
        int(pages) if pages and pages.isdigit() else 0,
        _front_value(front, "ocr_engine") or "-",
    )


def _normalise_report() -> dict:
    return {
        "enabled": False,  # CURATED_NORMALISE_FILED for this run (real renames)
        "max_per_run": 0,
        # {folder, from, to, sidecar_from, sidecar_to, date_source}: within this
        # run's cap (a real run renames exactly these)
        "planned": [],
        "renamed": [],  # real runs: the planned entries that were done
        "deferred": [],  # over the cap / action budget: a later run does them
        "review": [],  # {source, code: name_not_standard, why}: no rename possible
        # {source, code}: month_only_date | no_sidecar | target_not_standard |
        # extension_changed (the last two: planner guards, #946)
        "skipped": [],
        "counts": {},
    }


def _stale_review_report() -> dict:
    return {
        "max_per_run": 0,
        # {source, sidecar, outcome: clear | reasons, codes, proposed_doc_type}:
        # within this run's cap (a real run writes exactly these)
        "planned": [],
        "done": [],  # real runs: the planned entries that were written
        "deferred": [],  # {source}: over the cap / action budget, a later run
        # {source, code}: not_found | no_sidecar | text_source | no_front_matter
        # | resolved | override | source_changed | model_answer_* | no_code
        "skipped": [],
        "counts": {},
    }


# ── #919 item 4: filed documents held in review without a reason ─────


def _front_lines_without(front: str, key: str) -> tuple[list[str], int | None]:
    """The front-matter lines without ``key`` (and its indented / list
    continuation lines), and the index where ``key`` stood (None = absent)."""
    lines = front.split("\n")
    out: list[str] = []
    at = None
    skipping = False
    for line in lines:
        if skipping and line.startswith((" ", "\t", "-")):
            continue
        skipping = False
        if re.match(rf"^{re.escape(key)}:", line):
            at = len(out) if at is None else at
            skipping = True
            continue
        out.append(line)
    return out, at


def _put_front(front: str, key: str, value) -> str:
    """``key: <json value>`` in place of the key's line(s), or appended."""
    lines, at = _front_lines_without(front, key)
    new = f"{key}: {_y(value)}"
    if at is None:
        lines.append(new)
    else:
        lines.insert(at, new)
    return "\n".join(lines)


def stale_review_skip(sidecar_text: str) -> str | None:
    """Why this filed sidecar is NOT a stale review to re-check, or None.

    A stale review: ``review_needed: true`` with no recognised reason code
    (``review_codes.codes_of`` gives nothing or only ``other``), the same rule
    as the read model (``document_stage.stale_review``), decided again from the
    sidecar itself because the index may lag the folder. A maintainer's
    override (#900) — active, or parked by a person with
    ``review_override_status`` — is never touched.
    """
    from oncofiles.curated.indexer import parse_sidecar  # indexer imports this module

    if _split_sidecar(sidecar_text) is None:
        return "no_front_matter"
    front, _pages = parse_sidecar(sidecar_text)
    if review_override(sidecar_text) is not None or front.get(OVERRIDE_STATUS_KEY):
        return "override"
    flag = front.get("review_needed")
    if not (flag is True or (isinstance(flag, str) and flag.strip().lower() == "true")):
        return "resolved"
    codes = review_codes.codes_of(front.get("review_reasons"))
    if any(code != review_codes.OTHER for code in codes):
        return "resolved"
    return None


def stale_review_cleared(sidecar_text: str, today) -> str:
    """The sidecar with only ``review_needed: false`` + who / when cleared it;
    every other byte (the body above all) unchanged."""
    front, rest = _split_sidecar(sidecar_text)
    front = _put_front(front, "review_needed", False)
    front = _put_front(front, "review_cleared_by", STALE_REVIEW_CLEARED_BY)
    front = _put_front(front, "review_cleared_at", today.isoformat())
    return f"---\n{front}\n---{rest}"


def stale_review_with_codes(sidecar_text: str, codes: list[str]) -> str:
    """The sidecar with only ``review_reasons`` set to recognised codes (the
    flag stays true); every other byte unchanged."""
    front, rest = _split_sidecar(sidecar_text)
    front = _put_front(front, "review_reasons", list(codes))
    return f"---\n{front}\n---{rest}"


def _vocab_report() -> dict:
    return {
        "auto_vocab_enabled": False,  # CURATED_AUTO_VOCAB for this run
        "readme_unsupported": None,  # why this README cannot be auto-edited, if so
        "planned": [],  # {label, token, document}: what a run with the feature on adds
        "planned_count": 0,
        "added": [],  # real runs with the feature on
        "added_count": 0,
        "outcomes": [],  # dry runs: per document, without vs with the feature
        "would_file_count": 0,
    }


_VOCAB_REVIEW_RE = re.compile(r"^new (institution|doctor) '.*' not in README vocabulary$")


def _is_vocabulary_only(reason: str) -> bool:
    """A #754 review reason that #854 no longer treats as ambiguity."""
    return reason == "institution unknown" or bool(_VOCAB_REVIEW_RE.match(reason))


def _age_seconds(modified: str, now: datetime) -> float | None:
    try:
        ts = datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds()


def _journal_summary(line: str) -> str:
    try:
        a = json.loads(line)
    except ValueError:
        return line[:80]
    target = a.get("to") or a.get("path") or ""
    if isinstance(target, list):
        target = target[0] if target else ""
    return f"{a.get('action')} {str(target)[:80]}"


# ── run ──────────────────────────────────────────────────────────────


class Runner:
    def __init__(
        self,
        drive,
        root_id: str,
        opts: RunOptions,
        *,
        extract_fn: Callable = extract_mod.extract,
        propose_fn: Callable = classify_mod.propose,
    ):
        self.drive = drive
        self.root_id = root_id
        self.opts = opts
        self.now = opts.now or datetime.now(UTC)
        self.run_id = opts.run_id or f"oncofiles-{self.now:%Y%m%dT%H%M}"
        self.report = RunReport(run_id=self.run_id, dry_run=opts.dry_run)
        self.extract_fn = extract_fn
        self.propose_fn = propose_fn
        self.actions_used = 0
        self.canary_id: str | None = None
        self._journal = f"{self.run_id} {JOURNAL_MARK}\n"
        self._technical_attempts: dict[str, int] = {}
        self._readme: Item | None = None
        self._readme_text = ""
        self._auto_vocab = False  # set per run once the README is known
        self._readme_vocab_ok = False  # README layout can be auto-edited
        # dry run with the feature off: README §3 as the feature WOULD grow it
        self._shadow_text = ""
        self._shadow_contract: Contract | None = None
        self._preview_requeue = False
        self._docs_base: int | None = None  # documents counted against the cap
        self._new_docs = 0  # documents this run sent to OCR (#855)

    # -- writes (no-ops in dry run, always logged) --

    def _journal_append(self, line: str) -> None:
        try:
            self.drive.update_text(self.canary_id, self._journal + line + "\n", "text/plain")
        except Exception as exc:
            raise RunAbortError(f"journal write failed ({type(exc).__name__})") from exc
        self._journal += line + "\n"

    def _confirm(self, seq: int) -> None:
        """Journal that action ``seq`` is complete and verified."""
        if self.canary_id and not self.opts.dry_run:
            self._journal_append(json.dumps({"ok": seq}))

    def _act(
        self,
        action: dict,
        fn: Callable | None,
        *,
        pair_half: bool = False,
        confirm: bool = True,
    ) -> int:
        """Log the action first, then perform it — so a failure is always on record.

        ``pair_half``: this write moves one half of a source+sidecar pair; if it
        fails after Drive applied it, the pair may be split, and the message says so.
        ``confirm=False``: the caller confirms (``_confirm(seq)``) once the rest
        of the action is done. Returns the action's journal ``seq``.
        """
        action = {"ts": self.now.strftime("%Y-%m-%dT%H:%M"), "run": self.run_id, **action}
        self.report.actions.append(action)
        self.actions_used += 1
        seq = len(self.report.actions)
        if self.opts.dry_run or fn is None:
            return seq
        if self.canary_id:  # journal first, so a kill mid-action is on record
            entry = {k: v for k, v in action.items() if k != "undo"}
            self._journal_append(json.dumps({**entry, "seq": seq}, ensure_ascii=False))
        try:
            fn()
        except Exception as exc:  # unverified write, Drive error, refused write, …
            action["status"] = "failed"
            split = " — pair possibly split, see undo" if pair_half else ""
            kind = "not verified" if isinstance(exc, WriteVerificationError) else "failed"
            raise RunAbortError(
                f"write {kind} ({type(exc).__name__}): {action['action']}{split}"
            ) from exc
        action["status"] = "done"
        if confirm:
            self._confirm(seq)
        return seq

    def run(self) -> RunReport:
        try:
            self._run()
        except RunAbortError as exc:
            self.report.status = "aborted"
            self.report.message = str(exc)
        except ContractError as exc:
            self.report.status = "aborted"
            self.report.message = f"contract: {exc}"
        except Exception as exc:  # never report an unexpected crash as a clean run
            logger.exception("curated run crashed")
            self.report.status = "aborted"
            self.report.message = f"unexpected error ({type(exc).__name__})"
        return self.report

    def _run(self) -> None:
        tree = walk(self.drive, self.root_id)
        readme = next((i for i in tree.root_files if i.name == "README.md"), None)
        if readme is None or "_index.json" not in {i.name for i in tree.root_files}:
            raise RunAbortError("not a curated folder (README.md + _index.json missing)")
        readme_text = self.drive.download(readme.id).decode("utf-8")
        contract = parse_contract(readme_text)
        self._contract = contract
        self._readme, self._readme_text = readme, readme_text
        self._set_auto_vocab(readme_text, contract)
        self._shadow_text, self._shadow_contract = readme_text, contract
        # A dry run previews documents held only for vocabulary even with the
        # feature off (nothing is written; a real run with it off skips them).
        self._preview_requeue = self.opts.dry_run and self._readme_vocab_ok
        self.report.contract_version = contract.version

        if not self.opts.dry_run:
            self._clear_stale_canary(tree)
            try:
                self.canary_id = self.drive.create_text(
                    self.root_id, CANARY, self._journal, "text/plain"
                )
                self.report.gates.append(Gate("canary write + read-back", True))
            except Exception as exc:
                self.report.gates.append(
                    Gate("canary write + read-back", False, type(exc).__name__)
                )
                raise RunAbortError("preflight failed: canary write") from exc
        else:
            self.report.gates.append(Gate("canary write + read-back", True, "skipped (dry run)"))

        # From here on the canary exists: always log the outcome and clean up,
        # including a preflight abort (a run that stops and explains is a success).
        try:
            self.report.gates.extend(preflight(self.drive, tree, contract))
            for g in self.report.gates:
                if not g.ok and not g.blocking:
                    self.report.flags.append(f"{g.name}: {g.detail} (not processed)")
            failed = [g for g in self.report.gates if not g.ok and g.blocking]
            if failed:
                raise RunAbortError(
                    "preflight failed: " + "; ".join(f"{g.name} ({g.detail})" for g in failed)
                )
            stray_root = [
                i.name
                for i in tree.root_files
                if i.name not in ROOT_STATE_FILES and i.name != CANARY
            ]
            if stray_root:
                self.report.flags.append(
                    "root holds files outside the four state files: " + ", ".join(stray_root)
                )
            self._process(tree, contract)
            # #919: after the inbox, before the backfill (whose new sidecars do
            # not exist yet in a dry run) and before the index that follows the run
            self._normalise_filed(tree, self._contract)
            # #919 item 4: stale review flags of filed documents (key-only edits)
            self._recheck_stale_reviews(tree, self._contract)
            self._backfill_sidecars(tree, contract)
            if not self.opts.dry_run:
                self._recheck(contract)
            self.report.inbox_drained = not self.report.not_processed and all(
                h.get("reason") == HOLD_DOCUMENT_CAP for h in self.report.held
            )
        except RunAbortError as exc:
            self.report.status = "aborted"
            self.report.message = str(exc)
        except Exception as exc:  # a crash is logged as an abort, never as a clean run
            logger.exception("curated run crashed")
            self.report.status = "aborted"
            self.report.message = f"unexpected error ({type(exc).__name__})"
        finally:
            if not self.opts.dry_run:
                self._write_state(tree, contract)

    # -- vocabulary (#854) --

    def _set_auto_vocab(self, readme_text: str, contract: Contract) -> None:
        reason = vocab_mod.unsupported_reason(readme_text, contract)
        self._readme_vocab_ok = reason is None
        self.report.vocabulary["auto_vocab_enabled"] = bool(self.opts.auto_vocab)
        self.report.vocabulary["readme_unsupported"] = reason
        self._auto_vocab = bool(self.opts.auto_vocab) and reason is None
        if self.opts.auto_vocab and reason:  # fail closed: new values go to review as before
            self.report.flags.append(f"automatic vocabulary off for this README: {reason}")

    def _record_vocabulary(self, tokens: list[dict], source: str, kind: str) -> None:
        """Report tokens as ``planned`` or ``added``; server logs see counts + hashes."""
        if not tokens:
            return
        v = self.report.vocabulary
        v[kind].extend({**t, "document": source} for t in tokens)
        v[f"{kind}_count"] = len(v[kind])
        logger.info(
            "curated %s: README vocabulary %s %d token(s) [%s]",
            self.run_id,
            kind,
            len(tokens),
            ", ".join(vocab_mod.token_digest(t["token"]) for t in tokens),
        )

    def _preview_vocabulary(self, cls, raw: dict, text: str, source: str) -> None:
        """Dry run only: the document's outcome without and with the feature.

        With CURATED_AUTO_VOCAB=false the "with" side is a pure second
        validation against a shadow README that grows like the real one would;
        its additions are reported as ``planned``. Nothing is written.
        """
        kw = {"today": self.now.date(), "max_age_days": self.opts.max_doc_age_days}
        if self._auto_vocab:
            with_cls = cls  # planned tokens were recorded by _write_vocabulary
            without_cls = classify_mod.validate(self._contract, raw, text, auto_vocab=False, **kw)
        else:
            without_cls = cls
            with_cls = classify_mod.validate(
                self._shadow_contract, raw, text, auto_vocab=True, **kw
            )
            if with_cls.additions and not with_cls.review_needed:
                try:
                    new_text, after = vocab_mod.add_to_readme(
                        self._shadow_text,
                        self._shadow_contract,
                        with_cls.additions,
                        run_id=self.run_id,
                        day=f"{self.now:%Y-%m-%d}",
                    )
                except vocab_mod.AutoVocabUnsupportedError:
                    with_cls.review_needed = True
                    with_cls.additions = []
                else:
                    known = self._shadow_contract.known_tokens
                    tokens = [
                        {"label": a.label, "token": a.token}
                        for a in with_cls.additions
                        if a.token not in known
                    ]
                    self._shadow_text, self._shadow_contract = new_text, after
                    self._record_vocabulary(tokens, source, "planned")
        files_without = not without_cls.review_needed
        files_with = not with_cls.review_needed
        if files_without == files_with and not with_cls.additions:
            return  # the feature changes nothing for this document
        v = self.report.vocabulary
        v["outcomes"].append(
            {
                "document": source,
                "outcome_without": "filed_without" if files_without else "review_without",
                "outcome_with": (
                    "would_file_with_auto_vocab" if files_with else "would_review_with_auto_vocab"
                ),
                "tokens": [{"label": a.label, "token": a.token} for a in with_cls.additions],
            }
        )
        v["would_file_count"] = sum(
            1
            for o in v["outcomes"]
            if o["outcome_with"] == "would_file_with_auto_vocab"
            and o["outcome_without"] == "review_without"
        )

    def _plan_vocabulary(self, cls: classify_mod.Classification) -> tuple[str, Contract] | None:
        """The README text with this document's new tokens, or None (nothing new).

        Pure. A README the code cannot edit safely sends the document to review.
        """
        if not cls.additions or cls.review_needed:
            return None
        try:
            text, after = vocab_mod.add_to_readme(
                self._readme_text,
                self._contract,
                cls.additions,
                run_id=self.run_id,
                day=f"{self.now:%Y-%m-%d}",
            )
        except vocab_mod.AutoVocabUnsupportedError as exc:
            cls.review_needed = True
            cls.reasons.append(f"automatic vocabulary refused: {exc}")
            cls.additions = []
            return None
        return (text, after) if text != self._readme_text else None

    def _write_vocabulary(self, cls, planned: tuple[str, Contract], source: str) -> None:
        """Journal, then write README §3 (verified, never retried); abort on failure."""
        text, after = planned
        new = [a for a in cls.additions if a.token not in self._contract.known_tokens]
        tokens = [{"label": a.label, "token": a.token} for a in new]
        before_sha = hashlib.sha256(self._readme_text.encode("utf-8")).hexdigest()
        after_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        readme_id = self._readme.id

        def _write() -> None:
            # Someone edited the README since this run read it: never overwrite that.
            current = self.drive.download(readme_id).decode("utf-8")
            if current != self._readme_text:
                raise WriteVerificationError("README changed during the run — not overwritten")
            self.drive.update_text(readme_id, text)

        self._act(
            {
                "action": "readme_vocabulary_add",
                "path": "README.md",
                "tokens": tokens,
                "for": source,
                "sha256_before": before_sha,
                "sha256_after": after_sha,
                "undo": {
                    "op": "readme_vocabulary_remove",
                    "path": "README.md",
                    "tokens": tokens,
                    "restores_sha256": before_sha,
                },
            },
            None if self.opts.dry_run else _write,
        )
        self._readme_text, self._contract = text, after
        self._record_vocabulary(tokens, source, "planned" if self.opts.dry_run else "added")

    # -- documents --

    def _candidates(self, tree: Tree, contract: Contract) -> list[tuple[str, Item]]:
        out: list[tuple[str, Item]] = []
        inbox = contract.inbox
        for folder, items in ((inbox, tree.files.get(inbox, [])), ("", tree.root_files)):
            siblings = {nfc(i.name) for i in items}
            for i in items:
                n = nfc(i.name)
                if folder == "" and (n in ROOT_STATE_FILES or n == CANARY):
                    continue
                if _is_sidecar(n, siblings):
                    continue
                out.append((folder, i))
        return sorted(out, key=lambda fi: fi[1].modified or "")

    def _existing_sidecar(self, tree: Tree, folder: str, item: Item) -> Item | None:
        items = tree.files.get(folder, []) if folder else tree.root_files
        want = {nfc(item.name) + ".md", nfc(item.name) + ".gdoc.md"}
        return next((i for i in items if nfc(i.name) in want), None)

    def _filed_hashes(self, tree: Tree, contract: Contract) -> dict[str, tuple[str, str]]:
        out: dict[str, tuple[str, str]] = {}
        for folder, items in tree.files.items():
            if contract.folders.get(folder) is None:
                continue  # only DocumentType folders hold survivors
            for i in items:
                if i.sha256:
                    out[i.sha256] = (folder, nfc(i.name))
        return out

    def _process(self, tree: Tree, contract: Contract) -> None:
        candidates = self._candidates(tree, contract)
        filed = self._filed_hashes(tree, contract)
        done = 0
        stopping = False
        for folder, item in candidates:
            contract = self._contract  # README §3 may have grown (#854)
            label = f"{folder or '(root)'}/{item.name}"
            if not stopping and self.opts.should_stop and self.opts.should_stop():
                stopping = True
                self.report.flags.append("stopped early: the server is shutting down")
            if stopping or done >= self.opts.max_docs:
                self.report.not_processed.append(label)
                continue
            if self.actions_used + ACTIONS_PER_DOCUMENT > self.opts.max_actions:
                self.report.not_processed.append(label)
                continue
            if _is_text_source(item) and (item.id, item.modified) in self.opts.reviewed_text:
                self.report.awaiting_review.append(label)
                continue
            sidecar = self._existing_sidecar(tree, folder, item)
            override = None
            if sidecar is not None:
                side_text = self._sidecar_text(sidecar)
                if self._awaiting_review(sidecar, item, side_text):
                    self.report.awaiting_review.append(label)
                    continue
                override = self._override_for(side_text, item)
            duplicate = bool(item.sha256 and item.sha256 in filed)
            # an override reads no file and runs no OCR: only the document cap applies
            if not duplicate and (hold := self._hold_reason(tree, contract, ocr=not override)):
                self.report.held.append({"source": label, "reason": hold})
                continue  # #855: stays in the inbox, unread
            done += 1
            if duplicate:
                self._quarantine(tree, contract, folder, item, sidecar, filed[item.sha256])
                continue
            self._new_docs += 1
            self._file_document(tree, contract, folder, item, sidecar, override)
            last = self.report.documents[-1] if self.report.documents else {}
            if item.sha256 and last.get("source") == item.name and last.get("result") == "filed":
                # Later candidates in this same run must see it as already filed:
                # two identical uploads in one batch → second one is a duplicate.
                to_folder, _, to_name = last["to"].partition("/")
                filed[item.sha256] = (to_folder, nfc(to_name))

    def _filed_count(self, tree: Tree, contract: Contract) -> int:
        n = 0
        for folder, doc_type in contract.folders.items():
            if doc_type is None:
                continue
            items = tree.files.get(folder, [])
            names = {nfc(i.name) for i in items}
            n += sum(1 for i in items if not _is_sidecar(nfc(i.name), names))
        return n

    def _hold_reason(self, tree: Tree, contract: Contract, *, ocr: bool = True) -> str | None:
        """Why this document must not be read by the model now (#855), or None.

        ``ocr=False``: the document is not OCR'd (#900 override), so only the
        document cap applies, not the page budget."""
        if self.opts.doc_cap > 0:
            if self._docs_base is None:
                self._docs_base = max(self.opts.docs_existing, self._filed_count(tree, contract))
            if self._docs_base + self._new_docs >= self.opts.doc_cap:
                return HOLD_DOCUMENT_CAP
        return self.opts.ocr_gate() if ocr and self.opts.ocr_gate else None

    def _content(self, item: Item) -> bytes:
        """Bytes to extract from: Drive's text export for Google Docs/Sheets/Slides."""
        export_mime = extract_mod.NATIVE_EXPORT.get(item.mime)
        if export_mime:
            return self.drive.export(item.id, export_mime)
        if item.mime.startswith(extract_mod.GOOGLE_NATIVE_PREFIX):
            return b""  # Forms, Drawings…: nothing to read
        return self.drive.download(item.id)

    def _sidecar_text(self, sidecar: Item) -> str:
        return self.drive.download(sidecar.id).decode("utf-8", errors="replace")

    def _override_for(self, sidecar_text: str, item: Item) -> _Override | None:
        """An active maintainer override (#900) for an unchanged source, or None."""
        if _split_sidecar(sidecar_text) is None:
            return None
        reason = review_override(sidecar_text)
        if reason is None:
            return None
        sha = re.search(r'^sha256:\s*"?([0-9a-f]{64})', sidecar_text[:4000], re.M)
        if sha and item.sha256 and sha.group(1) != item.sha256:
            return None  # the file changed since the override: read it again
        dropped = _vision_soft_codes(_review_reasons(sidecar_text[:4000]))
        return _Override(reason[:OVERRIDE_REASON_MAX], sidecar_text, dropped)

    def _awaiting_review(self, sidecar: Item, item: Item, text: str | None = None) -> bool:
        if text is None:
            text = self._sidecar_text(sidecar)
        if self._override_for(text, item) is not None:
            return False  # #900: the maintainer decided; the run files it (hard checks)
        text = text[:4000]
        review = re.search(r"^review_needed:\s*(true|\"true\")", text, re.M | re.I)
        sha = re.search(r"^sha256:\s*\"?([0-9a-f]{64})", text, re.M)
        unchanged = sha is None or not item.sha256 or sha.group(1) == item.sha256
        if not (review and unchanged):
            return False
        if item.mime in extract_mod.NATIVE_EXPORT and re.search(
            r'^ocr_engine:\s*"?google-native', text, re.M
        ):
            return False  # written before Google Docs were exported as text: read it now
        reasons = _review_reasons(text)
        requeue = self._auto_vocab or self._preview_requeue
        if reasons and requeue and all(_is_vocabulary_only(r) for r in reasons):
            return False  # #854: only a new institution / doctor kept it here — file it now
        if reasons and all(_is_technical(r) for r in reasons):
            m = re.search(r"^technical_attempts:\s*(\d+)", text, re.M)
            attempts = int(m.group(1)) if m else 1
            if attempts < MAX_TECHNICAL_ATTEMPTS:
                self._technical_attempts[item.id] = attempts
                return False  # retry: nobody needs to look at a technical failure
        return True

    def _folder_id(self, tree: Tree, folder: str) -> str:
        return tree.folders[folder].id if folder else self.root_id

    def _quarantine(self, tree, contract, folder, item, sidecar, survivor) -> None:
        dup = contract.duplicates
        if dup not in tree.folders:
            self.report.flags.append(f"exact duplicate of {survivor[0]}/{survivor[1]} but no {dup}")
            return
        taken = tree.names_in(dup)
        moving = [item] + ([sidecar] if sidecar else [])
        if any(nfc(m.name) in taken for m in moving):
            self.report.flags.append(f"{item.name}: duplicate, but the name is taken in {dup}")
            return
        src_id, dst_id = self._folder_id(tree, folder), tree.folders[dup].id
        for m in moving:
            self._act(
                {
                    "action": "quarantine_duplicate",
                    "from": f"{folder}/{m.name}",
                    "to": f"{dup}/{m.name}",
                    "survivor": f"{survivor[0]}/{survivor[1]}",
                    "sha256": item.sha256,
                    "undo": {"op": "move", "from": f"{dup}/{m.name}", "to": f"{folder}/{m.name}"},
                },
                lambda m=m: self.drive.move_rename(
                    m.id, m.name, src_id, dst_id, allowed_parents=tree.folder_ids()
                ),
                pair_half=len(moving) > 1,
            )
            tree.files.setdefault(dup, []).append(m)
        self.report.documents.append(
            {
                "source": item.name,
                "result": "quarantined",
                "survivor": f"{survivor[0]}/{survivor[1]}",
            }
        )

    def _file_document(self, tree, contract, folder, item, sidecar, override=None) -> None:
        native = item.mime.startswith(extract_mod.GOOGLE_NATIVE_PREFIX)
        if override is not None:
            # #900: the maintainer's sidecar text is the reading; never re-OCR it.
            # Chart blocks stay fenced in it, so the parsers still skip them.
            ex = _override_extraction(override.sidecar_text)
            logger.info("curated %s: review override used for one document", self.run_id)
        elif item.size > extract_mod.MAX_FILE_BYTES:  # never download it
            limit = extract_mod.MAX_FILE_BYTES >> 20
            ex = extract_mod.Extraction("", "none", 0, "-", error=f"file larger than {limit} MB")
        else:
            content = self._content(item)
            ex = self.extract_fn(
                item.name, content, item.mime, min_text_chars=self.opts.min_text_chars
            )
            self.report.count_orientation(ex)
        cls = None
        raw = None
        note = ""
        if not ex.ok:
            note = f"extraction failed: {ex.error}"
            if _llm_down(getattr(ex, "llm_failure", None)):
                raise RunAbortError(f"local model unavailable — {ex.error}")
        elif not ex.text.strip():
            note = "no text to classify"
        elif reason := llm_input.inbox_exclusion(item.name, item.mime, ex.text):
            # raw genotype data (#815): never sent to the model; stays for review
            note = f"{reason}: not sent to the model"
        else:
            try:
                raw = self.propose_fn(contract, ex.text)
            except llm.LocalLLMError as exc:
                if _llm_down(exc.failure):
                    raise RunAbortError(f"local model unavailable — {exc}") from exc
                note = f"classification failed: {exc}"  # this document stays for review
        if cls is None and raw is not None:
            cls = classify_mod.validate(
                contract,
                raw,
                ex.text,
                today=self.now.date(),
                max_age_days=self.opts.max_doc_age_days,
                auto_vocab=self._auto_vocab,
            )
        if cls is not None and override is None and (why := _vision_review_notes(ex)):
            # #609: a chart, a rotated region or an unsure orientation goes to a
            # person before anything is filed (a confident rotation does not)
            cls.review_needed = True
            cls.reasons = [*cls.reasons, *why]
        planned_vocab = self._plan_vocabulary(cls) if cls is not None else None
        if cls is not None and self.opts.dry_run and self._readme_vocab_ok:
            self._preview_vocabulary(cls, raw, ex.text, item.name)
        self.report.flags.extend(f"{item.name}: {f}" for f in (cls.flags if cls else []))

        target_folder = contract.folder_for(cls.doc_type) if cls and not cls.review_needed else None
        if target_folder and target_folder not in tree.folders:
            note = f"target folder {target_folder!r} does not exist (never created)"
            target_folder = None
        # Final name first, so the sidecar's source_file names the file it ends up
        # next to — and a name the checker would reject is never filed (#919).
        new_name = item.name
        if target_folder is not None:
            built = self._standard_name(tree, target_folder, item, cls, native)
            if built is None:
                note = NAME_NOT_STANDARD_NOTE
                target_folder = None
            else:
                new_name = built
        review = target_folder is None

        inbox = contract.inbox
        if review and not folder:
            # The pair would move to the inbox under its own name; never create
            # same-name siblings there (Drive allows them, the contract does not).
            taken = tree.names_in(inbox)
            if nfc(item.name) in taken or nfc(item.name) + ".md" in taken:
                self.report.flags.append(
                    f"{item.name}: needs review but the name is taken in {inbox} — left in root"
                )
                self.report.documents.append(
                    {"source": item.name, "result": "skipped", "reasons": ["name taken in inbox"]}
                )
                return
        stay_folder = folder if folder else inbox  # loose root files go to the inbox for review
        src_folder_id = self._folder_id(tree, folder)
        extra = None
        if note and _is_technical(note):
            extra = {"technical_attempts": self._technical_attempts.get(item.id, 0) + 1}
        text = render_sidecar(
            source_name=new_name,
            contract=contract,
            item=item,
            extraction=ex,
            cls=cls,
            now=self.now,
            note=note,
            extra=extra,
        )
        if review:
            text = text.replace("review_needed: false", "review_needed: true")

        # 0) README §3 first (#854): a filed name never uses a token the README lacks
        if planned_vocab and not review:
            self._write_vocabulary(cls, planned_vocab, item.name)

        # Text files (notes, timelines, CSV) are their own text: no sidecar (README §4).
        if item.name.rsplit(".", 1)[-1].lower() in extract_mod.TEXT_EXTS:
            self._file_text_source(tree, folder, item, review, target_folder, new_name, cls, note)
            return

        # 1) sidecar next to the source, under the source's current name
        side_name = f"{item.name}.md"
        if override is not None:
            self._keep_override_sidecar(folder, side_name, sidecar, override, review, cls, note)
            side_id = sidecar.id
        elif sidecar is None:
            box = {}
            self._act(
                {
                    "action": "write_sidecar",
                    "path": f"{folder}/{side_name}",
                    "undo": {"op": "quarantine", "path": f"{folder}/{side_name}"},
                },
                lambda: box.setdefault(
                    "id", self.drive.create_text(src_folder_id, side_name, text)
                ),
            )
            side_id = box.get("id", "dry-run")
        else:
            self._act(
                {
                    "action": "update_sidecar",
                    "path": f"{folder}/{side_name}",
                    "undo": {"op": "none"},
                },
                lambda: self.drive.update_text(sidecar.id, text),
            )
            side_id = sidecar.id

        marks = {"review_override": True, "override_dropped": override.dropped} if override else {}
        if review:
            if not folder:  # loose root file → inbox, pair together
                self._move_pair(tree, "", inbox, item, side_id, item.name)
            self.report.documents.append(
                {
                    "source": item.name,
                    "result": "review_needed",
                    "stays_in": stay_folder,
                    "reasons": (cls.reasons if cls else []) + ([note] if note else []),
                    **marks,
                }
            )
            return

        self._move_pair(tree, folder, target_folder, item, side_id, new_name)
        self.report.documents.append(
            {
                "source": item.name,
                "result": "filed",
                "to": f"{target_folder}/{new_name}",
                "extraction": ex.method,
                "flags": cls.flags,
                **marks,
            }
        )

    def _standard_name(self, tree, target_folder, item, cls, native) -> str | None:
        """The filed name for ``item``, or None when the builder cannot produce one
        that the checker (``naming.is_standard``) accepts (#919)."""
        taken = tree.names_in(target_folder)
        if native:  # a Google Doc's title has no extension (the mount shows .gdoc)
            taken = taken | {f"{n}.{NATIVE_EXT}" for n in taken}
            ext = NATIVE_EXT
        else:
            ext = item.name.rsplit(".", 1)[-1] if "." in item.name else "bin"
        try:
            name = with_collision_suffix(build_filename(cls.fields, ext), taken)
        except ValueError:
            return None
        if native:
            name = name.removesuffix(f".{NATIVE_EXT}")
        if not is_standard(name, mime=item.mime):  # the same check as document_stage
            return None
        return name

    def _keep_override_sidecar(self, folder, side_name, sidecar, override, review, cls, note):
        """#900: journal the override; the sidecar text is never re-rendered.

        Filed: no sidecar write at all (the maintainer's front matter, incl. the
        override and its reason, moves with the source). Still review (a hard
        check failed on the kept text): only the review keys of the front
        matter change, the body stays byte for byte, and
        ``review_override_status: hard_checks_failed`` parks it until a person
        looks again (removing that line re-activates the override).
        """
        path = f"{folder}/{side_name}"
        self._act(
            {
                "action": "review_override",
                "path": path,
                "reason": override.reason,  # Drive journal only, never server logs
                "dropped": override.dropped,
                "outcome": "review" if review else "file",
                "undo": {"op": "none"},
            },
            lambda: None,
        )
        if review:
            reasons = (cls.reasons if cls else []) + ([note] if note else [])
            text = _override_blocked_sidecar(override.sidecar_text, reasons)
            self._act(
                {"action": "update_sidecar_review_keys", "path": path, "undo": {"op": "none"}},
                lambda: self.drive.update_text(sidecar.id, text),
            )

    def _file_text_source(self, tree, folder, item, review, target_folder, new_name, cls, note):
        """Move a text source on its own (it has no sidecar)."""
        reasons = (cls.reasons if cls else []) + ([note] if note else [])
        inbox = self._contract.inbox
        if review:
            if not folder:
                self._move_single(tree, "", inbox, item, item.name)
            self.report.documents.append(
                {
                    "source": item.name,
                    "result": "review_needed",
                    "stays_in": folder or inbox,
                    "reasons": reasons,
                    # Lets later runs skip it until the file changes (no sidecar).
                    "text_source": {"id": item.id, "modified": item.modified},
                }
            )
            return
        self._move_single(tree, folder, target_folder, item, new_name)
        self.report.documents.append(
            {
                "source": item.name,
                "result": "filed",
                "to": f"{target_folder}/{new_name}",
                "extraction": "plain",
                "flags": cls.flags if cls else [],
            }
        )

    def _move_single(self, tree, from_folder, to_folder, item, new_name) -> None:
        if nfc(new_name) in tree.names_in(to_folder):
            raise RunAbortError(f"{new_name!r} already exists in {to_folder}")
        src_id, dst_id = self._folder_id(tree, from_folder), tree.folders[to_folder].id
        self._act(
            {
                "action": "move",
                "from": f"{from_folder}/{item.name}",
                "to": f"{to_folder}/{new_name}",
                "sha256": item.sha256,
                "undo": {
                    "op": "move",
                    "from": f"{to_folder}/{new_name}",
                    "to": f"{from_folder}/{item.name}",
                },
            },
            lambda: self.drive.move_rename(
                item.id, new_name, src_id, dst_id, allowed_parents=tree.folder_ids()
            ),
        )
        tree.files.setdefault(to_folder, []).append(
            Item(id=item.id, name=new_name, mime=item.mime, parent=dst_id)
        )

    # -- #919: filed names that fail the standard --

    def _normalise_filed(self, tree: Tree, contract: Contract) -> None:
        """Rename filed documents whose name fails ``naming.is_standard`` (#919).

        The target name comes from ``normalise.plan_name`` (sidecar front matter,
        the folder's DocumentType, a validated printed date; the same builder as
        inbox filing). Same folder, extension kept, source + sidecar renamed as
        one journaled pair, at most ``normalise_max`` renames per run. A dry run
        plans and writes nothing; with ``normalise_filed`` off a real run does
        nothing (the preview still shows the plan). No date printed in the text
        → no rename, code ``name_not_standard``; a month-only note → skipped.
        """
        from oncofiles.curated.indexer import parse_sidecar  # indexer imports this module

        rep = self.report.normalise
        real = not self.opts.dry_run
        cap = max(0, int(self.opts.normalise_max))
        rep["enabled"] = bool(self.opts.normalise_filed)
        rep["max_per_run"] = cap
        if real and not self.opts.normalise_filed:
            return
        for folder, doc_type in contract.folders.items():
            if doc_type is None or folder not in tree.folders:
                continue
            items = list(tree.files.get(folder, []))
            names = {nfc(i.name) for i in items}  # snapshot: sidecar detection
            current = set(names)  # live: collisions after earlier renames
            for item in items:
                n = nfc(item.name)
                if n.startswith(".") or _is_sidecar(n, names) or is_standard(n, mime=item.mime):
                    continue
                label = f"{folder}/{item.name}"
                if normalise_mod.is_month_only(n):
                    rep["skipped"].append({"source": label, "code": normalise_mod.CODE_MONTH_ONLY})
                    continue
                # a text note is its own text (no sidecar); a Google Doc never is
                text_source = _is_text_source(item) and not is_google_native(item.mime)
                sidecar = None if text_source else self._existing_sidecar(tree, folder, item)
                if not text_source and sidecar is None:
                    rep["skipped"].append({"source": label, "code": normalise_mod.CODE_NO_SIDECAR})
                    continue
                cost = 1 if sidecar is None else 2
                over = (
                    len(rep["planned"]) >= cap
                    or self.actions_used + cost > self.opts.max_actions
                    or bool(self.opts.should_stop and self.opts.should_stop())
                )
                if over and real:  # a real run stays short: read nothing more
                    rep["deferred"].append({"source": label})
                    continue
                raw = self._sidecar_text(sidecar or item)
                front, pages = parse_sidecar(raw)
                own = {n} | ({nfc(sidecar.name)} if sidecar else set())
                kw = {
                    "name": n,
                    "mime": item.mime,
                    "doc_type": doc_type,
                    "front": front,
                    "contract": contract,
                    "today": self.now.date(),
                    "taken": current - own,
                }
                body = "\n".join(pages)
                plan = normalise_mod.plan_name(text=body, **kw)
                if plan.why == normalise_mod.WHY_NO_PRINTED_DATE and (
                    item.mime in extract_mod.NATIVE_EXPORT
                ):
                    # a pre-runner Google Doc sidecar may hold no text: read the
                    # document's own text export (a read, never a write)
                    exported = self._content(item).decode("utf-8", errors="replace")
                    plan = normalise_mod.plan_name(text=f"{body}\n{exported}", **kw)
                if plan.code in normalise_mod.GUARD_CODES:  # #946: never planned
                    rep["skipped"].append({"source": label, "code": plan.code})
                    continue
                if plan.target is None:
                    rep["review"].append({"source": label, "code": plan.code, "why": plan.why})
                    continue
                entry = {
                    "folder": folder,
                    "from": item.name,
                    "to": plan.target,
                    "sidecar_from": sidecar.name if sidecar else None,
                    "sidecar_to": f"{plan.target}.md" if sidecar else None,
                    "date_source": plan.date_source,
                }
                if over:  # dry run: show what a later run would do
                    rep["deferred"].append(entry)
                    continue
                rep["planned"].append(entry)
                logger.info(
                    "curated %s: filed name %s -> %s%s",
                    self.run_id,
                    normalise_mod.name_shape(item.name),
                    normalise_mod.name_shape(plan.target),
                    " (pair)" if sidecar else "",
                )
                self._rename_in_place(tree, folder, item, sidecar, plan.target)
                current -= own
                current |= {nfc(plan.target)} | ({nfc(entry["sidecar_to"])} if sidecar else set())
                if real:
                    rep["renamed"].append(entry)
        rep["counts"] = {
            "planned": len(rep["planned"]),
            "renamed": len(rep["renamed"]),
            "deferred": len(rep["deferred"]),
            "review": len(rep["review"]),
            "month_only": sum(
                1 for s in rep["skipped"] if s["code"] == normalise_mod.CODE_MONTH_ONLY
            ),
            "no_sidecar": sum(
                1 for s in rep["skipped"] if s["code"] == normalise_mod.CODE_NO_SIDECAR
            ),
            **{
                code: sum(1 for s in rep["skipped"] if s["code"] == code)
                for code in normalise_mod.GUARD_CODES
            },
        }
        if any(rep["counts"].values()):
            logger.info(
                "curated %s: filed-name normalisation %s",
                self.run_id,
                " ".join(f"{k}={v}" for k, v in rep["counts"].items()),
            )

    def _rename_in_place(self, tree, folder, item, sidecar, new_name) -> None:
        """Rename source (+ sidecar) inside ``folder``: journaled first, confirmed
        only after both renames are verified (``drive_ops`` reads back, no retry)."""
        fid = tree.folders[folder].id
        allowed = tree.folder_ids()
        old = [f"{folder}/{item.name}"]
        new = [f"{folder}/{new_name}"]
        new_side = f"{new_name}.md"
        ids = [item.id]
        if sidecar is not None:
            old.append(f"{folder}/{sidecar.name}")
            new.append(f"{folder}/{new_side}")
            ids.append(sidecar.id)
        seq = self._act(
            {
                "action": "normalise_rename",
                "from": old,
                "to": new,
                # Infra review of #946: the journal line (which never carries
                # ``undo``) is enough to undo on its own: file ids in the same
                # order as from/to, and the one parent both stay in.
                "ids": ids,
                "parent": fid,
                "sha256": item.sha256,
                "undo": {
                    "op": "rename_pair" if sidecar else "rename",
                    "from": new,
                    "to": old,
                    "ids": ids,
                    "parent": fid,
                },
            },
            lambda: self.drive.move_rename(item.id, new_name, fid, fid, allowed_parents=allowed),
            pair_half=sidecar is not None,
            confirm=sidecar is None,
        )
        if sidecar is not None:
            self.actions_used += 1  # the sidecar rename is a second file action
            if not self.opts.dry_run:
                try:
                    self.drive.move_rename(sidecar.id, new_side, fid, fid, allowed_parents=allowed)
                except WriteVerificationError as exc:
                    msg = f"sidecar rename not verified — pair split, undo logged: {exc}"
                    raise RunAbortError(msg) from exc
                except Exception as exc:
                    msg = f"sidecar rename failed ({type(exc).__name__}) — pair split"
                    raise RunAbortError(msg) from exc
                self._confirm(seq)
        renamed = {item.id: new_name, **({sidecar.id: new_side} if sidecar else {})}
        tree.files[folder] = [
            replace(i, name=renamed[i.id]) if i.id in renamed else i for i in tree.files[folder]
        ]

    # -- #919 item 4: stale review flags of filed documents --

    def _recheck_stale_reviews(self, tree: Tree, contract: Contract) -> None:
        """Re-check filed documents held in review with no recognised reason.

        Candidates are ``opts.stale_review_ids`` (from the read model); each
        sidecar is read again and must still be a stale review
        (``stale_review_skip``). The kept sidecar text is classified by the
        model and checked by ``classify.validate`` (no OCR, no file read except
        a Google Doc's text export when its sidecar holds no text; no age rule,
        as for the backfill: a filed document is no new scan). Every hard check
        passes → ``review_needed: false`` + ``review_cleared_by`` /
        ``review_cleared_at``; a check fails → its codes go into
        ``review_reasons``. Only those front-matter keys change, the body stays
        byte for byte, through ``drive.update_text`` (read back, no retry),
        journaled first as ``update_sidecar_review_keys``. Never a move, a
        rename, a README write or a write to the source. At most
        ``stale_review_max`` documents per run; a dry run plans the same.
        """
        rep = self.report.stale_review
        cap = max(0, int(self.opts.stale_review_max))
        rep["max_per_run"] = cap
        wanted = set(self.opts.stale_review_ids or ())
        if not wanted:
            return
        found: set[str] = set()
        for folder, doc_type in contract.folders.items():
            if doc_type is None or folder not in tree.folders:
                continue
            items = sorted(tree.files.get(folder, []), key=lambda i: nfc(i.name))
            names = {nfc(i.name) for i in items}
            for item in items:
                if item.id not in wanted or _is_sidecar(nfc(item.name), names):
                    continue
                found.add(item.id)
                label = f"{folder}/{item.name}"
                if _is_text_source(item) and not is_google_native(item.mime):
                    # a text note is its own text: its front matter is the
                    # original document, never edited by this pass
                    rep["skipped"].append({"source": label, "code": "text_source"})
                    continue
                sidecar = self._existing_sidecar(tree, folder, item)
                if sidecar is None:
                    rep["skipped"].append({"source": label, "code": "no_sidecar"})
                    continue
                if (
                    len(rep["planned"]) >= cap
                    or self.actions_used + 1 > self.opts.max_actions
                    or bool(self.opts.should_stop and self.opts.should_stop())
                ):
                    rep["deferred"].append({"source": label})
                    continue
                self._recheck_one(contract, folder, doc_type, item, sidecar, label)
        # moved out of the filed folders or gone since the index: nothing to do
        rep["skipped"].extend({"file_id": i, "code": "not_found"} for i in sorted(wanted - found))
        rep["counts"] = {
            "planned": len(rep["planned"]),
            "done": len(rep["done"]),
            "cleared": sum(1 for x in rep["planned"] if x["outcome"] == "clear"),
            "reasons": sum(1 for x in rep["planned"] if x["outcome"] == "reasons"),
            "deferred": len(rep["deferred"]),
            "skipped": len(rep["skipped"]),
        }
        logger.info(
            "curated %s: stale review re-check %s",
            self.run_id,
            " ".join(f"{k}={v}" for k, v in rep["counts"].items()),
        )

    def _recheck_one(self, contract, folder, doc_type, item, sidecar, label) -> None:
        rep = self.report.stale_review
        raw_text = self._sidecar_text(sidecar)
        if skip := stale_review_skip(raw_text):
            rep["skipped"].append({"source": label, "code": skip})
            return
        sha = re.search(r'^sha256:\s*"?([0-9a-f]{64})', raw_text[:4000], re.M)
        if sha and item.sha256 and sha.group(1) != item.sha256:
            # the source changed since its sidecar was written: the kept text
            # is no reading of it any more (a re-OCR decides, not this pass)
            rep["skipped"].append({"source": label, "code": "source_changed"})
            return
        text = _override_extraction(raw_text).text
        if not text.strip() and item.mime in extract_mod.NATIVE_EXPORT:
            # a pre-runner Google Doc sidecar may hold no text: read the
            # document's own text export (a read, never a write; no OCR)
            text = self._content(item).decode("utf-8", errors="replace").strip()
        note = ""
        cls = None
        if not text.strip():
            note = "no text to classify"
        elif reason := llm_input.inbox_exclusion(item.name, item.mime, text):
            note = f"{reason}: not sent to the model"  # raw genotype data (#815)
        else:
            try:
                raw = self.propose_fn(contract, text)
            except llm.LocalLLMError as exc:
                if _llm_down(exc.failure):
                    raise RunAbortError(f"local model unavailable — {exc}") from exc
                # a bad answer is no property of the document: nothing is
                # written, a later run asks again
                rep["skipped"].append({"source": label, "code": _model_failure_code(exc)})
                return
            cls = classify_mod.validate(
                contract, raw, text, today=self.now.date(), auto_vocab=self._auto_vocab
            )
        reasons = (cls.reasons if cls else []) + ([note] if note else [])
        entry = {"source": label, "sidecar": f"{folder}/{sidecar.name}"}
        if cls is not None and cls.doc_type != doc_type:
            entry["proposed_doc_type"] = cls.doc_type  # report only: never a move
        if not reasons:
            entry.update(outcome="clear", codes=[])
            new_text = stale_review_cleared(raw_text, self.now.date())
            undo = {"op": "front_keys", "path": entry["sidecar"], "review_needed": True}
        else:
            codes = [c for c in review_codes.codes_of(reasons) if c != review_codes.OTHER]
            if not codes:  # every reason unrecognised: writing would change nothing
                rep["skipped"].append({"source": label, "code": "no_code"})
                return
            entry.update(outcome="reasons", codes=codes)
            new_text = stale_review_with_codes(raw_text, codes)
            previous = _front_value(_split_sidecar(raw_text)[0], "review_reasons")
            undo = {"op": "front_keys", "path": entry["sidecar"], "review_reasons": previous}
        rep["planned"].append(entry)
        self._act(
            {
                "action": "update_sidecar_review_keys",
                "path": entry["sidecar"],
                "kind": "stale_review",  # #919 item 4
                "outcome": entry["outcome"],
                "codes": entry["codes"],
                "undo": undo,
            },
            lambda: self.drive.update_text(sidecar.id, new_text),
        )
        if not self.opts.dry_run:
            rep["done"].append(entry)

    def _backfill_sidecars(self, tree: Tree, contract: Contract) -> None:
        """Already-filed documents are read-only; only a missing sidecar may be added.

        A classification that disagrees with the folder becomes a *proposal* in
        the sidecar (``proposed_reclassification``) — never a move.
        """
        added = 0
        for folder, doc_type in contract.folders.items():
            if doc_type is None or folder not in tree.folders:
                continue
            items = list(tree.files.get(folder, []))
            names = {nfc(i.name) for i in items}
            for item in items:
                current = self._contract  # README §3 may have grown in this run (#854)
                n = nfc(item.name)
                if _is_sidecar(n, names) or n + ".md" in names or n + ".gdoc.md" in names:
                    continue
                if n.rsplit(".", 1)[-1].lower() in extract_mod.TEXT_EXTS:
                    continue
                label = f"{folder}/{item.name}"
                prior = self.opts.backfill_attempts.get(item.id) or {}
                attempts = int(prior.get("attempts") or 0)
                if attempts >= MAX_TECHNICAL_ATTEMPTS:
                    # gave up after repeated bad answers: no model call, a person looks
                    self.report.awaiting_review.append(label)
                    self.report.documents.append(
                        _backfill_entry(label, item.id, prior.get("code"), attempts)
                    )
                    continue
                if (
                    added >= self.opts.max_missing_sidecars
                    or self.actions_used + 1 > self.opts.max_actions
                    or (self.opts.should_stop and self.opts.should_stop())
                ):
                    self.report.not_processed.append(f"{folder}/{item.name} (missing sidecar)")
                    continue
                if self.opts.ocr_gate and (hold := self.opts.ocr_gate()):
                    self.report.held.append({"source": f"{folder}/{item.name}", "reason": hold})
                    continue
                content = b"" if item.size > extract_mod.MAX_FILE_BYTES else self._content(item)
                ex = self.extract_fn(
                    item.name, content, item.mime, min_text_chars=self.opts.min_text_chars
                )
                self.report.count_orientation(ex)
                if not ex.ok and _llm_down(getattr(ex, "llm_failure", None)):
                    raise RunAbortError(f"local model unavailable — {ex.error}")
                cls = None
                if (
                    ex.ok
                    and ex.text.strip()
                    # raw genotype data (#815): never sent to the model
                    and not llm_input.inbox_exclusion(item.name, item.mime, ex.text)
                ):
                    try:
                        raw = self.propose_fn(current, ex.text)
                    except llm.LocalLLMError as exc:
                        if _llm_down(exc.failure):
                            raise RunAbortError(f"local model unavailable — {exc}") from exc
                        # One bad answer (truncated, not JSON) is this document's
                        # problem: no sidecar is written, the file stays as it is
                        # and a later run tries it again (MAX_TECHNICAL_ATTEMPTS in
                        # all, counted across real runs by the caller through
                        # ``backfill_attempts``); the backfill goes on.
                        code = _model_failure_code(exc)
                        attempts += 1
                        self.report.backfill_skipped += 1
                        logger.info(
                            "curated %s: backfill skipped a document (%s, attempt %d)",
                            self.run_id,
                            code,
                            attempts,
                        )
                        if attempts >= MAX_TECHNICAL_ATTEMPTS:
                            self.report.awaiting_review.append(label)
                        self.report.documents.append(
                            _backfill_entry(label, item.id, code, attempts)
                        )
                        continue
                    cls = classify_mod.validate(current, raw, ex.text, today=self.now.date())
                extra = {}
                if cls and cls.doc_type != doc_type:
                    extra["proposed_reclassification"] = cls.doc_type
                text = render_sidecar(
                    source_name=item.name,
                    contract=current,
                    item=item,
                    extraction=ex,
                    cls=cls,
                    now=self.now,
                    extra=extra,
                    note="" if ex.ok else f"extraction failed: {ex.error}",
                )
                side_name = f"{item.name}.md"
                fid = tree.folders[folder].id
                self._act(
                    {
                        "action": "write_sidecar",
                        "path": f"{folder}/{side_name}",
                        "kind": "backfill",
                        "undo": {"op": "quarantine", "path": f"{folder}/{side_name}"},
                    },
                    lambda fid=fid, side_name=side_name, text=text: self.drive.create_text(
                        fid, side_name, text
                    ),
                )
                tree.files[folder].append(
                    Item(id="new", name=side_name, mime="text/markdown", parent=fid)
                )
                added += 1
                self.report.documents.append(
                    {
                        "source": f"{folder}/{item.name}",
                        "result": "sidecar_added",
                        "proposal": extra.get("proposed_reclassification"),
                    }
                )

    def _recheck(self, contract: Contract) -> None:
        """RECONCILE: re-run the gates after a writing run; Drive failing mid-run is loud."""
        fresh = walk(self.drive, self.root_id)
        failed = [g for g in preflight(self.drive, fresh, contract) if not g.ok and g.blocking]
        if failed:
            msg = "DRIVE CHANGED DURING RUN: " + "; ".join(f"{g.name} ({g.detail})" for g in failed)
            self.report.flags.insert(0, msg)
            self.report.status = "aborted"
            self.report.message = (self.report.message + "; " if self.report.message else "") + msg

    def _move_pair(self, tree, from_folder, to_folder, item, side_id, new_name) -> None:
        src_id, dst_id = self._folder_id(tree, from_folder), tree.folders[to_folder].id
        old_side = f"{item.name}.md"
        new_side = f"{new_name}.md"
        seq = self._act(
            {
                "action": "move_pair",
                "from": [f"{from_folder}/{item.name}", f"{from_folder}/{old_side}"],
                "to": [f"{to_folder}/{new_name}", f"{to_folder}/{new_side}"],
                "sha256": item.sha256,
                "undo": {
                    "op": "move_pair",
                    "from": [f"{to_folder}/{new_name}", f"{to_folder}/{new_side}"],
                    "to": [f"{from_folder}/{item.name}", f"{from_folder}/{old_side}"],
                },
            },
            lambda: self.drive.move_rename(
                item.id, new_name, src_id, dst_id, allowed_parents=tree.folder_ids()
            ),
            pair_half=True,
            confirm=False,
        )
        self.actions_used += 1  # the sidecar move is a second file action
        if not self.opts.dry_run:
            try:
                self.drive.move_rename(
                    side_id, new_side, src_id, dst_id, allowed_parents=tree.folder_ids()
                )
            except WriteVerificationError as exc:
                msg = f"sidecar move not verified — pair split, undo logged: {exc}"
                raise RunAbortError(msg) from exc
            except Exception as exc:
                msg = f"sidecar move failed ({type(exc).__name__}) — pair split"
                raise RunAbortError(msg) from exc
            self._confirm(seq)
        names = tree.files.setdefault(to_folder, [])
        names.append(Item(id=item.id, name=new_name, mime=item.mime, parent=dst_id))
        names.append(Item(id=side_id, name=new_side, mime="text/markdown", parent=dst_id))

    # -- state files --

    def _clear_stale_canary(self, tree: Tree) -> None:
        """Handle a canary left by an earlier run that never finished (#581).

        Only one run per patient is active in this process, so a canary found
        here belongs to a run that died (deploy, restart, crash). It is trashed
        only when its journal proves that run changed nothing; otherwise the
        run stops and names the journaled actions for a human to reconcile.
        """
        old = next((i for i in tree.root_files if i.name == CANARY), None)
        if old is None:
            return
        lines = self.drive.download(old.id).decode("utf-8", errors="replace").splitlines()
        head = (lines[0] if lines else "").split()
        old_run = head[0] if head else "?"
        if len(head) < 2 or head[1] != JOURNAL_MARK:
            raise RunAbortError(
                f"canary from run {old_run} has no journal (older version) — "
                "check that run's changes, then trash the canary"
            )
        age = _age_seconds(old.modified, self.now)
        if age is None or age < STALE_CANARY_MIN_AGE_S:
            raise RunAbortError(
                f"canary from run {old_run} is fresh — that run may still be active"
            )
        journaled = [ln for ln in lines[1:] if ln.strip()]
        actions, oks, unconfirmed = [], set(), []
        for ln in journaled:
            try:
                entry = json.loads(ln)
            except ValueError:
                unconfirmed.append(ln)
                continue
            if "ok" in entry:
                oks.add(entry["ok"])
            else:
                actions.append((ln, entry))
        unconfirmed += [ln for ln, e in actions if e.get("seq") is None or e["seq"] not in oks]
        if unconfirmed:
            summary = "; ".join(_journal_summary(ln) for ln in unconfirmed[:10])
            raise RunAbortError(
                f"run {old_run} was interrupted with {len(unconfirmed)} unconfirmed "
                f"journaled action(s) — reconcile them first ({summary}), then trash the canary"
            )
        # Every journaled action completed and was verified: the folder is
        # consistent. Carry the actions into this run's logs, then continue.
        for _, entry in actions:
            entry.pop("seq", None)
            self.report.actions.append({**entry, "status": "done", "recovered": True})
        self.drive.trash_canary(old.id)
        tree.root_files = [i for i in tree.root_files if i.id != old.id]
        if actions:
            msg = (
                f"interrupted run {old_run} completed all {len(actions)} journaled action(s) — "
                "recovered into this run's logs, its canary was trashed"
            )
        else:
            msg = f"interrupted run {old_run} left no actions — its canary was trashed"
        self.report.gates.append(Gate("stale canary", True, msg))
        self.report.flags.append(msg)

    def _write_state(self, tree: Tree, contract: Contract) -> None:
        """Append logs, rewrite the index, write the report, trash the canary.

        Runs even after an abort (``finally``) so the abort is logged; a failure
        here is reported, never retried.
        """
        try:
            root = {i.name: i for i in self.drive.list_children(self.root_id)}
            if self.report.actions and "_actions.jsonl" in root:
                f = root["_actions.jsonl"]
                old = self.drive.download(f.id).decode("utf-8")
                lines = "".join(
                    json.dumps(a, ensure_ascii=False) + "\n" for a in self.report.actions
                )
                self.drive.update_text(f.id, old + lines, "application/x-ndjson")
            if "_changelog.md" in root:
                f = root["_changelog.md"]
                old = self.drive.download(f.id).decode("utf-8")
                self.drive.update_text(f.id, old + self._changelog_entry())
            index = [
                {
                    "path": path,
                    "size": i.size,
                    "mtime": i.modified,
                    "sha256": i.sha256 or None,
                    "cloud_only": False,
                    "placeholder": False,
                }
                for path, i in _walk_all(self.drive, self.root_id)
                if path != CANARY
            ]
            text = json.dumps(index, ensure_ascii=False, indent=1)
            if "_index.json" in root:
                self.drive.update_text(root["_index.json"].id, text, "application/json")
            logs_id = tree.folders[contract.logs].id if contract.logs in tree.folders else None
            if logs_id:
                name = f"_daily-report-{self.now:%Y%m%d}-oncofiles.md"
                existing = {i.name: i for i in self.drive.list_children(logs_id)}
                body = self._report_markdown()
                if name in existing:
                    old = self.drive.download(existing[name].id).decode("utf-8")
                    self.drive.update_text(existing[name].id, old + "\n\n" + body)
                else:
                    self.drive.create_text(logs_id, name, body)
            if self.canary_id:
                self.drive.trash_canary(self.canary_id)
        except Exception as exc:
            logger.exception("curated: state write failed")
            self.report.status = "aborted"
            self.report.message = (self.report.message + "; " if self.report.message else "") + (
                f"state files not written ({type(exc).__name__}) — canary left in root"
            )

    def _changelog_entry(self) -> str:
        r = self.report
        lines = [
            f"\n## {self.now:%Y-%m-%d} — {self.run_id} ({self.actions_used} file actions)"
            + (f" — ABORTED: {r.message}" if r.status != "ok" else ""),
            "",
        ]
        for d in r.documents:
            if d["result"] == "filed":
                lines.append(f"- `{d['source']}` → `{d['to']}` ({d.get('extraction')})")
            elif d["result"] == "quarantined":
                lines.append(f"- `{d['source']}` exact duplicate of `{d['survivor']}` → quarantine")
            elif d["result"] == "sidecar_added":
                prop = f" — proposal: reclassify as {d['proposal']}" if d.get("proposal") else ""
                lines.append(f"- sidecar added to already-filed `{d['source']}`{prop}")
            elif d["result"] == "skipped":
                lines.append(f"- `{d['source']}` skipped ({'; '.join(d.get('reasons', []))})")
            else:
                lines.append(
                    f"- `{d['source']}` — **review_needed** ({'; '.join(d.get('reasons', []))}), "
                    f"stays in `{d.get('stays_in', '?')}`"
                )
        for x in r.normalise["renamed"]:
            lines.append(
                f"- filed name normalised (#919): `{x['folder']}/{x['from']}` → `{x['to']}`"
                + (" (with its sidecar)" if x.get("sidecar_to") else "")
            )
        if r.normalise["review"]:  # a count: the same documents recur every run
            lines.append(
                f"- filed names not standard, no rename possible: {len(r.normalise['review'])}"
            )
        if r.normalise["deferred"]:
            n = len(r.normalise["deferred"])
            lines.append(f"- filed names left for a later run (cap): {n}")
        for x in r.stale_review["done"]:
            if x["outcome"] == "clear":
                lines.append(f"- stale review flag cleared, checks passed (#919): `{x['source']}`")
            else:
                codes = ", ".join(x["codes"])
                lines.append(f"- review reason written (#919): `{x['source']}` ({codes})")
        if r.stale_review["deferred"]:
            n = len(r.stale_review["deferred"])
            lines.append(f"- stale review flags left for a later run (cap): {n}")
        for v in r.vocabulary["added"]:
            lines.append(
                f"- README vocabulary added: {v['label']} `{v['token']}` (for `{v['document']}`)"
            )
        for f in r.flags:
            lines.append(f"- flag: {f}")
        if r.not_processed:
            lines.append(f"- not processed this run (budget): {len(r.not_processed)}")
        for reason in sorted({h["reason"] for h in r.held}):
            n = sum(1 for h in r.held if h["reason"] == reason)
            lines.append(f"- left in place unread ({reason}): {n}")
        lines.append(
            f"- after this entry: report written, `_index.json` regenerated, canary `{CANARY}` "
            "trashed last. **If it is still in the root, this run did not finish.**"
        )
        return "\n".join(lines) + "\n"

    def _report_markdown(self) -> str:
        r = self.report
        gates = "\n".join(f"- [{'x' if g.ok else ' '}] {g.name} {g.detail}" for g in r.gates)
        return (
            f"# Oncofiles curated run {r.run_id}\n\nstatus: **{r.status}** {r.message}\n"
            f"contract version: {r.contract_version}\n\n## Preflight\n\n{gates}\n\n"
            f"## Changelog entry\n{self._changelog_entry()}"
        )


# ── probe (continuous mode) ──────────────────────────────────────────


@dataclass(frozen=True)
class InboxEntry:
    """One document waiting in the inbox (or loose in the root), for the
    dashboard's in-memory inbox rows (#921 follow-up). Never logged."""

    id: str  # Drive file id
    name: str
    size: int
    mime: str
    modified: str  # Drive modifiedTime (RFC 3339)
    review: bool  # its sidecar holds it for a human (``_awaiting_review``)


@dataclass
class Probe:
    """What a run would find to do right now — read-only, no model calls."""

    signature: str  # fingerprint of the whole tree (ids, names, mtimes, sizes)
    pending: dict[str, str] = field(default_factory=dict)  # file id -> label
    newest_age_s: float | None = None  # youngest file in the inbox / root
    canary: bool = False  # a leftover canary: a run must recover or report it
    # Every inbox/root document with its review flag; None when this probe read
    # no sidecars (an idle repeat of an unchanged tree, or no README).
    inbox: list[InboxEntry] | None = None


def _is_text_source(item: Item) -> bool:
    return item.name.rsplit(".", 1)[-1].lower() in extract_mod.TEXT_EXTS


def _signature(tree: Tree) -> str:
    rows = [f"/{i.id}/{i.name}/{i.modified}/{i.size}" for i in tree.root_files]
    for folder, items in tree.files.items():
        rows += [f"{folder}/{i.id}/{i.name}/{i.modified}/{i.size}" for i in items]
    return hashlib.sha256("\n".join(sorted(rows)).encode()).hexdigest()


def probe(
    drive,
    root_id: str,
    *,
    idle_signature: str | None = None,
    now=None,
    reviewed_text: frozenset = frozenset(),
    backfill_given_up: frozenset = frozenset(),
) -> Probe:
    """Cheap check for new work: one tree listing; sidecars are read only when
    the tree changed since the last probe that found nothing (``idle_signature``).

    Pending = what the next real run would process: inbox/root documents not
    awaiting a human review (new files, Cowork sidecars, technical retries)
    and filed documents without a sidecar.
    """
    tree = walk(drive, root_id)
    now = now or datetime.now(UTC)
    result = Probe(signature=_signature(tree))
    result.canary = any(i.name == CANARY for i in tree.root_files)
    readme = next((i for i in tree.root_files if i.name == "README.md"), None)
    if readme is None:
        return result
    readme_text = drive.download(readme.id).decode("utf-8")
    contract = parse_contract(readme_text)
    inbox_items = [
        i
        for i in tree.files.get(contract.inbox, []) + tree.root_files
        if nfc(i.name) not in ROOT_STATE_FILES and i.name != CANARY
    ]
    ages = [a for i in inbox_items if (a := _age_seconds(i.modified, now)) is not None]
    result.newest_age_s = min(ages) if ages else None
    if result.signature == idle_signature:
        return result
    r = Runner(drive, root_id, RunOptions(dry_run=True, now=now))
    r._set_auto_vocab(readme_text, contract)
    inbox: list[InboxEntry] = []
    for folder, item in r._candidates(tree, contract):
        if _is_text_source(item) and (item.id, item.modified) in reviewed_text:
            review = True
        else:
            side = r._existing_sidecar(tree, folder, item)
            review = side is not None and r._awaiting_review(side, item)
            if not review:
                result.pending[item.id] = f"{folder or '(root)'}/{item.name}"
        inbox.append(
            InboxEntry(item.id, item.name, int(item.size or 0), item.mime, item.modified, review)
        )
    result.inbox = inbox
    for folder, doc_type in contract.folders.items():
        if doc_type is None or folder not in tree.folders:
            continue
        items = tree.files.get(folder, [])
        names = {nfc(i.name) for i in items}
        for item in items:
            n = nfc(item.name)
            if _is_sidecar(n, names) or n + ".md" in names or n + ".gdoc.md" in names:
                continue
            if n.rsplit(".", 1)[-1].lower() in extract_mod.TEXT_EXTS:
                continue
            if item.id in backfill_given_up:  # bad model answers 3x: waits for a person
                continue
            result.pending[item.id] = f"{folder}/{item.name} (missing sidecar)"
    return result


def run_curated(drive, root_id: str, opts: RunOptions | None = None, **kwargs) -> RunReport:
    """Blocking. ``drive`` is a ``drive_ops.DriveOps`` (or a test fake)."""
    return Runner(drive, root_id, opts or RunOptions(), **kwargs).run()


__all__ = ["RunOptions", "RunReport", "Runner", "folder_key", "run_curated"]
