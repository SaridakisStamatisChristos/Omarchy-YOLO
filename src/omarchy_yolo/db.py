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
    StateTransitionError,
    validate_attempt_transition,
    validate_job_snapshot,
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
    """Durable store whose public mutations enforce the executable state model.

    v1.4.1 moves transition read/validation/write and cross-record snapshot checks
    into one BEGIN IMMEDIATE transaction. Recovery has dedicated multi-record
    transactions, but those validate their origin and final snapshot before commit.
    """

    def _snapshot_locked(self, job_id: str) -> tuple[JobState, tuple[TaskState, ...], bool]:
        job = self._conn.execute(
            "SELECT state, stop_requested FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise KeyError(job_id)
        tasks = self._conn.execute(
            "SELECT state FROM tasks WHERE job_id = ? ORDER BY seq", (job_id,)
        ).fetchall()
        return (
            JobState(str(job["state"])),
            tuple(TaskState(str(row["state"])) for row in tasks),
            bool(job["stop_requested"]),
        )

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
                target_stop = bool(values.get("stop_requested", row["stop_requested"]))
                validate_job_transition(current, target)

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

                _, task_states, _ = self._snapshot_locked(job_id)
                validate_job_snapshot(target, task_states, stop_requested=target_stop)
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

                if "state" in values:
                    values["state"] = target.value
                values["updated_at"] = utc_ts()
                assignments = ", ".join(f"{key} = ?" for key in values)
                params = (*values.values(), task_id, current.value)
                cur = self._conn.execute(
                    f"UPDATE tasks SET {assignments} WHERE id = ? AND state = ?", params
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale task state while updating {task_id}: expected {current.value}"
                    )

                job_state, task_states, stop_requested = self._snapshot_locked(job_id)
                validate_job_snapshot(
                    job_state,
                    task_states,
                    stop_requested=stop_requested,
                )
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
                task = self._conn.execute(
                    "SELECT job_id FROM tasks WHERE id = ?", (task_id,)
                ).fetchone()
                if task is None:
                    raise KeyError(task_id)
                owner = str(task["job_id"])
                if owner != job_id:
                    raise YoloError(
                        f"attempt ownership mismatch: task {task_id} belongs to {owner}, not {job_id}"
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
                    "SELECT state FROM attempts WHERE id = ?", (attempt_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(attempt_id)
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
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
