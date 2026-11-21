"""#921: "Add documents" on the dashboard.

Curated patient → the file goes unchanged into the README §2 inbox through
``drive_ops.upload_original`` inside ``contract_writes()`` (create-only, read
back by size + md5, never overwritten). Non-curated patient → the #777
Drive-only upload. ACL before the body is read; an admin's read-only view and
the operator bearer cannot upload. /demo sends nothing.

Synthetic data only (public repo): invented names, e-mails (example.test),
Drive ids and 2027+ dates.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import inspect
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import FormData, UploadFile
from starlette.responses import JSONResponse

from oncofiles import dashboard_upload, server
from oncofiles.curated import drive_ops, inbox_upload
from oncofiles.curated.drive_ops import FOLDER_MIME, WriteVerificationError
from oncofiles.database import Database
from tests.helpers import PATIENT_A_UUID
from tests.test_curated_originals_invariant import FakeGDrive, FakeService

SRC = Path(__file__).resolve().parent.parent / "src" / "oncofiles"
README = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
INBOX = "00-inbox — nové skeny"
PDF = b"%PDF-1.7\n% synthetic test page\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"JFIF"
HEIC = bytes(4) + b"ftypheic"
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBPVP8 "
MODIFIED_A = "2031-06-09T10:15:00Z"


# ── a fake patient client: guard + thread-scoped contract_writes ─────────


class ContractGDrive(FakeGDrive):
    """FakeGDrive plus the curated write guard of ``GDriveClient``."""

    def __init__(self, service: FakeService | None = None):
        super().__init__()
        if service is not None:
            self._service = service
        self.depth = 0
        self.guarded: list[tuple[str, int]] = []
        self.copies: list[ContractGDrive] = []
        self.closed = False

    def _guard_write(self, op):
        self.guarded.append((op, self.depth))
        if self.depth == 0:
            raise PermissionError(f"{op} outside contract_writes()")

    @contextlib.contextmanager
    def contract_writes(self):
        self.depth += 1
        try:
            yield self
        finally:
            self.depth -= 1

    def dedicated_copy(self):
        own = ContractGDrive(self._service)
        self.copies.append(own)
        return own

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def _download_from_fake(monkeypatch):
    monkeypatch.setattr(
        drive_ops, "download", lambda g, fid: g._service.items[fid]["content"], raising=True
    )


def _curated_tree(svc: FakeService, *, readme: str = README, index: bool = True):
    root = svc.folder("Jana", "top")
    inbox = svc.folder(INBOX, root)
    svc.folder("labs — laboratórne výsledky", root)
    svc.folder("98-logs — logy a archív", root)
    svc.folder("99-duplicates — duplikáty", root)
    if readme is not None:
        svc.add("README.md", root, readme.encode(), mime="text/markdown")
    if index:
        svc.add("_index.json", root, b"[]", mime="application/json")
    return root, inbox


# ── drive_ops.upload_original ────────────────────────────────────────────


def test_upload_original_creates_and_verifies():
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    with g.contract_writes():
        fid = drive_ops.upload_original(g, root, inbox, "scan_2027_03.pdf", PDF, "application/pdf")
    item = svc.items[fid]
    assert item["parent"] == inbox and item["name"] == "scan_2027_03.pdf"
    assert item["content"] == PDF and item["mime"] == "application/pdf"
    assert g.guarded == [("curated.upload_original", 1)]


def test_upload_original_respects_the_write_guard():
    g = ContractGDrive()
    root, inbox = _curated_tree(g._service)
    with pytest.raises(PermissionError, match="outside contract_writes"):
        drive_ops.upload_original(g, root, inbox, "scan.pdf", PDF, "application/pdf")
    assert g._service.names(inbox) == set()


def test_upload_original_respects_the_real_client_guard():
    from oncofiles.curated_folder import DriveWriteBlockedError
    from oncofiles.gdrive_client import GDriveClient

    c = object.__new__(GDriveClient)
    c.write_block_reason = "folder x is a curated folder (synthetic)"
    with pytest.raises(DriveWriteBlockedError):
        drive_ops.upload_original(
            c, "rootId0000001", "inboxId0000001", "scan.pdf", PDF, "application/pdf"
        )


@pytest.mark.parametrize(
    "existing,name",
    [
        ("scan.pdf", "scan.pdf"),  # never overwrite
        ("scan.pdf", "scan.pdf.md"),  # would become a sidecar of scan.pdf
        ("scan.pdf.md", "scan.pdf"),  # would adopt an orphan sidecar
        ("Sca\u0301n.pdf", "Sc\u00e1n.pdf"),  # NFD on Drive, NFC from the browser
    ],
)
def test_upload_original_refuses_clashes(existing, name):
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    svc.add(existing, inbox, b"OLD")
    before = dict(svc.items)
    with g.contract_writes(), pytest.raises(WriteVerificationError):
        drive_ops.upload_original(g, root, inbox, name, PDF, "application/pdf")
    assert svc.items.keys() == before.keys()
    assert svc.items[svc.find(inbox, existing)]["content"] == b"OLD"


@pytest.mark.parametrize(
    "name,data,mime",
    [
        ("README.md", b"# x", "text/markdown"),
        ("_index.json", b"[]", "application/json"),
        ("_healthcheck.tmp", b"x", "text/plain"),
        ("_reocr-candidate-x.md", b"x", "text/markdown"),
        (".hidden.pdf", PDF, "application/pdf"),
        ("a/b.pdf", PDF, "application/pdf"),
        ("empty.pdf", b"", "application/pdf"),
        ("big.pdf", b"x" * (drive_ops.UPLOAD_MAX_BYTES + 1), "application/pdf"),
        ("doc.pdf", PDF, "application/vnd.google-apps.document"),
        ("doc.pdf", PDF, ""),
    ],
)
def test_upload_original_refuses_bad_input(name, data, mime):
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    with g.contract_writes(), pytest.raises(WriteVerificationError):
        drive_ops.upload_original(g, root, inbox, name, data, mime)
    assert svc.names(inbox) == set()


def test_upload_original_refuses_any_folder_but_the_inbox():
    g = ContractGDrive()
    svc = g._service
    root, _ = _curated_tree(svc)
    labs = svc.find(root, "labs — laboratórne výsledky")
    for target in (root, labs):
        with g.contract_writes(), pytest.raises(WriteVerificationError, match="not the inbox"):
            drive_ops.upload_original(g, root, target, "scan.pdf", PDF, "application/pdf")
    pdf = svc.add("00-inbox.pdf", root, PDF)  # an inbox-looking FILE is no folder
    with g.contract_writes(), pytest.raises(WriteVerificationError, match="not the inbox"):
        drive_ops.upload_original(g, root, pdf, "scan.pdf", PDF, "application/pdf")


def test_upload_original_read_back_mismatch_raises_and_deletes_nothing(monkeypatch):
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    real_get = drive_ops.get

    def lying_get(gd, fid):
        meta = real_get(gd, fid)
        if meta.get("mimeType") != FOLDER_MIME:
            meta["md5Checksum"] = "0" * 32
        return meta

    monkeypatch.setattr(drive_ops, "get", lying_get)
    with g.contract_writes(), pytest.raises(WriteVerificationError, match="read-back"):
        drive_ops.upload_original(g, root, inbox, "scan.pdf", PDF, "application/pdf")
    assert svc.names(inbox) == {"scan.pdf"}  # left in place, never deleted
    assert all(not (u["body"] or {}).get("trashed") for u in svc.updates)


def test_upload_original_refuses_an_inbox_of_another_tree():
    """Infra review: the inbox must be a child of THIS patient's curated root."""
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    other_root, other_inbox = _curated_tree(svc)  # another patient's tree
    stray = svc.folder(INBOX, "top")  # an inbox-named folder outside any tree
    for target in (other_inbox, stray):
        with g.contract_writes(), pytest.raises(WriteVerificationError, match="not the inbox"):
            drive_ops.upload_original(g, root, target, "scan.pdf", PDF, "application/pdf")
        assert svc.names(target) == set()
    with g.contract_writes(), pytest.raises(WriteVerificationError, match="not the inbox"):
        drive_ops.upload_original(g, "", inbox, "scan.pdf", PDF, "application/pdf")
    with g.contract_writes():  # the right pair works
        drive_ops.upload_original(g, other_root, other_inbox, "scan.pdf", PDF, "application/pdf")
    assert svc.names(other_inbox) == {"scan.pdf"} and svc.names(inbox) == set()


