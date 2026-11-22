"""Prompt hash for ``prompt_log`` rows (#441 Layer 5).

``prompt_logger`` stores ``compute_prompt_hash`` in ``prompt_log.prompt_hash``
(migration 069) so identical AI calls can be counted. The lookup that once
answered a repeated call from the stored response
(``maybe_get_cached_response``) and its hit logger never had a caller and were
removed in #930; rows with ``status='dedup_hit'`` may still exist from before.
"""

from __future__ import annotations

import hashlib


def compute_prompt_hash(system_prompt: str, user_prompt: str, model: str) -> str:
    """Stable hash of the inputs that determine the AI response.

    Uses SHA-256 to avoid collision concerns at any realistic scale
    (we'd need ~2^128 calls before the first collision is likely).
    """
    h = hashlib.sha256()
    h.update(b"system\x00")
    h.update(system_prompt.encode("utf-8", errors="replace"))
    h.update(b"\x01user\x00")
    h.update(user_prompt.encode("utf-8", errors="replace"))
    h.update(b"\x01model\x00")
    h.update(model.encode("utf-8", errors="replace"))
    return h.hexdigest()
