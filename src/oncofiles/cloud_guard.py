"""Local-only zones: code inside ``local_only()`` can never reach the Anthropic API.

Policy (maintainer, 2026-11-09): background processing runs only on the local
model; the Anthropic cloud is used only when the user works with documents in
Claude.ai. Background jobs that process documents (curated index, fact
extraction #603, data backfill #619) run inside ``local_only()``. The shared
Anthropic client (``enhance._get_client``) and the Files API client refuse to
work there — fail closed, no fallback. The flag is a ContextVar, so it follows
``asyncio.to_thread`` into worker threads.

A server without ``ANTHROPIC_API_KEY`` has the cloud disabled altogether
(#618): the same two chokepoints raise ``CloudAIDisabledError`` with a clear
message instead of the SDK's bare ``TypeError`` on the first API call.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_local_only: ContextVar[str | None] = ContextVar("oncofiles_local_only", default=None)


class CloudForbiddenError(RuntimeError):
    """An Anthropic API call was attempted inside a local-only job."""


CLOUD_AI_DISABLED_MESSAGE = "cloud AI is disabled on this server (no ANTHROPIC_API_KEY)"


class CloudAIDisabledError(CloudForbiddenError):
    """The server has no ANTHROPIC_API_KEY, so no Anthropic API call can be made (#618).

    A configuration state, not a transient failure: never retry on it.
    """

    def __init__(self, message: str = CLOUD_AI_DISABLED_MESSAGE) -> None:
        super().__init__(message)


def cloud_ai_enabled() -> bool:
    """True when the server has an Anthropic API key. Read at call time."""
    from oncofiles import config

    return bool(config.ANTHROPIC_API_KEY)


def require_cloud_ai(api_key: str | None = None) -> None:
    """Raise ``CloudAIDisabledError`` unless a key is available.

    ``api_key`` is an explicit key handed to a client (e.g. ``FilesClient(api_key=...)``);
    without one the server-wide ``config.ANTHROPIC_API_KEY`` is read now.
    """
    if not (api_key or cloud_ai_enabled()):
        raise CloudAIDisabledError()


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