# ── curated.inbox_upload ─────────────────────────────────────────────────


def test_inbox_comes_from_the_readme_contract():
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    svc.folder("00-inbox (1)", root)  # a look-alike the contract does not name
    assert inbox_upload.resolve_inbox(g, root) == inbox


@pytest.mark.parametrize(
    "readme,index,code",
    [
        (None, True, "not_curated"),
        (README, False, "not_curated"),
        ("# just notes, no contract\n", True, "contract_unreadable"),
    ],
)
def test_no_usable_inbox_refuses_before_any_write(readme, index, code):
    g = ContractGDrive()
    svc = g._service
    root, _ = _curated_tree(svc, readme=readme, index=index)
    files = [inbox_upload.InboxFile("scan.pdf", PDF, "application/pdf")]
    with pytest.raises(inbox_upload.InboxUnavailableError) as exc:
        inbox_upload.upload_to_inbox(g, root, files)
    assert exc.value.code == code
    assert not [u for u in svc.updates if u["name"] == "scan.pdf"]


def test_missing_inbox_folder_is_refused():
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    svc.items[inbox]["trashed"] = True
    with pytest.raises(inbox_upload.InboxUnavailableError, match="inbox_missing"):
        inbox_upload.resolve_inbox(g, root)


def test_upload_to_inbox_writes_inside_contract_writes_with_suffixes():
    g = ContractGDrive()
    svc = g._service
    root, inbox = _curated_tree(svc)
    svc.add("scan.pdf", inbox, b"OLD")
    files = [
        inbox_upload.InboxFile("scan.pdf", PDF, "application/pdf"),
        inbox_upload.InboxFile("scan.pdf", PDF + b"2", "application/pdf"),
        inbox_upload.InboxFile("photo.png", PNG, "image/png"),
    ]
    results = inbox_upload.upload_to_inbox(g, root, files)
    assert [r["name"] for r in results] == ["scan-2.pdf", "scan-3.pdf", "photo.png"]
    assert svc.names(inbox) == {"scan.pdf", "scan-2.pdf", "scan-3.pdf", "photo.png"}
    assert svc.items[svc.find(inbox, "scan.pdf")]["content"] == b"OLD"
    own = g.copies[0]  # the shared client never lifts its own block
    assert g.guarded == [] and own.closed
    assert own.guarded == [("curated.upload_original", 1)] * 3
    # nothing but the three originals: no sidecar, no state file, no rename
    assert [u for u in svc.updates if u["body"] is not None] == []


