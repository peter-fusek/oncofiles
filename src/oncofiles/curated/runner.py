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
* a per-run action budget; a run that stops and explains itself is a success.

``dry_run=True`` (the default) performs every read, extraction and
classification but no write, and returns the plan.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from oncofiles.curated import classify as classify_mod
from oncofiles.curated import extract as extract_mod
from oncofiles.curated import llm
from oncofiles.curated.contract import (
    ROOT_STATE_FILES,
    Contract,
    ContractError,
    folder_key,
    nfc,
    parse_contract,
)
from oncofiles.curated.drive_ops import Item, WriteVerificationError
from oncofiles.curated.naming import build_filename, with_collision_suffix

logger = logging.getLogger(__name__)

CANARY = "_healthcheck.tmp"
# The canary doubles as the run's write-ahead journal (#581): line 1 is
# "<run_id> journal-v1", then one JSON line per action, written (and verified)
# BEFORE the action. A canary left by a killed run therefore says exactly
# what that run may have changed. Older canaries carry only the run id.
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


@dataclass
class RunOptions:
    dry_run: bool = True
    max_docs: int = 20
    max_actions: int = 40
    min_text_chars: int = 100
    run_id: str = ""
    now: datetime | None = None
    # Oncology contract (Cowork erika-daily-cleanup): a date > 2 years old on a
    # new scan is suspect → review. None = no age rule (general patients).
    max_doc_age_days: int | None = None
    # Already-filed documents without a sidecar get one (sidecar only) per run.
    max_missing_sidecars: int = 20


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

    def to_dict(self) -> dict:
        return asdict(self)


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


def _llm_down(error: str | None) -> bool:
    """True when the local model is unavailable (abort the run); False for a
    per-document problem such as an answer truncated by the reasoning budget."""
    msg = error or ""
    return "local LLM" in msg and "truncated" not in msg


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
    for folder, items in tree.files.items():
        siblings = {nfc(i.name) for i in items}
        for n in siblings:
            if n.endswith(".md") and SOURCE_EXT_RE.search(n[:-3]) and not _is_sidecar(n, siblings):
                if folder.startswith("98-logs"):
                    continue
                orphans.append(f"{folder}/{n}")
    gates.append(Gate("every sidecar has its source", not orphans, ", ".join(orphans[:10])))

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


def _review_reasons(sidecar_text: str) -> list[str]:
    m = re.search(r"^review_reasons:\s*(\[.*\])\s*$", sidecar_text, re.M)
    if not m:
        return []
    try:
        value = json.loads(m.group(1))
    except ValueError:
        return []
    return [str(r) for r in value] if isinstance(value, list) else []


