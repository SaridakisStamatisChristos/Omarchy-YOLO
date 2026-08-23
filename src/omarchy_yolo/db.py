from __future__ import annotations

from typing import Any

from .db_core import (
    CURRENT_SCHEMA_VERSION,
    MAX_EVENT_LIST_LIMIT,
    MAX_EVENT_PAYLOAD_CHARS,
    MAX_JOB_LIST_LIMIT,
)
from .db_provenance import ProvenanceMixin
from .db_records import RecordsMixin
from .db_recovery import RecoveryMixin
from .model import AttemptState, JobState, TaskState
from .state_machine import (
    validate_attempt_transition,
    validate_job_transition,
    validate_task_transition,
)

__all__ = [
    "Database",
    "CURRENT_SCHEMA_VERSION",
    "MAX_EVENT_LIST_LIMIT",
    "MAX_EVENT_PAYLOAD_CHARS",
    "MAX_JOB_LIST_LIMIT",
]


class Database(RecordsMixin, RecoveryMixin, ProvenanceMixin):
    """Durable store with a single validated state-transition boundary.

    RecoveryMixin intentionally performs multi-record recovery with direct SQL so its
    transaction can move attempts/tasks/jobs atomically. Ordinary mutations pass
    through these overrides and therefore cannot bypass the executable reference
    state machine.
    """

    def update_job(self, job_id: str, **fields: Any) -> None:
        state = fields.get("state")
        if state is not None:
            validate_job_transition(self.get_job(job_id).state, JobState(state))
        super().update_job(job_id, **fields)

    def update_task(self, task_id: str, **fields: Any) -> None:
        state = fields.get("state")
        if state is not None:
            validate_task_transition(self.get_task(task_id).state, TaskState(state))
        super().update_task(task_id, **fields)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        returncode: int | None,
        summary: str,
    ) -> None:
        row = self._fetchone("SELECT state FROM attempts WHERE id = ?", (attempt_id,))
        if row is None:
            raise KeyError(attempt_id)
        validate_attempt_transition(
            AttemptState(str(row["state"])),
            AttemptState(state),
        )
        super().finish_attempt(
            attempt_id,
            state=state,
            returncode=returncode,
            summary=summary,
        )