def test_first_failed_write_stops_the_rest(monkeypatch):
    g = ContractGDrive()
    root, _ = _curated_tree(g._service)
    calls = []

    def boom(gd, root_id, inbox_id, name, data, mime):
        calls.append(name)
        raise WriteVerificationError("synthetic")

    monkeypatch.setattr(drive_ops, "upload_original", boom)
    files = [inbox_upload.InboxFile(f"s{i}.pdf", PDF, "application/pdf") for i in range(3)]
    results = inbox_upload.upload_to_inbox(g, root, files)
    assert calls == ["s0.pdf"]
    assert [r["status"] for r in results] == ["failed", "not_attempted", "not_attempted"]
    assert results[0]["code"] == "WriteVerificationError"


def test_unique_name_rules():
    taken = {"scan.pdf", "scan-2.pdf", "note.md", "x.pdf.md"}
    assert inbox_upload.unique_name("scan.pdf", taken) == "scan-3.pdf"
    assert inbox_upload.unique_name("new.pdf", taken) == "new.pdf"
    assert inbox_upload.unique_name("x.pdf", taken) == "x-2.pdf"  # orphan sidecar
    assert inbox_upload.unique_name("noext", {"noext"}) == "noext-2"


# ── static locks ─────────────────────────────────────────────────────────


def _calls_named(attr: str) -> dict[str, list[ast.Call]]:
    out: dict[str, list[ast.Call]] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == attr:
                out.setdefault(str(path.relative_to(SRC)), []).append(node)
    return out


def test_only_inbox_upload_calls_upload_original():
    assert set(_calls_named("upload_original")) == {"curated/inbox_upload.py"}


def test_upload_original_is_called_inside_contract_writes():
    tree = ast.parse((SRC / "curated" / "inbox_upload.py").read_text(encoding="utf-8"))
    inside: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if any(
            isinstance(i.context_expr, ast.Call)
            and isinstance(i.context_expr.func, ast.Attribute)
            and i.context_expr.func.attr == "contract_writes"
            for i in node.items
        ):
            inside.update(id(n) for n in ast.walk(node))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "upload_original"
    ]
    assert calls and all(id(c) in inside for c in calls)


def test_media_creates_live_only_in_the_drive_helpers():
    """A Drive create with a media body (a new file's bytes) exists only in
    ``curated/drive_ops.py`` and the legacy ``gdrive_client.py``."""
    found = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "create"
                and any(k.arg == "media_body" for k in node.keywords)
            ):
                found.add(str(path.relative_to(SRC)))
    assert found == {"curated/drive_ops.py", "gdrive_client.py"}


def test_route_reaches_curated_writes_only_through_inbox_upload():
    assert "inbox_upload.upload_to_inbox" in inspect.getsource(dashboard_upload)
    writers = _calls_named("contract_writes")
    assert "dashboard_upload.py" not in writers and "server.py" not in writers


