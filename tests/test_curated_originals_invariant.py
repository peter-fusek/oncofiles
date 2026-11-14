"""Originals invariant (maintainer rule, 2026-11-14).

An original source file (PDF, image, any non-text file) in a curated Drive
folder is never deleted, trashed or has its content overwritten. It may only be
moved or renamed within the curated tree, and every such move carries an undo
record in the run journal. ``curated/drive_ops`` enforces this in the helpers
themselves; the legacy ``GDriveClient`` refuses the same class of writes
(``update`` only on text exports, ``trash_file`` only on an empty folder).

Synthetic data only (public repo): invented names, ids and 2027+ dates.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from oncofiles.curated import drive_ops
from oncofiles.curated.drive_ops import CANARY_NAME, FOLDER_MIME, DriveOps, WriteVerificationError
from oncofiles.curated.extract import Extraction
from oncofiles.curated.runner import CANARY, RunOptions, run_curated
from oncofiles.gdrive_client import DriveWriteRefusedError

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
README = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
NOW = datetime(2027, 11, 7, 6, 30, tzinfo=UTC)
MODIFIED = (NOW - timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
INBOX, LABS, REPORT, DUPS, LOGS = (
    "00-inbox — nové skeny",
    "labs — laboratórne výsledky",
    "report — lekárske správy",
    "99-duplicates — duplikáty",
    "98-logs — logy a archív",
)


# ── a fake Drive *service* (the googleapiclient surface drive_ops uses) ──


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeFiles:
    def __init__(self, store: FakeService):
        self.s = store

    def _meta(self, fid):
        d = self.s.items[fid]
        return {
            "id": fid,
            "name": d["name"],
            "mimeType": d["mime"],
            "parents": [d["parent"]],
            "size": str(len(d["content"])),
            "sha256Checksum": hashlib.sha256(d["content"]).hexdigest(),
            "modifiedTime": d["modified"],
            "trashed": d["trashed"],
        }

    def get(self, fileId, fields="", supportsAllDrives=True):  # noqa: N803
        return _Req(lambda: self._meta(fileId))

    def list(self, q="", fields="", pageSize=100, pageToken=None, **kw):  # noqa: N803
        parent = re.match(r"'([^']+)' in parents", q).group(1)

        def _run():
            files = [
                self._meta(k)
                for k, d in self.s.items.items()
                if d["parent"] == parent and not d["trashed"]
            ]
            return {"files": files[:pageSize]}

        return _Req(_run)

    def update(self, fileId, body=None, media_body=None, addParents=None, removeParents=None, **kw):  # noqa: N803
        def _run():
            d = self.s.items[fileId]
            self.s.updates.append(
                {"id": fileId, "name": d["name"], "body": body, "media": media_body is not None}
            )
            if media_body is not None:
                d["content"] = media_body.getbytes(0, media_body.size())
            for k, v in (body or {}).items():
                if k == "name":
                    d["name"] = v
                elif k == "trashed":
                    d["trashed"] = v
            if addParents:
                assert d["parent"] == removeParents
                d["parent"] = addParents
            return {"id": fileId}

        return _Req(_run)

    def create(self, body, media_body=None, fields="", **kw):
        def _run():
            fid = self.s.add(
                body["name"],
                body["parents"][0],
                media_body.getbytes(0, media_body.size()),
                mime=media_body.mimetype(),
            )
            self.s.updates.append({"id": fid, "name": body["name"], "body": None, "media": False})
            return {"id": fid}

        return _Req(_run)


class FakeService:
    def __init__(self):
        self.ids = itertools.count(1)
        self.items: dict[str, dict] = {}
        self.updates: list[dict] = []

    def add(self, name, parent, content=b"", *, mime="application/pdf"):
        fid = f"fid{next(self.ids)}"
        self.items[fid] = {
            "name": name,
            "parent": parent,
            "mime": mime,
            "content": content,
            "modified": MODIFIED,
            "trashed": False,
        }
        return fid

    def folder(self, name, parent):
        return self.add(name, parent, mime=FOLDER_MIME)

    def files(self):
        return FakeFiles(self)

    def find(self, parent, name):
        return next(
            k
            for k, d in self.items.items()
            if d["parent"] == parent and d["name"] == name and not d["trashed"]
        )

    def names(self, parent):
        return {
            d["name"] for d in self.items.values() if d["parent"] == parent and not d["trashed"]
        }


class FakeGDrive:
    def __init__(self):
        self._service = FakeService()

    def _guard_write(self, op):
        pass

    def _rebuild_service(self):
        pass


@pytest.fixture(autouse=True)
def _download_from_fake(monkeypatch):
    # googleapiclient's MediaIoBaseDownload needs a real HTTP request; read the store.
    monkeypatch.setattr(
        drive_ops, "download", lambda g, fid: g._service.items[fid]["content"], raising=True
    )


@pytest.fixture
def g():
    return FakeGDrive()


def _content_writes(svc):
    return [u for u in svc.updates if u["media"]]


# ── update_text: only text state files ───────────────────────────────


@pytest.mark.parametrize(
    "name,mime",
    [
        ("20271005-Scan.pdf", "application/pdf"),
        ("photo_001.jpg", "image/jpeg"),
        ("photo_002.png", "image/png"),
        ("notes.md", "application/pdf"),  # text name, non-text mime
        ("notes.txt", "image/png"),
        ("Draft", "application/vnd.google-apps.document"),
        ("_index.json", "application/vnd.google-apps.document"),
        ("scan.pdf.bak", "text/plain"),  # text mime, non-text name
    ],
)
def test_update_text_refuses_originals(g, name, mime):
    svc = g._service
    root = svc.folder("Root", "top")
    fid = svc.add(name, root, b"ORIGINAL BYTES", mime=mime)
    with pytest.raises(WriteVerificationError, match=fid):
        drive_ops.update_text(g, fid, "overwrite", "text/plain")
    assert _content_writes(svc) == []
    assert svc.items[fid]["content"] == b"ORIGINAL BYTES"


def test_update_text_refuses_a_non_text_upload_mime(g):
    svc = g._service
    fid = svc.add("x.pdf.md", svc.folder("Root", "top"), b"---\n", mime="text/markdown")
    with pytest.raises(WriteVerificationError):
        drive_ops.update_text(g, fid, "x", "application/pdf")
    with pytest.raises(WriteVerificationError):
        DriveOps(g).update_text(fid, "x", "application/pdf")
    assert _content_writes(svc) == []


@pytest.mark.parametrize(
    "name,stored_mime,upload_mime",
    [
        ("20271005-Scan.pdf.md", "text/markdown", "text/markdown"),  # sidecar
        ("Scan.gdoc.md", "text/x-markdown", "text/markdown"),  # mount-era sidecar
        (CANARY_NAME, "text/plain", "text/plain"),  # canary journal
        ("_actions.jsonl", "application/x-ndjson", "application/x-ndjson"),
        ("_actions.jsonl", "application/octet-stream", "application/x-ndjson"),
        ("_index.json", "application/json", "application/json"),
        ("_changelog.md", "text/markdown", "text/markdown"),
        ("CHANGELOG.md", "text/markdown", "text/markdown"),
        ("timeline.txt", "text/plain", "text/plain"),
    ],
)
def test_update_text_happy_paths(g, name, stored_mime, upload_mime):
    svc = g._service
    fid = svc.add(name, svc.folder("Root", "top"), b"old", mime=stored_mime)
    DriveOps(g).update_text(fid, "new text", upload_mime)
    assert svc.items[fid]["content"] == b"new text"


# ── trash_canary: only the canary ────────────────────────────────────


@pytest.mark.parametrize(
    "name,mime",
    [
        ("20271005-Scan.pdf", "application/pdf"),
        ("_actions.jsonl", "application/x-ndjson"),
        ("healthcheck.tmp", "text/plain"),
        ("_healthcheck.tmp.md", "text/markdown"),
        ("labs — laboratórne výsledky", FOLDER_MIME),
    ],
)
def test_trash_canary_refuses_anything_but_the_canary(g, name, mime):
    svc = g._service
    fid = svc.add(name, svc.folder("Root", "top"), b"x", mime=mime)
    with pytest.raises(WriteVerificationError):
        DriveOps(g).trash_canary(fid)
    assert svc.updates == [] and not svc.items[fid]["trashed"]


def test_trash_canary_trashes_the_canary(g):
    svc = g._service
    fid = svc.add(CANARY, svc.folder("Root", "top"), b"run journal-v1\n", mime="text/plain")
    DriveOps(g).trash_canary(fid)
    assert svc.items[fid]["trashed"]


def test_runner_canary_is_the_drive_ops_constant():
    assert CANARY == CANARY_NAME == "_healthcheck.tmp"


# ── move_rename: stays in the tree, keeps the extension ──────────────


def _tree(g):
    svc = g._service
    root = svc.folder("Root", "top")
    inbox, labs, dups = (svc.folder(n, root) for n in (INBOX, LABS, DUPS))
    outside = svc.folder("Elsewhere", "top")
    return svc, root, inbox, labs, dups, outside


def test_move_rename_refuses_a_parent_outside_the_tree(g):
    svc, root, inbox, labs, dups, outside = _tree(g)
    fid = svc.add("scan_001.pdf", inbox, b"%PDF")
    allowed = {root, inbox, labs, dups}
    with pytest.raises(WriteVerificationError, match="outside the curated tree"):
        DriveOps(g).move_rename(fid, "scan_001.pdf", inbox, outside, allowed_parents=allowed)
    with pytest.raises(WriteVerificationError, match="outside the curated tree"):
        drive_ops.move_rename(g, fid, "scan_001.pdf", inbox, outside, allowed_parents=allowed)
    assert svc.updates == [] and svc.items[fid]["parent"] == inbox


@pytest.mark.parametrize(
    "old,new",
    [
        ("scan_001.pdf", "20271005-Jana-NOU-Labs-X-Y.txt"),
        ("scan_001.pdf", "20271005-Jana-NOU-Labs-X-Y.pdf.md"),
        ("photo.jpg", "20271005-Jana-NOU-Labs-X-Y.png"),
        ("scan_001.pdf", "20271005-Jana-NOU-Labs-X-Y"),
        ("scan_001.pdf.md", "20271005-Jana-NOU-Labs-X-Y.pdf"),  # a sidecar keeps .md
    ],
)
def test_move_rename_refuses_an_extension_change(g, old, new):
    svc, root, inbox, labs, dups, _ = _tree(g)
    fid = svc.add(old, inbox, b"%PDF")
    with pytest.raises(WriteVerificationError, match="extension change"):
        DriveOps(g).move_rename(fid, new, inbox, labs, allowed_parents={root, inbox, labs})
    assert svc.updates == [] and svc.items[fid]["name"] == old


def test_move_rename_pair_and_quarantine_happy_paths(g):
    svc, root, inbox, labs, dups, _ = _tree(g)
    src = svc.add("scan_001.PDF", inbox, b"%PDF")
    side = svc.add("scan_001.PDF.md", inbox, b"---\n", mime="text/markdown")
    bare = svc.add("scan_noext", inbox, b"%PDF")
    native = svc.add("Notes v1.2", inbox, b"", mime="application/vnd.google-apps.document")
    dup = svc.add("scan_dup.pdf", inbox, b"%PDF")
    allowed = frozenset({root, inbox, labs, dups})
    ops = DriveOps(g)
    new = "20271005-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"  # case of the extension may change
    ops.move_rename(src, new, inbox, labs, allowed_parents=allowed)
    ops.move_rename(side, new + ".md", inbox, labs, allowed_parents=allowed)
    ops.move_rename(bare, "20271005-JanaNovakova-NOU-Labs-Kovacova-OKB-02.bin", inbox, labs)
    ops.move_rename(native, "20271005-JanaNovakova-NOU-Labs-Kovacova-OKB-03", inbox, labs)
    ops.move_rename(dup, "scan_dup.pdf", inbox, dups, allowed_parents=allowed)
    assert {new, new + ".md"} <= svc.names(labs)
    assert svc.names(dups) == {"scan_dup.pdf"}
    assert all(not u["media"] and "trashed" not in (u["body"] or {}) for u in svc.updates)


# ── end to end: a real run through DriveOps on the fake service ──────


GOOD = {
    "doc_date": "2027-08-05",
    "doc_type": "Labs",
    "institution": "NOU",
    "doctor": "MUDr. Jana Kováčová",
    "hospital": "OKB",
    "confidence": 0.95,
    "summary": "Krvný obraz.",
}


def _extract(name, content, mime="", *, min_text_chars=100):
    return Extraction(text=content.decode(), method="text_layer", pages=1, engine="test")


def _propose(contract, text):
    if "REPORT" in text:
        return {**GOOD, "doc_type": "Report", "doc_date": "2027-09-01"}
    return dict(GOOD)


def test_real_run_through_drive_ops_keeps_every_original(g):
    svc = g._service
    root = svc.folder("Jana", "top")
    f = {
        n: svc.folder(n, root)
        for n in [INBOX, LABS, REPORT, "imaging — zobrazovanie", "other — ostatné", DUPS, LOGS]
    }
    svc.add("README.md", root, README.encode(), mime="text/markdown")
    svc.add("_index.json", root, b"[]", mime="application/json")
    svc.add("_changelog.md", root, b"# changelog\n", mime="text/markdown")
    svc.add("_actions.jsonl", root, b"", mime="application/x-ndjson")
    svc.add("scan_001.pdf", f[INBOX], b"VYSLEDKY 05.08.2027")
    svc.add("scan_003.pdf", f[INBOX], b"REPORT 01.09.2027")
    # an existing sidecar from an earlier review is updated in place
    svc.add("scan_003.pdf.md", f[INBOX], b"---\nreview_needed: false\n---\n", mime="text/markdown")
    svc.add("20270101-JanaNovakova-NOU-Labs-X-Y.pdf", f[LABS], b"SAME BYTES")
    svc.add("scan_dup.pdf", f[INBOX], b"SAME BYTES")
    originals = {k: dict(d) for k, d in svc.items.items() if d["mime"] == "application/pdf"}

    r = run_curated(
        DriveOps(g),
        root,
        RunOptions(dry_run=False, now=NOW, run_id="test-run"),
        extract_fn=_extract,
        propose_fn=_propose,
    )
    assert r.status == "ok", r.message

    labs = svc.names(f[LABS])
    filed = "20270805-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"
    assert {filed, filed + ".md"} <= labs  # inbox → filed pair move
    report = "20270901-JanaNovakova-NOU-Report-Kovacova-OKB.pdf"
    assert {report, report + ".md"} <= svc.names(f[REPORT])
    assert "scan_dup.pdf" in svc.names(f[DUPS])  # quarantine to 99-duplicates
    actions = svc.items[svc.find(root, "_actions.jsonl")]["content"].decode().splitlines()
    assert actions and all("undo" in json.loads(a) for a in actions)
    assert "test-run" in svc.items[svc.find(root, "_changelog.md")]["content"].decode()
    assert json.loads(svc.items[svc.find(root, "_index.json")]["content"])
    assert CANARY not in svc.names(root)

    # The invariant: every original still exists with its bytes and extension,
    # untrashed, inside the tree; only the canary was ever trashed.
    tree = set(f.values()) | {root}
    for fid, before in originals.items():
        now = svc.items[fid]
        assert now["content"] == before["content"] and not now["trashed"]
        assert now["parent"] in tree
        assert drive_ops._ext(now["name"]) == drive_ops._ext(before["name"])
    trashed = [u["name"] for u in svc.updates if (u["body"] or {}) == {"trashed": True}]
    assert trashed == [CANARY]
    media = {u["name"] for u in _content_writes(svc)}
    assert media and all(n.endswith((".md", ".json", ".jsonl", ".tmp")) for n in media)


# ── legacy GDriveClient: update only text exports, trash only empty folders ──


def _client():
    from tests.test_gdrive_client import _make_client

    client, _ = _make_client()
    svc = FakeService()
    client._service = svc
    return client, svc


@pytest.mark.parametrize(
    "name,mime",
    [
        ("20271005-Scan.pdf", "application/pdf"),
        ("photo.jpg", "image/jpeg"),
        ("_manifest.json", "application/vnd.google-apps.document"),
        ("treatment-timeline", "application/vnd.google-apps.document"),
        ("notes.md", "application/octet-stream"),
        ("research-library.md.pdf", "text/markdown"),
    ],
)
def test_legacy_update_refuses_non_text(name, mime):
    client, svc = _client()
    fid = svc.add(name, svc.folder("Root", "top"), b"ORIGINAL", mime=mime)
    with pytest.raises(DriveWriteRefusedError):
        client.update(fid, b"overwrite", "text/markdown")
    assert svc.updates == [] and svc.items[fid]["content"] == b"ORIGINAL"


def test_legacy_update_refuses_a_non_text_upload_mime():
    client, svc = _client()
    fid = svc.add("x.txt", svc.folder("Root", "top"), b"old", mime="text/plain")
    with pytest.raises(DriveWriteRefusedError):
        client.update(fid, b"%PDF", "application/pdf")
    assert svc.updates == []


@pytest.mark.parametrize(
    "name,mime",
    [
        ("20271005_Jana_NOU_Labs_Panel_OCR.txt", "text/plain"),  # OCR .txt export
        ("_manifest.json", "application/json"),
        ("treatment-timeline.md", "text/markdown"),
        ("_actions.jsonl", "text/plain"),
    ],
)
def test_legacy_update_text_export_happy_path(name, mime):
    client, svc = _client()
    fid = svc.add(name, svc.folder("Root", "top"), b"old", mime=mime)
    client.update(fid, b"new", mime)
    assert svc.items[fid]["content"] == b"new"


def test_legacy_trash_refuses_a_file():
    client, svc = _client()
    fid = svc.add("20271005-Scan.pdf", svc.folder("Root", "top"), b"%PDF")
    with pytest.raises(DriveWriteRefusedError, match="not a folder"):
        client.trash_file(fid)
    assert svc.updates == [] and not svc.items[fid]["trashed"]


def test_legacy_trash_refuses_a_non_empty_folder():
    client, svc = _client()
    folder = svc.folder("labs (1)", svc.folder("Root", "top"))
    svc.add("20271005-Scan.pdf", folder, b"%PDF")
    with pytest.raises(DriveWriteRefusedError, match="not empty"):
        client.trash_file(folder)
    assert svc.updates == [] and not svc.items[folder]["trashed"]


def test_legacy_trash_trashes_an_empty_folder():
    client, svc = _client()
    folder = svc.folder("labs (1)", svc.folder("Root", "top"))
    gone = svc.add("old.pdf", folder, b"%PDF")
    svc.items[gone]["trashed"] = True  # trashed children do not count
    client.trash_file(folder)
    assert svc.items[folder]["trashed"]


def test_legacy_refusals_are_never_retried():
    from oncofiles.gdrive_client import _is_connection_error

    assert not _is_connection_error(DriveWriteRefusedError("x"))


# ── static lock: all Drive writes go through the helpers ─────────────

CURATED_SCOPE = [*sorted((SRC / "curated").rglob("*.py")), SRC / "tools" / "curated.py"]
WRITE_HELPER_FILES = {SRC / "curated" / "drive_ops.py", SRC / "gdrive_client.py"}
_SERVICE_WRITES = {"update", "delete", "create", "trash", "emptyTrash", "batchDelete", "copy"}


def _files_chain(node: ast.AST) -> bool:
    """True for ``<x>.files()`` (the Drive service's files collection)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "files"
        and not node.args
    )


