"""Tests for #465 — lab_values extraction on non-`labs` categories.

Covers:
- `has_lab_table` heuristic (positive / negative)
- `extract_lab_values` second Haiku call (mocked) parsing + filtering
- `extract_structured_metadata` fires the second call only when a table is
  detected (don't waste tokens on narrative notes)
- `_persist_lab_values_from_metadata` writes rows + resolves lab_date
- `backfill_lab_values_from_metadata` MCP tool:
  - dry_run=True → reports detected/extracted counts but writes 0 rows
  - dry_run=False → inserts into lab_values table for a fixture doc,
    honors batch_size, returns the expected shape
"""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from oncofiles.database import Database
from oncofiles.enhance import (
    extract_lab_values,
    extract_structured_metadata,
    has_lab_table,
)
from oncofiles.models import DocumentCategory
from oncofiles.sync import _persist_lab_values_from_metadata
from tests.helpers import ERIKA_UUID, make_doc

# Sample chemo_sheet text — the #465 case. Bundled pre-chemo CBC printout
# underneath a chemo administration form.
CHEMO_SHEET_WITH_LABS = """\
Cyklus 4 / mFOLFOX6 / 15.4.2026
Pacient: Erika Fusekova
Klinika klinickej onkologie, Klenova 1, Bratislava

Predchemo krvny obraz (Medirex, odber 2026-04-15):

WBC          6.80    10^9/L    4.0 - 10.0
NEUT%        62.5    %         50.0 - 70.0
ABS_NEUT     4.25    10^9/L    2.0 - 7.0
HGB          128     g/L       135 - 175    L
PLT          210     10^9/L    150 - 400
ALT          32      U/L       10 - 40

Regimen: Oxaliplatina 85 mg/m2 + 5-FU bolus + infuzia 46h.
Premedikacia: Dexametazon 8 mg, Ondansetron 8 mg.
Signed: MUDr. Porsok
"""

# Pure narrative consultation note — no numeric table. has_lab_table must
# NOT fire so we don't waste Haiku tokens.
NARRATIVE_NOTE = """\
Pacient na kontrole po 3. cykle mFOLFOX6.
Subjektivne sa citi dobre, bez febrilnej teploty.
Uziva Ciprinol a Zarzio podla predpisu.
Plan: pokracovat v chemoterapii, kontrola o 2 tyzdne.
Signed: MUDr. Minarikova
"""


# ── has_lab_table heuristic ────────────────────────────────────────────────


def test_has_lab_table_positive_chemo_sheet():
    """Chemo sheet with embedded CBC table → heuristic fires."""
    assert has_lab_table(CHEMO_SHEET_WITH_LABS) is True


def test_has_lab_table_negative_narrative_note():
    """Plain narrative consultation → heuristic does NOT fire."""
    assert has_lab_table(NARRATIVE_NOTE) is False


def test_has_lab_table_empty_text():
    assert has_lab_table("") is False
    assert has_lab_table("   \n\n") is False


def test_has_lab_table_rejects_date_columns():
    """A visit-history table with dates in the value column must NOT trigger
    the heuristic — dates aren't lab values."""
    text = """Visit history
    Cycle1 2026-01-05
    Cycle2 2026-01-19
    Cycle3 2026-02-02
    Cycle4 2026-02-16
    """
    assert has_lab_table(text) is False


def test_has_lab_table_min_rows_threshold():
    """Only 2 parameter/value lines → below default threshold of 3."""
    text = "WBC 6.8\nHGB 128\n"
    assert has_lab_table(text, min_rows=3) is False
    assert has_lab_table(text, min_rows=2) is True


# ── extract_lab_values (mocked Haiku) ──────────────────────────────────────


def _mock_anthropic_with_response(payload: dict | str):
    """Helper: mock anthropic client so messages.create returns `payload`."""
    mock_response = MagicMock()
    text = payload if isinstance(payload, str) else json.dumps(payload)
    mock_response.content = [MagicMock(text=text)]
    mock_response.usage = MagicMock(input_tokens=50, output_tokens=80)

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    return mock_client


def test_extract_lab_values_parses_clean_response():
    """Valid Haiku JSON → parsed lab_values dict."""
    payload = {
        "lab_date": "2026-04-15",
        "values": [
            {"parameter": "WBC", "value": 6.8, "unit": "10^9/L", "flag": ""},
            {
                "parameter": "HGB",
                "value": 128,
                "unit": "g/L",
                "flag": "L",
                "reference_low": 135,
                "reference_high": 175,
            },
        ],
    }
    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = _mock_anthropic_with_response(payload)
        result = extract_lab_values(CHEMO_SHEET_WITH_LABS)

    assert result is not None
    assert result["lab_date"] == "2026-04-15"
    assert len(result["values"]) == 2
    wbc = next(v for v in result["values"] if v["parameter"] == "WBC")
    assert wbc["value"] == 6.8
    assert wbc["unit"] == "10^9/L"
    hgb = next(v for v in result["values"] if v["parameter"] == "HGB")
    assert hgb["reference_low"] == 135.0
    assert hgb["flag"] == "L"


