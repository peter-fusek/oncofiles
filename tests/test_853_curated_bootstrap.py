"""#853: the New Patient wizard writes the README contract and enrols the patient.

Synthetic data only (public repo): invented names, ids, e-mails on
example.test and birth dates that are obviously not real. No real folder,
README or Drive id is used anywhere here.
"""

from __future__ import annotations

import ast
import contextlib
import itertools
import json
import re
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import config, curated_folder, patient_context, server
from oncofiles.curated import bootstrap
from oncofiles.curated.bootstrap import (
    BootstrapFailedError,
    BootstrapRefusedError,
    bootstrap_folder,
    make_plan,
    patient_token,
    plan_for_patient,
)
from oncofiles.curated.contract import ROOT_STATE_FILES, parse_contract
from oncofiles.curated.drive_ops import CANARY_NAME, FOLDER_MIME, Item, WriteVerificationError
from oncofiles.curated.runner import RunOptions, probe, run_curated
from oncofiles.curated_folder import DriveWriteBlockedError, mark_if_curated
from oncofiles.database import Database
from oncofiles.gdrive_client import GDriveClient
from oncofiles.models import OAuthToken, Patient
from oncofiles.patient_consent import CONSENT_VERSION

SRC = Path(server.__file__).parent
TODAY = date(2027, 3, 4)
DOB = "1901-02-03"  # obviously invented
MY_DRIVE = "idMyDriveRoot00001"


@pytest.fixture(autouse=True)
def _clear_cache():
    curated_folder.clear_cache()
    yield
    curated_folder.clear_cache()


def _plan(patient_type: str = "oncology", name: str = "Testa Vymyslená") -> bootstrap.Plan:
    return make_plan(
        patient_type=patient_type,
        display_name=name,
        date_of_birth=DOB,
        today=TODAY,
        run_id="bootstrap-test-0001",
    )


# ── templates ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("ptype", ["oncology", "general"])
def test_template_renders_a_contract_the_pipeline_accepts(ptype):
    plan = _plan(ptype)
    assert "$" not in plan.readme  # every placeholder was filled
    contract = parse_contract(plan.readme)
    assert contract.patient_display == "Testa Vymyslená"
    assert contract.patient_token == "TestaVymyslena"
    assert contract.birth_dates == frozenset({"19010203"})
    assert contract.known_tokens == frozenset()  # vocabularies start empty
    assert contract.vocab_text  # §3 exists, it only lists no values
    assert contract.inbox.startswith("00-inbox")
    assert contract.logs.startswith("98-logs") and contract.duplicates.startswith("99-duplicates")
    assert "Other" in contract.doc_types
    assert contract.version == "1.0"
    assert f"template `{ptype}`" in plan.readme


def test_oncology_and_general_folder_sets_differ_as_in_562():
    onco = set(_plan("oncology").contract.doc_types)
    general = set(_plan("general").contract.doc_types)
    assert {"Treatment", "Reference", "Pathology", "Genetics"} <= onco
    assert not {"Preventive", "Vaccination", "Dental"} & onco
    assert {"Preventive", "Vaccination", "Dental"} <= general
    assert not {"Treatment", "Reference"} & general  # retired for general patients (#562)
    assert len(_plan("oncology").folders) == 17 and len(_plan("general").folders) == 18


def test_templates_are_generic():
    for path in (SRC / "curated" / "templates").glob("README_*.md"):
        text = path.read_text(encoding="utf-8")
        assert "${display}" in text and "${token}" in text and "${dob}" in text
        # No institution, doctor or person is baked into a template.
        assert not re.search(r"MUDr|NOU|Bory|@", text), path.name


def test_name_with_markdown_syntax_cannot_break_the_contract():
    plan = _plan(name="Testa `X` (Y) **Z** | W\nnew line")
    contract = parse_contract(plan.readme)
    assert contract.patient_display == "Testa X Y Z W new line"
    assert contract.patient_token == "TestaXYZWNewLine"


def test_patient_token_is_letters_only():
    assert patient_token("Ána-Mária O'Testová 3") == "AnaMariaOTestova"
    assert patient_token("123") == ""


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"date_of_birth": None}, "missing_dob"),
        ({"date_of_birth": ""}, "missing_dob"),
        ({"date_of_birth": "2099-01-01"}, "missing_dob"),
        ({"date_of_birth": "1899-12-31"}, "missing_dob"),
        ({"date_of_birth": "not a date"}, "missing_dob"),
        ({"patient_type": "veterinary"}, "invalid_patient_type"),
        ({"display_name": "123 !!"}, "invalid_name"),
        ({"display_name": ""}, "invalid_name"),
    ],
)
def test_plan_refusals(kwargs, reason):
    args = {"patient_type": "oncology", "display_name": "Testa", "date_of_birth": DOB}
    args.update(kwargs)
    with pytest.raises(BootstrapRefusedError) as exc:
        make_plan(**args, today=TODAY)
    assert exc.value.reason == reason


# ── fake Drive ───────────────────────────────────────────────────────────


