"""Locks the #441 Layer 4 + Layer 6 contract on every AI call site.

Two invariants:

1. **cache_control on the system block.** Every `client.messages.create()` call
   in `enhance.py` and `doc_analysis.py` passes `system` as a structured list
   whose last block carries `cache_control: {type: "ephemeral"}`. This is a
   no-op on Haiku 4.5 right now (system prompts are 145–1173 tokens, below the
   4096-token minimum cacheable prefix), but it activates automatically when
   prompts cross the threshold — and the marker shape itself is tested here so
   a future refactor can't silently drop it.

2. **Cache token observability.** `cache_creation_input_tokens` and
   `cache_read_input_tokens` from `response.usage` are forwarded to
   `log_ai_call`, which persists them in `prompt_log`. Required for measuring
   cache hit rate empirically once prompts pad above 4096 tokens.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import oncofiles.doc_analysis as doc_analysis
import oncofiles.enhance as enhance


def _make_response(text: str, *, cache_creation: int = 0, cache_read: int = 0) -> MagicMock:
    """Build a mock Anthropic Messages response with usage carrying cache fields."""
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 50
    usage.cache_creation_input_tokens = cache_creation
    usage.cache_read_input_tokens = cache_read
    response.usage = usage
    return response


def _assert_system_has_ephemeral_cache_control(call_kwargs: dict) -> None:
    """Lock the cache_control shape on the system block."""
    system = call_kwargs["system"]
    assert isinstance(system, list), (
        f"system must be a list of content blocks (not a string) so we can attach "
        f"cache_control; got {type(system).__name__}"
    )
    assert system, "system list must be non-empty"
    last = system[-1]
    assert last.get("type") == "text", "last system block must be type=text"
    assert last.get("cache_control") == {"type": "ephemeral"}, (
        f"last system block must carry cache_control={{'type': 'ephemeral'}}; "
        f"got {last.get('cache_control')!r}"
    )
    # The actual prompt text must still be present.
    assert isinstance(last.get("text"), str)
    assert last["text"].strip(), "system prompt text cannot be empty"


# ── enhance.py — 5 call sites ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("call_fn", "input_text", "mock_text"),
    [
        (
            enhance.enhance_document_text,
            "CEA 15.2 ng/mL",
            '{"summary": "x", "summary_sk": "y", "tags": ["a"]}',
        ),
        (
            enhance.classify_document,
            "NOU letterhead, mFOLFOX6 cycle 4",
            '{"institution_code": "NOU", "category": "chemo_sheet", "document_date": null}',
        ),
        (
            enhance.generate_filename_description,
            "MR brain, contrast, no acute findings",
            "BrainMRINoAcuteFindings",
        ),
    ],
)
def test_enhance_call_sites_attach_cache_control_and_capture_cache_tokens(
    call_fn, input_text, mock_text
):
    """Three of the five enhance.py paths take a single text arg — covered here."""
    response = _make_response(mock_text, cache_creation=0, cache_read=0)
    captured_kwargs = {}
    captured_log_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return response

    def fake_log(*_args, **kwargs):
        captured_log_kwargs.update(kwargs)

    with (
        patch.object(enhance, "_get_client") as mock_get_client,
        patch.object(enhance, "log_ai_call", side_effect=fake_log),
    ):
        mock_get_client.return_value.messages.create.side_effect = fake_create
        call_fn(input_text)

    _assert_system_has_ephemeral_cache_control(captured_kwargs)

    # log_ai_call must have received the two new fields with the values from usage.
    assert "cache_creation_input_tokens" in captured_log_kwargs
    assert "cache_read_input_tokens" in captured_log_kwargs
    assert captured_log_kwargs["cache_creation_input_tokens"] == 0
    assert captured_log_kwargs["cache_read_input_tokens"] == 0


def test_extract_structured_metadata_attaches_cache_control_and_forwards_cache_tokens():
    """METADATA call site has more args than the simple ones — exercised separately."""
    response = _make_response(
        '{"document_type": "labs", "findings": [], "diagnoses": [], '
        '"medications": [], "dates_mentioned": [], "providers": [], '
        '"doctors": [], "handwritten": false, "plain_summary": "x", '
        '"plain_summary_sk": "y", "institution_code": "NOU", '
        '"category": "labs", "document_date": "2026-04-01"}',
        cache_creation=4096,
        cache_read=0,
    )
    captured_kwargs = {}
    captured_log_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return response

    def fake_log(*_args, **kwargs):
        captured_log_kwargs.update(kwargs)

    with (
        patch.object(enhance, "_get_client") as mock_get_client,
        patch.object(enhance, "log_ai_call", side_effect=fake_log),
    ):
        mock_get_client.return_value.messages.create.side_effect = fake_create
        enhance.extract_structured_metadata("Some labs text", filename="x.pdf")

    _assert_system_has_ephemeral_cache_control(captured_kwargs)
    # Simulated cache write should propagate (not silently dropped).
    assert captured_log_kwargs["cache_creation_input_tokens"] == 4096
    assert captured_log_kwargs["cache_read_input_tokens"] == 0


def test_extract_lab_values_attaches_cache_control_and_forwards_cache_read_hit():
    """LAB_VALUES — also exercises the cache-HIT path (cache_read > 0)."""
    response = _make_response(
        '{"lab_date": "2026-04-01", "values": ['
        '{"parameter": "CEA", "value": 12.3, "unit": "ng/ml"}'
        "]}",
        cache_creation=0,
        cache_read=4096,
    )
    captured_kwargs = {}
    captured_log_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return response

    def fake_log(*_args, **kwargs):
        captured_log_kwargs.update(kwargs)

    with (
        patch.object(enhance, "_get_client") as mock_get_client,
        patch.object(enhance, "log_ai_call", side_effect=fake_log),
    ):
        mock_get_client.return_value.messages.create.side_effect = fake_create
        enhance.extract_lab_values("CEA 12.3 ng/ml")

    _assert_system_has_ephemeral_cache_control(captured_kwargs)
    assert captured_log_kwargs["cache_creation_input_tokens"] == 0
    assert captured_log_kwargs["cache_read_input_tokens"] == 4096


# ── doc_analysis.py — 4 call sites ─────────────────────────────────────────


def test_analyze_document_composition_attaches_cache_control():
    response = _make_response('{"documents": []}')
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return response

    with patch.object(doc_analysis, "_get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = fake_create
        doc_analysis.analyze_document_composition("text")

    _assert_system_has_ephemeral_cache_control(captured_kwargs)


def test_analyze_consolidation_attaches_cache_control():
    """analyze_consolidation takes list[tuple[Document, str]] and short-circuits
    when fewer than 2 docs are passed — supply two real Document instances."""
    from tests.helpers import make_doc

    response = _make_response('{"groups": []}')
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return response

    doc_a = make_doc(file_id="a", filename="a.pdf")
    doc_a.id = 1
    doc_b = make_doc(file_id="b", filename="b.pdf")
    doc_b.id = 2

    with patch.object(doc_analysis, "_get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = fake_create
        doc_analysis.analyze_consolidation([(doc_a, "text-a"), (doc_b, "text-b")])

    _assert_system_has_ephemeral_cache_control(captured_kwargs)


def test_analyze_document_relationships_attaches_cache_control():
    """analyze_document_relationships(doc_text, doc_id, candidates)."""
    response = _make_response('{"relationships": []}')
    captured_kwargs = {}

    def fake_create(**kwargs):
        captured_kwargs.update(kwargs)
        return response

    candidates = [
        {
            "id": 2,
            "filename": "b.pdf",
            "document_date": "2026-04-01",
            "institution": "NOU",
            "category": "imaging",
            "ai_summary": "CT scan",
        }
    ]
    with patch.object(doc_analysis, "_get_client") as mock_get_client:
        mock_get_client.return_value.messages.create.side_effect = fake_create
        doc_analysis.analyze_document_relationships(
            doc_text="target text", doc_id=1, candidates=candidates
        )

    _assert_system_has_ephemeral_cache_control(captured_kwargs)
