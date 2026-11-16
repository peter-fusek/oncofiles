"""One Google API service per thread (#579).

The rule locked here: a googleapiclient service / ``httplib2.Http`` object is
never used by two threads at once and never crosses threads. The fake
``build`` below records the thread that built each service; every request's
``execute()`` checks that it runs on that thread and that no other request is
in flight on the same service. All ids, names and addresses are invented.
"""

from __future__ import annotations

import ast
import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from oncofiles import curated_folder, google_service, server
from oncofiles.calendar_client import CalendarClient
from oncofiles.curated_folder import DriveWriteBlockedError
from oncofiles.gdrive_client import FOLDER_MIME, GDriveClient
from oncofiles.gmail_client import GmailClient

SRC = Path(__file__).resolve().parents[1] / "src" / "oncofiles"
ROOT_ID = "idRootFolder00000579"
REOCR_DOC = {
    "document_id": 579,
    "filename": "20270304_InventedPatient_Clinic_Report_Checkup.pdf",
    "gdrive_id": "idFile0000000579",
    "sidecar_id": "idSidecar00000579",
    "mime": "application/pdf",
}
GENERIC = {
    "files": [],
    "items": [],
    "messages": [],
    "id": "idFake000000000579",
    "name": "Invented Folder",
    "mimeType": FOLDER_MIME,
    "parents": [],
    "owners": [{"emailAddress": "owner@example.com"}],
}


class Recorder:
    """Every service the fake ``build`` made, and every broken rule."""

    def __init__(self) -> None:
        self.services: list[FakeService] = []
        self.violations: list[str] = []
        self.lock = threading.Lock()

    def build(self, api: str, version: str, creds) -> FakeService:
        service = FakeService(self, api, creds)
        with self.lock:
            self.services.append(service)
        return service

    def executed(self) -> int:
        return sum(s.calls for s in self.services)

    def user_threads(self) -> set[int]:
        return {t for s in self.services for t in s.used_by}


class FakeHttp:
    def __init__(self, service: FakeService) -> None:
        self._service = service
        self.closed_by: list[int] = []

    def close(self) -> None:
        self.closed_by.append(threading.get_ident())

    def request(self, *_a, **_kw):
        """A media download chunk (``MediaIoBaseDownload`` talks to the Http itself)."""
        import httplib2

        self._service.use()
        body = b"# invented"
        head = {"status": "200", "content-range": f"bytes 0-{len(body) - 1}/{len(body)}"}
        return httplib2.Response(head), body


class FakeRequest:
    uri = "https://drive.invalid/download"
    headers: dict = {}

    def __init__(self, service: FakeService) -> None:
        self._service = service
        self.http = service._http

    def execute(self, **_kw) -> dict:
        self._service.use()
        return dict(GENERIC)


class _Use:
    def use(self) -> None:
        s, rec = self, self.rec
        me = threading.get_ident()
        with rec.lock:
            s.calls += 1
            s.used_by.add(me)
            if me != s.owner:
                rec.violations.append(f"{s.api}: service crossed threads")
            s.in_flight += 1
            if s.in_flight > 1:
                rec.violations.append(f"{s.api}: two requests at once on one service")
        time.sleep(0.002)  # long enough for another thread to collide
        with rec.lock:
            s.in_flight -= 1


class FakeResource:
    """``service.files().list(...)``, ``service.users().messages().get(...)``, …"""

    def __init__(self, service: FakeService) -> None:
        self._service = service

    def __getattr__(self, name: str):
        if name in ("messages", "attachments"):
            return lambda *a, **k: FakeResource(self._service)
        return lambda *a, **k: FakeRequest(self._service)


class FakeService(_Use):
    def __init__(self, rec: Recorder, api: str, creds) -> None:
        self.rec, self.api, self.creds = rec, api, creds
        self.owner = threading.get_ident()
        self.used_by: set[int] = set()
        self.calls = 0
        self.in_flight = 0
        self._http = FakeHttp(self)

    def __getattr__(self, name: str):
        return lambda *a, **k: FakeResource(self)


