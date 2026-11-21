"""Canonical form of an e-mail address for identity matching (#950 phase 2).

Gmail ignores dots in the local part and everything after ``+``, and
``googlemail.com`` is the same service as ``gmail.com``: ``jana.example+x@gmail.com``,
``janaexample@gmail.com`` and ``Jana.Example@googlemail.com`` are ONE mailbox.
:func:`canonical_email` maps all of them to ``janaexample@gmail.com``.

Only Gmail addresses are rewritten. Any other address is only stripped and
lowercased: other providers do not promise that dots or ``+tags`` are
ignored, so treating such variants as one person would widen an ACL.
"""

from __future__ import annotations

GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})


def canonical_email(address: str | None) -> str:
    """The canonical form ('' for an empty or malformed address)."""
    addr = (address or "").strip().lower()
    local, at, domain = addr.rpartition("@")
    if not at or not local or not domain:
        return ""
    domain = domain.rstrip(".")
    if domain in GMAIL_DOMAINS:
        local = local.split("+", 1)[0].replace(".", "")
        if not local:
            return ""
        return f"{local}@gmail.com"
    return f"{local}@{domain}"


def is_gmail(address: str | None) -> bool:
    return canonical_email(address).endswith("@gmail.com")