def _service_write_calls(path: Path) -> list[str]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        attr = node.func.attr
        if attr in _SERVICE_WRITES and _files_chain(node.func.value):
            out.append(f"{path.relative_to(SRC)}:{node.lineno}: .files().{attr}(")
    return out


def _is_trashed_true_body(node: ast.AST) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(k, ast.Constant)
        and k.value == "trashed"
        and isinstance(v, ast.Constant)
        and v.value is True
        for k, v in zip(node.keys, node.values, strict=True)
    )


def test_curated_code_writes_drive_only_through_drive_ops():
    offenders = []
    for path in CURATED_SCOPE:
        if path.name == "drive_ops.py":
            continue
        offenders += _service_write_calls(path)
    assert not offenders, "Drive writes outside curated/drive_ops.py:\n" + "\n".join(offenders)


def test_no_drive_delete_trash_body_or_media_update_outside_the_helpers():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            where = f"{path.relative_to(SRC)}:{getattr(node, 'lineno', '?')}"
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "delete"
                and _files_chain(node.func.value)
            ):
                offenders.append(f"{where}: .files().delete(")  # nowhere, not even helpers
            if path in WRITE_HELPER_FILES:
                continue
            # A Drive request body (``body={"trashed": True}``), not a report dict.
            if (
                isinstance(node, ast.keyword)
                and node.arg == "body"
                and _is_trashed_true_body(node.value)
            ):
                offenders.append(f'{path.relative_to(SRC)}: body={{"trashed": True}}')
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "update"
                and any(k.arg == "media_body" for k in node.keywords)
            ):
                offenders.append(f"{where}: media update(")
    assert not offenders, "\n".join(offenders)


def test_the_lock_scan_catches_a_planted_write(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text(
        "def f(g):\n"
        "    g._service.files().update(fileId='x', media_body=m).execute()\n"
        "    g._service.files().delete(fileId='x').execute()\n"
        "    g._service.files().trash(fileId='x').execute()\n"
    )
    global SRC
    old, SRC = SRC, tmp_path
    try:
        found = _service_write_calls(bad)
    finally:
        SRC = old
    assert any(".files().update(" in o for o in found)
    assert any(".files().delete(" in o for o in found)
    assert any(".files().trash(" in o for o in found)