# ── checks per file ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name,data,expected",
    [
        ("scan.pdf", PDF, ("scan.pdf", "application/pdf")),
        ("IMG_A.JPG", JPEG, ("IMG_A.JPG", "image/jpeg")),
        ("p.png", PNG, ("p.png", "image/png")),
        ("p.heic", HEIC, ("p.heic", "image/heic")),
        ("p.tiff", b"II*\x00rest", ("p.tiff", "image/tiff")),
        ("p.webp", WEBP, ("p.webp", "image/webp")),
        ("note.md", "Otázka na kontrolu 2027".encode(), ("note.md", "text/markdown")),
        ("../../etc/plan.txt", b"plain note", ("plan.txt", "text/plain")),
        ("variants.vcf", b"##fileformat=VCFv4.2", "raw_genotype_excluded"),
        ("export.csv", b"rsid,chromosome", "raw_genotype_excluded"),
        ("export.tsv", b"rs123\t1", "raw_genotype_excluded"),
        ("calls.vcf.gz", b"\x1f\x8b", "raw_genotype_excluded"),
        ("dna_raw_data.txt", b"plain", "raw_genotype_excluded"),
        ("notes.txt", b"rs10001 AA\nrs10002 AG\nrs10003 GG\n", "raw_genotype_excluded"),
        ("setup.exe", b"MZ", "type_not_allowed"),
        ("noext", PDF, "type_not_allowed"),
        ("fake.pdf", b"<html>", "content_mismatch"),
        ("fake.png", PDF, "content_mismatch"),
        ("bin.txt", b"a\x00b", "content_mismatch"),
        ("latin.txt", "Ž".encode("cp1250") + b"\xff", "content_mismatch"),
        ("empty.pdf", b"", "empty_file"),
        ("big.pdf", b"%PDF-" + b"x" * dashboard_upload.MAX_FILE_BYTES, "file_too_large"),
    ],
)
def test_check_file(name, data, expected):
    assert dashboard_upload.check_file(name, data) == expected


# ── handle_upload: limits and where files go ─────────────────────────────


def _upload(name: str, data: bytes) -> UploadFile:
    import io

    return UploadFile(io.BytesIO(data), filename=name, size=len(data))


@pytest.fixture
def no_drive_calls(monkeypatch):
    """Fail loudly if a refused request reaches Drive."""
    monkeypatch.setattr(
        server, "_create_patient_clients", AsyncMock(side_effect=AssertionError("Drive reached"))
    )


async def test_no_files_and_too_many_files(db: Database, no_drive_calls):
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [])
    assert (out.status, out.payload["error"]) == (400, "no_files")
    many = [_upload(f"s{i}.pdf", PDF) for i in range(dashboard_upload.MAX_FILES + 1)]
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, many)
    assert (out.status, out.payload["error"]) == (400, "too_many_files")
    assert out.payload["message_sk"] and out.payload["message_en"]


async def test_refused_files_never_reach_drive(db: Database, no_drive_calls, monkeypatch):
    monkeypatch.setattr(dashboard_upload, "MAX_FILES", 2)
    out = await dashboard_upload.handle_upload(
        db, PATIENT_A_UUID, [_upload("x.vcf", b"##"), _upload("y.exe", b"MZ")]
    )
    assert out.status == 200 and out.payload["uploaded"] == 0
    assert [f["code"] for f in out.payload["files"]] == [
        "raw_genotype_excluded",
        "type_not_allowed",
    ]
    assert all("name" not in f for f in out.payload["files"])


async def test_document_cap_is_counted_before_writing(db: Database, no_drive_calls, monkeypatch):
    from oncofiles import config

    monkeypatch.setattr(config, "MAX_DOCUMENTS_PER_PATIENT", 2)
    monkeypatch.setattr(db, "count_documents", AsyncMock(return_value=2))
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("a.pdf", PDF)])
    assert (out.status, out.payload["error"], out.payload["limit"]) == (409, "document_cap", 2)


async def test_no_drive_is_refused(db: Database, monkeypatch):
    monkeypatch.setattr(server, "_create_patient_clients", AsyncMock(return_value=None))
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("a.pdf", PDF)])
    assert (out.status, out.payload["error"]) == (409, "no_drive")


def _clients(monkeypatch, reason):
    gdrive = MagicMock(name="patient-gdrive")
    monkeypatch.setattr(
        server,
        "_create_patient_clients",
        AsyncMock(return_value=(gdrive, None, None, "rootFolder0001")),
    )
    monkeypatch.setattr("oncofiles.curated_folder.curated_reason", lambda g, f, use_cache: reason)
    return gdrive


async def test_curated_patient_goes_to_the_inbox(db: Database, monkeypatch, caplog):
    gdrive = _clients(monkeypatch, "is a curated folder")
    monkeypatch.setattr(dashboard_upload, "MAX_FILES", 2)
    seen = {}

    def fake_upload(g, root_id, files):
        seen.update(g=g, root=root_id, files=files)
        return [{"status": "uploaded", "name": f.name} for f in files]

    monkeypatch.setattr(inbox_upload, "upload_to_inbox", fake_upload)
    legacy = AsyncMock()
    monkeypatch.setattr("oncofiles.tools.documents.store_drive_only_upload", legacy)
    caplog.set_level("INFO")
    out = await dashboard_upload.handle_upload(
        db, PATIENT_A_UUID, [_upload("Sken 2027-03-04.pdf", PDF), _upload("bad.csv", b"x")]
    )
    assert out.status == 200 and out.payload["path"] == "curated_inbox"
    assert seen["g"] is gdrive and seen["root"] == "rootFolder0001"
    assert [(f.name, f.mime) for f in seen["files"]] == [("Sken 2027-03-04.pdf", "application/pdf")]
    assert [f["status"] for f in out.payload["files"]] == ["uploaded", "refused"]
    legacy.assert_not_called()
    # logs: counts, sizes, mimes and name lengths, never the file name
    assert "Sken" not in caplog.text and "name len=" in caplog.text


