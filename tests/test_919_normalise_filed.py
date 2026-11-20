"""#919 item 2: filed documents whose name fails the standard are renamed in place.

Synthetic data only (invented names, institutions and dates in 2027+).
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime

import pytest

from oncofiles.curated import drive_ops, normalise
from oncofiles.curated import runner as runner_mod
from oncofiles.curated.contract import parse_contract
from oncofiles.curated.naming import is_standard
from oncofiles.curated.runner import CANARY, RunOptions, run_curated
from tests.test_curated_runner import (
    LABS,
    README,
    REPORT,
    FakeDrive,
    JournalCheckingDrive,
    KillOnSidecarMoveDrive,
    _Killed,
    fake_extract,
    fake_propose,
)

NOW = datetime.fromisoformat("2027-06-01T06:30:00+00:00")
GDOC = "application/vnd.google-apps.document"
CONTRACT = parse_contract(README)


def sidecar(doc_date=None, institution=None, doctor=None, body="(no text extracted)", **extra):
    front = {
        "source_file": "x",
        "patient": "Jana Nováková",
        "doc_date": doc_date,
        "institution": institution,
        "doctor": doctor,
        "review_needed": False,
        **extra,
    }
    lines = "\n".join(f"{k}: {json.dumps(v, ensure_ascii=False)}" for k, v in front.items())
    return f"---\n{lines}\n---\n\n{body}\n".encode()


def run(d, **opts):
    # The env default is OFF (infra review of #946); these tests turn the phase
    # on explicitly unless they test the switch itself.
    opts.setdefault("normalise_filed", True)
    o = RunOptions(now=NOW, run_id="norm-run", **opts)
    return run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=fake_propose)


DIGIT = "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB-1234567890123456789.pdf"
LEGACY = "20270510_JanaNovakova_NOU_Labs_krvnyobraz.pdf"
GDOC_TITLE = "Sprava_kontrola_jar"


def _drive_with_919_shapes():
    """The three #919 shapes, each with its sidecar, filed in their folders."""
    d = FakeDrive()
    d.add(DIGIT, d.folder[LABS], b"PDF")
    d.add(
        DIGIT + ".md",
        d.folder[LABS],
        sidecar("2027-03-12", "NOU", "MUDr. Jana Kováčová", "VYSLEDKY 12.03.2027"),
    )
    d.add(LEGACY, d.folder[LABS], b"PDF2")
    d.add(LEGACY + ".md", d.folder[LABS], sidecar(None, "NOU", None, "Krvny obraz 10.05.2027"))
    doc = d.add(GDOC_TITLE, d.folder[REPORT], b"Kontrola dna 02.04.2027")
    d.items[doc]["mime"] = GDOC
    d.add(GDOC_TITLE + ".md", d.folder[REPORT], sidecar("2027-04-02", "SampleHospital", "Hrušková"))
    return d, doc


EXPECTED = {
    DIGIT: "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf",
    LEGACY: "20270510-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf",
    GDOC_TITLE: "20270402-JanaNovakova-SampleHospital-Report-Hruskova-Unknown",
}


# ── plan (dry run) ───────────────────────────────────────────────────


def test_dry_run_plans_each_919_shape_and_writes_nothing():
    d, doc = _drive_with_919_shapes()
    before = json.dumps(d.items, default=str, sort_keys=True)
    r = run(d)
    assert r.status == "ok", r.message
    assert json.dumps(d.items, default=str, sort_keys=True) == before
    assert d.writes == []
    plan = {p["from"]: p for p in r.normalise["planned"]}
    assert {k: v["to"] for k, v in plan.items()} == EXPECTED
    assert plan[DIGIT]["sidecar_to"] == EXPECTED[DIGIT] + ".md"
    assert plan[LEGACY]["date_source"] == "filename"  # front matter had no date
    assert plan[GDOC_TITLE]["date_source"] == "front_matter"
    # the pre-runner Google Doc sidecar holds no text: its text export was read
    assert (doc, "text/plain") in d.exports
    assert r.normalise["renamed"] == []
    assert r.normalise["counts"]["planned"] == 3
    for target in EXPECTED.values():
        mime = GDOC if "." not in target else "application/pdf"
        assert is_standard(target, mime=mime)
    assert "." not in EXPECTED[GDOC_TITLE]  # Google-native: no extension (#922 rule)


