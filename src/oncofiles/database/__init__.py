"""Database package — composed from domain-specific mixins.

Usage unchanged: ``from oncofiles.database import Database``
"""

from __future__ import annotations

from ._base import DatabaseBase
from ._clinical import ClinicalMixin
from ._conversations import ConversationMixin
from ._documents import DocumentMixin
from ._operational import OperationalMixin


class Database(
    DocumentMixin,
    ConversationMixin,
    ClinicalMixin,
    OperationalMixin,
    DatabaseBase,
):
    """Async database for document metadata. Uses aiosqlite locally, Turso in cloud."""


__all__ = ["Database"]