async def test_curated_inbox_unavailable(db: Database, monkeypatch):
    _clients(monkeypatch, "is a curated folder")

    def no_inbox(g, root_id, files):
        raise inbox_upload.InboxUnavailableError("inbox_missing")

    monkeypatch.setattr(inbox_upload, "upload_to_inbox", no_inbox)
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("a.pdf", PDF)])
    assert (out.status, out.payload["error"], out.payload["reason"]) == (
        409,
        "inbox_unavailable",
        "inbox_missing",
    )


@pytest.mark.parametrize(
    "reason",
    ["lies inside curated folder abc", "curated-folder detection failed (fail closed)"],
)
async def test_overlapping_or_unchecked_folder_is_refused(db: Database, monkeypatch, reason):
    _clients(monkeypatch, reason)
    monkeypatch.setattr(inbox_upload, "upload_to_inbox", MagicMock(side_effect=AssertionError))
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("a.pdf", PDF)])
    assert (out.status, out.payload["error"]) == (409, "folder_not_writable")


async def test_non_curated_patient_uses_the_drive_only_path(db: Database, monkeypatch):
    gdrive = _clients(monkeypatch, None)
    gdrive.upload.return_value = {"id": "driveFileA", "modifiedTime": MODIFIED_A}
    monkeypatch.setattr("oncofiles.tools.documents.ensure_folder_structure", lambda g, f: {})
    monkeypatch.setattr(
        "oncofiles.tools.documents.resolve_category_folder", lambda m, c, f: "catFolder0001"
    )
    monkeypatch.setattr(inbox_upload, "upload_to_inbox", MagicMock(side_effect=AssertionError))
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("plan.pdf", PDF)])
    assert out.status == 200 and out.payload["path"] == "drive_only"
    assert out.payload["files"][0]["status"] == "uploaded"
    docs = await db.list_documents(patient_id=PATIENT_A_UUID)
    assert [(d.filename, d.file_id) for d in docs] == [("plan.pdf", "gdrive:driveFileA")]
    # a second file of the same name gets a suffix; nothing is superseded
    gdrive.upload.return_value = {"id": "driveFile0002", "modifiedTime": "2027-03-04T08:01:00Z"}
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("plan.pdf", PDF)])
    assert out.payload["files"][0]["name"] == "plan-2.pdf"
    names = sorted(d.filename for d in await db.list_documents(patient_id=PATIENT_A_UUID))
    assert names == ["plan-2.pdf", "plan.pdf"]


async def test_non_curated_drive_failure_stores_nothing(db: Database, monkeypatch):
    monkeypatch.setattr(dashboard_upload, "MAX_FILES", 2)
    gdrive = _clients(monkeypatch, None)
    gdrive.upload.side_effect = RuntimeError("synthetic")
    monkeypatch.setattr("oncofiles.tools.documents.ensure_folder_structure", lambda g, f: {})
    monkeypatch.setattr(
        "oncofiles.tools.documents.resolve_category_folder", lambda m, c, f: "catFolder0001"
    )
    out = await dashboard_upload.handle_upload(
        db, PATIENT_A_UUID, [_upload("a.pdf", PDF), _upload("b.pdf", PDF)]
    )
    assert [f["status"] for f in out.payload["files"]] == ["failed", "not_attempted"]
    assert await db.list_documents(patient_id=PATIENT_A_UUID) == []


# ── memory: one file's bytes at a time (infra review) ────────────────────


class TrackedUpload(UploadFile):
    def __init__(self, name: str, data: bytes, events: list):
        import io

        super().__init__(io.BytesIO(data), filename=name, size=len(data))
        self.events = events

    async def read(self, size: int = -1) -> bytes:
        self.events.append(("read", self.filename))
        return await super().read(size)

    async def close(self) -> None:
        self.events.append(("close", self.filename))
        await super().close()


def test_server_takes_one_file_per_request():
    assert dashboard_upload.MAX_FILES == 1
    src = inspect.getsource(server.api_upload)
    assert "max_files=dashboard_upload.MAX_FILES + 1" in src