def test_real_run_renames_pairs_in_place_and_logs_it():
    d, doc = _drive_with_919_shapes()
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    labs, report = d.names(d.folder[LABS]), d.names(d.folder[REPORT])
    for old, new in EXPECTED.items():
        folder = report if old == GDOC_TITLE else labs
        assert old not in folder and old + ".md" not in folder
        assert new in folder and new + ".md" in folder  # pair stays together
    assert d.items[doc]["parent"] == d.folder[REPORT]  # same folder, never moved
    assert {x["from"] for x in r.normalise["renamed"]} == set(EXPECTED)
    acts = [a for a in r.actions if a["action"] == "normalise_rename"]
    assert len(acts) == 3 and all(a["status"] == "done" for a in acts)
    assert all(a["undo"]["op"] == "rename_pair" for a in acts)
    changelog = d.items[d.find(d.root, "_changelog.md")]["content"].decode()
    assert "filed name normalised (#919)" in changelog
    # sidecar text is not rewritten: only names change
    side = d.find(d.folder[LABS], EXPECTED[DIGIT] + ".md")
    assert b"VYSLEDKY 12.03.2027" in d.items[side]["content"]


# ── no date / month-only / no sidecar ────────────────────────────────


@pytest.mark.parametrize(
    "front_date, body",
    [
        (None, "no printed date at all"),
        ("2027-02-11", "the text prints no such day"),  # front date not printed
        ("1970-02-03", "Narodena 03.02.1970"),  # README birth date
        ("2027-09-09", "Termin 09.09.2027"),  # in the future (NOW = 2027-06-01)
    ],
)
def test_no_validated_printed_date_means_review_code_never_a_rename(front_date, body):
    d = FakeDrive()
    d.add("scan_unknown.pdf", d.folder[LABS], b"PDF")
    d.add("scan_unknown.pdf.md", d.folder[LABS], sidecar(front_date, "NOU", None, body))
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert "scan_unknown.pdf" in d.names(d.folder[LABS])
    assert not [w for w in d.writes if w[0] == "move"]
    assert r.normalise["review"] == [
        {
            "source": f"{LABS}/scan_unknown.pdf",
            "code": "name_not_standard",
            "why": "no_printed_date",
        }
    ]
    assert r.normalise["counts"]["review"] == 1


def test_month_only_note_is_left_alone_with_its_code():
    d = FakeDrive()
    note = d.add("2027-03-Advocate-Poznamky.md", d.folder[REPORT], b"Marec 2027: poznamky")
    d.items[note]["mime"] = "text/markdown"
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert d.items[note]["name"] == "2027-03-Advocate-Poznamky.md"
    assert r.normalise["skipped"] == [
        {"source": f"{REPORT}/2027-03-Advocate-Poznamky.md", "code": "month_only_date"}
    ]
    assert r.normalise["planned"] == []


def test_text_source_with_a_printed_date_is_renamed_alone():
    d = FakeDrive()
    note = d.add(
        "poznamka_kontrola.md",
        d.folder[REPORT],
        sidecar("2027-04-20", "NOU", None, "Kontrola 20.04.2027"),
    )
    d.items[note]["mime"] = "text/markdown"
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    assert d.items[note]["name"] == "20270420-JanaNovakova-NOU-Report-Unknown-Unknown.md"
    (act,) = [a for a in r.actions if a["action"] == "normalise_rename"]
    assert act["undo"]["op"] == "rename" and len(act["to"]) == 1


def test_filed_source_without_sidecar_waits_for_the_backfill():
    d = FakeDrive()
    d.add("scan_bez_sidecaru.pdf", d.folder[LABS], b"Krvny obraz 05.08.2027")
    r = run(d)
    assert r.normalise["skipped"] == [
        {"source": f"{LABS}/scan_bez_sidecaru.pdf", "code": "no_sidecar"}
    ]
    assert r.normalise["planned"] == []


def test_standard_names_and_sidecars_are_never_candidates():
    d = FakeDrive()
    ok = "20270101-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"
    d.add(ok, d.folder[LABS], b"PDF")
    d.add(ok + ".md", d.folder[LABS], sidecar("2027-01-01", "NOU", None, "01.01.2027"))
    r = run(d)
    assert r.normalise["planned"] == [] and r.normalise["review"] == []
    assert r.normalise["skipped"] == []


# ── journal (#581) ───────────────────────────────────────────────────