class FakeDrive:
    """In-memory Drive with the bootstrap ops, the runner's DriveOps and contract_writes()."""

    def __init__(self):
        self.ids = itertools.count(1)
        self.items: dict[str, dict] = {
            MY_DRIVE: {"name": "My Drive", "parent": None, "folder": True}
        }
        self.log: list[tuple] = []
        self.fail_on: dict[str, int] = {}  # op -> fail on the n-th call (1-based)
        self.calls: dict[str, int] = {}
        self.depth = 0
        self.journal_at_first_folder: str | None = None
        self.not_writable: set[str] = set()

    # helpers
    def add(self, name, parent, content=b"", folder=False, trashed=False):
        fid = f"idFake{next(self.ids):012d}"
        self.items[fid] = {
            "name": name,
            "parent": parent,
            "folder": folder,
            "content": content,
            "mime": FOLDER_MIME if folder else "application/pdf",
            "trashed": trashed,
        }
        return fid

    def children(self, parent):
        return [k for k, d in self.items.items() if d["parent"] == parent and not d["trashed"]]

    def names(self, parent):
        return {self.items[k]["name"] for k in self.children(parent)}

    def find(self, parent, name):
        return next((k for k in self.children(parent) if self.items[k]["name"] == name), None)

    def is_curated_root(self, fid):
        return all(m in self.names(fid) for m in curated_folder.CURATED_MARKERS)

    @contextlib.contextmanager
    def contract_writes(self):
        self.depth += 1
        try:
            yield self
        finally:
            self.depth -= 1

    def _write(self, op):
        assert self.depth > 0, f"{op} outside contract_writes()"
        self.calls[op] = self.calls.get(op, 0) + 1
        if self.fail_on.get(op) == self.calls[op]:
            raise WriteVerificationError(f"{op}: simulated read-back mismatch")

    # bootstrap reads
    def get_meta(self, fid):
        if fid == "root":
            fid = MY_DRIVE
        d = self.items[fid]
        meta = {
            "id": fid,
            "name": d["name"],
            "mimeType": FOLDER_MIME if d["folder"] else d["mime"],
            "trashed": d.get("trashed", False),
            "capabilities": {"canAddChildren": fid not in self.not_writable},
        }
        if d["parent"]:
            meta["parents"] = [d["parent"]]
        return meta

    def my_drive_root_id(self):
        return MY_DRIVE

    def ancestors(self, fid):
        chain, cur = [], self.items[fid]["parent"]
        while cur:
            chain.append(cur)
            cur = self.items[cur]["parent"]
        return chain

    def curated_reason(self, fid):
        if self.is_curated_root(fid):
            return "is a curated folder"
        for anc in self.ancestors(fid):
            if self.is_curated_root(anc):
                return f"lies inside curated folder {anc}"
        for k, d in self.items.items():
            if d["folder"] and fid in self.ancestors(k) and self.is_curated_root(k):
                return f"contains curated folder {k}"
        return None

    # DriveOps (runner interface)
    def _item(self, fid):
        d = self.items[fid]
        return Item(fid, d["name"], FOLDER_MIME if d["folder"] else d["mime"], d["parent"])

    def list_children(self, fid):
        return [self._item(k) for k in self.children(fid)]

    def list_my_drive_root(self):
        return self.list_children(MY_DRIVE)

    def download(self, fid):
        return self.items[fid]["content"]

    # writes
    def create_folder(self, parent, name):
        if self.journal_at_first_folder is None:
            journal = self.find(parent, "_actions.jsonl")
            self.journal_at_first_folder = (
                self.items[journal]["content"].decode() if journal else ""
            )
        self._write("create_folder")
        assert name not in self.names(parent)
        self.log.append(("create_folder", name))
        return self.add(name, parent, folder=True)

    def create_text(self, parent, name, text, mime="text/markdown"):
        self._write("create_text")
        assert name not in self.names(parent)
        self.log.append(("create_text", name))
        fid = self.add(name, parent, text.encode())
        self.items[fid]["mime"] = mime
        return fid

    def update_text(self, fid, text, mime="text/markdown"):
        self._write("update_text")
        self.log.append(("update_text", self.items[fid]["name"]))
        self.items[fid]["content"] = text.encode()

    def move_rename(self, *a, **kw):  # never called by setup or a dry run
        raise AssertionError("bootstrap must never move anything")

    def trash_canary(self, fid):
        self._write("trash_canary")
        assert self.items[fid]["name"] == CANARY_NAME
        self.log.append(("trash", self.items[fid]["name"]))
        self.items[fid]["trashed"] = True


def _patient_folder(drive: FakeDrive, parent: str = MY_DRIVE) -> str:
    health = drive.add("Zdravie", parent, folder=True)
    return drive.add("Testa", health, folder=True)


def _run(drive: FakeDrive, folder: str, plan=None, others=frozenset()) -> dict:
    return bootstrap_folder(
        drive, folder, plan or _plan(), other_patient_folders=set(others), ops=drive
    )


# ── refusals: nothing is written ─────────────────────────────────────────


def _refused(drive, folder, others=frozenset()) -> str:
    with pytest.raises(BootstrapRefusedError) as exc:
        _run(drive, folder, others=others)
    assert drive.log == [], "a refusal must write nothing"
    return exc.value.reason


def test_refuses_my_drive_root():
    drive = FakeDrive()
    assert _refused(drive, "root") == "drive_root"
    assert _refused(drive, MY_DRIVE) == "drive_root"


def test_refuses_an_already_curated_folder():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.add("README.md", folder, b"x")
    drive.add("_index.json", folder, b"[]")
    assert _refused(drive, folder) == "already_curated"


def test_refuses_a_folder_inside_a_curated_folder():
    drive = FakeDrive()
    curated = _patient_folder(drive)
    drive.add("README.md", curated, b"x")
    drive.add("_index.json", curated, b"[]")
    inner = drive.add("plain", curated, folder=True)
    assert _refused(drive, inner) == "curated_overlap"


def test_refuses_another_patients_folder_and_anything_inside_it():
    drive = FakeDrive()
    other = _patient_folder(drive)
    inner = drive.add("empty", other, folder=True)
    assert _refused(drive, other, others={other}) == "other_patient_folder"
    assert _refused(drive, inner, others={other}) == "other_patient_folder"


def test_refuses_a_parent_of_other_folders():
    """A parent of another patient's folder always has a subfolder: refused."""
    drive = FakeDrive()
    patient = _patient_folder(drive)
    parent = drive.items[patient]["parent"]
    assert _refused(drive, parent, others={patient}) == "has_subfolders"


@pytest.mark.parametrize("name", [*ROOT_STATE_FILES, CANARY_NAME])
def test_refuses_a_folder_with_state_files(name):
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.add(name, folder, b"x")
    assert _refused(drive, folder) == "state_files_present"


def test_refuses_a_file_a_trashed_folder_and_a_read_only_folder():
    drive = FakeDrive()
    afile = drive.add("scan.pdf", MY_DRIVE, b"x")
    assert _refused(drive, afile) == "not_a_folder"
    folder = _patient_folder(drive)
    drive.not_writable.add(folder)
    assert _refused(drive, folder) == "not_writable"


