"""End-to-end tests for the curated-folder runner (#562) on an in-memory Drive.

Synthetic data only (public repo). Extraction and classification are injected.
"""

import hashlib
import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from oncofiles.curated import llm
from oncofiles.curated.drive_ops import FOLDER_MIME, Item, WriteVerificationError
from oncofiles.curated.extract import Extraction
from oncofiles.curated.runner import CANARY, RunOptions, run_curated

README = (Path(__file__).parent / "fixtures" / "curated" / "README_oncology.md").read_text()
NOW = datetime(2026, 11, 7, 6, 30, tzinfo=UTC)
INBOX, LABS, REPORT, DUPS, LOGS = (
    "00-inbox — nové skeny",
    "labs — laboratórne výsledky",
    "report — lekárske správy",
    "99-duplicates — duplikáty",
    "98-logs — logy a archív",
)


class FakeDrive:
    def __init__(self):
        self.ids = itertools.count(1)
        self.items: dict[str, dict] = {}
        self.fail_on: set[str] = set()  # op names that raise WriteVerificationError
        self.writes: list[tuple] = []
        self.root = self.add("Jana", "rootid", folder=True)
        self.folder = {}
        for name in [INBOX, LABS, REPORT, "imaging — zobrazovanie", "other — ostatné", DUPS, LOGS]:
            self.folder[name] = self.add(name, self.root, folder=True)
        self.add("README.md", self.root, README.encode())
        self.add("_index.json", self.root, b"[]")
        self.add("_changelog.md", self.root, b"# changelog\n")
        self.add("_actions.jsonl", self.root, b"")

    def add(self, name, parent, content=b"", folder=False, modified="2026-11-01T00:00:00Z"):
        fid = f"id{next(self.ids)}"
        self.items[fid] = {
            "name": name,
            "parent": parent,
            "mime": FOLDER_MIME if folder else "application/pdf",
            "content": content,
            "modified": modified,
            "trashed": False,
        }
        return fid

    def _item(self, fid):
        d = self.items[fid]
        sha = "" if d["mime"] == FOLDER_MIME else hashlib.sha256(d["content"]).hexdigest()
        return Item(
            fid, d["name"], d["mime"], d["parent"], len(d["content"]), sha, "", d["modified"]
        )

    def names(self, parent):
        return {
            d["name"] for d in self.items.values() if d["parent"] == parent and not d["trashed"]
        }

    def find(self, parent, name):
        return next(
            (
                k
                for k, d in self.items.items()
                if d["parent"] == parent and d["name"] == name and not d["trashed"]
            ),
            None,
        )

    # DriveOps interface
    def list_children(self, folder_id):
        return [
            self._item(k)
            for k, d in self.items.items()
            if d["parent"] == folder_id and not d["trashed"]
        ]

    def list_my_drive_root(self):
        return []

    def download(self, fid):
        return self.items[fid]["content"]

    def _check(self, op):
        if op in self.fail_on:
            raise WriteVerificationError(f"{op}: simulated read-back mismatch")

    def create_text(self, folder_id, name, text, mime="text/markdown"):
        self._check("create_text")
        assert name not in self.names(folder_id), f"collision {name}"
        self.writes.append(("create", name))
        fid = self.add(name, folder_id, text.encode())
        self.items[fid]["mime"] = mime
        return fid

    def update_text(self, fid, text, mime="text/markdown"):
        self._check("update_text")
        self.writes.append(("update", self.items[fid]["name"]))
        self.items[fid]["content"] = text.encode()

    def move_rename(self, fid, new_name, from_parent, to_parent):
        self._check("move_rename")
        d = self.items[fid]
        assert d["parent"] == from_parent
        assert new_name == d["name"] or new_name not in self.names(to_parent)
        self.writes.append(("move", d["name"], new_name))
        d["name"], d["parent"] = new_name, to_parent

    def trash_canary(self, fid):
        self.writes.append(("trash", self.items[fid]["name"]))
        self.items[fid]["trashed"] = True


