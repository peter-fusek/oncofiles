"""Fair scheduling on the shared local model (#855).

Synthetic patients and documents only (public repo): invented ids, names and
2027+ dates. The probe and the run sequence are faked where the scheduler is
under test; the cap/budget tests drive the real runner on an in-memory Drive.
"""

import inspect
import json
from datetime import UTC, datetime, time, timedelta
from unittest.mock import AsyncMock

import pytest

from oncofiles.curated.extract import Extraction
from oncofiles.curated.runner import HOLD_DOCUMENT_CAP, Probe, RunOptions, run_curated
from oncofiles.tools import curated as tool
from tests.test_curated_runner import INBOX, LABS, FakeDrive, fake_propose

BIG = "00000000-0000-4000-8000-00000000b001"  # invented patients
SMALL = "00000000-0000-4000-8000-00000000b002"
LATE = "00000000-0000-4000-8000-00000000b003"
NOW = datetime(
    2027,
    6,
    15,
    tzinfo=UTC,
)


JAN = datetime(
    2027,
    1,
    1,
    tzinfo=UTC,
)


def _at(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(day: int) -> str:
    """An invented enrolment / upload time in January 2027 (day 1 = JAN)."""
    return _at(JAN + timedelta(days=day - 1))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("CURATED_DAILY_PAGE_BUDGET", "CURATED_ONBOARDING_MIN_PENDING"):
        monkeypatch.delenv(k, raising=False)

    def reset():
        tool._watch.clear()
        tool._probe_stats.clear()
        tool._reocr_jobs.clear()
        tool._runs.clear()
        tool._import_done.clear()
        tool._onboarding.clear()
        tool._onboarding_active = None
        tool._round_no = 0
        tool._inflight_pages.clear()

    reset()
    yield
    reset()


def _pending(n: int, prefix: str = "f") -> dict[str, str]:
    return {f"{prefix}{i}": f"inbox/scan_{i:03d}.pdf" for i in range(n)}


@pytest.fixture
def sched(monkeypatch):
    """Fake probes (per patient) and a fake one-turn sequence that records calls."""
    pending: dict[str, dict] = {}
    turns: list[tuple[str, dict]] = []
    outcome: dict[str, dict] = {}

    async def fake_probe(db, pid, idle):
        return Probe(signature=f"sig-{pid}-{len(pending[pid])}", pending=pending[pid],
                     newest_age_s=600.0)  # fmt: skip

    async def fake_seq(db, pid, **kw):
        turns.append((pid, kw))
        rep = {"dry_run": False, "status": "ok", "documents": [{"result": "filed"}],
               "actions": [{"action": "move"}], "held": [], "inbox_drained": False}  # fmt: skip
        rep.update(outcome.get(pid, {}))
        return [rep]

    monkeypatch.setattr(tool, "probe_patient", fake_probe)
    monkeypatch.setattr(tool, "nightly_for_patient", fake_seq)
    return pending, turns, outcome


async def _round(db, pids):
    return await tool.continuous_round(db, pids, settle_s=120, chunk=5)


# ── fair turns ───────────────────────────────────────────────────────


async def test_round_robin_big_and_small_each_get_one_bounded_turn(db, sched):
    """A 60-file first import never holds a patient with one new file: every
    round gives each a single run of CHUNK documents, the steady patient first."""
    pending, turns, _ = sched
    pending[BIG], pending[SMALL] = _pending(60), _pending(1, "s")
    for _ in range(3):
        assert await _round(db, [BIG, SMALL]) is True
    assert [pid for pid, _ in turns] == [SMALL, BIG] * 3
    assert all(kw["max_real_runs"] == 1 and kw["max_docs"] == 5 for _, kw in turns)
    assert tool._probe_stats[BIG].onboarding == "active"
    assert tool._probe_stats[BIG].onboarding_queue_position == 0
    assert tool._probe_stats[SMALL].onboarding is None


async def test_steady_patients_rotate_their_start(db, sched):
    pending, turns, _ = sched
    a, b = SMALL, LATE
    pending[a], pending[b] = _pending(1, "a"), _pending(2, "b")
    await _round(db, [a, b])
    await _round(db, [a, b])
    assert [pid for pid, _ in turns] == [a, b, b, a]


async def test_a_first_import_below_the_threshold_is_an_ordinary_patient(db, sched, monkeypatch):
    pending, turns, _ = sched
    monkeypatch.setenv("CURATED_ONBOARDING_MIN_PENDING", "10")
    pending[BIG] = _pending(9)
    pending[LATE] = _pending(9, "x")
    await _round(db, [BIG, LATE])
    assert {pid for pid, _ in turns} == {BIG, LATE}
    assert tool.onboarding_queue() == [] and tool._onboarding_active is None


async def test_loop_starts_the_next_round_right_away_while_work_flows(db, sched, monkeypatch):
    """The loop sleeps ROUND_PAUSE_S (not a whole poll) after a round that moved work."""
    pending, turns, _ = sched
    pending[SMALL] = _pending(1)
    monkeypatch.setenv("CURATED_NIGHTLY_ENABLED", "true")
    from zoneinfo import ZoneInfo

    not_now = (datetime.now(ZoneInfo("Europe/Bratislava")).hour + 12) % 24
    monkeypatch.setenv("CURATED_NIGHTLY_HOUR", str(not_now))
    monkeypatch.setattr(db, "list_curated_nightly_patients", AsyncMock(return_value=[SMALL]))
    sleeps: list[float] = []
    real_sleep = tool.asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            tool._stopping.set()
        await real_sleep(0)

    monkeypatch.setattr(tool.asyncio, "sleep", fake_sleep)
    try:
        await tool.curated_nightly_loop(db, startup_grace_s=0, probe_delay_s=0)
    finally:
        tool._stopping.clear()
    assert sleeps[1] == tool.ROUND_PAUSE_S  # after a round that moved a document
    assert len(turns) >= 1


# ── one onboarding at a time ─────────────────────────────────────────


async def _enrol(db, pid: str, at: str) -> None:
    await db.set_curated_nightly(patient_id=pid, enabled=True, updated_by="test")
    await db.db.execute(
        "UPDATE curated_settings SET updated_at = ? WHERE patient_id = ?", (at, pid)
    )
    await db.db.commit()


async def test_second_onboarding_waits_in_fifo_until_the_first_finishes(db, sched):
    pending, turns, outcome = sched
    await _enrol(db, LATE, _ts(2))
    await _enrol(db, BIG, _ts(1))  # enrolled first
    pending[BIG] = _pending(40)
    pending[LATE] = _pending(30, "y")
    pending[SMALL] = _pending(1, "s")

    await _round(db, [LATE, BIG, SMALL])
    assert [pid for pid, _ in turns] == [SMALL, BIG]
    assert tool._probe_stats[LATE].runs_blocked == {"onboarding_queue": 1}
    assert tool._probe_stats[LATE].onboarding == "queued"
    assert tool._probe_stats[LATE].onboarding_queue_position == 1
    detail = tool.scheduling_detail()
    assert detail["onboarding_active"] == BIG and detail["onboarding_queue"] == [LATE]
    assert tool.heartbeat_detail()["scheduling"]["onboarding_queue"] == [LATE]

    # BIG drains its inbox: the slot passes to LATE in the next round
    outcome[BIG] = {"inbox_drained": True}
    pending[SMALL] = {}
    await _round(db, [LATE, BIG, SMALL])
    assert tool._probe_stats[BIG].onboarding == "done"
    turns.clear()
    await _round(db, [LATE, BIG, SMALL])
    assert [pid for pid, _ in turns] == [BIG, LATE]  # BIG is now an ordinary patient
    assert tool._onboarding_active == LATE and tool.onboarding_queue() == []


async def test_a_patient_that_already_started_keeps_priority_after_restart(db, sched):
    pending, turns, _ = sched
    await _enrol(db, BIG, _ts(1))
    await _enrol(db, LATE, _ts(5))
    rid = await db.insert_curated_run(patient_id=LATE, dry_run=False, trigger="auto")
    await db.finish_curated_run(
        rid, patient_id=LATE, status="ok", message="", report={"not_processed": ["x"]}
    )
    pending[BIG], pending[LATE] = _pending(40), _pending(40, "y")
    await _round(db, [BIG, LATE])
    assert [pid for pid, _ in turns] == [LATE]
    assert tool.onboarding_queue() == [BIG]


async def test_an_old_drained_run_means_the_import_is_done(db, sched):
    """Rows written before #855 have no inbox_drained: an empty not_processed counts."""
    pending, turns, _ = sched
    rid = await db.insert_curated_run(patient_id=BIG, dry_run=False, trigger="auto")
    await db.finish_curated_run(
        rid, patient_id=BIG, status="ok", message="", report={"not_processed": []}
    )
    assert (await db.curated_import_state(patient_id=BIG))["done"] is True
    pending[BIG], pending[LATE] = _pending(40), _pending(40, "y")
    await _round(db, [BIG, LATE])
    assert {pid for pid, _ in turns} == {BIG, LATE}
    assert tool._probe_stats[BIG].onboarding == "done"


async def test_an_idle_onboarding_patient_releases_the_slot(db, sched):
    pending, turns, _ = sched
    await _enrol(db, BIG, _ts(1))
    await _enrol(db, LATE, _ts(2))
    pending[BIG], pending[LATE] = _pending(40), _pending(40, "y")
    await _round(db, [BIG, LATE])
    pending[BIG] = {}  # everything moved to review: nothing runnable
    turns.clear()
    await _round(db, [BIG, LATE])
    assert [pid for pid, _ in turns] == [LATE]


# ── document cap before OCR ──────────────────────────────────────────


def _drive(n_inbox: int, n_filed: int = 0) -> FakeDrive:
    d = FakeDrive()
    for i in range(n_filed):
        d.add(f"20270105_Synth_LAB_Labs_Filed{i}.pdf", d.folder[LABS], f"FILED {i}".encode())
        d.add(f"20270105_Synth_LAB_Labs_Filed{i}.pdf.md", d.folder[LABS], b"---\n---\n")
    for i in range(n_inbox):
        d.add(f"scan_{i:03d}.pdf", d.folder[INBOX], f"VYSLEDKY {i} 05.01.2027".encode(),
              modified=_ts(1 + i % 8))  # fmt: skip
    return d


def _counting_extract(calls):
    def extract(name, content, mime="", *, min_text_chars=100):
        calls.append(name)
        return Extraction(text=content.decode(), method="vision", pages=2, engine="test")

    return extract


def test_cap_holds_documents_before_any_model_call_and_leaves_them_in_place():
    d = _drive(n_inbox=3, n_filed=2)
    calls: list[str] = []
    opts = RunOptions(dry_run=False, now=NOW, run_id="t", doc_cap=2, docs_existing=1)
    r = run_curated(d, d.root, opts, extract_fn=_counting_extract(calls), propose_fn=fake_propose)
    assert r.status == "ok", r.message
    assert calls == []  # never read by the model
    assert sorted(h["source"] for h in r.held) == [f"{INBOX}/scan_00{i}.pdf" for i in range(3)]
    assert {h["reason"] for h in r.held} == {HOLD_DOCUMENT_CAP}
    assert {f"scan_00{i}.pdf" for i in range(3)} <= d.names(d.folder[INBOX])  # nothing moved
    assert not [w for w in d.writes if w[0] == "move" and w[1].startswith("scan_")]
    assert r.not_processed == [] and r.inbox_drained is True  # cap-held: import is "done"
    assert (
        "left in place unread (document_cap): 3"
        in d.download(d.find(d.root, "_changelog.md")).decode()
    )


def test_cap_counts_documents_sent_to_ocr_in_this_run():
    d = _drive(n_inbox=3)
    calls: list[str] = []
    opts = RunOptions(dry_run=True, now=NOW, run_id="t", doc_cap=2, docs_existing=0)
    r = run_curated(d, d.root, opts, extract_fn=_counting_extract(calls), propose_fn=fake_propose)
    assert len(calls) == 2
    assert [h["reason"] for h in r.held] == [HOLD_DOCUMENT_CAP]


def test_budget_gate_stops_new_ocr_and_marks_the_import_unfinished():
    d = _drive(n_inbox=3)
    calls: list[str] = []
    opts = RunOptions(
        dry_run=True,
        now=NOW,
        run_id="t",
        ocr_gate=lambda: "daily_page_budget" if len(calls) >= 1 else None,
    )
    r = run_curated(d, d.root, opts, extract_fn=_counting_extract(calls), propose_fn=fake_propose)
    assert len(calls) == 1
    assert [h["reason"] for h in r.held] == ["daily_page_budget"] * 2
    assert r.inbox_drained is False


async def test_prepare_passes_the_cap_and_the_document_count(db, monkeypatch):
    from unittest.mock import MagicMock, patch

    from oncofiles import config

    monkeypatch.setattr(config, "LOCAL_LLM_BASE_URL", "http://llm.test/v1")
    monkeypatch.setattr(config, "LOCAL_LLM_MODEL", "m")
    monkeypatch.setattr(config, "MAX_DOCUMENTS_PER_PATIENT", 7)
    monkeypatch.setattr(db, "count_documents", AsyncMock(return_value=4))
    clients = (MagicMock(), None, None, "folder1")
    with (
        patch("oncofiles.server._create_patient_clients", AsyncMock(return_value=clients)),
        patch("oncofiles.curated_folder.curated_reason", return_value="is a curated folder"),
        patch("oncofiles.patient_context.get_context", return_value={}),
    ):
        err, ctx = await tool._prepare(db, BIG, dry_run=True, max_docs=None, max_actions=40)
    assert err is None
    opts = ctx[2]
    assert opts.doc_cap == 7 and opts.docs_existing == 4 and opts.ocr_gate is not None


# ── daily page budget ────────────────────────────────────────────────


async def _run_row(db, pid, *, pages, started_at):
    rid = await db.insert_curated_run(patient_id=pid, dry_run=True, trigger="auto-preview")
    await db.finish_curated_run(
        rid, patient_id=pid, status="ok", message="", report={"model_pages": pages}
    )
    await db.db.execute("UPDATE curated_runs SET started_at = ? WHERE id = ?", (started_at, rid))
    await db.db.commit()


async def test_pages_used_today_counts_this_utc_day_only(db):
    midnight = NOW.replace(hour=0)
    before = _at(midnight - timedelta(minutes=1))
    after = _at(midnight + timedelta(seconds=1))
    await _run_row(db, BIG, pages=40, started_at=before)
    await _run_row(db, BIG, pages=25, started_at=after)
    await _run_row(db, LATE, pages=99, started_at=after)
    assert await tool.pages_used_today(db, BIG, now=NOW) == 25
    tool._inflight_pages[BIG] = 3  # the run executing now
    assert await tool.pages_used_today(db, BIG, now=NOW) == 28
    tomorrow = midnight + timedelta(days=1)  # reset at UTC midnight
    assert await tool.pages_used_today(db, BIG, now=tomorrow) == 3


async def test_over_budget_patient_is_skipped_until_the_next_utc_day(db, sched, monkeypatch):
    pending, turns, _ = sched
    monkeypatch.setenv("CURATED_DAILY_PAGE_BUDGET", "20")
    start = datetime.combine(datetime.now(UTC).date(), time.min, tzinfo=UTC)
    today = _at(start + timedelta(seconds=1))
    await _run_row(db, BIG, pages=21, started_at=today)
    pending[BIG], pending[SMALL] = _pending(3), _pending(1, "s")
    await _round(db, [BIG, SMALL])
    assert [pid for pid, _ in turns] == [SMALL]
    st = tool._probe_stats[BIG]
    assert st.runs_blocked == {"daily_page_budget": 1}
    assert st.last_blocked_reason == "daily_page_budget"
    assert st.pages_today == 21 and st.page_budget == 20
    # the next UTC day: the row now dates from yesterday, so it no longer counts
    yesterday = _at(start - timedelta(seconds=1))
    await db.db.execute("UPDATE curated_runs SET started_at = ?", (yesterday,))
    await db.db.commit()
    await _round(db, [BIG, SMALL])
    assert BIG in [pid for pid, _ in turns]


async def test_budget_zero_disables_the_check(db, sched, monkeypatch):
    pending, turns, _ = sched
    monkeypatch.setenv("CURATED_DAILY_PAGE_BUDGET", "0")
    await _run_row(
        db, BIG, pages=10_000, started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    pending[BIG] = _pending(2)
    await _round(db, [BIG])
    assert [pid for pid, _ in turns] == [BIG]
    assert tool._budget_gate(BIG, 10_000) is None


def test_budget_gate_counts_the_run_in_flight(monkeypatch):
    monkeypatch.setenv("CURATED_DAILY_PAGE_BUDGET", "10")
    gate = tool._budget_gate(BIG, 8)
    assert gate() is None
    tool._inflight_pages[BIG] = 2
    assert gate() == "daily_page_budget"


def test_cached_extract_reports_model_pages_only_for_fresh_vision(monkeypatch):
    from oncofiles.curated import extract as extract_mod

    tool._extract_cache.clear()
    seen: list[int] = []
    monkeypatch.setattr(
        extract_mod,
        "extract",
        lambda name, content, mime="", *, min_text_chars=100: Extraction(
            text="x" * 200, method="vision", pages=3, engine="test"
        ),
    )
    tool.cached_extract("a.pdf", b"synthetic-1", on_model_pages=seen.append)
    tool.cached_extract("a.pdf", b"synthetic-1", on_model_pages=seen.append)  # cache hit
    assert seen == [3]
    monkeypatch.setattr(
        extract_mod,
        "extract",
        lambda name, content, mime="", *, min_text_chars=100: Extraction(
            text="x" * 200, method="text_layer", pages=4, engine="pymupdf"
        ),
    )
    tool.cached_extract("b.pdf", b"synthetic-2", on_model_pages=seen.append)
    assert seen == [3]  # a text layer costs no model pages
    tool._extract_cache.clear()


async def test_execute_persists_the_pages_of_the_run(db, monkeypatch):
    """The model pages of a run land in its row, so the budget survives a restart."""
    from unittest.mock import MagicMock, patch

    from oncofiles.curated.runner import RunReport

    def fake_run(drive, folder_id, opts, *, extract_fn, propose_fn):
        extract_fn("a.pdf", b"synthetic-run-bytes", "application/pdf", min_text_chars=100)
        return RunReport(run_id="r", dry_run=True)

    from oncofiles.curated import extract as extract_mod

    monkeypatch.setattr(
        extract_mod,
        "extract",
        lambda name, content, mime="", *, min_text_chars=100: Extraction(
            text="x" * 200, method="vision", pages=4, engine="test"
        ),
    )
    tool._extract_cache.clear()
    with patch("oncofiles.curated.runner.run_curated", side_effect=fake_run):
        report = await tool._execute_locked(
            db, BIG, MagicMock(), "folder1", RunOptions(dry_run=True), trigger="test"
        )
    tool._extract_cache.clear()
    assert report["model_pages"] == 4
    assert tool._inflight_pages[BIG] == 0
    row = await db.get_curated_run(report["_row_id"], patient_id=BIG)
    assert row["report"]["model_pages"] == 4
    today = datetime.now(UTC)
    assert await tool.pages_used_today(db, BIG, now=today) == 4


# ── visibility: operator surfaces only ───────────────────────────────


def test_stats_are_on_readiness_and_admin_not_on_public_health():
    from oncofiles.server import health, readiness

    assert "probe_stats()" in inspect.getsource(readiness)
    health_src = inspect.getsource(health)
    for name in ("probe_stats", "heartbeat_detail", "scheduling_detail", "onboarding_queue"):
        assert name not in health_src
    assert set(tool.heartbeat()) == {"curated_last_probe_age_s", "curated_loop_suspected"}


async def test_probe_stats_carry_queue_pages_and_held_counts(db, sched, monkeypatch):
    pending, _, outcome = sched
    monkeypatch.setenv("CURATED_DAILY_PAGE_BUDGET", "300")
    await _enrol(db, BIG, _ts(1))
    await _enrol(db, LATE, _ts(2))
    pending[BIG], pending[LATE] = _pending(40), _pending(40, "y")
    outcome[BIG] = {"held": [{"source": "inbox/scan_999.pdf", "reason": "document_cap"}]}
    await _round(db, [BIG, LATE])
    stats = tool.probe_stats()
    assert stats[BIG]["onboarding"] == "active"
    assert stats[BIG]["last_held"] == {"document_cap": 1}
    assert stats[BIG]["held_total"] == {"document_cap": 1}
    assert stats[BIG]["pages_today"] == 0 and stats[BIG]["page_budget"] == 300
    assert stats[LATE]["onboarding_queue_position"] == 1
    # counts and reasons only: no file names on the operator surface
    assert "scan_999" not in json.dumps(stats)
