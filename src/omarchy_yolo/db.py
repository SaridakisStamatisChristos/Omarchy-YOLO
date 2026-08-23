from __future__ import annotations

from .db_core import (
    CURRENT_SCHEMA_VERSION,
    MAX_EVENT_LIST_LIMIT,
    MAX_EVENT_PAYLOAD_CHARS,
    MAX_JOB_LIST_LIMIT,
)
from .db_provenance import ProvenanceMixin
from .db_records import RecordsMixin
from .db_recovery import RecoveryMixin

__all__ = [
    "Database",
    "CURRENT_SCHEMA_VERSION",
    "MAX_EVENT_LIST_LIMIT",
    "MAX_EVENT_PAYLOAD_CHARS",
    "MAX_JOB_LIST_LIMIT",
]


class Database(RecordsMixin, RecoveryMixin, ProvenanceMixin):
    """Durable SQLite store composed from focused record/recovery modules."""

    pass
