"""#618: without ANTHROPIC_API_KEY the cloud is disabled, with a clear typed error.

The key is read at call time, so patching ``oncofiles.config.ANTHROPIC_API_KEY``
flips the behaviour. All values here are synthetic.
"""

from __future__ import annotations

import base64
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oncofiles import enhance
from oncofiles.cloud_guard import (
    CLOUD_AI_DISABLED_MESSAGE,
    CloudAIDisabledError,
    CloudForbiddenError,
    local_only,
)
from oncofiles.database import Database
from oncofiles.files_api import FilesClient
from tests.helpers import ERIKA_UUID

FAKE_KEY = "sk-ant-test-not-a-real-key"


@pytest.fixture
def no_key():
    with patch("oncofiles.config.ANTHROPIC_API_KEY", ""):
        yield


@pytest.fixture
def with_key():
    with patch("oncofiles.config.ANTHROPIC_API_KEY", FAKE_KEY):
        yield


@pytest.fixture(autouse=True)
def _fresh_shared_client():
    saved = enhance._shared_client
    enhance._shared_client = None
    yield
    enhance._shared_client = saved


def _ctx(db: Database, files) -> MagicMock:
    ctx = MagicMock()
    ctx.request_context.lifespan_context = {"db": db, "files": files, "gdrive": None}
    ctx.info = AsyncMock()
    return ctx


# ── the error type ───────────────────────────────────────────────────────────


def test_error_is_a_cloud_forbidden_error_with_clear_message():
    err = CloudAIDisabledError()
    assert isinstance(err, CloudForbiddenError)
    assert str(err) == "cloud AI is disabled on this server (no ANTHROPIC_API_KEY)"
    assert str(err) == CLOUD_AI_DISABLED_MESSAGE


# ── _get_client ──────────────────────────────────────────────────────────────


def test_get_client_raises_when_key_unset(no_key):
    with pytest.raises(CloudAIDisabledError, match="no ANTHROPIC_API_KEY"):
        enhance._get_client()
    assert enhance._shared_client is None  # nothing was constructed


def test_get_client_works_when_key_set(with_key):
    client = enhance._get_client()
    assert client is not None
    assert enhance._get_client() is client  # still the shared singleton


def test_local_only_still_wins_over_missing_key(no_key):
    with local_only("synthetic job"), pytest.raises(CloudForbiddenError, match="synthetic job"):
        enhance._get_client()


# ── FilesClient ──────────────────────────────────────────────────────────────


def test_files_client_upload_raises_when_key_unset(no_key):
    fc = FilesClient()
    fc._client = MagicMock()
    with pytest.raises(CloudAIDisabledError, match="no ANTHROPIC_API_KEY"):
        fc.upload(io.BytesIO(b"synthetic"), "20270101_Synthetic.pdf", "application/pdf")
    fc._client.beta.files.upload.assert_not_called()


def test_files_client_explicit_key_is_not_disabled(no_key):
    fc = FilesClient(api_key="test")
    fc._client = MagicMock()
    fc.upload(io.BytesIO(b"synthetic"), "20270101_Synthetic.pdf", "application/pdf")
    fc._client.beta.files.upload.assert_called_once()


def test_files_client_upload_unchanged_when_key_set(with_key):
    fc = FilesClient()
    fc._client = MagicMock()
    fc.upload(io.BytesIO(b"synthetic"), "20270101_Synthetic.pdf", "application/pdf")
    fc._client.beta.files.upload.assert_called_once()


# ── upload_document ──────────────────────────────────────────────────────────


async def test_upload_document_refuses_and_stores_nothing(db: Database, no_key):
    from oncofiles.tools.documents import upload_document

    files = MagicMock()
    before = await db.count_documents(patient_id=ERIKA_UUID)

    content = base64.b64encode(b"synthetic pdf bytes").decode()
    result = json.loads(
        await upload_document(_ctx(db, files), content=content, filename="20270101_Synth.pdf")
    )

    assert result["error"] == CLOUD_AI_DISABLED_MESSAGE
    assert await db.count_documents(patient_id=ERIKA_UUID) == before
    files.upload.assert_not_called()


async def test_upload_document_unchanged_when_key_set(db: Database, with_key):
    from oncofiles.tools.documents import upload_document

    files = MagicMock()
    files.upload.return_value = MagicMock(
        id="file_synth_1", mime_type="application/pdf", size_bytes=19
    )
    before = await db.count_documents(patient_id=ERIKA_UUID)

    content = base64.b64encode(b"synthetic pdf bytes").decode()
    with patch("oncofiles.webhook.notify_oncoteam"):
        result = json.loads(
            await upload_document(_ctx(db, files), content=content, filename="20270101_Synth.pdf")
        )

    assert "error" not in result
    files.upload.assert_called_once()
    assert await db.count_documents(patient_id=ERIKA_UUID) == before + 1


# ── user-facing AI tools ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tool_name, kwargs",
    [
        ("enhance_documents", {}),
        ("extract_document_metadata", {"document_id": 1}),
        ("extract_all_metadata", {}),
        ("detect_and_split_documents", {}),
        ("backfill_lab_values_from_metadata", {}),
        ("detect_and_consolidate_documents", {}),
        ("backfill_ai_classification", {}),
        ("detect_and_clone_vaccinations", {}),
    ],
)
async def test_ai_tools_return_clear_message(db: Database, no_key, tool_name, kwargs):
    from oncofiles.tools import enhance_tools

    tool = getattr(enhance_tools, tool_name)
    result = json.loads(await tool(_ctx(db, MagicMock()), **kwargs))
    assert result["error"] == CLOUD_AI_DISABLED_MESSAGE


# ── CLOUD_AI_ENABLED: off by default, the key alone never enables it ─────────


