"""Second-pass local re-OCR of uncertain transcriptions (#660, benchmark #658).

Documents whose sidecar text did not come from our local pipeline (Cowork-era
tesseract English-only, cloud vision) or that carry ``[?]`` markers are read
again from the **source file**, on the **local** model only:

* a PDF with a real text layer → that text, page by page (exact, no model);
* otherwise two vision passes with high reasoning effort at two resolutions.
  A line of the first pass whose numbers are not all found on the same page of
  the second pass is marked ``[?]``. Digits are accepted only where both
  passes agree; the parser keeps ``[?]`` lines unverified.

Guard: the old and the new text are both parsed; when the new one loses a
code with its value, or dates it differently, the sidecar is NOT rewritten
(result ``review``, the lost values listed).

Writing (never in a dry run): the old sidecar is first copied to the logs
folder (``_reocr-backup-<date>-<sidecar>``), then the sidecar is updated in
place (same file id, read-back verified by ``drive_ops``). Nothing is deleted;
the front matter is kept, only the extraction keys change.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from oncofiles.curated import extract as extract_mod
from oncofiles.curated import llm

logger = logging.getLogger(__name__)

LOCAL_ENGINE_KEYS = ("qwen", "local", "google-export", "utf-8", "pymupdf")
SECOND_PASS_DPI = 220
REOCR_BUDGET = 16_000
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_FRONT = re.compile(r"^---\n(.*?)\n---\n?", re.S)

REOCR_SYSTEM_PROMPT = extract_mod.OCR_SYSTEM_PROMPT + (
    "\n\nThis is a careful second reading. Take your time on every digit, decimal "
    "comma and unit. Keep each laboratory result on ONE line: test name, value, "
    "unit, reference range, flag. Examples:\n"
    "Leukocyty 6,37 G/l 4,0 - 11\n"
    "CEA 12,6 ug/l < 5,0 H\n"
    "Odber: 14.03.2027 07:40\n"
    "If a digit is not clearly legible, write the value followed by [?]."
)


def is_local_engine(engine: str | None) -> bool:
    return any(k in (engine or "").lower() for k in LOCAL_ENGINE_KEYS)


def needs_reocr(meta: dict, pages: list[str]) -> list[str]:
    """Why a document should be read again (empty list: it should not)."""
    reasons = []
    engine = str(meta.get("ocr_engine") or meta.get("extraction") or "")
    if not is_local_engine(engine):
        reasons.append("cowork_ocr")
    if any("[?]" in (p or "") for p in pages):
        reasons.append("ocr_uncertain")
    return reasons


def reconcile(first: list[str], second: list[str]) -> tuple[list[str], int]:
    """Mark first-pass lines whose numbers the second pass does not confirm.

    Comparison is per page and by number tokens (order-independent), so a
    re-wrapped line still matches; a digit read differently does not."""
    out, flagged = [], 0
    for i, page in enumerate(first):
        other = second[i] if i < len(second) else ""
        pool = _NUM.findall(other)
        lines = []
        for line in page.splitlines():
            nums = _NUM.findall(line)
            missing = [n for n in nums if n not in pool]
            if missing and "[?]" not in line:
                line, flagged = f"{line} [?]", flagged + 1
            lines.append(line)
        out.append("\n".join(lines))
    return out, flagged


def _render_pages(content: bytes, ext: str, dpi: int) -> list[bytes]:
    import pymupdf

    if ext == "pdf":
        with pymupdf.open(stream=content, filetype="pdf") as doc:
            if doc.page_count > extract_mod.MAX_VISION_PAGES:
                return []
            return [extract_mod._pixmap_png(p, dpi) for p in doc]
    with pymupdf.open(stream=content, filetype=ext) as doc:
        return [extract_mod._pixmap_png(doc[0], dpi)]


def _vision(pngs: list[bytes]) -> tuple[list[str], bool]:
    pages, handwritten = [], False
    for i, png in enumerate(pngs, start=1):
        answer = llm.chat(
            REOCR_SYSTEM_PROMPT,
            [{"type": "text", "text": f"Page {i} of {len(pngs)}."}, llm.image_part(png)],
            max_tokens=REOCR_BUDGET,
            reasoning_effort="high",
        )
        if answer.lower().startswith("[handwritten]"):
            handwritten = True
            answer = answer.split("\n", 1)[1] if "\n" in answer else ""
        pages.append(answer.strip())
    return pages, handwritten


@dataclass
class Reread:
    pages: list[str]
    method: str  # text_layer | vision2 | plain
    engine: str
    handwritten: bool = False
    flagged_lines: int = 0
    error: str | None = None


def reread(name: str, content: bytes, mime: str = "", *, passes: int = 2) -> Reread:
    """Read a source again, locally. Never raises."""
    ext = extract_mod._ext(name)
    try:
        if ext in extract_mod.TEXT_EXTS:
            return Reread([content.decode("utf-8", errors="replace")], "plain", "utf-8")
        if ext == "pdf":
            import pymupdf

            with pymupdf.open(stream=content, filetype="pdf") as doc:
                layer = [p.get_text() for p in doc]
            if len("".join(layer).strip()) >= 100:
                return Reread([t.strip() for t in layer], "text_layer", "pymupdf")
        elif ext not in extract_mod.IMAGE_EXTS:
            return Reread([], "none", "-", error=f"unsupported file type .{ext}")
        first_png = _render_pages(content, ext, extract_mod.RENDER_DPI)
        if not first_png:
            return Reread([], "none", "-", error="too many pages for vision")
        first, hand = _vision(first_png)
        flagged = 0
        if passes >= 2:
            second, _ = _vision(_render_pages(content, ext, SECOND_PASS_DPI))
            first, flagged = reconcile(first, second)
        return Reread(first, "vision2" if passes >= 2 else "vision", llm.model_id(), hand, flagged)
    except llm.LocalLLMError as exc:
        return Reread([], "none", llm.model_id(), error=str(exc))
    except Exception as exc:  # corrupt file, codec, …
        logger.warning("re-OCR failed for %s", name, exc_info=True)
        return Reread([], "none", "-", error=type(exc).__name__)


def rewrite_sidecar(old_text: str, rr: Reread, *, previous_engine: str, now: datetime) -> str:
    """Same front matter with updated extraction keys; the body is the new text."""
    m = _FRONT.match(old_text)
    lines = m.group(1).splitlines() if m else []
    updates = {
        "extraction": rr.method,
        "ocr_engine": rr.engine,
        "handwritten": "true" if rr.handwritten else "false",
        "extracted_at": now.date().isoformat(),
        "reocr_at": now.date().isoformat(),
        "reocr_previous_engine": previous_engine or "unknown",
        "reocr_uncertain_lines": str(rr.flagged_lines),
    }
    out, seen = [], set()
    for line in lines:
        key = line.split(":", 1)[0].strip() if ":" in line and not line.startswith(" ") else None
        if key in updates:
            out.append(f"{key}: {updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    out += [f"{k}: {v}" for k, v in updates.items() if k not in seen]
    body = "\n\n".join(f"<!-- page {i} -->\n{p}" for i, p in enumerate(rr.pages, start=1))
    return "---\n" + "\n".join(out) + "\n---\n\n" + body.strip() + "\n"


@dataclass
class DocResult:
    document_id: int
    filename: str
    reasons: list[str]
    result: str = "pending"  # previewed | rewritten | review | failed | skipped
    method: str | None = None
    engine: str | None = None
    pages: int = 0
    uncertain_before: int = 0
    uncertain_after: int = 0
    facts_machine: int = 0
    facts_unverified: int = 0
    backup: str | None = None
    lost: list[str] = field(default_factory=list)  # "CODE value date" the old text had
    error: str | None = None


@dataclass
class ReocrReport:
    dry_run: bool
    documents: list[DocResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"dry_run": self.dry_run, "documents": [asdict(d) for d in self.documents]}


_INLINE_CATEGORIES = ("report", "discharge", "chemo_sheet", "imaging", "surgery")


def _facts_of(text: str, vocab) -> set[tuple]:
    """(code, value, day) of every coded fact a sidecar text yields."""
    from oncofiles.curated.facts_rules import extract_lab_facts
    from oncofiles.curated.indexer import category_for, parse_sidecar

    front, pages = parse_sidecar(text)
    day = str(front.get("doc_date") or "")[:10] or None
    category = category_for(str(front.get("doc_type") or "other")).value
    facts, _ = extract_lab_facts(pages, vocab, report_date=day,
                                 inline=category in _INLINE_CATEGORIES)  # fmt: skip
    return {
        (f["code"], f["value_num"] if f["value_num"] is not None else f["value_text"],
         (f["occurred_at"] or day or "")[:10])
        for f in facts if f.get("code")
    }  # fmt: skip


def lost_values(old_text: str, new_text: str, vocab) -> list[str]:
    """What the new transcription would silently drop (#660 guard): a code with
    its value the old text had, missing from the new one, or present with a
    different date. A re-OCR that loses anything is not written."""
    old, new = _facts_of(old_text, vocab), _facts_of(new_text, vocab)
    new_cv = {(c, v): d for c, v, d in new}
    out = []
    for c, v, d in sorted(old, key=str):
        if (c, v) not in new_cv:
            out.append(f"{c} {v} missing")
        elif new_cv[(c, v)] != d:
            out.append(f"{c} {v} re-dated {d}->{new_cv[(c, v)]}")
    return out


def run_reocr(
    drive,
    root_id: str,
    docs: list[dict],
    vocab,
    *,
    dry_run: bool = True,
    passes: int = 2,
    now: datetime,
    stop=lambda: False,
) -> ReocrReport:
    """Blocking. ``docs``: document_id, filename, gdrive_id, sidecar_id, mime,
    engine, reasons. Writes only when ``dry_run`` is False (caller holds
    ``contract_writes()``); one document at a time, backup before update."""
    from oncofiles.curated.contract import parse_contract
    from oncofiles.curated.facts_rules import extract_lab_facts
    from oncofiles.curated.runner import nfc, walk

    report = ReocrReport(dry_run=dry_run)
    logs_id = None
    if not dry_run:
        tree = walk(drive, root_id)
        readme = next((i for i in tree.root_files if nfc(i.name) == "README.md"), None)
        if readme is None:
            raise RuntimeError("not a curated folder (no README.md)")
        contract = parse_contract(drive.download(readme.id).decode("utf-8"))
        logs = tree.folders.get(contract.logs)
        if logs is None:
            raise RuntimeError("the contract's logs folder is missing")
        logs_id = logs.id
    for d in docs:
        res = DocResult(d["document_id"], d["filename"], list(d.get("reasons") or []))
        report.documents.append(res)
        if stop():
            res.result, res.error = "skipped", "stopping"
            continue
        try:
            old = drive.download(d["sidecar_id"]).decode("utf-8", errors="replace")
            res.uncertain_before = old.count("[?]")
            rr = reread(d["filename"], drive.download(d["gdrive_id"]), d.get("mime") or "",
                        passes=passes)  # fmt: skip
            if rr.error or not any(p.strip() for p in rr.pages):
                res.result, res.error = "failed", rr.error or "no text"
                continue
            res.method, res.engine, res.pages = rr.method, rr.engine, len(rr.pages)
            new = rewrite_sidecar(old, rr, previous_engine=d.get("engine") or "", now=now)
            res.uncertain_after = new.count("[?]")
            res.lost = lost_values(old, new, vocab)
            facts, _ = extract_lab_facts(rr.pages, vocab, unreliable=rr.handwritten, inline=True)
            coded = [f for f in facts if f.get("code")]
            res.facts_machine = sum(1 for f in coded if f["verification"] == "machine")
            res.facts_unverified = len(coded) - res.facts_machine
            if dry_run:
                res.result = "previewed"
                continue
            if res.lost:  # never shrink the record silently: a person decides
                res.result = "review"
                continue
            name = f"_reocr-backup-{now:%Y%m%d}-{d['filename']}.md"
            res.backup = drive.create_text(logs_id, name, old)  # before any change
            drive.update_text(d["sidecar_id"], new)
            res.result = "rewritten"
        except Exception as exc:
            logger.exception("re-OCR of %s failed", d.get("filename"))
            res.result, res.error = "failed", type(exc).__name__
    return report