def test_extract_lab_values_empty_text_returns_none():
    """Empty text → no Haiku call, returns None."""
    result = extract_lab_values("")
    assert result is None


def test_extract_lab_values_filters_malformed_rows():
    """Rows without parameter or with non-numeric value are dropped."""
    payload = {
        "lab_date": "2026-04-15",
        "values": [
            {"parameter": "WBC", "value": 6.8},
            {"parameter": "", "value": 5.0},  # empty param → drop
            {"value": 3.2},  # no param → drop
            {"parameter": "HGB", "value": "not a number"},  # bad value → drop
            {"parameter": "PLT", "value": 210},  # keep
        ],
    }
    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = _mock_anthropic_with_response(payload)
        result = extract_lab_values("WBC 6.8\nHGB 128\nPLT 210\n")

    assert result is not None
    params = [v["parameter"] for v in result["values"]]
    assert sorted(params) == ["PLT", "WBC"]


def test_extract_lab_values_malformed_json_returns_none():
    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = _mock_anthropic_with_response("not json")
        result = extract_lab_values("some text")
    assert result is None


def test_extract_lab_values_empty_values_list_returns_none():
    payload = {"lab_date": None, "values": []}
    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = _mock_anthropic_with_response(payload)
        result = extract_lab_values("text")
    assert result is None


# ── extract_structured_metadata integration (fires / skips second call) ────


def test_extract_structured_metadata_fires_second_call_on_lab_table():
    """Chemo sheet text with a lab table → metadata response + labs response
    merged into structured_metadata['lab_values']."""
    metadata_payload = {
        "document_type": "chemo_sheet",
        "findings": ["Pre-cycle 4 CBC"],
        "diagnoses": [],
        "medications": ["Oxaliplatina", "5-FU"],
        "dates_mentioned": ["2026-04-15"],
        "providers": ["NOU"],
        "doctors": ["MUDr. Porsok"],
        "handwritten": False,
        "plain_summary": "Chemo sheet for cycle 4 of mFOLFOX6.",
        "plain_summary_sk": "Chemo list pre cyklus 4 mFOLFOX6.",
    }
    labs_payload = {
        "lab_date": "2026-04-15",
        "values": [
            {"parameter": "WBC", "value": 6.8, "unit": "10^9/L"},
            {"parameter": "ABS_NEUT", "value": 4.25, "unit": "10^9/L"},
            {"parameter": "HGB", "value": 128, "unit": "g/L", "flag": "L"},
        ],
    }

    # Two sequential Haiku calls: first for metadata, second for labs.
    mock_metadata_resp = MagicMock()
    mock_metadata_resp.content = [MagicMock(text=json.dumps(metadata_payload))]
    mock_metadata_resp.usage = MagicMock(input_tokens=10, output_tokens=20)

    mock_labs_resp = MagicMock()
    mock_labs_resp.content = [MagicMock(text=json.dumps(labs_payload))]
    mock_labs_resp.usage = MagicMock(input_tokens=15, output_tokens=30)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = [mock_metadata_resp, mock_labs_resp]

    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        result = extract_structured_metadata(CHEMO_SHEET_WITH_LABS)

    # Both calls fired
    assert mock_client.messages.create.call_count == 2
    # Base metadata merged
    assert result["document_type"] == "chemo_sheet"
    assert "Oxaliplatina" in result["medications"]
    # Lab values were attached
    assert "lab_values" in result
    assert result["lab_values"]["lab_date"] == "2026-04-15"
    params = [v["parameter"] for v in result["lab_values"]["values"]]
    assert sorted(params) == ["ABS_NEUT", "HGB", "WBC"]


def test_extract_structured_metadata_skips_second_call_on_narrative():
    """Narrative note with no lab table → only 1 Haiku call, no lab_values
    field added. Don't waste tokens on prose."""
    metadata_payload = {
        "document_type": "consultation",
        "findings": ["Patient stable"],
        "plain_summary": "Follow-up visit, patient stable, plan continue chemo.",
    }
    mock_resp = MagicMock()
    mock_resp.content = [MagicMock(text=json.dumps(metadata_payload))]
    mock_resp.usage = MagicMock(input_tokens=10, output_tokens=20)

    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_resp

    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = mock_client
        result = extract_structured_metadata(NARRATIVE_NOTE)

    # Only the metadata call fired
    assert mock_client.messages.create.call_count == 1
    assert result["document_type"] == "consultation"
    assert "lab_values" not in result