async def test_too_many_files_reads_no_file(db: Database, no_drive_calls):
    events: list = []
    ups = [TrackedUpload(f"s{i}.pdf", PDF, events) for i in range(2)]
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, ups)
    assert out.payload["error"] == "too_many_files"
    assert not [e for e in events if e[0] == "read"]


async def test_each_file_is_read_stored_and_closed_before_the_next(db: Database, monkeypatch):
    """Even with a raised limit, never two files' bytes at once."""
    monkeypatch.setattr(dashboard_upload, "MAX_FILES", 3)
    _clients(monkeypatch, "is a curated folder")
    events: list = []

    def fake_upload(g, root_id, files):
        assert len(files) == 1  # one file per Drive call
        events.append(("store", files[0].name))
        return [{"status": "uploaded", "name": files[0].name}]

    monkeypatch.setattr(inbox_upload, "upload_to_inbox", fake_upload)
    ups = [TrackedUpload(n, PDF, events) for n in ("a.pdf", "b.exe", "c.pdf")]
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, ups)
    assert [f["status"] for f in out.payload["files"]] == ["uploaded", "refused", "uploaded"]
    assert events == [
        ("read", "a.pdf"),
        ("store", "a.pdf"),
        ("close", "a.pdf"),
        ("read", "b.exe"),
        ("close", "b.exe"),
        ("read", "c.pdf"),
        ("store", "c.pdf"),
        ("close", "c.pdf"),
    ]


def test_upload_code_never_collects_the_payloads():
    """No list/comprehension of file bytes in the route or the handler (AST)."""
    for fn in (server.api_upload, dashboard_upload.handle_upload):
        tree = ast.parse(_dedent(inspect.getsource(fn)))
        for node in ast.walk(tree):
            if isinstance(node, (ast.ListComp, ast.GeneratorExp, ast.SetComp, ast.DictComp)):
                names = (
                    {getattr(n, "attr", getattr(n, "id", None)) for n in ast.walk(node.elt)}
                    if hasattr(node, "elt")
                    else set()
                )
                assert not names & {"read", "data", "_read"}, fn.__name__
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("append", "extend")
            ):
                args = {getattr(n, "id", None) for a in node.args for n in ast.walk(a)}
                assert "data" not in args, fn.__name__
    assert ".read(" not in inspect.getsource(server.api_upload)


def _dedent(src: str) -> str:
    import textwrap

    return textwrap.dedent(src)


# ── the deploy gate: an upload is not a run or a job ─────────────────────


async def test_upload_never_registers_as_a_run_or_job(db: Database, monkeypatch):
    from oncofiles.tools import curated as curated_tools

    _clients(monkeypatch, "is a curated folder")
    before = (
        dict(curated_tools._runs),
        dict(curated_tools._reocr_jobs),
        dict(curated_tools._restoring),
    )
    seen = {}

    def fake_upload(g, root_id, files):
        seen["run_active"] = curated_tools.run_active()
        seen["blocked"] = curated_tools.run_block_reason(PATIENT_A_UUID)
        seen["registries"] = (
            dict(curated_tools._runs),
            dict(curated_tools._reocr_jobs),
            dict(curated_tools._restoring),
        )
        return [{"status": "uploaded", "name": files[0].name}]

    monkeypatch.setattr(inbox_upload, "upload_to_inbox", fake_upload)
    out = await dashboard_upload.handle_upload(db, PATIENT_A_UUID, [_upload("a.pdf", PDF)])
    assert out.payload["uploaded"] == 1
    assert seen == {"run_active": False, "blocked": None, "registries": before}
    assert not curated_tools.run_active()
    for mod in (dashboard_upload, inbox_upload):
        src = inspect.getsource(mod)
        for name in ("_runs", "_reocr_jobs", "_restoring", "patient_lock", "insert_curated_run"):
            assert name not in src, (mod.__name__, name)


# ── the route: ACL before the body, read-only admin view ─────────────────

A_EMAIL = "carer-a@example.test"
B_UUID = "00000000-0000-4000-8000-0000000009b1"
B_EMAIL = "carer-b@example.test"


@pytest.fixture
async def acl_db(db: Database) -> Database:
    await db.db.execute(
        "UPDATE patients SET slug = ?, caregiver_email = ? WHERE patient_id = ?",
        ("up921-a", A_EMAIL, PATIENT_A_UUID),
    )
    await db.db.execute(
        "INSERT INTO patients (patient_id, slug, display_name, caregiver_email) "
        "VALUES (?, ?, ?, ?)",
        (B_UUID, "up921-b", "Bea Synthetic", B_EMAIL),
    )
    await db.db.commit()
    return db


def _request(db: Database, patient: str | None, *, auth="Bearer session:synthetic"):
    request = MagicMock()
    request.query_params = {"patient_id": patient} if patient is not None else {}
    request.headers = {"authorization": auth}
    request.app.state.fastmcp_server._lifespan_result = {"db": db, "files": None}
    form = FormData([("files", _upload("a.pdf", PDF))])
    request.form = AsyncMock(return_value=form)
    return request


