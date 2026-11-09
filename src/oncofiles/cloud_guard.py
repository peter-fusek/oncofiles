"""Local-only zones: code inside ``local_only()`` can never reach the Anthropic API.

Policy (maintainer, 2026-11-09): background processing runs only on the local
model; the Anthropic cloud is used only when the user works with documents in
Claude.ai. Background jobs that process documents (curated index, fact
extraction #603, data backfill #619) run inside ``local_only()``. The shared
Anthropic client (``enhance._get_client``) and the Files API client refuse to
work there — fail closed, no fallback. The flag is a ContextVar, so it follows
``asyncio.to_thread`` into worker threads.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar

_local_only: ContextVar[str | None] = ContextVar("oncofiles_local_only", default=None)


class CloudForbiddenError(RuntimeError):
    """An Anthropic API call was attempted inside a local-only job."""


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