GOOD = {
    "doc_date": "2026-08-05",
    "doc_type": "Labs",
    "institution": "NOU",
    "doctor": "MUDr. Jana Kováčová",
    "hospital": "OKB",
    "confidence": 0.95,
    "summary": "Krvný obraz.",
}


def fake_extract(name, content, mime="", *, min_text_chars=100):
    text = content.decode("utf-8", errors="replace")
    return Extraction(text=text, method="text_layer", pages=1, engine="test")


def fake_propose(contract, text):
    if "BIRTH" in text:
        return {**GOOD, "doc_date": "1970-02-03"}
    if "REPORT" in text:
        return {**GOOD, "doc_type": "Report", "doc_date": "2026-09-01"}
    return dict(GOOD)


def _drive_with_inbox():
    d = FakeDrive()
    d.add("scan_001.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026", modified="2026-11-02T00:00:00Z")
    d.add("scan_002.pdf", d.folder[INBOX], b"BIRTH 03.02.1970", modified="2026-11-03T00:00:00Z")
    d.add("scan_003.pdf", d.folder[INBOX], b"REPORT 01.09.2026", modified="2026-11-04T00:00:00Z")
    # an exact duplicate of an already-filed document
    d.add("20260101-JanaNovakova-NOU-Labs-X-Y.pdf", d.folder[LABS], b"SAME BYTES")
    d.add("scan_dup.pdf", d.folder[INBOX], b"SAME BYTES", modified="2026-11-01T00:00:00Z")
    return d


def run(d, **opts):
    o = RunOptions(now=NOW, run_id="test-run", **opts)
    return run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=fake_propose)


def test_dry_run_plans_everything_and_writes_nothing():
    d = _drive_with_inbox()
    before = json.dumps(d.items, default=str, sort_keys=True)
    r = run(d)
    assert r.status == "ok", r.message
    assert json.dumps(d.items, default=str, sort_keys=True) == before
    assert d.writes == []
    results = {doc["source"]: doc["result"] for doc in r.documents}
    assert results == {
        "scan_dup.pdf": "quarantined",
        "scan_001.pdf": "filed",
        "scan_002.pdf": "review_needed",
        "scan_003.pdf": "filed",
    }


def test_real_run_files_pairs_quarantines_and_logs():
    d = _drive_with_inbox()
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message

    labs = d.names(d.folder[LABS])
    filed = "20260805-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"
    assert filed in labs and filed + ".md" in labs  # source + sidecar together
    report = d.names(d.folder[REPORT])
    assert "20260901-JanaNovakova-NOU-Report-Kovacova-OKB.pdf" in report
    # review stays in the inbox with a sidecar that says why
    inbox = d.names(d.folder[INBOX])
    assert {"scan_002.pdf", "scan_002.pdf.md"} <= inbox
    side = d.download(d.find(d.folder[INBOX], "scan_002.pdf.md")).decode()
    assert "review_needed: true" in side and "birth date" in side
    # exact duplicate quarantined, survivor untouched
    assert "scan_dup.pdf" in d.names(d.folder[DUPS])
    assert "20260101-JanaNovakova-NOU-Labs-X-Y.pdf" in labs

    # sidecar front matter is parseable and points at the renamed source
    meta = d.download(d.find(d.folder[LABS], filed + ".md")).decode().split("---")[1]
    assert f'source_file: "{filed}"' in meta  # names the file it sits next to
    assert 'doc_type: "Labs"' in meta and 'doc_date: "2026-08-05"' in meta

    # state files
    actions = [
        json.loads(x) for x in d.download(d.find(d.root, "_actions.jsonl")).decode().splitlines()
    ]
    assert actions and all("undo" in a for a in actions)
    assert "test-run" in d.download(d.find(d.root, "_changelog.md")).decode()
    index = json.loads(d.download(d.find(d.root, "_index.json")))
    assert any(e["path"] == f"{LABS}/{filed}" for e in index)
    assert not any(e["path"] == CANARY for e in index)
    assert d.find(d.folder[LOGS], "_daily-report-20261107-oncofiles.md")
    # canary created then trashed
    assert ("create", CANARY) in d.writes and ("trash", CANARY) in d.writes
    assert CANARY not in d.names(d.root)
    # never a folder created
    assert not [w for w in d.writes if w[0] == "create" and w[1].endswith("—")]