def test_refuses_too_many_loose_files(monkeypatch):
    monkeypatch.setattr(bootstrap, "MAX_LOOSE_FILES", 2)
    drive = FakeDrive()
    folder = _patient_folder(drive)
    for i in range(3):
        drive.add(f"scan{i}.pdf", folder, b"x")
    assert _refused(drive, folder) == "too_many_files"


def test_a_failed_check_refuses_instead_of_passing():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.get_meta = MagicMock(side_effect=OSError("drive down (test)"))
    assert _refused(drive, folder) == "check_failed"


def test_detection_failure_is_check_failed():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.curated_reason = lambda fid: "curated-folder detection failed (fail closed)"
    assert _refused(drive, folder) == "check_failed"


# ── the writes ───────────────────────────────────────────────────────────


def _journal(drive, folder):
    fid = drive.find(folder, "_actions.jsonl")
    return [json.loads(line) for line in drive.items[fid]["content"].decode().splitlines()]


def test_bootstrap_writes_journal_first_and_readme_last():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    plan = _plan()
    result = _run(drive, folder, plan)

    assert drive.log[0] == ("create_text", "_actions.jsonl")  # write-ahead journal first
    creates = [entry for entry in drive.log if entry[0].startswith("create")]
    assert creates[-1] == ("create_text", "README.md")  # README last
    assert not any(op == "trash" for op, _ in drive.log)  # no canary, nothing trashed
    assert [n for op, n in drive.log if op == "create_folder"] == plan.folders

    # The journal listed every planned write before the first folder existed.
    planned = [json.loads(line) for line in drive.journal_at_first_folder.splitlines()]
    assert {p["status"] for p in planned} == {"planned"}
    assert [p["path"] for p in planned][: len(plan.folders)] == plan.folders
    assert [p["seq"] for p in planned] == list(range(1, 22))  # 17 folders + 4 files
    assert planned[-1]["path"] == "README.md" and planned[-1]["run_id"] == plan.run_id

    # The folder is now what the pipeline recognises.
    assert drive.curated_reason(folder) == "is a curated folder"
    readme = drive.items[drive.find(folder, "README.md")]["content"].decode()
    assert readme == plan.readme
    assert set(drive.names(folder)) == {*plan.folders, *ROOT_STATE_FILES}
    assert result["folders_created"] == 17 and result["files_created"] == 4
    assert result["journal_complete"] is True
    assert json.loads(drive.items[drive.find(folder, "_index.json")]["content"]) == []


def test_every_planned_write_is_confirmed_done():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    _run(drive, folder)
    entries = _journal(drive, folder)
    planned = {e["seq"] for e in entries if e["status"] == "planned"}
    done = {e["seq"] for e in entries if e["status"] == "done"}
    assert planned == done == set(range(1, 22))
    assert entries[-1]["path"] == "README.md" and entries[-1]["status"] == "done"


def test_a_lost_readme_journal_line_does_not_fail_a_curated_folder():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.fail_on["update_text"] = 3  # folders, state files, then README's line fails
    result = _run(drive, folder)
    assert result["journal_complete"] is False
    assert drive.curated_reason(folder) == "is a curated folder"


@pytest.mark.parametrize(("op", "nth"), [("create_folder", 3), ("create_text", 2)])
def test_a_read_back_failure_stops_without_retry_or_readme(op, nth):
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.fail_on[op] = nth
    with pytest.raises(BootstrapFailedError):
        _run(drive, folder)
    assert drive.calls[op] == nth  # no retry after the failure
    assert "README.md" not in drive.names(folder)
    assert drive.curated_reason(folder) is None  # not curated: nothing processes it
    assert "_actions.jsonl" in drive.names(folder)  # the journal stays as the record
    assert not any(op == "trash" for op, _ in drive.log)  # nothing deleted
    # A second attempt on the same folder is refused, never "repaired".
    drive.log.clear()
    assert _refused(drive, folder) in {"has_subfolders", "state_files_present"}


def test_a_failed_journal_write_writes_nothing_else():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    drive.fail_on["create_text"] = 1
    with pytest.raises(BootstrapFailedError) as exc:
        _run(drive, folder)
    assert exc.value.step == "journal"
    assert drive.log == []


def test_loose_files_stay_where_they_are_and_become_inbox_candidates():
    drive = FakeDrive()
    folder = _patient_folder(drive)
    scan = drive.add("scan_2027.pdf", folder, b"%PDF invented")
    result = _run(drive, folder)
    assert result["loose_files"] == 1
    assert drive.items[scan]["parent"] == folder and not drive.items[scan]["trashed"]
    pending = probe(drive, folder).pending
    assert scan in pending  # the pipeline files it on its first real run


@pytest.mark.parametrize("ptype", ["oncology", "general"])
def test_a_dry_run_on_the_new_folder_passes_preflight(ptype):
    drive = FakeDrive()
    folder = _patient_folder(drive)
    _run(drive, folder, _plan(ptype))
    writes_before = list(drive.log)
    report = run_curated(drive, folder, RunOptions(dry_run=True))
    assert report.status == "ok", report.message
    assert all(g.ok for g in report.gates if g.blocking), report.gates
    assert drive.log == writes_before  # a dry run writes nothing


def test_after_bootstrap_legacy_writes_are_blocked(monkeypatch):
    drive = FakeDrive()
    folder = _patient_folder(drive)
    _run(drive, folder)

    # Real detection code, answering from the fake tree.
    monkeypatch.setattr(curated_folder, "_marker_names", lambda g, fid: drive.names(fid))
    monkeypatch.setattr(
        curated_folder, "_parents", lambda g, fid: [p] if (p := drive.items[fid]["parent"]) else []
    )
    client = object.__new__(GDriveClient)
    client._service = MagicMock()
    client._creds = MagicMock()
    client.owner_email = None
    assert mark_if_curated(client, folder) is True
    with pytest.raises(DriveWriteBlockedError):
        client.upload("x.pdf", b"x")
    with pytest.raises(DriveWriteBlockedError):
        client.create_folder("labs", folder)


