"""Verified Drive operations for the curated pipeline (#562).

Contract rules enforced here, at the lowest layer:

* every write goes through ``gdrive._guard_write`` — it only succeeds inside
  ``gdrive.contract_writes()``, never from legacy code;
* every write is **verified by reading it back** (name, parent, bytes);
  a mismatch raises ``WriteVerificationError`` and the run aborts;
* **no retries** — unlike ``GDriveClient``'s methods these calls are not
  wrapped in the transient-retry decorator (Drive allows same-name siblings,
  and retried writes forked the tree in Aug 2026);
* nothing is ever deleted; the only trash call is for the run's own canary;
* same-name collisions are checked against a fresh listing before each write.

**Originals invariant** (maintainer rule, 2026-11-14): an original source file
(PDF, image, any non-text file) in a curated folder is never deleted, trashed
or overwritten. It may only be moved or renamed within the curated tree, and
every such move carries an undo record in the run journal. The helpers below
enforce this themselves instead of relying on where they are called from:

* ``update_text`` reads the target first and only overwrites a text state
  file (``TEXT_STATE_EXTS`` + ``TEXT_STATE_MIMES``);
* ``trash_canary`` trashes only a file named ``CANARY_NAME``;
* ``move_rename`` keeps the file's extension and, given ``allowed_parents``
  (the runner always passes the curated tree), never leaves that tree.

Lock test: ``tests/test_curated_originals_invariant.py``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS = "id,name,mimeType,parents,size,md5Checksum,sha256Checksum,modifiedTime,webViewLink"


# The run's own canary / write-ahead journal (``runner.CANARY`` is this name).
CANARY_NAME = "_healthcheck.tmp"

# ``update_text`` may only overwrite files with one of these extensions…
# sidecars and README/CHANGELOG (.md), ``_index.json``, ``_actions.jsonl``,
# plain-text notes (.txt) and the canary (.tmp).
TEXT_STATE_EXTS = (".md", ".json", ".jsonl", ".txt", ".tmp")
# …and only while Drive reports one of these mime types. Drive stores what the
# upload declared (``text/markdown``, ``text/plain``, ``application/json``,
# ``application/x-ndjson``); files that arrived through the Drive for Desktop
# mount can show as ``application/octet-stream``.
TEXT_STATE_MIMES = ("application/json", "application/x-ndjson", "application/octet-stream")
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps."


class WriteVerificationError(RuntimeError):
    """A write could not be confirmed by reading it back — abort the run.

    Also raised, before anything is written, when a write would break the
    originals invariant (see the module docstring)."""


def is_text_state_mime(mime: str) -> bool:
    mime = (mime or "").split(";", 1)[0].strip().lower()
    return mime.startswith("text/") or mime in TEXT_STATE_MIMES


def _ext(name: str) -> str:
    """The extension the runner keeps when it renames (``naming.build_filename``)."""
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def check_extension_kept(current_name: str, new_name: str, mime: str) -> None:
    """Raise unless a rename keeps the file's extension.

    An original keeps its extension (``X.pdf`` → ``Y.pdf``; case may change, the
    runner lowercases it); a sidecar keeps ``.md`` (``X.pdf.md`` → ``Y.pdf.md``).
    Exceptions that mirror the runner: an extensionless file is filed as
    ``….bin``, and a Google-native file (no content bytes, the title carries no
    real extension) is not checked.
    """
    if (mime or "").startswith(_GOOGLE_NATIVE_PREFIX):
        return
    old, new = _ext(current_name), _ext(new_name)
    if old == new or (old == "" and new == "bin"):
        return
    raise WriteVerificationError(
        f"refusing to rename {current_name!r} → {new_name!r}: extension change ({old!r} → {new!r})"
    )


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    mime: str
    parent: str
    size: int = 0
    sha256: str = ""
    md5: str = ""
    modified: str = ""
    link: str = ""

    @property
    def is_folder(self) -> bool:
        return self.mime == FOLDER_MIME


_READ_ATTEMPTS = 3
STALE_AFTER_S = 60.0


def _read(gdrive, fn):
    """Run a Drive READ, retrying on connection errors with a fresh connection.

    Reads are idempotent, so retrying is safe — unlike writes, which are never
    retried (README §1b). 2026-11-07: after ~7 min of local-LLM work the
    httplib2 HTTPS connection was stale and the next download failed (SSLError).
    """
    from oncofiles.gdrive_client import _is_connection_error

    for attempt in range(_READ_ATTEMPTS):
        try:
            return fn()
        except Exception as exc:
            if attempt == _READ_ATTEMPTS - 1 or not _is_connection_error(exc):
                raise
            logger.warning("curated: Drive read failed (%s), fresh connection", type(exc).__name__)
            gdrive._rebuild_service()
            time.sleep(1 + attempt)
    raise RuntimeError("unreachable")


def _item(f: dict, parent: str) -> Item:
    return Item(
        id=f["id"],
        name=f.get("name", ""),
        mime=f.get("mimeType", ""),
        parent=parent,
        size=int(f.get("size") or 0),
        sha256=f.get("sha256Checksum", "") or "",
        md5=f.get("md5Checksum", "") or "",
        modified=f.get("modifiedTime", "") or "",
        link=f.get("webViewLink", "") or "",
    )


def list_children(gdrive, folder_id: str) -> list[Item]:
    items: list[Item] = []
    page_token = None
    while True:
        resp = _read(
            gdrive,
            lambda page_token=page_token: (
                gdrive._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields=f"nextPageToken, files({_FIELDS})",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            ),
        )
        items.extend(_item(f, folder_id) for f in resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def get(gdrive, file_id: str) -> dict:
    return _read(
        gdrive,
        lambda: (
            gdrive._service.files()
            .get(fileId=file_id, fields=_FIELDS + ",trashed", supportsAllDrives=True)
            .execute()
        ),
    )


def download(gdrive, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    def _once() -> bytes:  # a retry restarts the whole download on a fresh connection
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, gdrive._service.files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    return _read(gdrive, _once)


def export(gdrive, file_id: str, mime_type: str) -> bytes:
    """Drive's export of a Google-native file (Docs → text). A read: no write guard."""
    from googleapiclient.http import MediaIoBaseDownload

    def _once() -> bytes:
        buf = io.BytesIO()
        request = gdrive._service.files().export_media(fileId=file_id, mimeType=mime_type)
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()

    return _read(gdrive, _once)


