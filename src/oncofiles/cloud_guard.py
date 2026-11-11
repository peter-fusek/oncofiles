"""Local-only zones: code inside ``local_only()`` can never reach the Anthropic API.

Policy (maintainer, 2026-11-09): background processing runs only on the local
model; the Anthropic cloud is used only when the user works with documents in
Claude.ai. Background jobs that process documents (curated index, fact
extraction #603, data backfill #619) run inside ``local_only()``. The shared
Anthropic client (``enhance._get_client``) and the Files API client refuse to
work there — fail closed, no fallback. The flag is a ContextVar, so it follows
``asyncio.to_thread`` into worker threads.

The cloud is OFF by default: it needs ``CLOUD_AI_ENABLED`` switched on AND an
``ANTHROPIC_API_KEY`` (#618, privacy text #699). Otherwise the same two
chokepoints raise ``CloudAIDisabledError`` with a clear message instead of
calling the API (or failing with the SDK's bare ``TypeError``). A key alone
never enables it.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_local_only: ContextVar[str | None] = ContextVar("oncofiles_local_only", default=None)


class CloudForbiddenError(RuntimeError):
    """An Anthropic API call was attempted inside a local-only job."""


CLOUD_AI_DISABLED_MESSAGE = "cloud AI is disabled on this server (no ANTHROPIC_API_KEY)"
CLOUD_AI_FLAG_OFF_MESSAGE = "cloud AI is disabled on this server (CLOUD_AI_ENABLED is off)"


class CloudAIDisabledError(CloudForbiddenError):
    """Cloud AI is off on this server (flag off or no key), so no Anthropic call is made (#618).

    A configuration state, not a transient failure: never retry on it.
    """

    def __init__(self, message: str = CLOUD_AI_DISABLED_MESSAGE) -> None:
        super().__init__(message)


def cloud_ai_disabled_reason(api_key: str | None = None) -> str | None:
    """Why cloud AI is unavailable right now, or None when it may be used.

    Reads ``config.CLOUD_AI_ENABLED`` and ``config.ANTHROPIC_API_KEY`` at call
    time. The flag is checked first: with it off, a key changes nothing.
    ``api_key`` is an explicit key handed to a client (e.g.
    ``FilesClient(api_key=...)``); it can stand in for the server key but never
    for the flag.
    """
    from oncofiles import config

    if not config.CLOUD_AI_ENABLED:
        return CLOUD_AI_FLAG_OFF_MESSAGE
    if not (api_key or config.ANTHROPIC_API_KEY):
        return CLOUD_AI_DISABLED_MESSAGE
    return None


def cloud_ai_enabled() -> bool:
    """True when CLOUD_AI_ENABLED is on AND the server has a key. Read at call time."""
    return cloud_ai_disabled_reason() is None


def require_cloud_ai(api_key: str | None = None) -> None:
    """Raise ``CloudAIDisabledError`` unless the flag is on and a key is available."""
    reason = cloud_ai_disabled_reason(api_key)
    if reason is not None:
        raise CloudAIDisabledError(reason)


@contextmanager
def local_only(job: str):
    token = _local_only.set(job)
    try:
        yield
    finally:
        _local_only.reset(token)


def check_cloud_allowed(what: str) -> None:
    job = _local_only.get()
    if job:
        raise CloudForbiddenError(
            f"{what}: '{job}' runs on the local model only — the Anthropic API is "
            "not available to it (policy 2026-11-09)"
        )