# ── _persist_lab_values_from_metadata ──────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_lab_values_writes_rows(db: Database):
    """Given a doc + metadata carrying lab_values, rows land in lab_values."""
    doc = make_doc(
        file_id="f_chemo_1",
        filename="20260415_chemo_cycle4.pdf",
        document_date=date(2026, 4, 15),
        category=DocumentCategory.CHEMO_SHEET,
    )
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    metadata = {
        "document_type": "chemo_sheet",
        "lab_values": {
            "lab_date": "2026-04-15",
            "values": [
                {"parameter": "WBC", "value": 6.8, "unit": "10^9/L"},
                {"parameter": "ABS_NEUT", "value": 4.25, "unit": "10^9/L"},
                {"parameter": "HGB", "value": 128, "unit": "g/L", "flag": "L"},
            ],
        },
    }

    written = await _persist_lab_values_from_metadata(db, doc, metadata)
    assert written == 3

    stored = await db.get_lab_snapshot(doc.id, patient_id=ERIKA_UUID)
    params = sorted(v.parameter for v in stored)
    assert params == ["ABS_NEUT", "HGB", "WBC"]
    hgb = next(v for v in stored if v.parameter == "HGB")
    assert hgb.flag == "L"
    assert hgb.lab_date == date(2026, 4, 15)


@pytest.mark.asyncio
async def test_persist_lab_values_falls_back_to_document_date(db: Database):
    """When the AI didn't extract a lab_date, fall back to doc.document_date."""
    doc = make_doc(
        file_id="f_chemo_2",
        filename="20260415_chemo.pdf",
        document_date=date(2026, 4, 15),
        category=DocumentCategory.CHEMO_SHEET,
    )
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    metadata = {
        "lab_values": {
            "lab_date": None,
            "values": [{"parameter": "WBC", "value": 6.8, "unit": "10^9/L"}],
        },
    }

    written = await _persist_lab_values_from_metadata(db, doc, metadata)
    assert written == 1

    stored = await db.get_lab_snapshot(doc.id, patient_id=ERIKA_UUID)
    assert stored[0].lab_date == date(2026, 4, 15)


@pytest.mark.asyncio
async def test_persist_lab_values_no_block_returns_zero(db: Database):
    """Metadata without lab_values block → no writes."""
    doc = make_doc(file_id="f_no_labs")
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)

    metadata = {"document_type": "consultation", "findings": []}
    written = await _persist_lab_values_from_metadata(db, doc, metadata)
    assert written == 0


# ── backfill_lab_values_from_metadata MCP tool ─────────────────────────────


class _FakeCtx:
    """Minimal Context stand-in exposing `request_context.lifespan_context`
    for `_get_db` / `_resolve_patient_id` path when patient_slug is omitted."""

    def __init__(self, db: Database, patient_id: str):
        self._db = db
        self._pid = patient_id

    @property
    def request_context(self):
        return self

    @property
    def lifespan_context(self):
        return self

    def __getitem__(self, key: str):
        return {"db": self._db}[key]


def _make_fake_ctx(db: Database) -> MagicMock:
    """Return a MagicMock ctx whose _get_db / _get_patient_id path works."""
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db}
    return ctx


async def _insert_doc_with_metadata(
    db: Database,
    *,
    file_id: str,
    filename: str,
    category: DocumentCategory,
    doc_date: date,
    ocr_text: str,
    metadata_json: str | None,
) -> int:
    """Insert a document, seed OCR text, and stamp structured_metadata.
    Returns the doc id."""
    doc = make_doc(
        file_id=file_id,
        filename=filename,
        document_date=doc_date,
        category=category,
    )
    doc = await db.insert_document(doc, patient_id=ERIKA_UUID)
    await db.save_ocr_page(doc.id, 1, ocr_text, "pymupdf-native")
    if metadata_json is not None:
        await db.update_structured_metadata(doc.id, metadata_json)
    return doc.id


@pytest.mark.asyncio
async def test_backfill_lab_values_dry_run_writes_nothing(db: Database):
    """dry_run=True → lab_values_added is 0 even though detection fires."""
    from oncofiles.tools.enhance_tools import backfill_lab_values_from_metadata

    # Chemo sheet with metadata but no lab_values — a candidate doc.
    doc_id = await _insert_doc_with_metadata(
        db,
        file_id="f_bck_dry_1",
        filename="20260415_chemo.pdf",
        category=DocumentCategory.CHEMO_SHEET,
        doc_date=date(2026, 4, 15),
        ocr_text=CHEMO_SHEET_WITH_LABS,
        metadata_json=json.dumps({"document_type": "chemo_sheet", "findings": []}),
    )

    labs_payload = {
        "lab_date": "2026-04-15",
        "values": [
            {"parameter": "WBC", "value": 6.8, "unit": "10^9/L"},
            {"parameter": "HGB", "value": 128, "unit": "g/L"},
        ],
    }
    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = _mock_anthropic_with_response(labs_payload)
        ctx = _make_fake_ctx(db)
        raw = await backfill_lab_values_from_metadata(
            ctx,
            patient_slug=None,
            dry_run=True,
            batch_size=10,
        )
    result = json.loads(raw)

    assert result["dry_run"] is True
    assert result["processed"] >= 1
    assert result["detected_lab_tables"] >= 1
    assert result["extracted_nonempty"] >= 1
    # The contract under test — nothing is written when dry_run=True
    assert result["lab_values_added"] == 0

    # DB confirms no rows were inserted
    stored = await db.get_lab_snapshot(doc_id, patient_id=ERIKA_UUID)
    assert stored == []


