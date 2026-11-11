"""Curated patient folders (#562) — detection and the legacy write guard.

A patient's Drive root folder is *curated* when its root holds both
``README.md`` (the folder contract) and ``_index.json`` (the contract's live
index). Such a folder is maintained under strict rules: never create folders,
never delete, source + sidecar move as one unit, verify every write. The
legacy sync/rename/refolder/OCR-cleanup code knows none of those rules — on
2026-11-06 it created 8 forbidden category folders in one.

So the legacy paths must never write into a curated folder. The guard is
enforced at the lowest layer: a ``GDriveClient`` built for a curated folder is
marked with ``write_block_reason``, and every write method on the client
raises ``DriveWriteBlockedError`` while it is set. Higher layers (``sync``,
``ensure_folder_structure``, the dashboard folder picker) additionally skip
early so they do not produce one error per file.

Only the curated-folder pipeline (#562, which implements the contract) may
lift the block, explicitly, via ``GDriveClient.contract_writes()``.

Detection fails closed: if the root cannot be listed, the folder is treated as
curated. A false positive costs one skipped sync tick; a false negative can
rename a real medical record.
"""

from __future__ import annotations

import logging
import re
import time

logger = logging.getLogger(__name__)

CURATED_MARKERS: tuple[str, ...] = ("README.md", "_index.json")
_MAX_DEPTH = 32

# folder_id -> (reason or None, checked_at_monotonic)
_cache: dict[str, tuple[str | None, float]] = {}
_CACHE_TTL_S = 600.0


class DriveWriteBlockedError(PermissionError):
    """Raised when legacy code tries to write into a curated folder."""


def write_block_reason(gdrive) -> str | None:
    """Return the client's block reason, or None.

    Only a real ``str`` counts — test doubles (``MagicMock``) answer every
    attribute with a truthy mock, which must not read as "blocked".
    """
    reason = getattr(gdrive, "write_block_reason", None)
    return reason if isinstance(reason, str) and reason else None


# ── Drive lookups (the only functions that talk to the API; patched in tests) ──


def _marker_names(gdrive, folder_id: str) -> set[str]:
    """Names of the curated marker files directly inside ``folder_id``."""
    names = " or ".join(f"name = '{m}'" for m in CURATED_MARKERS)
    resp = (
        gdrive._service.files()
        .list(
            q=f"'{folder_id}' in parents and ({names}) and trashed = false",
            fields="files(name)",
            pageSize=10,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        )
        .execute()
    )
    return {f.get("name", "") for f in resp.get("files", [])}


def _parents(gdrive, file_id: str) -> list[str]:
    meta = (
        gdrive._service.files()
        .get(fileId=file_id, fields="id,parents", supportsAllDrives=True)
        .execute()
    )
    return list(meta.get("parents") or [])


def _index_file_parents(gdrive) -> set[str]:
    """Every folder that holds an ``_index.json`` anywhere the caller can see."""
    out: set[str] = set()
    page_token = None
    while True:
        resp = (
            gdrive._service.files()
            .list(
                q="name = '_index.json' and trashed = false",
                fields="nextPageToken, files(parents)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for f in resp.get("files", []):
            out.update(f.get("parents") or [])
        page_token = resp.get("nextPageToken")
        if not page_token:
            return out


# ── Detection ─────────────────────────────────────────────────────────


def _is_curated_root(gdrive, folder_id: str) -> bool:
    return all(m in _marker_names(gdrive, folder_id) for m in CURATED_MARKERS)


def _ancestors(gdrive, folder_id: str) -> list[str]:
    chain: list[str] = []
    current = folder_id
    for _ in range(_MAX_DEPTH):
        parents = _parents(gdrive, current)
        if not parents:
            return chain
        current = parents[0]
        chain.append(current)
    raise RuntimeError(f"folder {folder_id}: ancestry deeper than {_MAX_DEPTH}")


def _detect(gdrive, folder_id: str) -> str | None:
    if _is_curated_root(gdrive, folder_id):
        return "is a curated folder"
    if folder_id != "root":
        for anc in _ancestors(gdrive, folder_id):
            if _is_curated_root(gdrive, anc):
                return f"lies inside curated folder {anc}"
    # Does it contain a curated folder further down? (e.g. Health/ above Health/<Patient>)
    for holder in _index_file_parents(gdrive):
        if not _is_curated_root(gdrive, holder):
            continue
        if folder_id == "root" or folder_id in _ancestors(gdrive, holder):
            return f"contains curated folder {holder}"
    return None


def curated_reason(gdrive, folder_id: str, *, use_cache: bool = True) -> str | None:
    """Why ``folder_id`` must not be touched by legacy code, or None if it is free.

    A folder is off-limits when it is curated itself, lies anywhere inside a
    curated folder, or contains one anywhere below it — legacy sync walks
    recursively and creates folders, so any overlap is a write into the tree.

    Blocking (Drive API calls) — run via ``asyncio.to_thread`` from async code.
    Fails closed: any lookup error is reported as a reason.
    """
    if not folder_id:
        return None
    now = time.monotonic()
    if use_cache:
        hit = _cache.get(folder_id)
        if hit is not None and now - hit[1] < _CACHE_TTL_S:
            return hit[0]
    try:
        reason = _detect(gdrive, folder_id)
    except Exception:
        logger.warning(
            "curated-folder detection failed for %s — treating as curated (fail closed)",
            folder_id,
            exc_info=True,
        )
        return "curated-folder detection failed (fail closed)"  # not cached
    _cache[folder_id] = (reason, now)
    return reason


def is_curated_folder(gdrive, folder_id: str, *, use_cache: bool = True) -> bool:
    return curated_reason(gdrive, folder_id, use_cache=use_cache) is not None


def mark_if_curated(gdrive, folder_id: str) -> bool:
    """Detect and, if curated, block legacy writes on ``gdrive``. Returns the verdict.

    Never un-blocks: once a client is marked it stays read-only for its lifetime.
    """
    if gdrive is None or not folder_id:
        return False
    reason = curated_reason(gdrive, folder_id)
    if reason is not None:
        gdrive.write_block_reason = (
            f"folder {folder_id} {reason} (README.md contract, #562); "
            "legacy sync/rename/refolder writes are disabled"
        )
    return reason is not None


_DRIVE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{10,128}$")
_FOLDER_URL_RE = re.compile(r"/folders/([A-Za-z0-9_-]{10,128})")
_ID_PARAM_RE = re.compile(r"[?&]id=([A-Za-z0-9_-]{10,128})")


def parse_drive_folder_ref(value: str) -> str | None:
    """Return a Drive folder id from a pasted link or a bare id, else None (#563).

    Accepts ``https://drive.google.com/drive/folders/<id>?...``,
    ``https://drive.google.com/drive/u/0/folders/<id>``, ``...open?id=<id>``
    and a bare id. Anything else is rejected — the id ends up inside a Drive
    query string, so only the Drive id alphabet is allowed.
    """
    value = (value or "").strip()
    if not value:
        return None
    if _DRIVE_ID_RE.match(value):
        return value
    for rx in (_FOLDER_URL_RE, _ID_PARAM_RE):
        m = rx.search(value)
        if m:
            return m.group(1)
    return None


def clear_cache(folder_id: str | None = None) -> None:
    if folder_id is None:
        _cache.clear()
    else:
        _cache.pop(folder_id, None)
