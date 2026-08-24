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
    INFLIGHT_TASK_STATES,
    StateTransitionError,
    validate_attempt_transition,
    validate_job_transition,
    validate_task_transition,
)
from .util import YoloError, new_id, utc_ts

__all__ = [
    "Database",
    "CURRENT_SCHEMA_VERSION",
    "MAX_EVENT_LIST_LIMIT",
    "MAX_EVENT_PAYLOAD_CHARS",
    "MAX_JOB_LIST_LIMIT",
]

_JOB_FIELDS = {
    "state",
    "integration_path",
    "stop_requested",
    "final_summary",
    "error",
    "auto_apply",
}
_TASK_FIELDS = {
    "state",
    "attempts",
    "branch",
    "worktree",
    "base_commit",
    "last_error",
    "result_summary",
    "preferred_agent",
}


class Database(RecordsMixin, RecoveryMixin, ProvenanceMixin):
    """Durable store whose public mutations enforce the executable state model."""

    def update_job(self, job_id: str, **fields: Any) -> None:
        unknown = set(fields) - _JOB_FIELDS
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        if not fields:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state, stop_requested FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                current = JobState(str(row["state"]))
                values: dict[str, Any] = dict(fields)
                target = JobState(values.get("state", current))
                validate_job_transition(current, target)
                if target == JobState.COMPLETED and current != JobState.COMPLETED:
                    raise StateTransitionError(
                        "completed jobs must be published through publish_completed_job"
                    )

                values["state"] = target.value if "state" in values else current.value
                for key in ("stop_requested", "auto_apply"):
                    if key in values:
                        values[key] = int(bool(values[key]))
                values["updated_at"] = utc_ts()
                assignments = ", ".join(f"{key} = ?" for key in values)
                params = (*values.values(), job_id, current.value)
                cur = self._conn.execute(
                    f"UPDATE jobs SET {assignments} WHERE id = ? AND state = ?", params
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale job state while updating {job_id}: expected {current.value}"
                    )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def update_task(self, task_id: str, **fields: Any) -> None:
        unknown = set(fields) - _TASK_FIELDS
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if not fields:
            return
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT job_id, state FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                job_id = str(row["job_id"])
                current = TaskState(str(row["state"]))
                values: dict[str, Any] = dict(fields)
                target = TaskState(values.get("state", current))
                validate_task_transition(current, target)

                now = utc_ts()
                job_row = self._conn.execute(
                    "SELECT state, acceptance_phase FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job_row is None:
                    raise KeyError(job_id)
                job_state = JobState(str(job_row["state"]))
                acceptance_phase = str(job_row["acceptance_phase"])
                if acceptance_phase != "none":
                    raise StateTransitionError(
                        f"task graph is immutable after acceptance begins ({acceptance_phase})"
                    )

                if target in INFLIGHT_TASK_STATES and job_state in {
                    JobState.QUEUED,
                    JobState.PLANNING,
                }:
                    validate_job_transition(job_state, JobState.RUNNING)
                    job_cur = self._conn.execute(
                        """
                        UPDATE jobs SET state = ?, updated_at = ?
                        WHERE id = ? AND state = ?
                        """,
                        (JobState.RUNNING.value, now, job_id, job_state.value),
                    )
                    if job_cur.rowcount != 1:
                        raise StateTransitionError(
                            f"stale job state while activating task {task_id}"
                        )

                if target == TaskState.COMPLETED and current != TaskState.COMPLETED:
                    running_attempt = self._conn.execute(
                        "SELECT 1 FROM attempts WHERE task_id = ? AND state = 'running' LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if running_attempt is not None:
                        raise StateTransitionError(
                            "task cannot complete while an owning attempt is still running"
                        )

                if "state" in values:
                    values["state"] = target.value
                values["updated_at"] = now
                assignments = ", ".join(f"{key} = ?" for key in values)
                params = (*values.values(), task_id, current.value)
                cur = self._conn.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ? AND state = ?", params
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale task state while updating {task_id}: expected {current.value}"
                    )

                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def start_attempt(
        self,
        *,
        job_id: str,
        task_id: str,
        number: int,
        agent: str,
        worktree: str,
        branch: str,
        log_path: str,
    ) -> str:
        attempt_id = new_id("attempt")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT t.job_id AS owner_job_id, t.state AS task_state,
                           j.state AS job_state, j.acceptance_phase AS acceptance_phase
                    FROM tasks AS t
                    JOIN jobs AS j ON j.id = t.job_id
                    WHERE t.id = ?
                    """,
                    (task_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(task_id)
                owner = str(row["owner_job_id"])
                if owner != job_id:
                    raise YoloError(
                        f"attempt ownership mismatch: task {task_id} belongs to {owner}, not {job_id}"
                    )
                job_state = JobState(str(row["job_state"]))
                task_state = TaskState(str(row["task_state"]))
                acceptance_phase = str(row["acceptance_phase"])
                if job_state != JobState.RUNNING:
                    raise StateTransitionError(
                        f"attempt start requires running job, got {job_state.value}"
                    )
                if task_state != TaskState.RUNNING:
                    raise StateTransitionError(
                        f"attempt start requires running task, got {task_state.value}"
                    )
                if acceptance_phase != "none":
                    raise StateTransitionError(
                        f"attempt cannot start after acceptance begins ({acceptance_phase})"
                    )
                self._conn.execute(
                    """
                    INSERT INTO attempts(
                      id, job_id, task_id, number, agent, state, worktree, branch,
                      log_path, started_at
                    ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        job_id,
                        task_id,
                        number,
                        agent,
                        worktree,
                        branch,
                        log_path,
                        utc_ts(),
                    ),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        returncode: int | None,
        summary: str,
    ) -> None:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT job_id, state FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(attempt_id)
                job_id = str(row["job_id"])
                current = AttemptState(str(row["state"]))
                target = AttemptState(state)
                validate_attempt_transition(current, target)
                cur = self._conn.execute(
                    """
                    UPDATE attempts
                    SET state = ?, returncode = ?, summary = ?, finished_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        target.value,
                        returncode,
                        summary,
                        utc_ts(),
                        attempt_id,
                        current.value,
                    ),
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale attempt state while updating {attempt_id}: expected {current.value}"
                    )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