@pytest.fixture
def rec(monkeypatch) -> Recorder:
    recorder = Recorder()
    monkeypatch.setattr(google_service, "build_service", recorder.build)
    curated_folder.clear_cache()
    yield recorder
    curated_folder.clear_cache()


def _drive() -> GDriveClient:
    return GDriveClient.from_oauth("access", "refresh", "client-id", "client-secret")


def _in_threads(n: int, fn) -> None:
    """Run ``fn(i)`` in ``n`` threads that start together; re-raise the first error."""
    barrier = threading.Barrier(n)
    errors: list[BaseException] = []

    def run(i: int) -> None:
        barrier.wait()
        try:
            fn(i)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if errors:
        raise errors[0]


# ── the client itself ────────────────────────────────────────────────


def test_n_threads_on_one_drive_client_never_share_a_service(rec):
    client = _drive()

    def work(_i: int) -> None:
        for _ in range(10):
            client.list_child_folders("root")
            client.get_folder_owner(ROOT_ID)
            curated_folder.folder_overview(client, ROOT_ID)
            curated_folder.mark_if_curated(client, ROOT_ID)

    _in_threads(8, work)

    assert rec.violations == []
    used = [s for s in rec.services if s.calls]
    assert len(used) == 8  # one per worker thread; the constructor's stays unused
    assert all(len(s.used_by) == 1 for s in used)
    assert len({id(s._http) for s in used}) == 8
    assert len({id(s.creds) for s in rec.services}) == 1  # credentials are shared on purpose


@pytest.mark.parametrize(
    ("cls", "call"),
    [
        (GmailClient, lambda c: c.get_profile()),
        (GmailClient, lambda c: c.get_message("idMessage0000579")),
        (CalendarClient, lambda c: c.get_event("idEvent000000579")),
        (CalendarClient, lambda c: c.list_events()),
    ],
)
def test_gmail_and_calendar_clients_follow_the_same_rule(rec, cls, call):
    client = cls.from_oauth("access", "refresh", "client-id", "client-secret")
    _in_threads(6, lambda _i: [call(client) for _ in range(10)])
    assert rec.violations == []
    assert len([s for s in rec.services if s.calls]) == 6


def test_service_is_stable_within_a_thread_and_differs_between_threads(rec):
    client = _drive()
    mine = client._service
    assert client._service is mine  # no rebuild per call
    seen: list[object] = []
    t = threading.Thread(target=lambda: seen.append(client._service))
    t.start()
    t.join()
    assert seen[0] is not mine and seen[0].owner != mine.owner


def test_dedicated_copy_builds_in_the_thread_that_uses_it(rec):
    client = _drive()
    before = len(rec.services)
    own = client.dedicated_copy()
    assert len(rec.services) == before  # nothing built in the creating thread
    assert own._creds is client._creds

    def work(_i: int) -> None:
        own.list_child_folders("root")
        assert own._service is not client._service  # also apart on the same thread

    _in_threads(4, work)  # even a copy handed to several threads stays per-thread
    assert rec.violations == []
    assert own._service is not client._service


def test_rebuild_and_close_touch_only_the_calling_thread(rec):
    client = _drive()
    mine = client._service
    theirs: list[FakeService] = []
    ready, release = threading.Event(), threading.Event()

    def other() -> None:
        theirs.append(client._service)
        ready.set()
        release.wait()
        theirs.append(client._service)

    t = threading.Thread(target=other)
    t.start()
    ready.wait()
    client._rebuild_service()  # e.g. the retry decorator after an SSL error
    assert client._service is not mine
    assert mine._http.closed_by == [threading.get_ident()]
    client.close()
    release.set()
    t.join()
    assert theirs[0] is theirs[1]  # the other thread kept its service …
    assert theirs[0]._http.closed_by == []  # … and nobody closed it under it