def _nfc(value: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFC", value or "")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _names_in(gdrive, folder_id: str) -> set[str]:
    return {i.name for i in list_children(gdrive, folder_id)}


def create_text(gdrive, folder_id: str, name: str, text: str, mime: str = "text/markdown") -> str:
    """Create a new text file and verify name, parent and exact bytes. Returns its id."""
    from googleapiclient.http import MediaIoBaseUpload

    gdrive._guard_write("curated.create_text")
    if name in _names_in(gdrive, folder_id):
        raise WriteVerificationError(f"refusing to create {name!r}: name already exists")
    data = text.encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    created = (
        gdrive._service.files()
        .create(
            body={"name": name, "parents": [folder_id]},
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    fid = created["id"]
    meta = get(gdrive, fid)
    if meta.get("name") != name or folder_id not in (meta.get("parents") or []):
        raise WriteVerificationError(f"create {name!r}: metadata read-back mismatch")
    if download(gdrive, fid) != data:
        raise WriteVerificationError(f"create {name!r}: content read-back mismatch")
    return fid


def update_text(gdrive, file_id: str, text: str, mime: str = "text/markdown") -> None:
    """Replace a text state file's content and verify the bytes.

    Refuses (nothing written) unless the target's current name ends with one of
    ``TEXT_STATE_EXTS`` and its current mime type is text — an original is
    never overwritten.
    """
    from googleapiclient.http import MediaIoBaseUpload

    gdrive._guard_write("curated.update_text")
    meta = get(gdrive, file_id)
    name = meta.get("name") or ""
    if not name.lower().endswith(TEXT_STATE_EXTS):
        raise WriteVerificationError(
            f"refusing to update {file_id}: {name!r} is not a text state file"
        )
    if not is_text_state_mime(meta.get("mimeType", "")):
        raise WriteVerificationError(
            f"refusing to update {file_id}: mime {meta.get('mimeType')!r} is not text"
        )
    if not is_text_state_mime(mime):
        raise WriteVerificationError(f"refusing to update {file_id}: upload mime {mime!r}")
    data = text.encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    gdrive._service.files().update(
        fileId=file_id, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    if download(gdrive, file_id) != data:
        raise WriteVerificationError(f"update {file_id}: content read-back mismatch")


def move_rename(
    gdrive,
    file_id: str,
    new_name: str,
    from_parent: str,
    to_parent: str,
    *,
    allowed_parents: set[str] | frozenset[str] | None = None,
) -> None:
    """Rename and/or move in one atomic update, then verify name + parents.

    ``allowed_parents``: the curated tree's folder ids; when given, both ends
    of the move must be inside it. The extension is always kept
    (``check_extension_kept``). A refusal writes nothing.
    """
    gdrive._guard_write("curated.move_rename")
    check_parents(file_id, from_parent, to_parent, allowed_parents)
    current = get(gdrive, file_id)
    check_extension_kept(current.get("name") or "", new_name, current.get("mimeType", ""))
    renaming = current.get("name") != new_name
    if (renaming or from_parent != to_parent) and _nfc(new_name) in {
        _nfc(n) for n in _names_in(gdrive, to_parent)
    }:
        raise WriteVerificationError(f"refusing to move to {new_name!r}: name exists in target")
    kwargs = {
        "fileId": file_id,
        "body": {"name": new_name},
        "fields": "id",
        "supportsAllDrives": True,
    }
    if from_parent != to_parent:
        kwargs["addParents"] = to_parent
        kwargs["removeParents"] = from_parent
    gdrive._service.files().update(**kwargs).execute()
    meta = get(gdrive, file_id)
    parents = meta.get("parents") or []
    if (
        meta.get("name") != new_name
        or to_parent not in parents
        or (from_parent != to_parent and from_parent in parents)
    ):
        raise WriteVerificationError(f"move {file_id} → {new_name!r}: read-back mismatch")


def check_parents(file_id: str, from_parent: str, to_parent: str, allowed_parents) -> None:
    if allowed_parents is None:
        return
    for end, parent in (("target", to_parent), ("source", from_parent)):
        if parent not in allowed_parents:
            raise WriteVerificationError(
                f"refusing to move {file_id}: {end} folder {parent} is outside the curated tree"
            )


def trash_canary(gdrive, file_id: str) -> None:
    """Trash the run's own canary file (soft delete, 30 days) and verify.

    Refuses (nothing trashed) unless the file is named ``CANARY_NAME``.
    """
    gdrive._guard_write("curated.trash_canary")
    name = get(gdrive, file_id).get("name")
    if name != CANARY_NAME:
        raise WriteVerificationError(f"refusing to trash {file_id}: {name!r} is not the canary")
    gdrive._service.files().update(
        fileId=file_id, body={"trashed": True}, fields="id", supportsAllDrives=True
    ).execute()
    if not get(gdrive, file_id).get("trashed"):
        raise WriteVerificationError("canary was not trashed")


class DriveOps:
    """The runner's Drive interface — bound to one client. Tests substitute a fake.

    Renews the HTTPS connection before any call that follows a long pause
    (local-LLM work between Drive calls takes minutes), so writes — which are
    never retried — do not hit a stale connection.
    """

    def __init__(self, gdrive):
        self.gdrive = gdrive
        self._last = time.monotonic()

    def _fresh(self) -> None:
        now = time.monotonic()
        if now - self._last > STALE_AFTER_S:
            self.gdrive._rebuild_service()
        self._last = now

    def list_children(self, folder_id: str) -> list[Item]:
        self._fresh()
        return list_children(self.gdrive, folder_id)

    def list_my_drive_root(self) -> list[Item]:
        self._fresh()
        return list_children(self.gdrive, "root")

    def download(self, file_id: str) -> bytes:
        self._fresh()
        return download(self.gdrive, file_id)

    def export(self, file_id: str, mime_type: str) -> bytes:
        self._fresh()
        return export(self.gdrive, file_id, mime_type)

    def create_text(self, folder_id: str, name: str, text: str, mime: str = "text/markdown") -> str:
        self._fresh()
        return create_text(self.gdrive, folder_id, name, text, mime)

    def update_text(self, file_id: str, text: str, mime: str = "text/markdown") -> None:
        if not is_text_state_mime(mime):  # name + current mime are checked by update_text
            raise WriteVerificationError(f"refusing to update {file_id}: upload mime {mime!r}")
        self._fresh()
        update_text(self.gdrive, file_id, text, mime)

    def move_rename(
        self,
        file_id: str,
        new_name: str,
        from_parent: str,
        to_parent: str,
        *,
        allowed_parents: set[str] | frozenset[str] | None = None,
    ) -> None:
        # Same checks as the function (it re-checks after the fresh-connection step).
        check_parents(file_id, from_parent, to_parent, allowed_parents)
        self._fresh()
        move_rename(
            self.gdrive, file_id, new_name, from_parent, to_parent, allowed_parents=allowed_parents
        )

    def trash_canary(self, file_id: str) -> None:
        self._fresh()
        trash_canary(self.gdrive, file_id)
