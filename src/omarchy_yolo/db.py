from __future__ import annotations

from .db_core import MAX_EVENT_LIST_LIMIT, MAX_EVENT_PAYLOAD_CHARS, MAX_JOB_LIST_LIMIT
from .db_records import RecordsMixin
from .db_recovery import RecoveryMixin

__all__ = [
    "Database",
    "MAX_EVENT_LIST_LIMIT",
    "MAX_EVENT_PAYLOAD_CHARS",
    "MAX_JOB_LIST_LIMIT",
]


class Database(RecordsMixin, RecoveryMixin):
    """Durable SQLite store composed from focused record/recovery modules."""

    pass
