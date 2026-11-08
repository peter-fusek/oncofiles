"""End-to-end tests for the curated-folder runner (#562) on an in-memory Drive.

Synthetic data only (public repo). Extraction and classification are injected.
"""

import hashlib
import itertools
import json
from datetime import UTC, datetime, timedelta
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
        # already-filed survivor had no sidecar → backfilled (Cowork rule)
        f"{LABS}/20260101-JanaNovakova-NOU-Labs-X-Y.pdf": "sidecar_added",
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
    new_docs = [d for d in r.documents if d["result"] != "sidecar_added"]
    assert len(new_docs) == 1 and len(r.not_processed) == 3


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


# ── review findings on PR #568 ──────────────────────────────────────


def test_unexpected_crash_is_recorded_as_abort_not_success():
    """Review #2: any exception → status aborted, changelog says ABORTED, report kept."""
    d = _drive_with_inbox()

    def boom(contract, text):
        raise ValueError("unexpected")

    r = run_curated(
        d, d.root, RunOptions(now=NOW, dry_run=False), extract_fn=fake_extract, propose_fn=boom
    )
    assert r.status == "aborted" and "unexpected error" in r.message
    assert "ABORTED" in d.download(d.find(d.root, "_changelog.md")).decode()


def test_failed_move_is_logged_and_says_pair_may_be_split():
    """Review #5: the action is on record (status failed) and the split is reported."""
    d = FakeDrive()
    d.add("scan_001.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    d.fail_on.add("move_rename")
    r = run(d, dry_run=False)
    assert "pair possibly split" in r.message
    failed = [a for a in r.actions if a.get("status") == "failed"]
    assert len(failed) == 1 and "undo" in failed[0]


def test_loose_root_file_never_creates_same_name_sibling_in_inbox():
    """Review #4: root → inbox move is skipped when the name is taken."""
    d = FakeDrive()
    d.add("scan.pdf", d.folder[INBOX], b"BIRTH 03.02.1970")
    d.add("scan.pdf.md", d.folder[INBOX], b"---\nreview_needed: true\n---\n")
    d.add("scan.pdf", d.root, b"BIRTH 03.02.1970 other scan")
    r = run(d, dry_run=False)
    names = [x["name"] for x in d.items.values() if x["parent"] == d.folder[INBOX]]
    assert names.count("scan.pdf") == 1
    assert any("name is taken" in f for f in r.flags)


def test_sidecar_of_extensionless_source_is_not_a_candidate():
    """Review #6: a Google Doc 'Notes' and its 'Notes.md' sidecar — only the doc is a candidate."""
    d = FakeDrive()
    d.add("Notes", d.folder[INBOX], b"")
    d.add("Notes.md", d.folder[INBOX], b"---\nreview_needed: false\n---\n")
    r = run(d)
    assert [doc["source"] for doc in r.documents] == ["Notes"]


# ── parity with the Cowork erika-daily-cleanup instructions ─────────


def test_old_date_goes_to_review_when_age_rule_is_on():
    d = FakeDrive()
    d.add("old.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    o = RunOptions(now=datetime(2029, 1, 1, tzinfo=UTC), max_doc_age_days=730)
    r = run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=fake_propose)
    doc = r.documents[0]
    assert doc["result"] == "review_needed" and "years old" in doc["reasons"][0]


def test_age_rule_off_by_default_for_general_patients():
    d = FakeDrive()
    d.add("old.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    o = RunOptions(now=datetime(2029, 1, 1, tzinfo=UTC))
    r = run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=fake_propose)
    assert r.documents[0]["result"] == "filed"


def test_text_source_moves_without_a_sidecar():
    d = FakeDrive()
    d.add("notes.md", d.folder[INBOX], b"REPORT 01.09.2026")
    r = run(d, dry_run=False)
    assert r.documents[0]["result"] == "filed"
    report = d.names(d.folder[REPORT])
    assert "20260901-JanaNovakova-NOU-Report-Kovacova-OKB.md" in report
    assert not any(n.endswith(".md.md") for x in d.items.values() for n in [x["name"]])


def test_backfill_adds_sidecar_only_and_proposes_reclassification():
    d = FakeDrive()
    filed = d.add("20260901-JanaNovakova-NOU-Labs-X-Y.pdf", d.folder[LABS], b"REPORT 01.09.2026")
    r = run(d, dry_run=False)
    assert d.items[filed]["parent"] == d.folder[LABS]  # never moved
    assert d.items[filed]["name"] == "20260901-JanaNovakova-NOU-Labs-X-Y.pdf"  # never renamed
    side = d.download(d.find(d.folder[LABS], "20260901-JanaNovakova-NOU-Labs-X-Y.pdf.md")).decode()
    assert 'proposed_reclassification: "Report"' in side
    assert r.documents[0]["proposal"] == "Report"


def test_backfill_respects_its_cap():
    d = FakeDrive()
    for i in range(3):
        d.add(f"2026010{i}-JanaNovakova-NOU-Labs-X-Y.pdf", d.folder[LABS], b"VYSLEDKY 05.08.2026")
    r = run(d, max_missing_sidecars=2)
    assert len([x for x in r.documents if x["result"] == "sidecar_added"]) == 2
    assert any("missing sidecar" in n for n in r.not_processed)


def test_recheck_reports_drive_changes_during_run():
    d = _drive_with_inbox()
    orig = d.move_rename

    def move_and_fork(fid, new_name, from_parent, to_parent):
        orig(fid, new_name, from_parent, to_parent)
        if not d.find(d.root, "labs — laboratórne výsledky (1)"):
            d.add("labs — laboratórne výsledky (1)", d.root, folder=True)  # Drive forked a folder

    d.move_rename = move_and_fork
    r = run(d, dry_run=False)
    assert r.status == "aborted" and r.flags[0].startswith("DRIVE CHANGED DURING RUN")


# ── first live dry run (2026-11-07): mount vs API naming, nested subfolders ──


def test_gdoc_sidecar_from_the_mount_pairs_with_the_api_google_doc():
    """Cowork named a Google Doc's sidecar X.gdoc.md (mount name); the API name is X."""
    d = FakeDrive()
    doc = d.add("20260203_JanaNovakova_NOU_Reference_Notes", d.folder[LABS])
    d.items[doc]["mime"] = "application/vnd.google-apps.document"
    pdfdoc = d.add("20260220_JanaNovakova_Summary_Part2of2.pdf", d.folder[LABS])
    d.items[pdfdoc]["mime"] = "application/vnd.google-apps.document"  # title ends .pdf
    d.add("20260203_JanaNovakova_NOU_Reference_Notes.gdoc.md", d.folder[LABS], b"---\n---\n")
    d.add("20260220_JanaNovakova_Summary_Part2of2.pdf.gdoc.md", d.folder[LABS], b"---\n---\n")
    r = run(d)
    assert r.status == "ok", r.message
    # paired → not treated as missing a sidecar either
    assert not [x for x in r.documents if x["result"] == "sidecar_added"]


def test_real_orphan_sidecar_still_aborts():
    d = FakeDrive()
    d.add("gone.pdf.md", d.folder[LABS], b"---\n---\n")
    r = run(d)
    assert r.status == "aborted" and "every sidecar has its source" in r.message


def test_nested_subfolder_is_flagged_not_fatal_and_left_alone():
    d = FakeDrive()
    sub = d.add("Textbook-Chapter-Scans", d.folder[LABS], folder=True)
    inside = d.add("page1.pdf", sub, b"VYSLEDKY 05.08.2026")
    d.add("scan_001.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert any("flat" in f and "Textbook-Chapter-Scans" in f for f in r.flags)
    assert d.items[inside]["parent"] == sub and d.items[inside]["name"] == "page1.pdf"


def test_truncated_classification_is_per_document_not_a_run_abort():
    d = FakeDrive()
    d.add("scan_001.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    d.add("scan_002.pdf", d.folder[INBOX], b"REPORT 01.09.2026", modified="2026-11-05T00:00:00Z")

    def flaky(contract, text):
        if "VYSLEDKY" in text:
            raise llm.LocalLLMError("local LLM answer truncated: reasoning used the whole budget")
        return fake_propose(contract, text)

    r = run_curated(d, d.root, RunOptions(now=NOW), extract_fn=fake_extract, propose_fn=flaky)
    assert r.status == "ok", r.message
    res = {x["source"]: x for x in r.documents}
    assert res["scan_001.pdf"]["result"] == "review_needed"
    assert "truncated" in res["scan_001.pdf"]["reasons"][-1]
    assert res["scan_002.pdf"]["result"] == "filed"


# ── #581: canary journal + interrupted runs ─────────────────────────


class JournalCheckingDrive(FakeDrive):
    """Asserts every move is journaled in the canary BEFORE it happens."""

    def move_rename(self, fid, new_name, from_parent, to_parent):
        canary = self.find(self.root, CANARY)
        journal = self.download(canary).decode()
        assert new_name in journal, f"move to {new_name} not journaled first"
        super().move_rename(fid, new_name, from_parent, to_parent)


def _journaled_drive():
    d = _drive_with_inbox()
    d.__class__ = JournalCheckingDrive  # same state (ids, counters), checked moves
    return d


def test_every_action_is_journaled_in_the_canary_before_it_runs():
    d = _journaled_drive()
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert CANARY not in d.names(d.root)  # clean run: canary trashed at the end


def _old(minutes):
    return (NOW - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_stale_canary_without_actions_is_trashed_and_the_run_continues():
    d = _drive_with_inbox()
    d.add(CANARY, d.root, b"dead-run journal-v1\n", modified=_old(30))
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert any(g["name"] == "stale canary" for g in r.to_dict()["gates"])
    assert any("dead-run" in f for f in r.flags)
    assert ("trash", CANARY) in d.writes


def test_stale_canary_with_journaled_actions_stops_the_run():
    d = _drive_with_inbox()
    line = json.dumps({"action": "move_pair", "to": ["labs/x.pdf", "labs/x.pdf.md"]})
    d.add(CANARY, d.root, f"dead-run journal-v1\n{line}\n".encode(), modified=_old(30))
    r = run(d, dry_run=False)
    assert r.status == "aborted"
    assert "dead-run" in r.message and "1 unconfirmed" in r.message and "labs/x.pdf" in r.message
    assert not [w for w in d.writes if w[0] in ("move", "create")]


def test_fresh_canary_is_never_touched():
    d = _drive_with_inbox()
    d.add(CANARY, d.root, b"live-run journal-v1\n", modified=_old(1))
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "may still be active" in r.message
    assert d.writes == []


def test_legacy_canary_without_journal_stops_the_run():
    d = _drive_with_inbox()
    d.add(CANARY, d.root, b"oncofiles-20261108T1159\n", modified=_old(60))
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "no journal" in r.message
    assert d.writes == []


def test_dry_run_ignores_a_leftover_canary():
    d = _drive_with_inbox()
    d.add(CANARY, d.root, b"dead-run journal-v1\n", modified=_old(30))
    r = run(d)
    assert r.status == "ok", r.message
    assert d.writes == []


# ── technical failures are retried, human reviews are not ───────────


def _review_sidecar(reasons, attempts=None, content=b"VYSLEDKY 05.08.2026"):
    sha = hashlib.sha256(content).hexdigest()
    extra = f"technical_attempts: {attempts}\n" if attempts is not None else ""
    return (
        f'---\nreview_needed: true\nsha256: "{sha}"\nreview_reasons: {json.dumps(reasons)}\n'
        f"{extra}---\n"
    ).encode()


TRUNC = "classification failed: local LLM answer truncated: reasoning used the whole budget"


def test_technical_review_is_retried_and_files_when_it_works():
    d = FakeDrive()
    d.add("scan_7.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    d.add("scan_7.pdf.md", d.folder[INBOX], _review_sidecar([TRUNC]))
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert r.awaiting_review == []
    assert "20260805-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf" in d.names(d.folder[LABS])


def test_technical_failure_again_counts_attempts():
    d = FakeDrive()
    d.add("scan_7.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    d.add("scan_7.pdf.md", d.folder[INBOX], _review_sidecar([TRUNC]))

    def failing(contract, text):
        raise llm.LocalLLMError("local LLM answer truncated: reasoning used the whole budget")

    o = RunOptions(now=NOW, run_id="test-run", dry_run=False)
    r = run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=failing)
    assert r.status == "ok", r.message
    side = d.download(d.find(d.folder[INBOX], "scan_7.pdf.md")).decode()
    assert "technical_attempts: 2" in side and "review_needed: true" in side


def test_technical_review_stops_retrying_after_max_attempts():
    d = FakeDrive()
    d.add("scan_7.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    d.add("scan_7.pdf.md", d.folder[INBOX], _review_sidecar([TRUNC], attempts=3))
    r = run(d, dry_run=False)
    assert r.awaiting_review == [f"{INBOX}/scan_7.pdf"] and r.documents == []


def test_human_review_reason_is_never_retried():
    d = FakeDrive()
    d.add("scan_7.pdf", d.folder[INBOX], b"VYSLEDKY 05.08.2026")
    d.add("scan_7.pdf.md", d.folder[INBOX], _review_sidecar([TRUNC, "institution unknown"]))
    r = run(d, dry_run=False)
    assert r.awaiting_review == [f"{INBOX}/scan_7.pdf"] and r.documents == []


# ── journal confirmations, drain, probe (continuous mode) ────────────


def test_stale_canary_with_all_actions_confirmed_is_recovered():
    d = _drive_with_inbox()
    line = json.dumps({"action": "move_pair", "to": ["labs/x.pdf", "labs/x.pdf.md"], "seq": 1})
    body = f"dead-run journal-v1\n{line}\n{json.dumps({'ok': 1})}\n"
    d.add(CANARY, d.root, body.encode(), modified=_old(30))
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    recovered = [a for a in r.actions if a.get("recovered")]
    assert len(recovered) == 1 and recovered[0]["status"] == "done"
    assert any("completed all 1" in f for f in r.flags)
    assert ("trash", CANARY) in d.writes


def test_unconfirmed_action_among_confirmed_ones_still_stops_the_run():
    d = _drive_with_inbox()
    a1 = json.dumps({"action": "write_sidecar", "path": "a.md", "seq": 1})
    a2 = json.dumps({"action": "move_pair", "to": ["labs/y.pdf"], "seq": 2})
    body = f"dead-run journal-v1\n{a1}\n{json.dumps({'ok': 1})}\n{a2}\n"
    d.add(CANARY, d.root, body.encode(), modified=_old(30))
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "1 unconfirmed" in r.message and "labs/y.pdf" in r.message


class _Killed(BaseException):
    """Simulates the process being killed mid-run."""


class KillOnSidecarMoveDrive(FakeDrive):
    """Dies at the first sidecar move; keeps the journal as the kill left it
    (a real kill never reaches the run's ``finally``)."""

    def move_rename(self, fid, new_name, from_parent, to_parent):
        if new_name.endswith(".md"):
            self.journal_at_kill = self.download(self.find(self.root, CANARY)).decode()
            raise _Killed
        super().move_rename(fid, new_name, from_parent, to_parent)


def test_pair_is_confirmed_only_after_both_moves():
    d = _drive_with_inbox()
    d.__class__ = KillOnSidecarMoveDrive
    with pytest.raises(_Killed):
        run(d, dry_run=False)
    journal = d.journal_at_kill.splitlines()
    entries = [json.loads(x) for x in journal[1:]]
    moves = [e for e in entries if e.get("action") == "move_pair"]
    oks = {e["ok"] for e in entries if "ok" in e}
    assert moves and moves[-1]["seq"] not in oks  # half-moved pair stays unconfirmed
    assert all(e["seq"] in oks for e in entries if "action" in e and e is not moves[-1])


def test_every_completed_action_is_confirmed_in_the_journal():
    d = _drive_with_inbox()
    seen = []
    orig = d.trash_canary

    def keep(fid):
        seen.append(d.download(fid).decode())
        orig(fid)

    d.trash_canary = keep
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    entries = [json.loads(x) for x in seen[0].splitlines()[1:]]
    seqs = [e["seq"] for e in entries if "action" in e]
    assert seqs and sorted(seqs) == sorted(e["ok"] for e in entries if "ok" in e)


def test_should_stop_ends_the_run_cleanly_before_the_next_document():
    d = _drive_with_inbox()
    r = run(d, dry_run=False, should_stop=lambda: True)
    assert r.status == "ok", r.message
    assert not r.documents and r.not_processed
    assert any("shutting down" in f for f in r.flags)
    assert CANARY not in d.names(d.root)


def test_probe_finds_new_inbox_work_without_writing():
    from oncofiles.curated.runner import probe

    d = _drive_with_inbox()
    p = probe(d, d.root, now=NOW)
    assert len(p.pending) == 5 and not p.canary  # 4 inbox + 1 filed without sidecar
    assert d.writes == []
    assert p.newest_age_s is not None and p.newest_age_s > 0


def test_probe_skips_sidecar_reads_when_tree_is_unchanged():
    from oncofiles.curated.runner import probe

    d = _drive_with_inbox()
    first = probe(d, d.root, now=NOW)
    again = probe(d, d.root, idle_signature=first.signature, now=NOW)
    assert again.signature == first.signature and again.pending == {}
    d.add("scan_new.pdf", d.folder[INBOX], b"NEW 01.11.2026", modified=_old(1))
    changed = probe(d, d.root, idle_signature=first.signature, now=NOW)
    assert changed.signature != first.signature and len(changed.pending) == 6
    assert changed.newest_age_s == pytest.approx(60)


def test_probe_ignores_items_awaiting_review():
    from oncofiles.curated.runner import probe

    d = _drive_with_inbox()
    run(d, dry_run=False)  # files what it can, the rest waits for review
    p = probe(d, d.root, now=NOW)
    assert p.pending == {}