@contextlib.contextmanager
def _caller(email: str | None, *, bearer: bool = False, admin: bool = False):
    unauthorized = JSONResponse({"error": "unauthorized"}, status_code=401)
    with (
        patch.object(server, "_get_dashboard_email", return_value=None if bearer else email),
        patch.object(server, "_check_bearer", return_value=None if bearer else unauthorized),
        patch.object(server, "_is_admin_email", return_value=admin),
    ):
        yield


@pytest.fixture
def handled(monkeypatch):
    calls = []

    async def fake(db, pid, uploads):
        calls.append((pid, [u.filename for u in uploads]))
        return dashboard_upload.Outcome(200, {"path": "curated_inbox", "uploaded": 1, "files": []})

    monkeypatch.setattr(dashboard_upload, "handle_upload", fake)
    return calls


async def test_route_own_patient_by_slug(acl_db, handled):
    with _caller(A_EMAIL):
        resp = await server.api_upload(_request(acl_db, "up921-a"))
    assert resp.status_code == 200
    assert handled == [(PATIENT_A_UUID, ["a.pdf"])]


@pytest.mark.parametrize("target", [B_UUID, "up921-b", "no-such-patient"])
async def test_route_foreign_or_unknown_is_the_same_404_and_reads_no_body(acl_db, handled, target):
    req = _request(acl_db, target)
    with _caller(A_EMAIL):
        resp = await server.api_upload(req)
    assert resp.status_code == 404 and json.loads(resp.body) == {"error": "not_found"}
    req.form.assert_not_called()
    assert handled == []


async def test_route_admin_read_only_view_is_refused(acl_db, handled):
    req = _request(acl_db, "up921-b")
    with _caller(A_EMAIL, admin=True):
        resp = await server.api_upload(req)
    assert resp.status_code == 403 and json.loads(resp.body)["error"] == "read_only"
    req.form.assert_not_called()
    assert handled == []


async def test_route_admin_who_is_the_caregiver_may_upload(acl_db, handled):
    with _caller(A_EMAIL, admin=True):
        resp = await server.api_upload(_request(acl_db, "up921-a"))
    assert resp.status_code == 200 and handled


async def test_route_operator_bearer_is_read_only(acl_db, handled):
    with _caller(None, bearer=True):
        resp = await server.api_upload(_request(acl_db, "up921-a", auth="Bearer synthetic"))
    assert resp.status_code == 403 and handled == []


async def test_route_needs_auth_and_a_patient(acl_db, handled):
    with _caller(None):
        assert (await server.api_upload(_request(acl_db, "up921-a"))).status_code == 401
    with _caller(A_EMAIL):
        assert (await server.api_upload(_request(acl_db, None))).status_code == 400
    assert handled == []


def test_route_uses_the_single_body_acl_and_reads_the_body_last():
    src = inspect.getsource(server.api_upload)
    assert src.index("_dashboard_body_patient(") < src.index("_dashboard_write_refusal(")
    assert src.index("_dashboard_write_refusal(") < src.index("await request.form(")
    assert "_email_matches_caregiver" not in src


# ── rate limiter: own body limit for the upload path ─────────────────────


def test_upload_path_has_its_own_body_limit():
    from oncofiles import ratelimit

    cfg = ratelimit.RateLimitConfig()
    assert cfg.upload_max_body_bytes > dashboard_upload.MAX_FILE_BYTES
    assert cfg.upload_max_body_bytes < 2 * dashboard_upload.MAX_FILE_BYTES
    assert ratelimit.DEFAULTS[ratelimit.API].max_body_bytes < dashboard_upload.MAX_FILE_BYTES
    assert "/api/upload" in ratelimit.UPLOAD_PATHS
    assert ratelimit.classify("/api/upload") == ratelimit.API


async def test_rate_limiter_413s_an_oversized_upload_before_the_app():
    from oncofiles import ratelimit

    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    cfg = ratelimit.RateLimitConfig(upload_max_body_bytes=1000)
    mw = ratelimit.RateLimitMiddleware(app, cfg)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    for path, length, status in (("/api/upload", 1001, 413), ("/api/upload", 999, None)):
        scope = {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-length", str(length).encode())],
            "client": ("127.0.0.1", 5000),
        }
        sent.clear()
        await mw(scope, receive, send)
        if status:
            assert sent[0]["status"] == status
        else:
            assert reached == ["/api/upload"]


# ── frontend: app and /demo ──────────────────────────────────────────────

JS = (SRC / "dashboard.js").read_text(encoding="utf-8")
HTML = (SRC / "dashboard.html").read_text(encoding="utf-8")
CSS = (SRC / "dashboard.css").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    start = re.search(rf"\n    (?:async )?function {re.escape(name)}\(", JS)
    assert start, name
    return JS[start.start() : JS.find("\n    }\n", start.start())]