def test_create_patient_root_refuses_an_empty_name_and_resolves_my_drive():
    drive = FakeDrive()
    with pytest.raises(BootstrapRefusedError) as exc:
        bootstrap.create_patient_root(drive, " / ", "root", ops=drive)
    assert exc.value.reason == "invalid_folder_name"
    with drive.contract_writes():
        fid = bootstrap.create_patient_root(drive, "Oncofiles - Testa", "root", ops=drive)
    assert drive.items[fid]["parent"] == MY_DRIVE


# ── DB helpers ───────────────────────────────────────────────────────────

OTHER_UUID = "44444444-4444-4444-8444-444444444853"


async def test_list_other_patient_folder_ids_excludes_own_and_empty(db: Database):
    from tests.helpers import ERIKA_UUID

    await db.insert_patient(Patient(patient_id=OTHER_UUID, slug="other-853", display_name="O"))
    for pid, folder in ((ERIKA_UUID, "idOwnFolder0000001"), (OTHER_UUID, "idOtherFolder00001")):
        await db.upsert_oauth_token(
            OAuthToken(patient_id=pid, access_token="a", refresh_token="r", gdrive_folder_id=folder)
        )
    assert await db.list_other_patient_folder_ids(patient_id=ERIKA_UUID) == {"idOtherFolder00001"}
    assert await db.list_other_patient_folder_ids(patient_id=OTHER_UUID) == {"idOwnFolder0000001"}


async def test_plan_for_patient_reads_type_dob_and_name_from_the_patient(db: Database):
    await db.insert_patient(
        Patient(patient_id=OTHER_UUID, slug="testa-853", display_name="Testa Vymyslená")
    )
    await patient_context.ensure_loaded(db.db, OTHER_UUID)
    ctx = patient_context.update_context(
        {"patient_type": "general", "date_of_birth": DOB}, patient_id=OTHER_UUID
    )
    await patient_context.save_to_db(db.db, ctx, patient_id=OTHER_UUID)
    plan = await plan_for_patient(db, OTHER_UUID)
    assert plan.patient_type == "general" and plan.token == "TestaVymyslena"
    assert plan.contract.birth_dates == frozenset({"19010203"})


async def test_plan_for_patient_without_dob_is_refused(db: Database):
    await db.insert_patient(Patient(patient_id=OTHER_UUID, slug="nodob-853", display_name="Testa"))
    with pytest.raises(BootstrapRefusedError) as exc:
        await plan_for_patient(db, OTHER_UUID)
    assert exc.value.reason == "missing_dob"


# ── /api/gdrive-set-folder ───────────────────────────────────────────────

FOLDER_ID = "idTestaFolder00001"


def _db_mock() -> MagicMock:
    db = MagicMock()
    db.resolve_patient_id = AsyncMock(return_value="pid-853")
    token = MagicMock(token_expiry=None, access_token="a", refresh_token="r", gdrive_folder_id=None)
    db.get_oauth_token = AsyncMock(return_value=token)
    db.update_oauth_folder = AsyncMock()
    db.update_oauth_owner_email = AsyncMock()
    db.set_curated_nightly = AsyncMock()
    db.list_other_patient_folder_ids = AsyncMock(return_value={"idOtherFolder00001"})
    db.get_patient = AsyncMock(
        return_value=MagicMock(caregiver_email="owner@example.test", display_name="Testa")
    )
    return db


def _client() -> GDriveClient:
    c = object.__new__(GDriveClient)
    c._service = MagicMock()
    c._service.files.return_value.get.return_value.execute.return_value = {
        "id": FOLDER_ID,
        "name": "Testa",
        "mimeType": FOLDER_MIME,
    }
    c._creds = MagicMock()
    c.owner_email = None
    c.get_folder_owner = MagicMock(return_value=None)
    c.create_folder = MagicMock(return_value="idLegacyCreated001")
    c.find_folder = MagicMock(return_value=None)
    return c


class _Endpoint:
    """Runs the endpoint with Drive, the bootstrap and auth patched; records calls."""

    def __init__(self, *, reasons=(None, "is a curated folder"), email="owner@example.test"):
        self.db = _db_mock()
        self.client = _client()
        self.reasons = list(reasons)
        self.email = email
        self.bootstrap = MagicMock(
            return_value={
                "folders_created": 17,
                "files_created": 4,
                "loose_files": 2,
                "run_id": "bootstrap-test",
                "patient_type": "oncology",
                "folder_name": "Testa",
            }
        )
        self.plan = AsyncMock(return_value=_plan())
        self.create_root = MagicMock(return_value=FOLDER_ID)
        self.store_dob = AsyncMock(return_value=True)
        self.mode = "open"  # ONBOARDING_MODE (#852); the real gate decides
        self.invites: frozenset[str] = frozenset()
        self.real_acl = False
        self.admin = False

    def _reason(self, gdrive, folder_id, **_kw):
        return self.reasons.pop(0) if len(self.reasons) > 1 else self.reasons[0]

    async def call(self, body: dict):
        req = MagicMock()
        req.headers = {"authorization": "Bearer session:test"}
        req.json = AsyncMock(return_value={"patient_id": "testa-853", **body})
        req.app.state.fastmcp_server._lifespan_result = {"db": self.db}
        denied = server.JSONResponse({"error": "admin only"}, status_code=403)
        patches = [
            patch.object(server, "_check_dashboard_auth", return_value=None),
            patch.object(server, "_check_bearer", return_value=denied),  # a session caller
            patch.object(config, "ONBOARDING_MODE", self.mode),
            patch.object(config, "ONBOARDING_INVITE_EMAILS", self.invites),
            patch.object(server, "_get_dashboard_email", return_value=self.email),
            patch.object(server, "_is_admin_email", return_value=self.admin),
            patch.object(server, "_require_admin", return_value=None if self.admin else denied),
            patch("oncofiles.oauth.is_token_expired", return_value=False),
            patch.object(server.GDriveClient, "from_oauth", return_value=self.client),
            patch.object(
                curated_folder,
                "folder_overview",
                return_value={
                    "curated": False,
                    "subfolder_count": 0,
                    "curated_children": 0,
                    "container_reason": None,
                },
            ),
            patch.object(curated_folder, "curated_reason", side_effect=self._reason),
            patch.object(bootstrap, "bootstrap_folder", self.bootstrap),
            patch.object(bootstrap, "plan_for_patient", self.plan),
            patch.object(bootstrap, "create_patient_root", self.create_root),
            patch.object(bootstrap, "store_dob_if_missing", self.store_dob),
        ]
        if not self.real_acl:
            patches.append(
                patch.object(server, "_require_body_patient_access", AsyncMock(return_value=None))
            )
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            resp = await server.api_gdrive_set_folder(req)
        return resp.status_code, json.loads(resp.body)

    def assert_nothing_persisted(self):
        self.db.update_oauth_folder.assert_not_called()
        self.db.set_curated_nightly.assert_not_called()
        self.client.create_folder.assert_not_called()


