"""Lock-in for #441 Layer 4: shared oncology preamble drives Anthropic
prompt-cache hits across all 9 AI call sites.

Each ``client.messages.create`` call must now pass a two-block ``system``
list:

  [0] shared preamble (carries ``cache_control: {type: "ephemeral"}``)
  [1] task-specific prompt (also carries ``cache_control``)

The shared block is identical across the fleet — first call lands the
cache entry, all subsequent calls in the 5-min TTL hit it. The task
block is a SECOND breakpoint so batches of repeated calls of the SAME
task (e.g. enhancing 100 docs in a row) also hit a cache for the task
prefix on top of the shared one.

These tests fail fast if a future refactor:
  * regresses to a single-block ``system`` list
  * drops the shared preamble from any of the 9 call sites
  * shrinks the preamble below the 4096-token Haiku cache threshold
  * silently changes the preamble text (which would rotate the cache
    key and force a full warmup across the fleet — change once per
    release, never casually)
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock, patch

import pytest

from oncofiles import doc_analysis, enhance
from oncofiles.ai_preamble import (
    SHARED_ONCOLOGY_PREAMBLE,
    get_shared_preamble_block,
)

# ── Preamble itself ────────────────────────────────────────────────────


def test_preamble_above_haiku_cache_threshold():
    """Haiku 4.5 has a 4096-token minimum cacheable prefix. The preamble
    must clear that with margin (target ~4500). Below threshold, the
    cache_control marker is silently a no-op and #441 Layer 4 doesn't
    activate.

    We don't have the real Anthropic tokenizer at test time, so we use a
    conservative chars/4 estimator (English-leaning). Slovak content
    tokenizes a bit denser (closer to chars/3.5), so the real token count
    is higher than this estimate — chars/4 ≥ 4096 is a SAFE lower bound.
    """
    chars = len(SHARED_ONCOLOGY_PREAMBLE)
    approx_tokens = chars // 4
    assert approx_tokens >= 4096, (
        f"shared preamble at ~{approx_tokens} tokens (chars/4 lower bound, "
        f"{chars} chars total) does NOT clear the Haiku 4096-token cache "
        f"threshold — cache_control will be a silent no-op (#441 Layer 4 "
        f"won't activate). Add more content to ai_preamble.py."
    )


def test_preamble_carries_oncology_context():
    """Sanity guards against an accidental rewrite that strips the actual
    operational context. These markers MUST stay — they're the reason
    the preamble is useful even pre-cache-hit, and they justify its
    size in code review."""
    p = SHARED_ONCOLOGY_PREAMBLE
    # Setting markers
    assert "Oncofiles" in p
    assert "Slovak" in p
    # Output contract
    assert "JSON" in p
    assert "markdown" in p.lower()
    # Slovak healthcare specifics — institution codes
    for code in ("NOU", "OUSA", "Medirex", "Synlab", "Cytopathos", "BoryNemocnica"):
        assert code in p, f"institution code {code!r} missing from preamble"
    # Medical abbreviations
    for abbr in ("KO+L", "FOLFOX", "CEA", "CA 19-9", "KRAS", "MSI"):
        assert abbr in p, f"medical abbreviation {abbr!r} missing from preamble"
    # Worked examples — guards "we deleted half the preamble" scenarios
    assert "Example A" in p
    assert "Example F" in p
    assert "Example L" in p


def test_get_shared_preamble_block_shape():
    """The helper returns the exact dict shape Anthropic expects: type=text,
    cache_control={type: ephemeral}, the full preamble as text."""
    block = get_shared_preamble_block()
    assert block["type"] == "text"
    assert block["cache_control"] == {"type": "ephemeral"}
    assert block["text"] == SHARED_ONCOLOGY_PREAMBLE


# ── All 9 call sites use the shared preamble as their first block ──────


def _capture_system_blocks(
    call_fn, *fn_args, mock_text='{"summary":"x","summary_sk":"y","tags":[]}'
):
    """Invoke call_fn with mocked Anthropic client and return the captured
    `system` argument so the test can assert on its shape."""
    response = MagicMock()
    response.content = [MagicMock(text=mock_text)]
    response.usage = MagicMock(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=4500,
        cache_read_input_tokens=0,
    )
    captured: dict = {}

    def fake_create(**kwargs):
        captured.update(kwargs)
        return response

    target_module = enhance if call_fn.__module__.endswith("enhance") else doc_analysis
    with (
        patch.object(target_module, "_get_client") as mock_client,
        patch.object(target_module, "log_ai_call"),
    ):
        mock_client.return_value.messages.create.side_effect = fake_create
        call_fn(*fn_args)
    return captured.get("system")


@pytest.mark.parametrize(
    ("call_fn", "args"),
    [
        (enhance.enhance_document_text, ("CEA 12.4 µg/L",)),
        (enhance.classify_document, ("NOU letterhead, mFOLFOX6 cycle 4",)),
        (enhance.generate_filename_description, ("CT abdomen, no acute findings",)),
    ],
)
def test_enhance_single_arg_sites_prepend_preamble(call_fn, args):
    """The 3 enhance.py call sites that take a single text arg — must
    pass system=[shared_preamble, task_block]."""
    if call_fn is enhance.classify_document:
        mock = '{"institution_code":"NOU","category":"chemo_sheet","document_date":null}'
    elif call_fn is enhance.generate_filename_description:
        mock = "CTAbdomenNoAcute"
    else:
        mock = '{"summary":"x","summary_sk":"y","tags":[]}'
    system = _capture_system_blocks(call_fn, *args, mock_text=mock)
    assert isinstance(system, list)
    assert len(system) == 2, (
        f"system list must be [shared_preamble, task_block]; got {len(system)} blocks"
    )
    # First block IS the shared preamble.
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # Second block is the task-specific prompt — different text, also cached.
    assert system[1]["text"] != SHARED_ONCOLOGY_PREAMBLE
    assert system[1]["cache_control"] == {"type": "ephemeral"}


def test_extract_structured_metadata_prepends_preamble():
    """Task block here is METADATA_SYSTEM_PROMPT."""
    system = _capture_system_blocks(
        enhance.extract_structured_metadata,
        "Pathology report — colon adenocarcinoma",
        mock_text='{"document_type":"pathology","findings":[],"diagnoses":[],"medications":[],"dates_mentioned":[],"providers":[],"doctors":[],"handwritten":false,"plain_summary":"","plain_summary_sk":""}',
    )
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE
    assert "document_type" in system[1]["text"]


def test_extract_lab_values_prepends_preamble():
    """Task block here is LAB_VALUES_SYSTEM_PROMPT."""
    system = _capture_system_blocks(
        enhance.extract_lab_values,
        "WBC 5.2  10^9/L  4.0-10.0",
        mock_text='{"lab_date":"2026-04-29","values":[{"parameter":"WBC","value":5.2,"unit":"10^9/L","flag":""}]}',
    )
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE
    # Task block markers — the lab-values prompt is keyed on these strings.
    assert "lab-report parser" in system[1]["text"] or "lab_date" in system[1]["text"]


# ── doc_analysis.py — 4 sites ───────────────────────────────────────────


def test_analyze_document_composition_prepends_preamble():
    system = _capture_system_blocks(
        doc_analysis.analyze_document_composition,
        "Discharge summary, then a CT report on a different date",
        mock_text='{"document_count":1,"documents":[]}',
    )
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE


def test_analyze_consolidation_prepends_preamble():
    """Two-doc consolidation call — verify shape on a real invocation."""
    from oncofiles.models import DocumentCategory
    from tests.helpers import make_doc

    doc1 = make_doc(file_id="f1", filename="a.pdf")
    doc1.id = 1
    doc1.category = DocumentCategory.PATHOLOGY
    doc2 = make_doc(file_id="f2", filename="b.pdf")
    doc2.id = 2
    doc2.category = DocumentCategory.PATHOLOGY
    system = _capture_system_blocks(
        doc_analysis.analyze_consolidation,
        [(doc1, "page 1 of 3"), (doc2, "pages 2-3 of 3")],
        mock_text='{"groups":[]}',
    )
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE


def test_analyze_vaccination_events_prepends_preamble():
    system = _capture_system_blocks(
        doc_analysis.analyze_vaccination_events,
        "Hexacima 2024-02-01 primary 1",
        mock_text='{"events":[]}',
    )
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE


def test_analyze_document_relationships_prepends_preamble():
    system = _capture_system_blocks(
        doc_analysis.analyze_document_relationships,
        "CEA 12.4 µg/L 2026-04-12",
        1,
        [
            {
                "id": 2,
                "filename": "earlier.pdf",
                "document_date": "2026-01-15",
                "institution": "NOU",
                "category": "labs",
            }
        ],
        mock_text='{"relationships":[]}',
    )
    assert isinstance(system, list) and len(system) == 2
    assert system[0]["text"] == SHARED_ONCOLOGY_PREAMBLE


# ── Source-level invariant: every system=[ block leads with the preamble ──


def test_no_call_site_uses_single_block_system():
    """A sanity sweep over the source: any ``system=[`` block followed
    immediately by a ``{`` (instead of ``get_shared_preamble_block()``)
    is a regression — that call site is bypassing the shared cache."""
    for module in (enhance, doc_analysis):
        src = inspect.getsource(module)
        # Find every `system=[` and look at the next non-whitespace token.
        idx = 0
        while True:
            idx = src.find("system=[", idx)
            if idx == -1:
                break
            tail = src[idx + len("system=[") :]
            # Strip leading whitespace + newlines.
            stripped = tail.lstrip()
            assert stripped.startswith("get_shared_preamble_block()"), (
                f"in {module.__name__}: system=[ at offset {idx} does NOT lead "
                f"with get_shared_preamble_block() — call site bypasses the shared "
                f"prompt cache (#441 Layer 4). Next 80 chars: {stripped[:80]!r}"
            )
            idx += 1