def test_preflight_failure_aborts_before_any_document_but_logs():
    d = _drive_with_inbox()
    d.add("consultation — konzultácie", d.root, folder=True)  # not in README §2
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "folder set" in r.message
    assert not [w for w in d.writes if w[0] == "move"]
    assert "ABORTED" in d.download(d.find(d.root, "_changelog.md")).decode()
    assert CANARY not in d.names(d.root)


def test_first_write_failure_aborts_the_run():
    d = _drive_with_inbox()
    d.fail_on.add("move_rename")
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "not verified" in r.message
    # nothing after the failure was attempted
    assert [w for w in d.writes if w[0] == "move"] == []
    assert len([doc for doc in r.documents if doc["result"] == "filed"]) == 0


def test_action_budget_limits_the_run():
    d = _drive_with_inbox()
    r = run(d, dry_run=False, max_actions=4)
    assert r.actions and len(r.not_processed) >= 2
    assert len([w for w in d.writes if w[0] == "move"]) <= 4


def test_max_docs():
    r = run(_drive_with_inbox(), max_docs=1)
    assert len(r.documents) == 1 and len(r.not_processed) == 3


def test_name_collision_gets_suffix():
    d = _drive_with_inbox()
    d.add("20260805-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf", d.folder[LABS], b"other")
    run(d, dry_run=False)
    assert "20260805-JanaNovakova-NOU-Labs-Kovacova-OKB-02.pdf" in d.names(d.folder[LABS])


def test_unchanged_review_sidecar_is_not_reprocessed():
    d = FakeDrive()
    content = b"BIRTH 03.02.1970"
    sha = hashlib.sha256(content).hexdigest()
    d.add("scan_9.pdf", d.folder[INBOX], content)
    d.add(
        "scan_9.pdf.md",
        d.folder[INBOX],
        f'---\nreview_needed: true\nsha256: "{sha}"\n---\n'.encode(),
    )
    r = run(d, dry_run=False)
    assert r.awaiting_review == [f"{INBOX}/scan_9.pdf"] and r.documents == []


def test_local_model_down_aborts():
    d = _drive_with_inbox()

    def down(contract, text):
        raise llm.LocalLLMError("local LLM unreachable: ConnectError")

    r = run_curated(
        d,
        d.root,
        RunOptions(now=NOW, dry_run=False),
        extract_fn=fake_extract,
        propose_fn=down,
    )
    assert r.status == "aborted" and "local model unavailable" in r.message


def test_not_curated_folder_is_refused():
    d = FakeDrive()
    d.items[d.find(d.root, "_index.json")]["trashed"] = True
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "not a curated folder" in r.message
    assert d.writes == []


def test_loose_root_file_needing_review_moves_to_inbox_with_sidecar():
    d = FakeDrive()
    d.add("notes.pdf", d.root, b"BIRTH 03.02.1970")
    run(d, dry_run=False)
    assert {"notes.pdf", "notes.pdf.md"} <= d.names(d.folder[INBOX])


@pytest.mark.parametrize("folder", [INBOX])
def test_sidecars_are_never_candidates(folder):
    d = FakeDrive()
    d.add("a.pdf", d.folder[folder], b"VYSLEDKY 05.08.2026")
    d.add("a.pdf.md", d.folder[folder], b"---\nreview_needed: false\n---\n")
    r = run(d)
    assert [doc["source"] for doc in r.documents] == ["a.pdf"]


def test_oversized_file_goes_to_review_without_download(monkeypatch):
    from oncofiles.curated import extract as ex_mod

    monkeypatch.setattr(ex_mod, "MAX_FILE_BYTES", 10)
    d = FakeDrive()
    huge = d.add("huge.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026 " * 5)
    real = d.download
    d.download = lambda fid: pytest.fail("must not download") if fid == huge else real(fid)
    r = run(d)
    assert r.documents[0]["result"] == "review_needed"
    assert "larger than" in r.documents[0]["reasons"][0]
