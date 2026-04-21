"""Database package — composed from domain-specific mixins.

Usage unchanged: ``from oncofiles.database import Database``
"""

from __future__ import annotations

from ._analytics import AnalyticsMixin
from ._base import DatabaseBase
from ._calendar import CalendarMixin
from ._clinical import ClinicalMixin
from ._clinical_records import ClinicalRecordsMixin
from ._conversations import ConversationMixin
from ._documents import DocumentMixin
from ._gmail import GmailMixin
from ._operational import OperationalMixin
from ._patients import PatientsMixin
from ._prompt_log import PromptLogMixin


class Database(
    DocumentMixin,
    ConversationMixin,
    ClinicalMixin,
    ClinicalRecordsMixin,
    GmailMixin,
    CalendarMixin,
    OperationalMixin,
    PatientsMixin,
    PromptLogMixin,
    AnalyticsMixin,
    DatabaseBase,
):
    """Async database for document metadata. Uses aiosqlite locally, Turso in cloud."""


__all__ = ["Database"]
