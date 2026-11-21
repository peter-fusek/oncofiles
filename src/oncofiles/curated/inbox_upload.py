"""Put caregiver uploads into a curated patient's inbox (#921).

The dashboard's "Add documents" for a curated patient writes each file,
unchanged, into the inbox folder named by the README §2 contract
(``Contract.inbox``, the ``00-inbox…`` utility folder) and nothing else: no
sidecar, no rename, no state file. The curated pipeline does the rest (the
probe sees the new inbox file, waits ``CURATED_SETTLE_S``, previews, then
files it with local OCR).

Rules:

* the root must carry the contract (``README.md`` + ``_index.json``) and its
  README must parse; the inbox is the contract's folder, never guessed from a
  display name;
* every write is ``drive_ops.upload_original`` (create-only, read back by size
  and md5, never retried) inside ``gdrive.contract_writes()`` on a
  ``dedicated_copy()`` of the patient client; this module is the only
  ``contract_writes()`` caller outside the pipeline and the bootstrap
  (lock: ``tests/test_853_curated_bootstrap.py``);
* a name clash gets a numeric suffix before the extension (``scan.pdf`` →
  ``scan-2.pdf``); an existing file is never overwritten;
* the first failed write stops the request (later files are reported
  ``not_attempted``), so a Drive problem never produces a half-understood
  burst of writes;
* no patient lock: a run in progress treats a file that lands mid-run as new
  work for the next probe, exactly like a file dropped in through Drive.

Blocking (Drive calls) — the route runs it in ``asyncio.to_thread``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oncofiles.curated import drive_ops
from oncofiles.curated.contract import ContractError, nfc, parse_contract

logger = logging.getLogger(__name__)

MAX_SUFFIX = 99


class InboxUnavailableError(RuntimeError):
    """The patient folder has no usable inbox; ``code`` says why (no file written)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InboxFile:
    name: str  # already sanitised by the caller (files_api.sanitize_filename)
    data: bytes
    mime: str


def resolve_inbox(gdrive, root_id: str) -> str:
    """The inbox folder id from the root's README contract (one listing + one read)."""
    items = drive_ops.list_children(gdrive, root_id)
    files = {nfc(i.name): i for i in items if not i.is_folder}
    readme = files.get("README.md")
    if readme is None or "_index.json" not in files:
        raise InboxUnavailableError("not_curated")
    try:
        contract = parse_contract(drive_ops.download(gdrive, readme.id).decode("utf-8"))
        inbox_name = contract.inbox
    except (ContractError, UnicodeDecodeError) as exc:
        raise InboxUnavailableError("contract_unreadable") from exc
    folder = next((i for i in items if i.is_folder and nfc(i.name) == inbox_name), None)
    if folder is None:
        raise InboxUnavailableError("inbox_missing")
    return folder.id


def split_ext(name: str) -> tuple[str, str]:
    if "." in name.lstrip("."):
        stem, ext = name.rsplit(".", 1)
        return stem, "." + ext
    return name, ""


def _free(name: str, taken: set[str]) -> bool:
    n = nfc(name)
    if n in taken or f"{n}.md" in taken:
        return False
    return not (n.lower().endswith(".md") and n[:-3] in taken)


def unique_name(name: str, taken: set[str]) -> str:
    """``name`` or the first ``stem-2.ext`` … ``stem-99.ext`` free in the inbox.

    Free = no file of that name, and no sidecar pair formed or adopted (the
    same rule ``drive_ops.upload_original`` enforces).
    """
    if _free(name, taken):
        return name
    stem, ext = split_ext(name)
    for n in range(2, MAX_SUFFIX + 1):
        candidate = f"{stem}-{n}{ext}"
        if _free(candidate, taken):
            return candidate
    raise ValueError("no free name in the inbox")


def inbox_md5s(gdrive, root_id: str) -> set[str]:
    """md5 of the files in the inbox (read-only: one README read + listings).

    For the #952 inbound queue's duplicate check before a retried write. A
    folder without a usable inbox gives an empty set (the upload refuses it
    anyway); any other error propagates. Blocking, own connection
    (``dedicated_copy`` made here, inside the worker thread).
    """
    own = gdrive.dedicated_copy()
    try:
        try:
            inbox_id = resolve_inbox(own, root_id)
        except InboxUnavailableError:
            return set()
        return {i.md5 for i in drive_ops.list_children(own, inbox_id) if i.md5 and not i.is_folder}
    finally:
        own.close()


def upload_to_inbox(gdrive, root_id: str, files: list[InboxFile]) -> list[dict]:
    """Write ``files`` into the inbox; one result per file, in order.

    Result: ``{"status": "uploaded", "name": <name in the inbox>, "id": <Drive
    file id>}``, or
    ``{"status": "failed", "code": <exception class>}`` for the file whose write
    failed, then ``{"status": "not_attempted"}`` for the rest. Raises
    ``InboxUnavailableError`` before any write when there is no usable inbox.
    """
    own = gdrive.dedicated_copy()  # own connection and contract_writes() scope
    try:
        inbox_id = resolve_inbox(own, root_id)
        taken = {nfc(i.name) for i in drive_ops.list_children(own, inbox_id)}
        results: list[dict] = []
        stopped = False
        with own.contract_writes():
            for f in files:
                if stopped:
                    results.append({"status": "not_attempted"})
                    continue
                try:
                    name = unique_name(f.name, taken)
                    file_id = drive_ops.upload_original(
                        own, root_id, inbox_id, name, f.data, f.mime
                    )
                except Exception as exc:
                    logger.warning(
                        "inbox upload: write failed (%s), %d byte(s), stopping",
                        type(exc).__name__,
                        len(f.data),
                    )
                    results.append({"status": "failed", "code": type(exc).__name__})
                    stopped = True
                    continue
                taken.add(nfc(name))
                results.append({"status": "uploaded", "name": name, "id": file_id})
        return results
    finally:
        own.close()