def test_pair_rename_is_journaled_before_and_confirmed_after():
    d, _ = _drive_with_919_shapes()
    d.__class__ = JournalCheckingDrive  # asserts each new name is in the journal first
    seen = []
    orig = d.trash_canary

    def keep(fid):
        seen.append(d.download(fid).decode())
        orig(fid)

    d.trash_canary = keep
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    entries = [json.loads(x) for x in seen[0].splitlines()[1:]]
    renames = [e for e in entries if e.get("action") == "normalise_rename"]
    oks = {e["ok"] for e in entries if "ok" in e}
    assert len(renames) == 3 and all(e["seq"] in oks for e in renames)
    assert all(len(e["to"]) == 2 for e in renames)  # source + sidecar in one entry
    assert CANARY not in d.names(d.root)


class ParentCheckingDrive(FakeDrive):
    """Records every move and applies the real ``drive_ops.check_parents``."""

    def move_rename(self, fid, new_name, from_parent, to_parent, *, allowed_parents=None):
        self.calls = getattr(self, "calls", [])
        self.calls.append((fid, new_name, from_parent, to_parent, frozenset(allowed_parents)))
        drive_ops.check_parents(fid, from_parent, to_parent, allowed_parents)
        super().move_rename(fid, new_name, from_parent, to_parent, allowed_parents=allowed_parents)


def test_rename_never_changes_the_parent():
    d, _ = _drive_with_919_shapes()
    d.__class__ = ParentCheckingDrive
    parents_before = {fid: x["parent"] for fid, x in d.items.items()}
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    renames = [c for c in d.calls if c[1] in EXPECTED.values() or c[1].endswith(".md")]
    assert len(renames) == 6  # three pairs
    for fid, _name, src, dst, allowed in renames:
        assert src == dst == parents_before[fid]  # same folder, in and out
        assert src in allowed  # the walked curated tree
    # no file that existed before the run changed folder
    assert {fid: d.items[fid]["parent"] for fid in parents_before} == parents_before


def test_a_parent_outside_the_curated_tree_is_refused(monkeypatch):
    """The phase passes ``allowed_parents``; a folder outside it is never written."""
    d = FakeDrive()
    d.add(DIGIT, d.folder[LABS], b"PDF")
    d.add(DIGIT + ".md", d.folder[LABS], sidecar("2027-03-12", "NOU", None, "12.03.2027"))
    d.__class__ = ParentCheckingDrive
    real_ids = runner_mod.Tree.folder_ids
    labs = d.folder[LABS]
    monkeypatch.setattr(
        runner_mod.Tree, "folder_ids", lambda self: real_ids(self) - {labs}
    )  # simulate the labs folder being outside the walked tree
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "normalise_rename" in r.message
    assert DIGIT in d.names(labs) and DIGIT + ".md" in d.names(labs)  # nothing renamed
    assert not [w for w in d.writes if w[0] == "move"]


def test_each_journal_entry_alone_is_enough_to_undo_the_pair():
    d, _ = _drive_with_919_shapes()
    original = {fid: (x["name"], x["parent"]) for fid, x in d.items.items()}
    seen = []
    orig = d.trash_canary

    def keep(fid):
        seen.append(d.download(fid).decode())
        orig(fid)

    d.trash_canary = keep
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    entries = [json.loads(x) for x in seen[0].splitlines()[1:]]
    renames = [e for e in entries if e.get("action") == "normalise_rename"]
    assert len(renames) == 3
    for e in renames:
        # old and new names of source AND sidecar, the file ids, the one parent
        assert len(e["from"]) == len(e["to"]) == len(e["ids"]) == 2
        assert all(d.items[i]["parent"] == e["parent"] for i in e["ids"])
        old_names = [original[i][0] for i in e["ids"]]
        assert old_names == [p.split("/")[-1] for p in e["from"]]
        assert [d.items[i]["name"] for i in e["ids"]] == [p.split("/")[-1] for p in e["to"]]
    # the persisted action log carries the same in its undo record
    log = d.items[d.find(d.root, "_actions.jsonl")]["content"].decode().splitlines()
    undo = [json.loads(x)["undo"] for x in log if '"normalise_rename"' in x]
    assert len(undo) == 3 and all(u["op"] == "rename_pair" and len(u["ids"]) == 2 for u in undo)
    # undo from the journal alone (no tree, no report): every file is back
    for e in renames:
        for fid, old in zip(e["ids"], e["from"], strict=True):
            d.move_rename(
                fid, old.rsplit("/", 1)[1], e["parent"], e["parent"], allowed_parents={e["parent"]}
            )
    for fid, (name, parent) in original.items():
        assert (d.items[fid]["name"], d.items[fid]["parent"]) == (name, parent)


