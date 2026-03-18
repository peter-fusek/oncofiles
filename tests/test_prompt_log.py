"""Tests for prompt log observability."""

from __future__ import annotations

from oncofiles.models import PromptCallType, PromptLogEntry, PromptLogQuery
from oncofiles.prompt_logger import _extract_result_summary

# ── _extract_result_summary ──────────────────────────────────────────


def test_extract_summary_tags():
    raw = '{"summary": "Blood count results show normal WBC.", "tags": ["labs"]}'
    result = _extract_result_summary("summary_tags", raw)
    assert "Blood count" in result


def test_extract_structured_metadata():
    raw = '{"plain_summary": "Patient had a CT scan showing no progression."}'
    result = _extract_result_summary("structured_metadata", raw)
    assert "CT scan" in result


def test_extract_filename_description():
    raw = "BloodCountBeforeCycle3"
    result = _extract_result_summary("filename_description", raw)
    assert result == "BloodCountBeforeCycle3"


def test_extract_ocr():
    raw = "Pacientka: Erika Fusekova\nDatum: 2026-01-15\nWBC: 5.2"
    result = _extract_result_summary("ocr", raw)
    assert "Erika Fusekova" in result
    assert len(result) <= 150


def test_extract_empty():
    assert _extract_result_summary("ocr", "") == ""


def test_extract_invalid_json():
    result = _extract_result_summary("summary_tags", "not json at all")
    assert result == "not json at all"[:200]


# ── Database CRUD ────────────────────────────────────────────────────


async def test_insert_and_get_prompt_log(db):
    entry = PromptLogEntry(
        call_type=PromptCallType.SUMMARY_TAGS,
        document_id=1,
        model="claude-haiku-4-5",
        system_prompt="You are a medical analyst.",
        user_prompt="Document text:\n\ntest content",
        raw_response='{"summary": "Test summary"}',
        input_tokens=100,
        output_tokens=50,
        duration_ms=1500,
        result_summary="Test summary",
    )
    inserted = await db.insert_prompt_log(entry)
    assert inserted.id is not None

    fetched = await db.get_prompt_log(inserted.id)
    assert fetched is not None
    assert fetched.call_type == PromptCallType.SUMMARY_TAGS
    assert fetched.document_id == 1
    assert fetched.system_prompt == "You are a medical analyst."
    assert fetched.input_tokens == 100


async def test_search_prompt_log_by_type(db):
    for ct in [PromptCallType.OCR, PromptCallType.SUMMARY_TAGS, PromptCallType.OCR]:
        await db.insert_prompt_log(
            PromptLogEntry(
                call_type=ct,
                model="claude-haiku-4-5",
                result_summary="test",
            )
        )

    query = PromptLogQuery(call_type="ocr")
    results = await db.search_prompt_log(query)
    assert len(results) == 2
    assert all(r.call_type == PromptCallType.OCR for r in results)


async def test_search_prompt_log_by_document(db):
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.SUMMARY_TAGS,
            document_id=42,
            model="claude-haiku-4-5",
        )
    )
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.SUMMARY_TAGS,
            document_id=99,
            model="claude-haiku-4-5",
        )
    )

    query = PromptLogQuery(document_id=42)
    results = await db.search_prompt_log(query)
    assert len(results) == 1
    assert results[0].document_id == 42


async def test_search_prompt_log_text_search(db):
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.OCR,
            model="claude-haiku-4-5",
            raw_response="Erika Fusekova blood count results",
            result_summary="blood count",
        )
    )
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.OCR,
            model="claude-haiku-4-5",
            raw_response="CT scan imaging report",
            result_summary="CT scan",
        )
    )

    query = PromptLogQuery(text="blood")
    results = await db.search_prompt_log(query)
    assert len(results) == 1
    assert "blood" in results[0].result_summary


async def test_get_prompt_log_stats(db):
    for _ in range(3):
        await db.insert_prompt_log(
            PromptLogEntry(
                call_type=PromptCallType.OCR,
                model="claude-haiku-4-5",
                input_tokens=200,
                output_tokens=100,
                duration_ms=1000,
            )
        )
    await db.insert_prompt_log(
        PromptLogEntry(
            call_type=PromptCallType.SUMMARY_TAGS,
            model="claude-haiku-4-5",
            input_tokens=500,
            output_tokens=50,
            duration_ms=2000,
        )
    )

    stats = await db.get_prompt_log_stats()
    assert "ocr" in stats
    assert stats["ocr"]["count"] == 3
    assert stats["ocr"]["total_input_tokens"] == 600
    assert "summary_tags" in stats
    assert stats["summary_tags"]["count"] == 1


async def test_prompt_log_not_found(db):
    result = await db.get_prompt_log(9999)
    assert result is None


# ── log_ai_call ──────────────────────────────────────────────────────


def test_log_ai_call_with_none_db():
    """log_ai_call with db=None should silently skip."""
    from oncofiles.prompt_logger import log_ai_call

    # Should not raise
    log_ai_call(
        None,
        call_type="ocr",
        model="test",
        system_prompt="test",
        user_prompt="test",
        raw_response="test",
        duration_ms=100,
    )
