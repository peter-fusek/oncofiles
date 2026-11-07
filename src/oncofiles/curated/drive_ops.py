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
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass

FOLDER_MIME = "application/vnd.google-apps.folder"
_FIELDS = "id,name,mimeType,parents,size,md5Checksum,sha256Checksum,modifiedTime,webViewLink"


class WriteVerificationError(RuntimeError):
    """A write could not be confirmed by reading it back — abort the run."""


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
        resp = (
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
        )
        items.extend(_item(f, folder_id) for f in resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def get(gdrive, file_id: str) -> dict:
    return (
        gdrive._service.files()
        .get(fileId=file_id, fields=_FIELDS + ",trashed", supportsAllDrives=True)
        .execute()
    )


def download(gdrive, file_id: str) -> bytes:
    from googleapiclient.http import MediaIoBaseDownload

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, gdrive._service.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    return buf.getvalue()


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
    """Replace a text file's content and verify the bytes."""
    from googleapiclient.http import MediaIoBaseUpload

    gdrive._guard_write("curated.update_text")
    data = text.encode("utf-8")
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime, resumable=False)
    gdrive._service.files().update(
        fileId=file_id, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    if download(gdrive, file_id) != data:
        raise WriteVerificationError(f"update {file_id}: content read-back mismatch")


def move_rename(gdrive, file_id: str, new_name: str, from_parent: str, to_parent: str) -> None:
    """Rename and/or move in one atomic update, then verify name + parents."""
    gdrive._guard_write("curated.move_rename")
    current = get(gdrive, file_id)
    if current.get("name") != new_name and new_name in _names_in(gdrive, to_parent):
        raise WriteVerificationError(f"refusing to move to {new_name!r}: name exists")
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


def trash_canary(gdrive, file_id: str) -> None:
    """Trash the run's own canary file (soft delete, 30 days) and verify."""
    gdrive._guard_write("curated.trash_canary")
    gdrive._service.files().update(
        fileId=file_id, body={"trashed": True}, fields="id", supportsAllDrives=True
    ).execute()
    if not get(gdrive, file_id).get("trashed"):
        raise WriteVerificationError("canary was not trashed")


class DriveOps:
    """The runner's Drive interface — bound to one client. Tests substitute a fake."""

    def __init__(self, gdrive):
        self.gdrive = gdrive

    def list_children(self, folder_id: str) -> list[Item]:
        return list_children(self.gdrive, folder_id)

    def list_my_drive_root(self) -> list[Item]:
        return list_children(self.gdrive, "root")

    def download(self, file_id: str) -> bytes:
        return download(self.gdrive, file_id)

    def create_text(self, folder_id: str, name: str, text: str, mime: str = "text/markdown") -> str:
        return create_text(self.gdrive, folder_id, name, text, mime)

    def update_text(self, file_id: str, text: str, mime: str = "text/markdown") -> None:
        update_text(self.gdrive, file_id, text, mime)

    def move_rename(self, file_id: str, new_name: str, from_parent: str, to_parent: str) -> None:
        move_rename(self.gdrive, file_id, new_name, from_parent, to_parent)

    def trash_canary(self, file_id: str) -> None:
        trash_canary(self.gdrive, file_id)