def test_a_kill_between_the_two_renames_leaves_the_pair_unconfirmed():
    d = FakeDrive()
    d.add(DIGIT, d.folder[LABS], b"PDF")
    d.add(DIGIT + ".md", d.folder[LABS], sidecar("2027-03-12", "NOU", None, "12.03.2027"))
    d.__class__ = KillOnSidecarMoveDrive
    with pytest.raises(_Killed):
        run(d, dry_run=False)
    entries = [json.loads(x) for x in d.journal_at_kill.splitlines()[1:]]
    (rename,) = [e for e in entries if e.get("action") == "normalise_rename"]
    assert rename["seq"] not in {e["ok"] for e in entries if "ok" in e}


def test_failed_sidecar_rename_aborts_and_says_pair_split():
    d = FakeDrive()
    d.add(DIGIT, d.folder[LABS], b"PDF")
    d.add(DIGIT + ".md", d.folder[LABS], sidecar("2027-03-12", "NOU", None, "12.03.2027"))
    orig = d.move_rename

    def fail_on_sidecar(fid, new_name, *a, **kw):
        if new_name.endswith(".md"):
            raise RuntimeError("drive down")
        return orig(fid, new_name, *a, **kw)

    d.move_rename = fail_on_sidecar
    r = run(d, dry_run=False)
    assert r.status == "aborted" and "pair split" in r.message


# ── cap / budget / off-switch ────────────────────────────────────────


def _drive_with_n(n):
    d = FakeDrive()
    for i in range(1, n + 1):
        name = f"legacy_scan_{i}.pdf"
        d.add(name, d.folder[LABS], f"PDF{i}".encode())
        d.add(name + ".md", d.folder[LABS], sidecar(f"2027-02-{i:02d}", "NOU", None, f"{i}.2.2027"))
    return d


def test_cap_limits_renames_per_run_and_defers_the_rest():
    d = _drive_with_n(3)
    r = run(d, dry_run=False, normalise_max=2)
    assert r.status == "ok", r.message
    assert len(r.normalise["renamed"]) == 2
    assert r.normalise["deferred"] == [{"source": f"{LABS}/legacy_scan_3.pdf"}]
    assert "legacy_scan_3.pdf" in d.names(d.folder[LABS])
    # the next run does the rest
    later = run(d, dry_run=False, normalise_max=2)
    assert later.normalise["deferred"] == []
    assert len(later.normalise["renamed"]) == 1


def test_dry_run_shows_deferred_targets_beyond_the_cap():
    d = _drive_with_n(3)
    r = run(d, normalise_max=1)
    assert len(r.normalise["planned"]) == 1
    assert [x["to"] for x in r.normalise["deferred"]] == [
        "20270202-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf",
        "20270203-JanaNovakova-NOU-Labs-Unknown-Unknown.pdf",
    ]
    assert d.writes == []


def test_env_cap_and_off_switch(monkeypatch):
    monkeypatch.setenv("CURATED_NORMALISE_MAX", "1")
    monkeypatch.setenv("CURATED_NORMALISE_FILED", "false")
    o = RunOptions()
    assert o.normalise_max == 1 and o.normalise_filed is False
    monkeypatch.setenv("CURATED_NORMALISE_MAX", "nonsense")
    monkeypatch.setenv("CURATED_NORMALISE_FILED", "true")
    o = RunOptions()
    assert o.normalise_max == normalise.DEFAULT_MAX and o.normalise_filed is True


@pytest.mark.parametrize("value", [None, "", "maybe", "0", "off", "False"])
def test_renaming_is_off_unless_the_env_says_true(monkeypatch, value):
    """Infra review of #946: a missing or unclear env never means renaming is on."""
    if value is None:
        monkeypatch.delenv("CURATED_NORMALISE_FILED", raising=False)
    else:
        monkeypatch.setenv("CURATED_NORMALISE_FILED", value)
    assert RunOptions().normalise_filed is False


def test_default_options_rename_nothing_but_preview_the_full_plan(monkeypatch):
    monkeypatch.delenv("CURATED_NORMALISE_FILED", raising=False)
    d, _ = _drive_with_919_shapes()

    def default_run(dry_run):  # RunOptions straight from the env, no override
        o = RunOptions(now=NOW, run_id="norm-run", dry_run=dry_run)
        return run_curated(d, d.root, o, extract_fn=fake_extract, propose_fn=fake_propose)

    real = default_run(False)
    assert real.status == "ok", real.message
    assert not [a for a in real.actions if a["action"] == "normalise_rename"]
    assert not [w for w in d.writes if w[0] == "move"]
    assert set(EXPECTED) <= d.names(d.folder[LABS]) | d.names(d.folder[REPORT])
    assert real.normalise["enabled"] is False
    preview = default_run(True)
    assert preview.normalise["enabled"] is False
    assert {p["from"]: p["to"] for p in preview.normalise["planned"]} == EXPECTED