def test_assigning_a_test_double_still_works():
    client = object.__new__(GDriveClient)
    double = MagicMock()
    client._service = double
    assert client._service is double
    seen: list[object] = []
    t = threading.Thread(target=lambda: seen.append(client._service))
    t.start()
    t.join()
    assert seen == [double]


def test_build_uses_the_bundled_discovery_document_and_no_network(monkeypatch):
    """``build()`` must not open a socket: with a discovery fetch, building in
    one thread would be a request of its own."""
    import socket

    def refuse(*_a, **_k):
        raise AssertionError("build() touched the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    client = _drive()
    gmail = GmailClient.from_oauth("access", "refresh", "client-id", "client-secret")
    calendar = CalendarClient.from_oauth("access", "refresh", "client-id", "client-secret")
    a, b = client._service, client.dedicated_copy()._service
    assert a._http is not b._http and a._http.http is not b._http.http
    assert gmail._service._http.http is not calendar._service._http.http


# ── credentials: one refresh at a time ───────────────────────────────


class _Creds:
    def __init__(self) -> None:
        self.token = "token-0"
        self.refreshes = 0
        self.at_once = 0
        self.max_at_once = 0
        self._guard = threading.Lock()

    def refresh(self, _request) -> None:
        with self._guard:
            self.at_once += 1
            self.max_at_once = max(self.max_at_once, self.at_once)
        time.sleep(0.01)
        with self._guard:
            self.refreshes += 1
            self.token = f"token-{self.refreshes}"
            self.at_once -= 1


def test_refresh_is_serialised_and_waiters_reuse_the_new_token():
    creds = google_service.serialize_refresh(_Creds())
    assert google_service.serialize_refresh(creds) is creds  # idempotent, one lock
    _in_threads(8, lambda _i: creds.refresh(None))
    assert creds.max_at_once == 1
    assert creds.refreshes == 1  # the other seven found the new token
    creds.refresh(None)  # a later forced refresh (401) still refreshes
    assert creds.refreshes == 2


def test_clients_and_their_copies_share_one_serialised_credentials_object(rec):
    client = _drive()
    own = client.dedicated_copy()
    own.list_child_folders("root")
    assert getattr(client._creds, google_service._REFRESH_LOCK_ATTR) is not None
    assert {id(s.creds) for s in rec.services} == {id(client._creds)}


# ── write guard with per-thread services ─────────────────────────────


def test_write_block_applies_on_every_thread_even_when_set_later(rec):
    client = _drive()
    _in_threads(3, lambda _i: client.list_child_folders("root"))  # services exist
    client.write_block_reason = "curated (test)"
    outcomes: list[str] = []

    def write(_i: int) -> None:
        try:
            client.rename_file("idFile0000000579", "x.pdf")
            outcomes.append("wrote")
        except DriveWriteBlockedError:
            outcomes.append("blocked")

    calls = rec.executed()
    _in_threads(5, write)
    assert outcomes == ["blocked"] * 5
    assert rec.executed() == calls  # refused before any request


def test_contract_writes_lifts_the_block_for_its_own_thread_only(rec):
    client = _drive()
    client.write_block_reason = "curated (test)"
    inside, done = threading.Event(), threading.Event()
    outcomes: dict[str, str] = {}

    def attempt(name: str) -> None:
        try:
            client.rename_file("idFile0000000579", "x.pdf")
            outcomes[name] = "wrote"
        except DriveWriteBlockedError:
            outcomes[name] = "blocked"

    def pipeline() -> None:
        with client.contract_writes():
            attempt("pipeline")
            inside.set()
            done.wait()
        attempt("pipeline_after")

    t = threading.Thread(target=pipeline)
    t.start()
    inside.wait()
    attempt("main_during")
    _in_threads(3, lambda i: attempt(f"other_{i}"))
    done.set()
    t.join()

    assert outcomes["pipeline"] == "wrote"
    assert outcomes["pipeline_after"] == "blocked"  # the scope ended with the block
    assert outcomes["main_during"] == "blocked"
    assert [outcomes[f"other_{i}"] for i in range(3)] == ["blocked"] * 3
    assert rec.violations == []


async def test_contract_writes_does_not_follow_into_to_thread_workers(rec):
    """The scope is the thread, not the asyncio context: ``to_thread`` copies
    context variables into the worker, and a context-wide bypass would open the
    folder to every helper started inside the block."""
    client = _drive()
    client.write_block_reason = "curated (test)"
    with client.contract_writes():
        with pytest.raises(DriveWriteBlockedError):
            await asyncio.to_thread(client.rename_file, "idFile0000000579", "x.pdf")
        client.rename_file("idFile0000000579", "x.pdf")  # this thread may


def test_contract_scope_of_a_copy_is_its_own(rec):
    client = _drive()
    client.write_block_reason = "curated (test)"
    own = client.dedicated_copy()
    with own.contract_writes():
        own.rename_file("idFile0000000579", "x.pdf")
        with pytest.raises(DriveWriteBlockedError):
            client.rename_file("idFile0000000579", "y.pdf")
    with pytest.raises(DriveWriteBlockedError):
        own.rename_file("idFile0000000579", "z.pdf")


# ── entry points: the shared client reached through asyncio.to_thread ─


def _db() -> MagicMock:
    db = MagicMock()
    db.resolve_patient_id = AsyncMock(return_value="pid-579")
    token = MagicMock(token_expiry=None, access_token="a", refresh_token="r", gdrive_folder_id=None)
    db.get_oauth_token = AsyncMock(return_value=token)
    db.update_oauth_folder = AsyncMock()
    db.update_oauth_owner_email = AsyncMock()
    db.list_curated_runs = AsyncMock(return_value=[])
    return db


@pytest.fixture
def dashboard(monkeypatch):
    """Authenticated dashboard request whose handler gets ``client`` from ``from_oauth``.

    Patched once per test (monkeypatch), never per coroutine: ``patch`` blocks
    that interleave across ``asyncio.gather`` restore in the wrong order and
    leave the mock in place for the rest of the test session.
    """

    def use(client: GDriveClient) -> None:
        monkeypatch.setattr(server, "_check_dashboard_auth", lambda *_a, **_k: None)
        monkeypatch.setattr(server, "_get_dashboard_patient_id", AsyncMock(return_value="pid-579"))
        monkeypatch.setattr(server, "_require_body_patient_access", AsyncMock(return_value=None))
        monkeypatch.setattr("oncofiles.oauth.is_token_expired", lambda *_a, **_k: False)
        monkeypatch.setattr(
            server.GDriveClient, "from_oauth", classmethod(lambda cls, *a, **k: client)
        )
        monkeypatch.setattr(server, "_folder_404_counts", {})
        monkeypatch.setattr(server, "_patient_clients_cache", {})
        monkeypatch.setattr(server, "_curated_patients", set())

    return use


async def _gdrive_folders(client: GDriveClient, parent: str | None = None):
    req = MagicMock()
    req.query_params = {"parent": parent} if parent else {}
    req.app.state.fastmcp_server._lifespan_result = {"db": _db()}
    return await server.api_gdrive_folders(req)


async def _gdrive_set_folder(client: GDriveClient):
    req = MagicMock()
    req.json = AsyncMock(return_value={"patient_id": "pacient-579", "folder_id": ROOT_ID})
    req.app.state.fastmcp_server._lifespan_result = {"db": _db()}
    return await server.api_gdrive_set_folder(req)


async def test_entry_points_never_pass_a_service_across_threads(rec, monkeypatch, dashboard):
    """The lock test of the incident: ONE cached patient client, reached at the
    same time by the folder picker endpoints (#838), the curated check that
    every ``_create_patient_clients`` call makes, the continuous-mode probe, the
    curated index scan, the README read, a document download path and plain
    ``asyncio.to_thread`` tool calls."""
    from oncofiles.curated import indexer
    from oncofiles.curated.contract import ContractError
    from oncofiles.curated.drive_ops import DriveOps
    from oncofiles.curated.facts_rules import Vocabulary
    from oncofiles.curated.reocr import run_reocr
    from oncofiles.curated.runner import RunOptions, run_curated
    from oncofiles.tools import curated as tool

    vocab = Vocabulary.from_rows([])

    client = _drive()  # built on the event-loop thread, like the client cache does
    dashboard(client)
    loop_thread = threading.get_ident()
    monkeypatch.setattr(
        server, "_create_patient_clients", AsyncMock(return_value=(client, None, None, ROOT_ID))
    )
    monkeypatch.setattr(tool, "_text_reviews", {})

    def index_scan():
        own = client.dedicated_copy()
        try:
            return indexer.scan(DriveOps(own), ROOT_ID, known={})
        finally:
            own.close()

    def readme():
        own = client.dedicated_copy()
        try:
            return indexer.readme_birth_dates(DriveOps(own), ROOT_ID)
        finally:
            own.close()

    def curated_run(dry_run: bool):
        own = client.dedicated_copy()
        try:
            opts = RunOptions(dry_run=dry_run)
            if dry_run:
                return run_curated(DriveOps(own), ROOT_ID, opts)
            with own.contract_writes():
                return run_curated(DriveOps(own), ROOT_ID, opts)
        finally:
            own.close()

    def reocr():
        own = client.dedicated_copy()
        try:
            return run_reocr(
                DriveOps(own), ROOT_ID, [dict(REOCR_DOC)], vocab, dry_run=True,
                now=datetime(2027, 3, 4, tzinfo=UTC), reader=lambda *a, **k: None,
            )  # fmt: skip
        finally:
            own.close()

    async def tolerant(coro):
        # the fake Drive is an empty tree, so the contract readers stop after
        # their listing; what matters is which thread asked
        try:
            return await coro
        except ContractError:
            return None
        except RuntimeError as exc:
            if "no README.md" not in str(exc):
                raise
            return None

    jobs = []
    for _ in range(6):
        jobs += [
            _gdrive_folders(client),
            _gdrive_folders(client, ROOT_ID),
            _gdrive_set_folder(client),
            server._mark_patient_curated("pid-579", client, ROOT_ID),
            tool.probe_patient(_db(), "pid-579", None),
            asyncio.to_thread(index_scan),
            asyncio.to_thread(curated_run, True),
            asyncio.to_thread(curated_run, False),
            asyncio.to_thread(reocr),
            asyncio.to_thread(client.download, "idFile0000000579"),
            asyncio.to_thread(readme),
            asyncio.to_thread(curated_folder.curated_reason, client, ROOT_ID, use_cache=False),
            asyncio.to_thread(client.get_file_metadata, ROOT_ID, "id,name"),
            asyncio.to_thread(client.list_folder, ROOT_ID, recursive=False),
            asyncio.to_thread(
                lambda: client._service.files().get(fileId=ROOT_ID, fields="id").execute()
            ),
        ]
    await asyncio.gather(*(tolerant(j) for j in jobs))

    assert rec.violations == []
    assert rec.executed() > 100  # the paths really reached the fake Drive
    workers = rec.user_threads() - {loop_thread}
    assert len(workers) > 1  # … from several worker threads at once
    assert loop_thread not in rec.user_threads()  # and never from the event loop


async def test_one_request_local_client_may_hop_between_worker_threads(rec, dashboard):
    """#838: ``/api/gdrive-folders`` builds one client per request and makes up
    to three ``to_thread`` calls on it, each on whichever worker is free."""
    client = _drive()
    dashboard(client)
    resp = await asyncio.gather(*(_gdrive_folders(client, ROOT_ID) for _ in range(12)))
    assert {r.status_code for r in resp} == {200}
    assert rec.violations == []


# ── source locks ─────────────────────────────────────────────────────


def _trees():
    for path in sorted(SRC.rglob("*.py")):
        yield path, ast.parse(path.read_text(encoding="utf-8"))


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def test_only_google_service_builds_services_or_http_objects():
    """A second place that calls ``discovery.build`` (or makes an ``Http``) can
    store the result on a client again and share it between threads."""
    offenders = []
    for path, tree in _trees():
        if path.name == "google_service.py":
            continue
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            elif isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            for mod in mods:
                if mod.startswith(
                    ("googleapiclient.discovery", "httplib2", "google_auth_httplib2")
                ):
                    offenders.append(f"{path.relative_to(SRC)}:{node.lineno} imports {mod}")
    assert offenders == []


def test_a_service_is_only_ever_used_in_place_never_stored_or_passed():
    """``client._service`` must be read where the request is made
    (``client._service.files()…``), so the thread that executes is the thread
    that looked it up. Storing it (``svc = client._service``), passing it as an
    argument (``asyncio.to_thread(fn, client._service)``), returning it or
    assigning it would let it cross threads."""
    offenders = []
    for path, tree in _trees():
        if path.name == "google_service.py":
            continue
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Attribute) and node.attr == "_service"):
                continue
            parent = parents[node]
            in_place = isinstance(parent, ast.Attribute) and parent.value is node
            if not in_place or isinstance(node.ctx, ast.Store):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