async def test_endpoint_bootstraps_connects_and_enrols():
    ep = _Endpoint()
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200, data
    assert data["bootstrapped"] is True and data["enrolled"] is True
    assert data["curated"] is True and data["curated_root"] is True
    assert data["folders_created"] == 17 and data["loose_files"] == 2
    assert data["pipeline"]["kind"] == "curated" and data["pipeline"]["first_run"] == "preview"
    ep.bootstrap.assert_called_once()
    args, kwargs = ep.bootstrap.call_args
    assert args[1] == FOLDER_ID and kwargs["other_patient_folders"] == {"idOtherFolder00001"}
    ep.db.update_oauth_folder.assert_awaited_once()
    ep.db.set_curated_nightly.assert_awaited_once_with(
        patient_id="pid-853", enabled=True, updated_by="self-service (#853)"
    )
    ep.client.create_folder.assert_not_called()  # never the legacy category folders


async def test_endpoint_bootstrap_runs_before_anything_is_persisted():
    ep = _Endpoint()
    order = []
    ep.bootstrap.side_effect = lambda *a, **k: (
        order.append("bootstrap")
        or {
            "folders_created": 17,
            "files_created": 4,
            "loose_files": 0,
            "patient_type": "oncology",
        }
    )
    ep.db.update_oauth_folder.side_effect = lambda *a, **k: order.append("persist")
    ep.db.set_curated_nightly.side_effect = lambda **k: order.append("enrol")
    status, _ = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200
    assert order == ["bootstrap", "persist", "enrol"]


@pytest.mark.parametrize(
    "reason", ["has_subfolders", "missing_dob", "other_patient_folder", "state_files_present"]
)
async def test_endpoint_refusal_persists_nothing(reason):
    ep = _Endpoint()
    ep.bootstrap.side_effect = BootstrapRefusedError(reason)
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 409
    assert data["code"] == "bootstrap_refused" and data["reason"] == reason
    assert data["error"] and data["error_sk"]
    ep.assert_nothing_persisted()


async def test_endpoint_missing_dob_is_refused_before_any_drive_write():
    ep = _Endpoint()
    ep.plan.side_effect = BootstrapRefusedError("missing_dob")
    status, data = await ep.call({"folder_name": "Oncofiles - Testa", "parent_id": "root"})
    assert status == 409 and data["reason"] == "missing_dob"
    ep.create_root.assert_not_called()  # the new folder is not even created
    ep.bootstrap.assert_not_called()
    ep.assert_nothing_persisted()


async def test_endpoint_new_folder_is_created_verified_then_bootstrapped():
    ep = _Endpoint()
    status, data = await ep.call({"folder_name": "Oncofiles - Testa", "parent_id": "root"})
    assert status == 200 and data["bootstrapped"] is True
    ep.create_root.assert_called_once_with(
        ep.client, "Oncofiles - Testa", "root", other_patient_folders={"idOtherFolder00001"}
    )
    ep.plan.assert_awaited_once()  # built once, before the folder was created
    ep.client.create_folder.assert_not_called()  # not the retrying legacy create


async def test_endpoint_write_failure_is_502_and_persists_nothing():
    ep = _Endpoint()
    ep.bootstrap.side_effect = BootstrapFailedError("folders", 3)
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 502 and data["code"] == "bootstrap_failed"
    assert "nothing was deleted" in data["error"]
    ep.assert_nothing_persisted()


async def test_endpoint_connects_an_existing_curated_folder_without_bootstrap():
    ep = _Endpoint(reasons=("is a curated folder",))
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200 and data["curated"] is True
    assert "bootstrapped" not in data
    ep.bootstrap.assert_not_called()
    ep.db.set_curated_nightly.assert_not_called()  # an admin enrols hand-made folders


async def test_endpoint_detection_failure_is_refused():
    ep = _Endpoint(reasons=("curated-folder detection failed (fail closed)",))
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 409 and data["reason"] == "check_failed"
    ep.bootstrap.assert_not_called()
    ep.assert_nothing_persisted()


@pytest.mark.parametrize("body", [{"folder_id": FOLDER_ID}, {"folder_name": "Novy"}])
async def test_endpoint_closed_mode_refuses_a_non_admin_before_any_drive_call(body):
    ep = _Endpoint()
    ep.mode = "closed"
    with patch.object(server.GDriveClient, "from_oauth") as from_oauth:
        ep.client = from_oauth.return_value
        status, data = await ep.call(body)
    assert status == 403 and data["error"] == "onboarding_closed"
    from_oauth.assert_not_called()  # no Drive client, so no Drive call
    ep.db.get_oauth_token.assert_not_called()
    ep.plan.assert_not_called()
    ep.store_dob.assert_not_called()
    ep.create_root.assert_not_called()
    ep.bootstrap.assert_not_called()
    ep.assert_nothing_persisted()


async def test_endpoint_invite_mode_lets_an_invited_caller_bootstrap():
    ep = _Endpoint(email="invited@example.test")
    ep.mode = "invite"
    ep.invites = frozenset({"invited@example.test"})
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200 and data["bootstrapped"] is True
    ep.bootstrap.assert_called_once()

    stranger = _Endpoint(email="stranger@example.test")
    stranger.mode = "invite"
    stranger.invites = frozenset({"invited@example.test"})
    status, data = await stranger.call({"folder_id": FOLDER_ID})
    assert status == 403 and data["error"] == "onboarding_closed"
    stranger.bootstrap.assert_not_called()
    stranger.assert_nothing_persisted()