def test_off_switch_stops_real_renames_but_the_preview_still_plans():
    d = _drive_with_n(1)
    r = run(d, dry_run=False, normalise_filed=False)
    assert r.status == "ok", r.message
    assert "legacy_scan_1.pdf" in d.names(d.folder[LABS])
    assert r.normalise["enabled"] is False and r.normalise["planned"] == []
    preview = run(d, normalise_filed=False)
    assert [x["from"] for x in preview.normalise["planned"]] == ["legacy_scan_1.pdf"]


def test_action_budget_defers_renames():
    d = _drive_with_n(2)
    r = run(d, dry_run=False, max_actions=2)
    assert len(r.normalise["renamed"]) == 1 and len(r.normalise["deferred"]) == 1


def test_collision_gets_a_suffix_and_the_pair_name_follows():
    d = FakeDrive()
    taken = "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB.pdf"
    d.add(taken, d.folder[LABS], b"OTHER")
    d.add(taken + ".md", d.folder[LABS], sidecar("2027-03-12", "NOU", None, "12.03.2027"))
    d.add(DIGIT, d.folder[LABS], b"PDF")
    d.add(DIGIT + ".md", d.folder[LABS], sidecar("2027-03-12", "NOU", None, "12.03.2027"))
    r = run(d, dry_run=False)
    assert r.status == "ok", r.message
    want = "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB-02.pdf"
    assert want in d.names(d.folder[LABS]) and want + ".md" in d.names(d.folder[LABS])


def test_server_logs_carry_name_shapes_only(caplog):
    d, _ = _drive_with_919_shapes()
    with caplog.at_level(logging.INFO, logger="oncofiles.curated.runner"):
        run(d, dry_run=False)
    text = caplog.text
    assert "filed-name normalisation" in text and "planned=3" in text
    for name in [*EXPECTED, *EXPECTED.values(), "Kovacova", "NOU", "JanaNovakova"]:
        assert name not in text


# ── pure planner ─────────────────────────────────────────────────────


def _plan(name, front, text, mime="application/pdf", taken=frozenset(), doc_type="Labs"):
    return normalise.plan_name(
        name=name,
        mime=mime,
        doc_type=doc_type,
        front=front,
        text=text,
        contract=CONTRACT,
        today=date(2027, 6, 1),
        taken=set(taken),
    )


def test_planner_uses_readme_tokens_only_and_never_invents_one():
    p = _plan(
        "scan.pdf",
        {"doc_date": "2027-03-01", "institution": "Some New Clinic", "doctor": "MUDr. X Novy"},
        "01.03.2027",
    )
    assert p.target == "20270301-JanaNovakova-Unknown-Labs-Unknown-Unknown.pdf"
    p = _plan("scan.pdf", {"doc_date": "2027-03-01", "institution": "nou"}, "01.03.2027")
    assert p.target.startswith("20270301-JanaNovakova-NOU-Labs-")


def test_planner_keeps_a_valid_subject_and_drops_id_runs():
    name = "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB-2Cyklus.pdf"
    p = _plan(name, {"doc_date": "2027-03-12"}, "12.03.2027")
    assert p.target == "20270312-JanaNovakova-NOU-Labs-Kovacova-OKB-Cyklus.pdf"


def test_planner_never_reuses_fields_of_another_patients_name():
    name = "20270312-InaOsoba-NOU-Labs-Kovacova-OKB-1234567890.pdf"
    p = _plan(name, {"doc_date": "2027-03-12"}, "12.03.2027")
    assert p.target == "20270312-JanaNovakova-Unknown-Labs-Unknown-Unknown.pdf"


@pytest.mark.parametrize(
    "name, month_only",
    [
        ("2027-03-W-W.md", True),
        ("2027-03.md", True),
        ("2027-03-15_kontrola.pdf", False),
        ("20270315-x.pdf", False),
        ("2027-031.pdf", False),
    ],
)
def test_month_only_shape(name, month_only):
    assert normalise.is_month_only(name) is month_only


def test_name_shape_hides_letters_and_digits():
    assert normalise.name_shape("20270312-JanaNovakova-Lab.pdf") == "99999999-A-A.A"
    assert normalise.name_shape("Správa_kontrola") == "A_A"