def _is_technical(reason: str) -> bool:
    """A failure of the machinery, not a property of the document."""
    return reason.startswith("classification failed:") or (
        reason.startswith("extraction failed:") and "local LLM" in reason
    )


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

    # -- writes (no-ops in dry run, always logged) --

    def _act(self, action: dict, fn: Callable | None, *, pair_half: bool = False) -> None:
        """Log the action first, then perform it — so a failure is always on record.

        ``pair_half``: this write moves one half of a source+sidecar pair; if it
        fails after Drive applied it, the pair may be split, and the message says so.
        """
        action = {"ts": self.now.strftime("%Y-%m-%dT%H:%M"), "run": self.run_id, **action}
        self.report.actions.append(action)
        self.actions_used += 1
        if self.opts.dry_run or fn is None:
            return
        if self.canary_id:  # journal first, so a kill mid-action is on record
            line = json.dumps({k: v for k, v in action.items() if k != "undo"}, ensure_ascii=False)
            try:
                self.drive.update_text(self.canary_id, self._journal + line + "\n", "text/plain")
            except Exception as exc:
                raise RunAbortError(f"journal write failed ({type(exc).__name__})") from exc
            self._journal += line + "\n"
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
        contract = parse_contract(self.drive.download(readme.id).decode("utf-8"))
        self._contract = contract
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
            self._backfill_sidecars(tree, contract)
            if not self.opts.dry_run:
                self._recheck(contract)
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
        for folder, item in candidates:
            label = f"{folder or '(root)'}/{item.name}"
            if done >= self.opts.max_docs:
                self.report.not_processed.append(label)
                continue
            if self.actions_used + ACTIONS_PER_DOCUMENT > self.opts.max_actions:
                self.report.not_processed.append(label)
                continue
            sidecar = self._existing_sidecar(tree, folder, item)
            if sidecar is not None and self._awaiting_review(sidecar, item):
                self.report.awaiting_review.append(label)
                continue
            done += 1
            if item.sha256 and item.sha256 in filed:
                self._quarantine(tree, contract, folder, item, sidecar, filed[item.sha256])
                continue
            self._file_document(tree, contract, folder, item, sidecar)

    def _awaiting_review(self, sidecar: Item, item: Item) -> bool:
        text = self.drive.download(sidecar.id).decode("utf-8", errors="replace")[:4000]
        review = re.search(r"^review_needed:\s*(true|\"true\")", text, re.M | re.I)
        sha = re.search(r"^sha256:\s*\"?([0-9a-f]{64})", text, re.M)
        unchanged = sha is None or not item.sha256 or sha.group(1) == item.sha256
        if not (review and unchanged):
            return False
        reasons = _review_reasons(text)
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
                lambda m=m: self.drive.move_rename(m.id, m.name, src_id, dst_id),
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

    def _file_document(self, tree, contract, folder, item, sidecar) -> None:
        native = item.mime.startswith(extract_mod.GOOGLE_NATIVE_PREFIX)
        if item.size > extract_mod.MAX_FILE_BYTES:  # never download it
            limit = extract_mod.MAX_FILE_BYTES >> 20
            ex = extract_mod.Extraction("", "none", 0, "-", error=f"file larger than {limit} MB")
        else:
            content = b"" if native else self.drive.download(item.id)
            ex = self.extract_fn(
                item.name, content, item.mime, min_text_chars=self.opts.min_text_chars
            )
        cls = None
        raw = None
        note = ""
        if not ex.ok:
            note = f"extraction failed: {ex.error}"
            if _llm_down(ex.error):
                raise RunAbortError(f"local model unavailable — {ex.error}")
        elif not ex.text.strip():
            note = "no text to classify"
        else:
            try:
                raw = self.propose_fn(contract, ex.text)
            except llm.LocalLLMError as exc:
                if _llm_down(str(exc)):
                    raise RunAbortError(f"local model unavailable — {exc}") from exc
                note = f"classification failed: {exc}"  # this document stays for review
        if cls is None and raw is not None:
            cls = classify_mod.validate(
                contract,
                raw,
                ex.text,
                today=self.now.date(),
                max_age_days=self.opts.max_doc_age_days,
            )
        self.report.flags.extend(f"{item.name}: {f}" for f in (cls.flags if cls else []))

        target_folder = contract.folder_for(cls.doc_type) if cls and not cls.review_needed else None
        if target_folder and target_folder not in tree.folders:
            note = f"target folder {target_folder!r} does not exist (never created)"
            target_folder = None
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
        # Final name first, so the sidecar's source_file names the file it ends up next to.
        new_name = item.name
        if not review:
            ext = item.name.rsplit(".", 1)[-1] if "." in item.name else "bin"
            new_name = with_collision_suffix(
                build_filename(cls.fields, ext), tree.names_in(target_folder)
            )
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

        # Text files (notes, timelines, CSV) are their own text: no sidecar (README §4).
        if item.name.rsplit(".", 1)[-1].lower() in extract_mod.TEXT_EXTS:
            self._file_text_source(tree, folder, item, review, target_folder, new_name, cls, note)
            return

        # 1) sidecar next to the source, under the source's current name
        side_name = f"{item.name}.md"
        if sidecar is None:
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

        if review:
            if not folder:  # loose root file → inbox, pair together
                self._move_pair(tree, "", inbox, item, side_id, item.name)
            self.report.documents.append(
                {
                    "source": item.name,
                    "result": "review_needed",
                    "stays_in": stay_folder,
                    "reasons": (cls.reasons if cls else []) + ([note] if note else []),
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
            }
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
            lambda: self.drive.move_rename(item.id, new_name, src_id, dst_id),
        )
        tree.files.setdefault(to_folder, []).append(
            Item(id=item.id, name=new_name, mime=item.mime, parent=dst_id)
        )

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
                n = nfc(item.name)
                if _is_sidecar(n, names) or n + ".md" in names or n + ".gdoc.md" in names:
                    continue
                if n.rsplit(".", 1)[-1].lower() in extract_mod.TEXT_EXTS:
                    continue
                if added >= self.opts.max_missing_sidecars or (
                    self.actions_used + 1 > self.opts.max_actions
                ):
                    self.report.not_processed.append(f"{folder}/{item.name} (missing sidecar)")
                    continue
                native = item.mime.startswith(extract_mod.GOOGLE_NATIVE_PREFIX)
                content = (
                    b""
                    if native or item.size > extract_mod.MAX_FILE_BYTES
                    else (self.drive.download(item.id))
                )
                ex = self.extract_fn(
                    item.name, content, item.mime, min_text_chars=self.opts.min_text_chars
                )
                if not ex.ok and _llm_down(ex.error):
                    raise RunAbortError(f"local model unavailable — {ex.error}")
                cls = None
                if ex.ok and ex.text.strip():
                    try:
                        raw = self.propose_fn(contract, ex.text)
                    except llm.LocalLLMError as exc:
                        raise RunAbortError(f"local model unavailable — {exc}") from exc
                    cls = classify_mod.validate(contract, raw, ex.text, today=self.now.date())
                extra = {}
                if cls and cls.doc_type != doc_type:
                    extra["proposed_reclassification"] = cls.doc_type
                text = render_sidecar(
                    source_name=item.name,
                    contract=contract,
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
        self._act(
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
            lambda: self.drive.move_rename(item.id, new_name, src_id, dst_id),
            pair_half=True,
        )
        self.actions_used += 1  # the sidecar move is a second file action
        if not self.opts.dry_run:
            try:
                self.drive.move_rename(side_id, new_side, src_id, dst_id)
            except WriteVerificationError as exc:
                msg = f"sidecar move not verified — pair split, undo logged: {exc}"
                raise RunAbortError(msg) from exc
            except Exception as exc:
                msg = f"sidecar move failed ({type(exc).__name__}) — pair split"
                raise RunAbortError(msg) from exc
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
        if journaled:
            summary = "; ".join(_journal_summary(ln) for ln in journaled[:10])
            raise RunAbortError(
                f"run {old_run} was interrupted after {len(journaled)} journaled action(s) — "
                f"reconcile them first ({summary}), then trash the canary"
            )
        self.drive.trash_canary(old.id)
        tree.root_files = [i for i in tree.root_files if i.id != old.id]
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
        for f in r.flags:
            lines.append(f"- flag: {f}")
        if r.not_processed:
            lines.append(f"- not processed this run (budget): {len(r.not_processed)}")
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


def run_curated(drive, root_id: str, opts: RunOptions | None = None, **kwargs) -> RunReport:
    """Blocking. ``drive`` is a ``drive_ops.DriveOps`` (or a test fake)."""
    return Runner(drive, root_id, opts or RunOptions(), **kwargs).run()


__all__ = ["RunOptions", "RunReport", "Runner", "folder_key", "run_curated"]
