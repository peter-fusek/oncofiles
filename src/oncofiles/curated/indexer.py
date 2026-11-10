"""Read model of a curated Drive folder for the MCP read tools (#603).

The curated pipeline (#562) files documents in Drive and writes a sidecar
next to each one; it never touched the ``documents`` table, so every chat
tool (search, view, labs, timeline) saw an empty patient. This module builds
one record per filed source document from the tree and its sidecars.

Rules:
* **Drive reads only.** Listing plus sidecar downloads — never a write.
* **No cloud.** Records are stored with ``file_id = "gdrive:<id>"``: they are
  never uploaded to the Files API, the original is read from Drive, and the
  text comes from the sidecar (local-model OCR). ``refuse_cloud`` keeps every
  cloud-AI enrichment path away from them.
* Utility folders (inbox, logs, duplicates) are not indexed.
* Incremental: a sidecar whose ``modified`` time is unchanged since the last
  index is not downloaded again.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date

from oncofiles.curated.contract import ContractError, nfc, parse_contract
from oncofiles.curated.drive_ops import Item
from oncofiles.curated.runner import _is_sidecar, walk
from oncofiles.models import DocumentCategory

CURATED_FILE_PREFIX = "gdrive:"
INDEX_ENGINE = "curated-sidecar"
# Bump when the index derives new data from the same sidecars (entities, facts,
# titles…): rows indexed by an older version are re-processed once (#603).
INDEX_VERSION = 13  # v13: sidecar sha + source md5 stored for verify-only re-reads (#678)
TEXT_SOURCE_EXTS = {"md", "txt", "csv", "json"}
MAX_TEXT_SOURCE_BYTES = 512 * 1024
_PAGE_RE = re.compile(r"^<!-- page (\d+) -->\s*$", re.M)
_DATE_PREFIX_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})[-_]")
_FRONT_RE = re.compile(r"^---\n(.*?)\n---\n?", re.S)

# Contract DocumentType → DB category. Names match the enum except these.
_CATEGORY_ALIASES = {"treatment": DocumentCategory.CHEMO_SHEET, "admin": DocumentCategory.OTHER}


# ── cloud guard ──────────────────────────────────────────────────────────

_curated_ids: set[int] = set()
# Fail closed: if the registry could not be loaded at startup, every cloud-AI
# call on a document is refused until it is (a missing id must never mean
# "not curated").
_guard_ready = True


class CuratedCloudError(RuntimeError):
    """A curated document must never be sent to a cloud model."""


def register_curated_ids(ids) -> None:
    _curated_ids.update(int(i) for i in ids)


def forget_curated_ids(ids) -> None:
    _curated_ids.difference_update(int(i) for i in ids)


def mark_guard_failed() -> None:
    global _guard_ready
    _guard_ready = False


def mark_guard_ready() -> None:
    global _guard_ready
    _guard_ready = True


def is_curated_id(document_id: int | None) -> bool:
    return document_id is not None and int(document_id) in _curated_ids


def refuse_cloud(document_id: int | None, what: str) -> None:
    """Raise before any cloud-AI call on a curated document."""
    if document_id is not None and not _guard_ready:
        raise CuratedCloudError(
            f"{what}: the curated-document guard is not loaded — cloud AI is "
            "refused for every document until it is (#603)"
        )
    if is_curated_id(document_id):
        raise CuratedCloudError(
            f"{what}: document {document_id} comes from a curated folder — "
            "its content is processed only by the local model (#562/#603)"
        )


def is_curated_file_id(file_id: str | None) -> bool:
    return bool(file_id) and file_id.startswith(CURATED_FILE_PREFIX)


# ── sidecar parsing ──────────────────────────────────────────────────────


_QUOTED_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"|^\'([^\']*)\'')


def _value(raw: str):
    raw = raw.strip()
    q = _QUOTED_RE.match(raw)
    if q:  # a quoted scalar, possibly followed by "  # comment"
        if q.group(2) is not None:
            return q.group(2)
        try:
            return json.loads(q.group(0))
        except ValueError:
            return q.group(1)
    raw = re.sub(r"\s+#.*$", "", raw)
    if raw in ("", "null", "~"):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        pass
    if raw.startswith("[") and raw.endswith("]"):
        return [v.strip().strip("'\"") for v in raw[1:-1].split(",") if v.strip()]
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    return raw.strip("'\"")


def parse_sidecar(text: str) -> tuple[dict, list[str]]:
    """Front matter (flat ``key: value`` lines) and the body split into pages."""
    front: dict = {}
    body = text
    m = _FRONT_RE.match(text)
    if m:
        body = text[m.end() :]
        for line in m.group(1).splitlines():
            if ":" not in line or line.startswith((" ", "\t", "-")):
                continue
            key, _, raw = line.partition(":")
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                front[key.strip()] = _value(raw)
    return front, split_pages(body)


def split_pages(body: str) -> list[str]:
    marks = list(_PAGE_RE.finditer(body))
    if not marks:
        text = body.strip()
        return [text] if text else []
    pages = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        pages.append(body[m.end() : end].strip())
    return pages


def _iso_date(value) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _filename_date(name: str) -> date | None:
    m = _DATE_PREFIX_RE.match(name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _filename_fields(name: str) -> list[str]:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    return stem.split("-")


def category_for(doc_type: str) -> DocumentCategory:
    key = doc_type.strip().lower()
    if key in _CATEGORY_ALIASES:
        return _CATEGORY_ALIASES[key]
    try:
        return DocumentCategory(key)
    except ValueError:
        return DocumentCategory.OTHER


# ── records ──────────────────────────────────────────────────────────────


@dataclass
class IndexRecord:
    gdrive_id: str
    filename: str
    folder: str
    category: DocumentCategory
    mime_type: str
    size_bytes: int
    modified: str
    sidecar_id: str = ""
    sidecar_modified: str = ""
    sidecar_sha: str = ""  # sha256 of the sidecar bytes this index read (#678)
    source_md5: str = ""  # Drive md5 of the source file (#678)
    unchanged: bool = False  # sidecar not re-read; keep the stored text
    original_filename: str = ""
    document_date: date | None = None
    institution: str | None = None
    description: str | None = None
    summary: str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    pages: list[str] = field(default_factory=list)

    @property
    def file_id(self) -> str:
        return CURATED_FILE_PREFIX + self.gdrive_id


@dataclass
class IndexScan:
    records: list[IndexRecord]
    contract_version: str
    skipped_folders: list[str]


def _record(folder: str, category: DocumentCategory, item: Item) -> IndexRecord:
    fields = _filename_fields(item.name)
    return IndexRecord(
        gdrive_id=item.id,
        filename=nfc(item.name),
        folder=folder,
        category=category,
        mime_type=item.mime or "application/octet-stream",
        size_bytes=item.size,
        modified=item.modified,
        source_md5=item.md5,
        original_filename=nfc(item.name),
        document_date=_filename_date(item.name),
        institution=fields[2] if len(fields) >= 6 and fields[2] != "Unknown" else None,
        description="-".join(fields[5:]) if len(fields) > 5 else None,
    )


def _apply_sidecar(rec: IndexRecord, front: dict, pages: list[str]) -> None:
    rec.pages = pages
    rec.document_date = _iso_date(front.get("doc_date")) or rec.document_date
    if not rec.institution and front.get("institution") not in (None, "Unknown"):
        rec.institution = str(front["institution"])
    summary = front.get("summary")
    rec.summary = summary if isinstance(summary, str) and summary.strip() else None
    if rec.summary:
        rec.description = rec.summary[:300]
    if front.get("original_filename"):
        rec.original_filename = str(front["original_filename"])
    diagnoses = front.get("diagnoses") if isinstance(front.get("diagnoses"), list) else []
    rec.tags = [str(d) for d in diagnoses] + [rec.category.value]
    keep = (
        "doctor", "hospital_unit", "diagnoses", "language", "pages", "handwritten",
        "extraction", "ocr_engine", "review_needed", "doc_date_source", "reviewed_by",
        "flags", "near_duplicate_of",
    )  # fmt: skip
    rec.metadata = {k: front[k] for k in keep if k in front and front[k] not in (None, "")}


def scan(drive, root_id: str, *, known: dict[str, str] | None = None) -> IndexScan:
    """Walk the curated tree (reads only) and build one record per filed source.

    ``known`` maps source Drive id → sidecar ``modified`` stored at the last
    index; those sidecars are not downloaded again.
    """
    known = known or {}
    tree = walk(drive, root_id)
    readme = next((i for i in tree.root_files if nfc(i.name) == "README.md"), None)
    if readme is None:
        raise ContractError("no README.md in the curated root")
    contract = parse_contract(drive.download(readme.id).decode("utf-8"))
    records: list[IndexRecord] = []
    skipped: list[str] = []
    for folder, doc_type in contract.folders.items():
        if doc_type is None:  # inbox, logs, duplicates
            skipped.append(folder)
            continue
        if folder not in tree.folders:
            continue
        category = category_for(doc_type)
        items = list(tree.files.get(folder, []))
        for sub in tree.subfolders.get(folder, []):  # e.g. a book chapter of scans
            items += [c for c in drive.list_children(sub.id) if not c.is_folder]
        names = {nfc(i.name) for i in items}
        by_name = {nfc(i.name): i for i in items}
        for item in items:
            name = nfc(item.name)
            if name.startswith(".") or _is_sidecar(name, names):
                continue
            rec = _record(folder, category, item)
            side = by_name.get(name + ".md") or by_name.get(name + ".gdoc.md")
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if side is not None:
                rec.sidecar_id, rec.sidecar_modified = side.id, side.modified
                if known.get(item.id) == side.modified:
                    rec.unchanged = True
                else:
                    raw = drive.download(side.id)
                    rec.sidecar_sha = hashlib.sha256(raw).hexdigest()
                    front, pages = parse_sidecar(raw.decode("utf-8", errors="replace"))
                    _apply_sidecar(rec, front, pages)
            elif ext in TEXT_SOURCE_EXTS and item.size <= MAX_TEXT_SOURCE_BYTES:
                # A text source (notes, timelines) is its own text; no sidecar.
                rec.sidecar_modified = item.modified
                if known.get(item.id) == item.modified:
                    rec.unchanged = True
                else:
                    text = drive.download(item.id).decode("utf-8", errors="replace")
                    front, pages = parse_sidecar(text)
                    _apply_sidecar(rec, front, pages)
                    rec.metadata["text_source"] = True
            records.append(rec)
    return IndexScan(records, contract.version, skipped)
