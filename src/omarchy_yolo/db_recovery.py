from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .db_core import DatabaseCore
from .model import JobState, TaskState
from .state_machine import StateTransitionError, validate_job_snapshot, validate_job_transition

if TYPE_CHECKING:
    from .model import JobRecord
from .util import utc_ts


class RecoveryMixin(DatabaseCore):
    if TYPE_CHECKING:
        def get_job(self, job_id: str) -> JobRecord: ...

        def event(
            self,
            job_id: str,
            kind: str,
            payload: dict[str, Any] | None = None,
            *,
            task_id: str | None = None,
        ) -> int: ...

    def _validate_snapshot_locked(self, job_id: str) -> None:
        job = self._conn.execute(
            "SELECT state, stop_requested FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if job is None:
            raise KeyError(job_id)
        tasks = self._conn.execute(
            "SELECT state FROM tasks WHERE job_id = ? ORDER BY seq", (job_id,)
        ).fetchall()
        validate_job_snapshot(
            JobState(str(job["state"])),
            (TaskState(str(row["state"])) for row in tasks),
            stop_requested=bool(job["stop_requested"]),
        )

    def retry_failed_tasks(self, job_id: str) -> None:
        """Compatibility helper: make all non-completed task states schedulable again."""
        job = self.get_job(job_id)
        if job.state not in {JobState.FAILED, JobState.STOPPED, JobState.QUEUED}:
            raise StateTransitionError(
                f"cannot retry tasks while job is {job.state.value}"
            )
        now = utc_ts()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    UPDATE tasks SET state = ?, updated_at = ?
                    WHERE job_id = ? AND state != ?
                    """,
                    (TaskState.PENDING.value, now, job_id, TaskState.COMPLETED.value),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def settle_inflight(
        self,
        job_id: str,
        *,
        task_state: TaskState,
        attempt_state: str,
        summary: str,
    ) -> None:
        """Durably close in-flight attempts/tasks after stop, interruption, or fatal failure."""
        if task_state not in {TaskState.PENDING, TaskState.FAILED, TaskState.STOPPED}:
            raise ValueError(f"invalid settled task state: {task_state}")
        if attempt_state not in {"cancelled", "failed"}:
            raise ValueError(f"invalid settled attempt state: {attempt_state}")
        bounded_summary = summary[-8_000:]
        now = utc_ts()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if self._conn.execute(
                    "SELECT 1 FROM jobs WHERE id = ?", (job_id,)
                ).fetchone() is None:
                    raise KeyError(job_id)
                self._conn.execute(
                    """
                    UPDATE attempts
                    SET state = ?, finished_at = ?,
                        summary = CASE WHEN summary = '' THEN ? ELSE summary END
                    WHERE job_id = ? AND state = 'running'
                    """,
                    (attempt_state, now, bounded_summary, job_id),
                )
                self._conn.execute(
                    """
                    UPDATE tasks
                    SET state = ?,
                        last_error = CASE WHEN ? = '' THEN last_error ELSE ? END,
                        updated_at = ?
                    WHERE job_id = ? AND state IN (?, ?, ?)
                    """,
                    (
                        task_state.value,
                        bounded_summary,
                        bounded_summary,
                        now,
                        job_id,
                        TaskState.RUNNING.value,
                        TaskState.REVIEWING.value,
                        TaskState.INTEGRATING.value,
                    ),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def prepare_resume(self, job_id: str) -> None:
        """Atomically resume only a durably stopped or failed job."""
        now = utc_ts()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                current = JobState(str(row["state"]))
                if current not in {JobState.STOPPED, JobState.FAILED}:
                    raise StateTransitionError(
                        f"resume requires stopped or failed job, got {current.value}"
                    )
                validate_job_transition(current, JobState.QUEUED)
                self._conn.execute(
                    """
                    UPDATE attempts
                    SET state = 'cancelled', finished_at = ?,
                        summary = CASE
                          WHEN summary = '' THEN 'cancelled before explicit resume'
                          ELSE summary
                        END
                    WHERE job_id = ? AND state = 'running'
                    """,
                    (now, job_id),
                )
                self._conn.execute(
                    """
                    UPDATE tasks SET state = ?, updated_at = ?
                    WHERE job_id = ? AND state != ?
                    """,
                    (TaskState.PENDING.value, now, job_id, TaskState.COMPLETED.value),
                )
                cur = self._conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, stop_requested = 0, error = '', updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (JobState.QUEUED.value, now, job_id, current.value),
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale job state while resuming {job_id}"
                    )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        self.event(job_id, "job.resumed")

    def recover_incomplete(self) -> list[str]:
        """Recover interrupted jobs while preserving an explicit stop across daemon restarts."""
        terminal = (JobState.COMPLETED.value, JobState.FAILED.value, JobState.STOPPED.value)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, state, stop_requested FROM jobs WHERE state NOT IN (?, ?, ?)", terminal
            ).fetchall()
            if not rows:
                return []

            active_rows = [
                row
                for row in rows
                if not bool(row["stop_requested"])
                and str(row["state"]) != JobState.STOPPING.value
            ]
            stopped_rows = [row for row in rows if row not in active_rows]
            active_ids = [str(row["id"]) for row in active_rows]
            stopped_ids = [str(row["id"]) for row in stopped_rows]

            for row in active_rows:
                validate_job_transition(JobState(str(row["state"])), JobState.QUEUED)
            for row in stopped_rows:
                # Recovery may observe either half of a stop request: the durable
                # boolean may be set before the state reaches STOPPING, or STOPPING
                # may have been committed just before interruption. Model every
                # such case through the explicit STOPPING state, then settle it to
                # STOPPED inside one recovery transaction.
                source = JobState(str(row["state"]))
                if source != JobState.STOPPING:
                    validate_job_transition(source, JobState.STOPPING)
                validate_job_transition(JobState.STOPPING, JobState.STOPPED)

            self._conn.execute("BEGIN IMMEDIATE")
            try:
                now = utc_ts()
                if active_ids:
                    placeholders = ",".join("?" for _ in active_ids)
                    self._conn.execute(
                        f"""
                        UPDATE tasks SET state = ?, updated_at = ?
                        WHERE job_id IN ({placeholders})
                          AND state IN (?, ?, ?)
                        """,
                        (
                            TaskState.PENDING.value,
                            now,
                            *active_ids,
                            TaskState.RUNNING.value,
                            TaskState.REVIEWING.value,
                            TaskState.INTEGRATING.value,
                        ),
                    )
                    self._conn.execute(
                        f"""
                        UPDATE attempts
                        SET state = 'cancelled', finished_at = ?,
                            summary = CASE
                              WHEN summary = '' THEN 'interrupted by daemon restart'
                              ELSE summary
                            END
                        WHERE job_id IN ({placeholders}) AND state = 'running'
                        """,
                        (now, *active_ids),
                    )
                    self._conn.execute(
                        f"UPDATE jobs SET state = ?, stop_requested = 0, updated_at = ? "
                        f"WHERE id IN ({placeholders})",
                        (JobState.QUEUED.value, now, *active_ids),
                    )

                if stopped_ids:
                    placeholders = ",".join("?" for _ in stopped_ids)
                    self._conn.execute(
                        f"""
                        UPDATE tasks SET state = ?, updated_at = ?
                        WHERE job_id IN ({placeholders})
                          AND state IN (?, ?, ?)
                        """,
                        (
                            TaskState.STOPPED.value,
                            now,
                            *stopped_ids,
                            TaskState.RUNNING.value,
                            TaskState.REVIEWING.value,
                            TaskState.INTEGRATING.value,
                        ),
                    )
                    self._conn.execute(
                        f"""
                        UPDATE attempts
                        SET state = 'cancelled', finished_at = ?,
                            summary = CASE
                              WHEN summary = '' THEN 'interrupted while stop was requested'
                              ELSE summary
                            END
                        WHERE job_id IN ({placeholders}) AND state = 'running'
                        """,
                        (now, *stopped_ids),
                    )
                    self._conn.execute(
                        f"UPDATE jobs SET state = ?, stop_requested = 1, updated_at = ? "
                        f"WHERE id IN ({placeholders})",
                        (JobState.STOPPED.value, now, *stopped_ids),
                    )

                for job_id in (*active_ids, *stopped_ids):
                    self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        for recovered_id in active_ids:
            self.event(recovered_id, "job.recovered")
        for stopped_id in stopped_ids:
            self.event(stopped_id, "job.recovered_stopped")
        return active_ids
