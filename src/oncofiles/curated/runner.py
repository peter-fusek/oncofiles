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


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str = ""


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


def _is_sidecar(name: str, siblings: set[str]) -> bool:
    return name.endswith(".md") and bool(SOURCE_EXT_RE.search(name[:-3])) and name[:-3] in siblings


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
            if n.endswith(".md") and SOURCE_EXT_RE.search(n[:-3]) and n[:-3] not in siblings:
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
    gates.append(Gate("DocumentType folders are flat", not nested, ", ".join(nested[:10])))
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
    if item.link and item.mime.startswith(extract_mod.GOOGLE_NATIVE_PREFIX):
        meta["link"] = item.link
    front = "\n".join(f"{k}: {_y(v)}" for k, v in meta.items())
    body = extraction.text.strip() if extraction and extraction.text else "(no text extracted)"
    return f"---\n{front}\n---\n\n{body}\n"


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

    # -- writes (no-ops in dry run, always logged) --

    def _act(self, action: dict, fn: Callable | None) -> None:
        action = {"ts": self.now.strftime("%Y-%m-%dT%H:%M"), "run": self.run_id, **action}
        if not self.opts.dry_run and fn is not None:
            try:
                fn()
            except WriteVerificationError as exc:
                raise RunAbortError(f"write not verified: {exc}") from exc
            except Exception as exc:  # Drive error, refused write, …
                msg = f"write failed ({type(exc).__name__}): {action['action']}"
                raise RunAbortError(msg) from exc
        self.actions_used += 1
        self.report.actions.append(action)

    def run(self) -> RunReport:
        try:
            self._run()
        except RunAbortError as exc:
            self.report.status = "aborted"
            self.report.message = str(exc)
        except ContractError as exc:
            self.report.status = "aborted"
            self.report.message = f"contract: {exc}"
        return self.report

    def _run(self) -> None:
        tree = walk(self.drive, self.root_id)
        readme = next((i for i in tree.root_files if i.name == "README.md"), None)
        if readme is None or "_index.json" not in {i.name for i in tree.root_files}:
            raise RunAbortError("not a curated folder (README.md + _index.json missing)")
        contract = parse_contract(self.drive.download(readme.id).decode("utf-8"))
        self.report.contract_version = contract.version

        if not self.opts.dry_run:
            try:
                self.canary_id = self.drive.create_text(
                    self.root_id, CANARY, f"{self.run_id}\n", "text/plain"
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
            failed = [g for g in self.report.gates if not g.ok]
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
        except RunAbortError as exc:
            self.report.status = "aborted"
            self.report.message = str(exc)
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
        want = nfc(item.name) + ".md"
        return next((i for i in items if nfc(i.name) == want), None)

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
        return bool(review) and unchanged

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
        note = ""
        if not ex.ok:
            note = f"extraction failed: {ex.error}"
            if "local LLM" in (ex.error or ""):
                raise RunAbortError(f"local model unavailable — {ex.error}")
        elif not ex.text.strip():
            note = "no text to classify"
        else:
            try:
                raw = self.propose_fn(contract, ex.text)
            except llm.LocalLLMError as exc:
                raise RunAbortError(f"local model unavailable — {exc}") from exc
            cls = classify_mod.validate(contract, raw, ex.text, today=self.now.date())
        self.report.flags.extend(f"{item.name}: {f}" for f in (cls.flags if cls else []))

        target_folder = contract.folder_for(cls.doc_type) if cls and not cls.review_needed else None
        if target_folder and target_folder not in tree.folders:
            note = f"target folder {target_folder!r} does not exist (never created)"
            target_folder = None
        review = target_folder is None

        inbox = contract.inbox
        stay_folder = folder if folder else inbox  # loose root files go to the inbox for review
        src_folder_id = self._folder_id(tree, folder)
        # Final name first, so the sidecar's source_file names the file it ends up next to.
        new_name = item.name
        if not review:
            ext = item.name.rsplit(".", 1)[-1] if "." in item.name else "bin"
            new_name = with_collision_suffix(
                build_filename(cls.fields, ext), tree.names_in(target_folder)
            )
        text = render_sidecar(
            source_name=new_name,
            contract=contract,
            item=item,
            extraction=ex,
            cls=cls,
            now=self.now,
            note=note,
        )
        if review:
            text = text.replace("review_needed: false", "review_needed: true")

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
            else:
                lines.append(
                    f"- `{d['source']}` — **review_needed** ({'; '.join(d['reasons'])}), stays in "
                    f"`{d['stays_in']}`"
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
