from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .db_core import DatabaseCore
from .model import JobState, TaskState

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

    def retry_failed_tasks(self, job_id: str) -> None:
        """Compatibility helper: make all non-completed task states schedulable again."""
        now = utc_ts()
        self._execute(
            """
            UPDATE tasks SET state = ?, updated_at = ?
            WHERE job_id = ? AND state != ?
            """,
            (TaskState.PENDING.value, now, job_id, TaskState.COMPLETED.value),
        )

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
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def prepare_resume(self, job_id: str) -> None:
        """Atomically close stale attempts and make a stopped/failed job schedulable."""
        self.get_job(job_id)
        now = utc_ts()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
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
                self._conn.execute(
                    """
                    UPDATE jobs SET state = ?, stop_requested = 0, error = '', updated_at = ?
                    WHERE id = ?
                    """,
                    (JobState.QUEUED.value, now, job_id),
                )
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
            active_ids = [
                str(row["id"])
                for row in rows
                if not bool(row["stop_requested"]) and str(row["state"]) != JobState.STOPPING.value
            ]
            stopped_ids = [str(row["id"]) for row in rows if str(row["id"]) not in active_ids]
            if not rows:
                return []

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
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        for recovered_id in active_ids:
            self.event(recovered_id, "job.recovered")
        for stopped_id in stopped_ids:
            self.event(stopped_id, "job.recovered_stopped")
        return active_ids