async def test_endpoint_closed_mode_admin_still_bootstraps():
    ep = _Endpoint()
    ep.mode = "closed"
    ep.admin = True
    status, data = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200 and data["bootstrapped"] is True


async def test_endpoint_legacy_layout_is_admin_only():
    ep = _Endpoint()
    status, _ = await ep.call({"folder_id": FOLDER_ID, "layout": "legacy"})
    assert status == 403
    ep.assert_nothing_persisted()
    status, _ = await ep.call({"folder_id": FOLDER_ID, "layout": "flat"})
    assert status == 400


async def test_endpoint_other_caregiver_cannot_bootstrap_someone_elses_patient():
    ep = _Endpoint(email="stranger@example.test")
    ep.real_acl = True
    status, _ = await ep.call({"folder_id": FOLDER_ID})
    assert status == 404  # #867: same answer as an unknown patient (#590)
    ep.bootstrap.assert_not_called()
    ep.plan.assert_not_called()
    ep.assert_nothing_persisted()


async def test_endpoint_own_caregiver_passes_the_acl():
    ep = _Endpoint(email="owner@example.test")
    ep.real_acl = True
    status, _ = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200
    ep.bootstrap.assert_called_once()


# ── lock tests ───────────────────────────────────────────────────────────


def _calls(attr: str) -> dict[str, int]:
    """Module path -> number of calls to ``<anything>.<attr>(...)`` or ``attr(...)``."""
    out: dict[str, int] = {}
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name == attr:
                rel = str(path.relative_to(SRC))
                out[rel] = out.get(rel, 0) + 1
    return out


def test_contract_writes_callers_are_the_pipeline_and_the_bootstrap_only():
    """Only the curated pipeline (tools/curated.py) and the bootstrap lift the write block."""
    assert set(_calls("contract_writes")) == {"tools/curated.py", "curated/bootstrap.py"}
    assert _calls("contract_writes")["curated/bootstrap.py"] == 1


def test_only_the_bootstrap_creates_folders_through_drive_ops():
    src = (SRC / "curated" / "runner.py").read_text(encoding="utf-8")
    assert "create_folder" not in src  # the pipeline never creates folders
    callers = {
        str(p.relative_to(SRC))
        for p in SRC.rglob("*.py")
        if "drive_ops.create_folder" in p.read_text(encoding="utf-8")
    }
    assert callers == {"curated/bootstrap.py"}


def test_no_code_path_outside_the_bootstrap_writes_readme_md():
    """Every write call with a literal README.md lives in the bootstrap (#853)."""
    write_names = {"create_text", "update_text", "upload", "create", "update", "create_file"}
    hits = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name not in write_names:
                continue
            consts = [
                c.value
                for c in ast.walk(node)
                if isinstance(c, ast.Constant) and c.value == "README.md"
            ]
            if consts:
                hits.append(str(path.relative_to(SRC)))
    assert hits == ["curated/bootstrap.py"]


def test_set_folder_legacy_folder_set_needs_the_legacy_layout():
    import inspect

    from oncofiles import drive_folder_link

    src = inspect.getsource(server.api_gdrive_set_folder)
    assert "_require_admin(request) is not None" in src
    assert 'create_subfolders=layout == "legacy"' in src
    assert 'bootstrap=layout == "curated"' in src
    assert "_dashboard_body_patient(" in src  # resolve + body ACL (#489, #867)
    # #852's gate runs first, before the body is even read.
    assert src.index('_dashboard_onboarding_refusal(request, "set_folder")') < src.index(
        "await request.json()"
    )
    link = inspect.getsource(drive_folder_link.connect_folder)
    # A bootstrapped folder never falls through to the legacy folder set.
    assert link.index("if bootstrap:\n        # Unreachable") < link.index(
        "for en_key in ALL_FOLDERS:"
    )
    # Bootstrap (and its persisting) happens before the folder is persisted.
    assert link.index("cb.bootstrap_folder") < link.index("await db.update_oauth_folder")


# ── dashboard (static) ───────────────────────────────────────────────────


def _html() -> str:
    return (SRC / "dashboard.html").read_text(encoding="utf-8")


def _js() -> str:
    return (SRC / "dashboard.js").read_text(encoding="utf-8")


def test_wizard_requires_the_birth_date():
    html, js = _html(), _js()
    tag = re.search(r'<input type="date" id="wiz-dob"[^>]*>', html).group(0)
    assert "required" in tag
    assert 'data-en="Date of birth *"' in html
    assert "Date of birth is required" in js


def test_wizard_always_confirms_before_connecting():
    js = _js()
    fn = js[js.index("function wizardStep2SetFolder") : js.index("async function wizardFolderSend")]
    assert "wizardFolderSend(" not in fn  # sending only happens from the confirmation box
    assert fn.count("wizardFolderAskConfirm(body, info);") == 1
    assert "has_subfolders" in fn
    assert "data.code === 'bootstrap_refused'" in js


def test_wizard_never_asks_for_the_legacy_layout():
    assert "layout" not in _js().split("// ── Onboarding wizard")[1].split("// ── #573")[0]


def test_wizard_shows_the_pipeline_bilingually():
    js, html = _js(), _html()
    assert 'id="wiz-step3-pipeline"' in html
    fn = js[js.index("function wizardPipelineNote") :]
    fn = fn[: fn.index("\n    }\n")]
    assert "wizSetBi(el, sk, en);" in fn and "innerHTML" not in fn
    assert "processing_enabled" in fn


def test_wizard_finish_selects_the_patient_by_uuid():
    js = _js()
    fn = js[js.index("async function wizardFinish") :]
    assert "currentPatientId = wizardPatientUuid || wizardPatientId;" in fn[:600]
    setup = js[js.index("function showGdriveSetup") :]
    assert "wizardPatientUuid = pid;" in setup[:800]


# ── drive_ops.create_folder (real function, mocked Drive service) ────────