@pytest.mark.asyncio
async def test_backfill_lab_values_writes_when_not_dry_run(db: Database):
    """dry_run=False → rows appear in lab_values, shape is correct,
    batch_size is honored."""
    from oncofiles.tools.enhance_tools import backfill_lab_values_from_metadata

    doc1 = await _insert_doc_with_metadata(
        db,
        file_id="f_bck_w_1",
        filename="20260415_chemo.pdf",
        category=DocumentCategory.CHEMO_SHEET,
        doc_date=date(2026, 4, 15),
        ocr_text=CHEMO_SHEET_WITH_LABS,
        metadata_json=json.dumps({"document_type": "chemo_sheet"}),
    )
    doc2 = await _insert_doc_with_metadata(
        db,
        file_id="f_bck_w_2",
        filename="20260501_chemo.pdf",
        category=DocumentCategory.CHEMO_SHEET,
        doc_date=date(2026, 5, 1),
        ocr_text=CHEMO_SHEET_WITH_LABS,
        metadata_json=json.dumps({"document_type": "chemo_sheet"}),
    )

    labs_payload = {
        "lab_date": "2026-04-15",
        "values": [
            {"parameter": "WBC", "value": 6.8, "unit": "10^9/L"},
            {"parameter": "HGB", "value": 128, "unit": "g/L"},
        ],
    }
    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        mock_anthropic.Anthropic.return_value = _mock_anthropic_with_response(labs_payload)
        ctx = _make_fake_ctx(db)
        # batch_size=1 so only ONE doc gets processed this call.
        raw = await backfill_lab_values_from_metadata(
            ctx,
            patient_slug=None,
            dry_run=False,
            batch_size=1,
        )
    result = json.loads(raw)

    assert result["dry_run"] is False
    assert result["processed"] == 1  # batch_size honored
    assert result["lab_values_added"] == 2
    assert result["remaining"] >= 1  # the other doc still pending
    assert result["status"] == "partial"
    assert set(result["categories"]) >= {"chemo_sheet"}

    # Exactly one doc has stored values
    stored1 = await db.get_lab_snapshot(doc1, patient_id=ERIKA_UUID)
    stored2 = await db.get_lab_snapshot(doc2, patient_id=ERIKA_UUID)
    # The tool orders by doc_date DESC so doc2 (2026-05-01) is processed first
    processed_docs = (stored1, stored2)
    total = len(stored1) + len(stored2)
    assert total == 2  # one doc got 2 rows, the other got 0
    assert any(len(s) == 2 for s in processed_docs)
    assert any(len(s) == 0 for s in processed_docs)


@pytest.mark.asyncio
async def test_backfill_lab_values_skips_docs_with_existing_lab_values_key(
    db: Database,
):
    """Docs whose structured_metadata already carries a lab_values block
    are excluded by the SQL filter — no Haiku call fires."""
    from oncofiles.tools.enhance_tools import backfill_lab_values_from_metadata

    await _insert_doc_with_metadata(
        db,
        file_id="f_skip_already",
        filename="20260415_chemo.pdf",
        category=DocumentCategory.CHEMO_SHEET,
        doc_date=date(2026, 4, 15),
        ocr_text=CHEMO_SHEET_WITH_LABS,
        metadata_json=json.dumps({"document_type": "chemo_sheet", "lab_values": {"values": []}}),
    )

    with patch("oncofiles.enhance.anthropic") as mock_anthropic:
        # Any call would be a bug — asserting the heuristic stage isn't reached
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = AssertionError(
            "Haiku should not be called for docs already carrying lab_values"
        )
        mock_anthropic.Anthropic.return_value = mock_client
        ctx = _make_fake_ctx(db)
        raw = await backfill_lab_values_from_metadata(
            ctx, patient_slug=None, dry_run=False, batch_size=10
        )
    result = json.loads(raw)

    assert result["processed"] == 0
    assert result["remaining"] == 0
    assert result["status"] == "complete"