def test_demo_sends_nothing():
    upload = _fn("uploadFiles")
    assert upload.index("if (DEMO) { simulateDemoUpload(files); return; }") < upload.index(
        "uploadOne("
    )
    assert "XMLHttpRequest" not in _fn("simulateDemoUpload")
    assert "fetch(" not in _fn("simulateDemoUpload")
    # the XHR chokepoint is installed with the fetch one, before any request
    assert JS.index("XMLHttpRequest.prototype.open = function") < JS.index("new XMLHttpRequest()")
    assert JS.index("XMLHttpRequest.prototype.open = function") < JS.index(
        "var DEMO_BLOCKED_ACTIONS"
    )
    # the fetch chokepoint itself is unchanged
    assert "var DEMO_ALLOWED_PATHS = ['/api/demo-data'];" in JS
    assert "Demo: nothing was uploaded" in JS


def test_uploads_go_one_at_a_time_to_the_upload_route():
    upload = _fn("uploadFiles")
    assert "uploadQueue = uploadQueue.then(" in upload
    one = _fn("uploadOne")
    assert (
        "xhr.open('POST', '/api/upload?patient_id=' + encodeURIComponent(currentPatientId));" in one
    )
    assert "xhr.setRequestHeader('Authorization', authHeader());" in one
    assert "form.append('files', file, file.name);" in one


def test_controls_exist_and_are_bilingual():
    for needle in (
        'id="upload-btn" data-action="toggleUploadPanel"',
        'id="upload-files" class="sr-only" multiple',
        "webkitdirectory",
        'accept="image/*" capture="environment"',
        'data-change="uploadFromInput|@this"',
        'data-sk="Pridať dokumenty" data-en="Add documents"',
    ):
        assert needle in HTML, needle
    # labels keep their text in an inner span: applyDashLang() would wipe the input
    for label in re.findall(r'<label class="copy-btn upload-pick">(.*?)</label>', HTML):
        assert label.startswith("<span data-sk=")
    assert "innerHTML" not in _fn("uploadItem") and "innerHTML" not in _fn("simulateDemoUpload")
    assert "confirm(" not in JS and "alert(" not in JS


def test_admin_read_only_view_hides_the_upload():
    mode = _fn("updateAdminMode")
    assert "uploadBtn.hidden = isAdminMode;" in mode
    assert "if (isAdminMode || !currentPatientId) return;" in _fn("uploadFiles")


def test_upload_state_is_a_shape():
    item = _fn("uploadItem")
    assert "stateMark('progress')" in item and "stateMark(kind)" in item
    assert ".upload-item .state-mark" in CSS


async def test_demo_page_has_the_upload_control():
    from starlette.requests import Request

    page = (
        await server.demo_dashboard(
            Request({"type": "http", "method": "GET", "path": "/demo", "headers": []})
        )
    ).body.decode()
    assert 'id="upload-panel"' in page and 'id="upload-btn"' in page


async def test_demo_names_follow_the_readme_standard():
    from starlette.requests import Request

    from oncofiles.curated.naming import is_standard

    resp = await server.api_demo_data(
        Request({"type": "http", "method": "GET", "path": "/api/demo-data", "headers": []})
    )
    rows = json.loads(resp.body)["dashboard"]["documents"]["documents"]
    named = [r for r in rows if r["is_standard_name"]]
    assert len(named) >= 8
    for r in named:
        assert is_standard(r["filename"], patient_token="JanaTestova"), r["filename"]
        assert is_standard(r["filename_en"], patient_token="JaneDoe"), r["filename_en"]
        assert "_" not in r["filename"] and int(r["filename"][:4]) >= 2027
    unnamed = [r["filename"] for r in rows if not r["is_standard_name"]]
    assert unnamed and not any(is_standard(n) for n in unnamed)


def test_fixture_hash_helper_is_md5():  # the fake service reports Drive's md5Checksum
    svc = FakeService()
    fid = svc.add("x.pdf", "p", PDF)
    meta = svc.files()._meta(fid)
    assert meta["md5Checksum"] == hashlib.md5(PDF, usedforsecurity=False).hexdigest()


def test_slow_connection_is_explained_and_never_retried():
    one = _fn("uploadOne")
    assert "xhr.onerror = tooSlow;" in one and "xhr.ontimeout = tooSlow;" in one
    assert "xhr.status === 502 || xhr.status === 504 || xhr.status === 0" in one
    assert "Try Wi-Fi or a smaller file." in JS and "Skúste Wi-Fi alebo menší súbor." in JS
    body = one.split("{", 1)[1]
    assert one.count("new XMLHttpRequest()") == 1 and "uploadOne(" not in body  # no retry