def test_curated_jobs_make_their_copy_inside_the_worker_function():
    """Runs, probes, index scans, re-OCR, re-read and restore take a
    ``dedicated_copy()`` in the function that ``asyncio.to_thread`` runs, and
    close it there."""
    tree = ast.parse((SRC / "tools" / "curated.py").read_text(encoding="utf-8"))
    parents = _parents(tree)
    sites = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "dedicated_copy"
    ]
    assert len(sites) >= 7
    for call in sites:
        node = call
        while not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node = parents[node]
        assert isinstance(node, ast.FunctionDef), f"line {call.lineno}: copy made in a coroutine"
        assert node.name == "_blocking", f"line {call.lineno}: copy made outside the worker"
        closes = [
            n
            for n in ast.walk(node)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "close"
        ]
        assert closes, f"line {call.lineno}: the copy is never closed"


# ── deploys wait for a real re-OCR / re-read job ─────────────────────


@pytest.mark.parametrize(
    ("kind", "dry_run", "expected"),
    [
        ("reocr", False, True),
        ("reread", False, True),
        ("restore", False, True),
        ("llm_shadow", True, False),
        ("clinical_preview", True, False),
        ("lab_preview", True, False),
        ("extraction_scorecard", True, False),
    ],
)
def test_run_active_counts_reocr_and_reread_jobs(monkeypatch, kind, dry_run, expected):
    from oncofiles.tools import curated as tool

    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(tool, "_restoring", {})
    monkeypatch.setattr(
        tool, "_reocr_jobs", {"pid-579": {"state": "running", "kind": kind, "dry_run": dry_run}}
    )
    assert tool.run_active() is expected
    tool._reocr_jobs["pid-579"]["state"] = "done"
    assert tool.run_active() is False


async def test_health_reports_a_running_reocr_job_without_the_db(monkeypatch):
    from oncofiles.tools import curated as tool

    monkeypatch.setattr(tool, "_runs", {})
    monkeypatch.setattr(tool, "_restoring", {})
    monkeypatch.setattr(
        tool, "_reocr_jobs", {"pid-579": {"state": "running", "kind": "reocr", "dry_run": False}}
    )
    src = Path(server.__file__).read_text(encoding="utf-8")
    assert 'result["curated_run_active"] = run_active()' in src
    assert tool.run_active() is True