def _service_client(names_in_parent=(), meta=None, blocked=False) -> GDriveClient:
    c = object.__new__(GDriveClient)
    files = MagicMock()
    files.list.return_value.execute.return_value = {
        "files": [{"id": f"idX{i}", "name": n} for i, n in enumerate(names_in_parent)]
    }
    files.create.return_value.execute.return_value = {"id": "idNewFolder0000001"}
    files.get.return_value.execute.return_value = meta or {
        "id": "idNewFolder0000001",
        "name": "labs — laboratórne výsledky",
        "mimeType": FOLDER_MIME,
        "parents": [FOLDER_ID],
    }
    c._service = MagicMock()
    c._service.files.return_value = files
    c._creds = MagicMock()
    c.owner_email = None
    if blocked:
        c.write_block_reason = "curated (test)"
    return c


def test_drive_ops_create_folder_verifies_and_never_retries():
    from oncofiles.curated import drive_ops

    c = _service_client()
    fid = drive_ops.create_folder(c, FOLDER_ID, "labs — laboratórne výsledky")
    assert fid == "idNewFolder0000001"
    body = c._service.files.return_value.create.call_args.kwargs["body"]
    assert body == {
        "name": "labs — laboratórne výsledky",
        "mimeType": FOLDER_MIME,
        "parents": [FOLDER_ID],
    }

    wrong_parent = _service_client(
        meta={"name": "labs — laboratórne výsledky", "mimeType": FOLDER_MIME, "parents": ["idX"]}
    )
    with pytest.raises(WriteVerificationError):
        drive_ops.create_folder(wrong_parent, FOLDER_ID, "labs — laboratórne výsledky")
    assert wrong_parent._service.files.return_value.create.call_count == 1  # no retry

    taken = _service_client(names_in_parent=["labs — laboratórne výsledky"])
    with pytest.raises(WriteVerificationError):
        drive_ops.create_folder(taken, FOLDER_ID, "labs — laboratórne výsledky")
    taken._service.files.return_value.create.assert_not_called()


def test_drive_ops_create_folder_respects_the_write_guard():
    from oncofiles.curated import drive_ops

    c = _service_client(blocked=True)
    with pytest.raises(DriveWriteBlockedError):
        drive_ops.create_folder(c, FOLDER_ID, "labs — laboratórne výsledky")
    with c.contract_writes():
        drive_ops.create_folder(c, FOLDER_ID, "labs — laboratórne výsledky")


# ── #835: the new patient's context carries its name ─────────────────────


async def test_create_patient_stores_the_display_name_in_the_context(db: Database, monkeypatch):
    async def _no_notify(**_kwargs):
        return None

    monkeypatch.setattr(server, "_notify_admin_onboarding_event", _no_notify)
    monkeypatch.setattr(server, "_check_dashboard_auth", lambda req: None)
    monkeypatch.setattr(server, "_check_rate_limit", lambda label: None)
    monkeypatch.setattr(server, "_get_dashboard_email", lambda req: "caregiver.853@example.test")
    req = MagicMock()
    req.headers = {}
    req.cookies = {}
    req.json = AsyncMock(
        return_value={
            "consent": True,
            "consent_version": CONSENT_VERSION,
            "patient_id": "testa-853",
            "display_name": "Testa Vymyslená",
            "date_of_birth": DOB,
        }
    )
    req.app.state.fastmcp_server._lifespan_result = {"db": db}
    resp = await server.api_create_patient(req)
    assert resp.status_code == 201
    pid = json.loads(resp.body)["patient"]["patient_id"]
    assert patient_context.get_context(pid)["name"] == "Testa Vymyslená"
    plan = await plan_for_patient(db, pid)  # the wizard's patient is ready for the bootstrap
    assert plan.token == "TestaVymyslena" and plan.patient_type == "oncology"


# ── follow-ups: DOB from the wizard, parent refused before a folder exists ──


def _parent_refused(drive, parent, others=frozenset()) -> str:
    before = set(drive.items)
    with pytest.raises(BootstrapRefusedError) as exc:
        bootstrap.create_patient_root(
            drive, "Oncofiles - Testa", parent, ops=drive, other_patient_folders=set(others)
        )
    assert set(drive.items) == before and drive.log == [], "no folder may be left behind"
    return exc.value.reason


def test_new_folder_parent_inside_or_equal_to_another_patients_folder_is_refused_first():
    drive = FakeDrive()
    other = _patient_folder(drive)
    inner = drive.add("sub", other, folder=True)
    assert _parent_refused(drive, other, {other}) == "other_patient_folder"
    assert _parent_refused(drive, inner, {other}) == "other_patient_folder"


def test_new_folder_parent_curated_or_inside_curated_is_refused_first():
    drive = FakeDrive()
    curated = _patient_folder(drive)
    drive.add("README.md", curated, b"x")
    drive.add("_index.json", curated, b"[]")
    inner = drive.add("labs", curated, folder=True)
    assert _parent_refused(drive, curated) == "curated_overlap"
    assert _parent_refused(drive, inner) == "curated_overlap"


def test_new_folder_parent_failures_are_refused_first():
    drive = FakeDrive()
    afile = drive.add("scan.pdf", MY_DRIVE, b"x")
    assert _parent_refused(drive, afile) == "not_a_folder"
    ro = _patient_folder(drive)
    drive.not_writable.add(ro)
    assert _parent_refused(drive, ro) == "not_writable"
    drive.get_meta = MagicMock(side_effect=OSError("drive down (test)"))
    assert _parent_refused(drive, "root") == "check_failed"


def test_new_folder_next_to_curated_folders_and_in_my_drive_is_allowed():
    drive = FakeDrive()
    curated = _patient_folder(drive)
    drive.add("README.md", curated, b"x")
    drive.add("_index.json", curated, b"[]")
    health = drive.items[curated]["parent"]  # contains a curated folder: a sibling is fine
    with drive.contract_writes():
        fid = bootstrap.create_patient_root(
            drive, "Oncofiles - Testa", health, ops=drive, other_patient_folders={curated}
        )
        assert drive.items[fid]["parent"] == health
        root_fid = bootstrap.create_patient_root(
            drive, "Oncofiles - Testa 2", "root", ops=drive, other_patient_folders={curated}
        )
    assert drive.items[root_fid]["parent"] == MY_DRIVE