@pytest.fixture
def flag_off_with_key():
    with (
        patch("oncofiles.config.CLOUD_AI_ENABLED", False),
        patch("oncofiles.config.ANTHROPIC_API_KEY", FAKE_KEY),
    ):
        yield


@pytest.fixture
def flag_on_with_key():
    with (
        patch("oncofiles.config.CLOUD_AI_ENABLED", True),
        patch("oncofiles.config.ANTHROPIC_API_KEY", FAKE_KEY),
    ):
        yield


@pytest.mark.parametrize(
    "env_value, expected",
    [
        (None, False),
        ("", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("on", False),
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
    ],
)
def test_cloud_ai_enabled_env_parsing_defaults_off(monkeypatch, env_value, expected):
    """Reload config under a patched env: unset → off; only true/1/yes enable it."""
    import importlib

    from oncofiles import config

    if env_value is None:
        monkeypatch.delenv("CLOUD_AI_ENABLED", raising=False)
    else:
        monkeypatch.setenv("CLOUD_AI_ENABLED", env_value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)  # a key must not flip the default
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)  # ignore any local .env
    try:
        reloaded = importlib.reload(config)
        assert reloaded.CLOUD_AI_ENABLED is expected
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_flag_off_message_is_distinct():
    from oncofiles.cloud_guard import CLOUD_AI_FLAG_OFF_MESSAGE

    assert CLOUD_AI_FLAG_OFF_MESSAGE == (
        "cloud AI is disabled on this server (CLOUD_AI_ENABLED is off)"
    )
    assert CLOUD_AI_FLAG_OFF_MESSAGE != CLOUD_AI_DISABLED_MESSAGE


def test_flag_off_and_no_key_reports_the_flag():
    with (
        patch("oncofiles.config.CLOUD_AI_ENABLED", False),
        patch("oncofiles.config.ANTHROPIC_API_KEY", ""),
        pytest.raises(CloudAIDisabledError, match="CLOUD_AI_ENABLED is off"),
    ):
        enhance._get_client()


def test_get_client_refuses_when_flag_off_even_with_key(flag_off_with_key):
    with pytest.raises(CloudAIDisabledError, match="CLOUD_AI_ENABLED is off"):
        enhance._get_client()
    assert enhance._shared_client is None  # nothing was constructed


def test_get_client_works_when_flag_on_and_key_set(flag_on_with_key):
    client = enhance._get_client()
    assert client is not None
    assert enhance._get_client() is client


def test_files_client_upload_refuses_when_flag_off(flag_off_with_key):
    fc = FilesClient()
    fc._client = MagicMock()
    with pytest.raises(CloudAIDisabledError, match="CLOUD_AI_ENABLED is off"):
        fc.upload(io.BytesIO(b"synthetic"), "20270101_Synthetic.pdf", "application/pdf")
    fc._client.beta.files.upload.assert_not_called()


def test_files_client_explicit_key_does_not_bypass_flag(flag_off_with_key):
    fc = FilesClient(api_key="test")
    fc._client = MagicMock()
    with pytest.raises(CloudAIDisabledError, match="CLOUD_AI_ENABLED is off"):
        fc.upload(io.BytesIO(b"synthetic"), "20270101_Synthetic.pdf", "application/pdf")
    fc._client.beta.files.upload.assert_not_called()


def test_files_client_upload_works_when_flag_on(flag_on_with_key):
    fc = FilesClient()
    fc._client = MagicMock()
    fc.upload(io.BytesIO(b"synthetic"), "20270101_Synthetic.pdf", "application/pdf")
    fc._client.beta.files.upload.assert_called_once()


async def test_upload_document_refuses_when_flag_off_and_stores_nothing(
    db: Database, flag_off_with_key
):
    from oncofiles.cloud_guard import CLOUD_AI_FLAG_OFF_MESSAGE
    from oncofiles.tools.documents import upload_document

    files = MagicMock()
    before = await db.count_documents(patient_id=ERIKA_UUID)

    content = base64.b64encode(b"synthetic pdf bytes").decode()
    result = json.loads(
        await upload_document(_ctx(db, files), content=content, filename="20270101_Synth.pdf")
    )

    assert result == {"error": CLOUD_AI_FLAG_OFF_MESSAGE, "kind": "CloudAIDisabledError"}
    assert await db.count_documents(patient_id=ERIKA_UUID) == before
    files.upload.assert_not_called()


async def test_upload_document_works_when_flag_on(db: Database, flag_on_with_key):
    from oncofiles.tools.documents import upload_document

    files = MagicMock()
    files.upload.return_value = MagicMock(
        id="file_synth_2", mime_type="application/pdf", size_bytes=19
    )
    before = await db.count_documents(patient_id=ERIKA_UUID)

    content = base64.b64encode(b"synthetic pdf bytes").decode()
    with patch("oncofiles.webhook.notify_oncoteam"):
        result = json.loads(
            await upload_document(_ctx(db, files), content=content, filename="20270101_Synth.pdf")
        )

    assert "error" not in result
    files.upload.assert_called_once()
    assert await db.count_documents(patient_id=ERIKA_UUID) == before + 1


async def test_gmail_and_calendar_classifiers_skip_cloud_when_flag_off(flag_off_with_key):
    from oncofiles.calendar_sync import _classify_event_medical
    from oncofiles.gmail_sync import _classify_medical_relevance

    with patch("oncofiles.enhance._get_client") as get_client:
        assert await _classify_medical_relevance("Synthetic subject", "x", "a@b.test") == (
            False,
            0.0,
        )
        is_medical, _, _ = await _classify_event_medical("Synthetic event", "", "")
        assert is_medical is False
    get_client.assert_not_called()