@pytest.mark.parametrize("raw", ["", None, "2099-01-01", "1899-12-31", "1990-02-30", "3.2.1990", 7])
def test_parse_new_dob_refuses_bad_values(raw):
    with pytest.raises(BootstrapRefusedError) as exc:
        bootstrap.parse_new_dob(raw, today=TODAY)
    assert exc.value.reason == "invalid_dob"


async def test_store_dob_if_missing_stores_once_and_never_overwrites(db: Database):
    await db.insert_patient(Patient(patient_id=OTHER_UUID, slug="dob-853", display_name="Testa"))
    with pytest.raises(BootstrapRefusedError):
        await bootstrap.store_dob_if_missing(db, OTHER_UUID, "2099-01-01")
    assert not patient_context.get_context(OTHER_UUID).get("date_of_birth")
    assert await bootstrap.store_dob_if_missing(db, OTHER_UUID, DOB) is True
    assert await bootstrap.store_dob_if_missing(db, OTHER_UUID, "1902-03-04") is False
    assert patient_context.get_context(OTHER_UUID)["date_of_birth"] == DOB
    plan = await plan_for_patient(db, OTHER_UUID)  # now the bootstrap can run
    assert plan.contract.birth_dates == frozenset({"19010203"})


async def test_endpoint_stores_the_wizard_dob_before_the_plan():
    ep = _Endpoint()
    order = []
    ep.store_dob.side_effect = lambda *a, **k: order.append(("store", a[2])) or True
    ep.plan.side_effect = lambda *a, **k: order.append(("plan",)) or _plan()
    status, _ = await ep.call({"folder_id": FOLDER_ID, "date_of_birth": DOB})
    assert status == 200
    assert order == [("store", DOB), ("plan",)]


async def test_endpoint_without_a_dob_in_the_body_does_not_touch_the_context():
    ep = _Endpoint()
    status, _ = await ep.call({"folder_id": FOLDER_ID})
    assert status == 200
    ep.store_dob.assert_not_called()


@pytest.mark.parametrize("body_extra", [{"folder_id": FOLDER_ID}, {"folder_name": "Novy"}])
async def test_endpoint_invalid_wizard_dob_refuses_before_anything(body_extra):
    ep = _Endpoint()
    ep.store_dob.side_effect = BootstrapRefusedError("invalid_dob")
    status, data = await ep.call({**body_extra, "date_of_birth": "2099-01-01"})
    assert status == 409 and data["reason"] == "invalid_dob" and data["error_sk"]
    ep.create_root.assert_not_called()
    ep.bootstrap.assert_not_called()
    ep.assert_nothing_persisted()


def test_wizard_asks_for_the_dob_when_the_server_misses_it():
    html, js = _html(), _js()
    assert 'id="wiz-folder-dob-input"' in html and 'data-action="wizardFolderDobGo"' in html
    tag = re.search(r'<button[^>]*id="wiz-folder-dob-go"[^>]*>', html).group(0)
    assert 'data-sk="' in tag and 'data-en="' in tag
    assert "wizShow('wiz-folder-dob', refused && reason === 'missing_dob');" in js
    fn = js[js.index("function wizardFolderDobGo") : js.index("function wizardFolderConfirmBack")]
    assert "date_of_birth: dob" in fn and "wizardFolderSend(" in fn


@pytest.mark.parametrize("ptype", ["oncology", "general"])
def test_template_supports_the_automatic_vocabulary(ptype):
    """#854/#862: a bootstrapped README must accept auto-added institutions/doctors."""
    from oncofiles.curated import vocabulary

    plan = _plan(ptype)
    assert vocabulary.unsupported_reason(plan.readme, plan.contract) is None
    additions = [
        vocabulary.VocabAddition("Institution", "SampleClinic", "Sample Clinic"),
        vocabulary.VocabAddition("Doctor", "Kovacova", "MUDr. Testa Kovacova"),
    ]
    text, contract = vocabulary.add_to_readme(
        plan.readme, plan.contract, additions, run_id="run-test", day="2027-03-04"
    )
    assert contract.known_tokens == frozenset({"SampleClinic", "Kovacova"})
    assert contract.folders == plan.contract.folders


@pytest.mark.parametrize("ptype", ["oncology", "general"])
def test_template_vocabulary_wording_holds_with_auto_vocab_on_and_off(ptype):
    """#865: section 3 must not promise automatic additions (CURATED_AUTO_VOCAB may be off)."""
    from oncofiles.curated import vocabulary

    plan = _plan(ptype)
    start, end = vocabulary._section3_span(plan.readme)
    section3 = " ".join(plan.readme[start:end].split())
    for kind in ("institution", "doctor"):
        assert (
            f"A new {kind} printed in a document is either added below automatically or the "
            "document waits in the inbox for review, depending on the server setting" in section3
        )
    assert "is added below automatically;" not in section3


@pytest.mark.parametrize(("env", "expected"), [("true", True), ("false", False)])
def test_pipeline_summary_reports_the_auto_vocab_flag(monkeypatch, env, expected):
    """#865: the wizard reads the real CURATED_AUTO_VOCAB from the set-folder payload."""
    monkeypatch.setenv("CURATED_AUTO_VOCAB", env)
    assert bootstrap.pipeline_summary()["auto_vocab"] is expected


def test_wizard_vocabulary_note_follows_the_flag_in_both_languages():
    js = _js()
    fn = js[js.index("function wizardPipelineNote") :]
    fn = fn[: fn.index("\n    }\n")]
    on, off = fn.split("if (p.auto_vocab) {")[1].split("} else {")
    off = off.split("}")[0]
    assert "added to the README automatically" in on and "doplní do README" in on
    assert "waits in 00-inbox for review" in off and "počká v 00-inbox na kontrolu" in off
    assert "automatically" not in off and "doplní" not in off
    # The unconditional part of the note makes no promise about the vocabulary.
    head = fn.split("if (p.auto_vocab) {")[0]
    assert "README" not in head


def test_onboarding_gate_sensitive_refs_cover_the_bootstrap():
    import importlib

    gate_tests = importlib.import_module("tests.test_onboarding_gate")
    assert {"bootstrap_folder", "create_patient_root", "create_folder"} <= gate_tests.SENSITIVE_REFS
