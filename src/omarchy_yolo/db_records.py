from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .db_core import MAX_EVENT_LIST_LIMIT, MAX_EVENT_PAYLOAD_CHARS, MAX_JOB_LIST_LIMIT, DatabaseCore
from .model import JobRecord, JobState, PlannedTask, TaskRecord, TaskState
from .state_machine import StateTransitionError
from .util import json_dumps, new_id, utc_ts


class RecordsMixin(DatabaseCore):
    def create_job(
        self,
        *,
        repo: str,
        goal: str,
        base_branch: str,
        base_commit: str,
        integration_branch: str,
        auto_apply: bool,
    ) -> JobRecord:
        now = utc_ts()
        job_id = new_id("job")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.execute(
                    """
                    INSERT INTO jobs(
                      id, repo, goal, state, base_branch, base_commit,
                      integration_branch, auto_apply, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        repo,
                        goal,
                        JobState.QUEUED.value,
                        base_branch,
                        base_commit,
                        integration_branch,
                        int(auto_apply),
                        now,
                        now,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (job_id, "job.created", json_dumps({"goal": goal, "repo": repo}), now),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        row = self._fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise KeyError(job_id)
        return self._job_from_row(row)

    def latest_job(self) -> JobRecord | None:
        row = self._fetchone("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1")
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        bounded = min(MAX_JOB_LIST_LIMIT, max(1, limit))
        rows = self._fetchall(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (bounded,)
        )
        return [self._job_from_row(row) for row in rows]

    def add_tasks(self, job_id: str, planned: Iterable[PlannedTask]) -> list[TaskRecord]:
        planned_tasks = tuple(planned)
        now = utc_ts()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._conn.execute(
                    "SELECT state, acceptance_phase FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                state = JobState(str(job["state"]))
                phase = str(job["acceptance_phase"])
                if state not in {JobState.QUEUED, JobState.PLANNING}:
                    raise StateTransitionError(
                        f"tasks can only be added while job is queued/planning, got {state.value}"
                    )
                if phase != "none":
                    raise StateTransitionError(
                        f"tasks cannot be added after acceptance begins ({phase})"
                    )
                seq_row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) AS seq FROM tasks WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                start_seq = int(seq_row["seq"]) if seq_row is not None else 0
                rows: list[tuple[Any, ...]] = []
                for offset, task in enumerate(planned_tasks, start=1):
                    rows.append(
                        (
                            new_id("task"),
                            job_id,
                            start_seq + offset,
                            task.logical_id,
                            task.title,
                            task.description,
                            TaskState.PENDING.value,
                            json_dumps(list(task.depends_on)),
                            json_dumps(list(task.acceptance)),
                            task.preferred_agent,
                            now,
                            now,
                        )
                    )
                if rows:
                    self._conn.executemany(
                        """
                        INSERT INTO tasks(
                          id, job_id, seq, logical_id, title, description, state,
                          dependencies, acceptance, preferred_agent, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        rows,
                    )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.list_tasks(job_id)

    def list_tasks(self, job_id: str) -> list[TaskRecord]:
        rows = self._fetchall(
            "SELECT * FROM tasks WHERE job_id = ? ORDER BY seq", (job_id,)
        )
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord:
        row = self._fetchone("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def get_task_by_logical_id(self, job_id: str, logical_id: str) -> TaskRecord:
        row = self._fetchone(
            "SELECT * FROM tasks WHERE job_id = ? AND logical_id = ?", (job_id, logical_id)
        )
        if row is None:
            raise KeyError((job_id, logical_id))
        return self._task_from_row(row)

    def event(
        self,
        job_id: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> int:
        encoded_payload = json_dumps(payload or {})
        if len(encoded_payload) > MAX_EVENT_PAYLOAD_CHARS:
            encoded_payload = json_dumps(
                {
                    "truncated": True,
                    "preview": encoded_payload[: MAX_EVENT_PAYLOAD_CHARS - 100],
                }
            )
        cur = self._execute(
            "INSERT INTO events(job_id, task_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, task_id, kind[:128], encoded_payload, utc_ts()),
        )
        rowid = cur.lastrowid
        if rowid is None:
            raise RuntimeError("SQLite did not return an event row id")
        return rowid

    def events(self, job_id: str, *, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        bounded = min(MAX_EVENT_LIST_LIMIT, max(1, limit))
        rows = self._fetchall(
            """
            SELECT id, job_id, task_id, kind, payload, created_at
            FROM events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?
            """,
            (job_id, max(0, after_id), bounded),
        )
        return [self._event_from_row(row) for row in rows]

    @staticmethod
    def _event_from_row(row: Any) -> dict[str, Any]:
        return {
            "id": int(row["id"]),
            "job_id": row["job_id"],
            "task_id": row["task_id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "created_at": float(row["created_at"]),
        }

    def recent_events(self, job_id: str, *, limit: int = 12) -> list[dict[str, Any]]:
        bounded = min(MAX_EVENT_LIST_LIMIT, max(1, limit))
        rows = self._fetchall(
            """
            SELECT id, job_id, task_id, kind, payload, created_at
            FROM events WHERE job_id = ? ORDER BY id DESC LIMIT ?
            """,
            (job_id, bounded),
        )
        rows.reverse()
        return [self._event_from_row(row) for row in rows]

    def attempt_metrics(self, job_id: str) -> dict[str, Any]:
        now = utc_ts()
        rows = self._fetchall(
            """
            SELECT agent, state, COUNT(*) AS attempts,
                   SUM(MAX(0.0, COALESCE(finished_at, ?) - started_at)) AS elapsed_seconds,
                   SUM(
                     CASE WHEN finished_at IS NULL THEN 0.0
                          ELSE MAX(0.0, finished_at - started_at)
                     END
                   ) AS completed_seconds
            FROM attempts WHERE job_id = ?
            GROUP BY agent, state
            """,
            (now, job_id),
        )
        states = {"running": 0, "passed": 0, "failed": 0, "cancelled": 0}
        by_agent: dict[str, dict[str, Any]] = {}
        elapsed_total = 0.0
        completed_total = 0.0
        attempts_total = 0
        for row in rows:
            agent = str(row["agent"])
            state = str(row["state"])
            attempts = int(row["attempts"])
            elapsed = float(row["elapsed_seconds"] or 0.0)
            completed = float(row["completed_seconds"] or 0.0)
            if state not in states:
                states[state] = 0
            states[state] += attempts
            attempts_total += attempts
            elapsed_total += elapsed
            completed_total += completed
            agent_metrics = by_agent.setdefault(
                agent,
                {
                    "attempts": 0,
                    "running": 0,
                    "passed": 0,
                    "failed": 0,
                    "cancelled": 0,
                    "elapsed_seconds": 0.0,
                },
            )
            agent_metrics["attempts"] += attempts
            agent_metrics[state] = int(agent_metrics.get(state, 0)) + attempts
            agent_metrics["elapsed_seconds"] = (
                float(agent_metrics["elapsed_seconds"]) + elapsed
            )
        for metrics in by_agent.values():
            metrics["elapsed_seconds"] = round(float(metrics["elapsed_seconds"]), 3)
        return {
            "attempts_total": attempts_total,
            "states": states,
            "elapsed_seconds_total": round(elapsed_total, 3),
            "completed_seconds_total": round(completed_total, 3),
            "by_agent": dict(sorted(by_agent.items())),
        }

    def latest_attempts(self, job_id: str) -> dict[str, dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT a.task_id, a.number, a.agent, a.state, a.started_at, a.finished_at
            FROM attempts AS a
            JOIN (
              SELECT task_id, MAX(number) AS number
              FROM attempts WHERE job_id = ? GROUP BY task_id
            ) AS latest ON latest.task_id = a.task_id AND latest.number = a.number
            WHERE a.job_id = ?
            """,
            (job_id, job_id),
        )
        now = utc_ts()
        return {
            str(row["task_id"]): {
                "number": int(row["number"]),
                "agent": str(row["agent"]),
                "state": str(row["state"]),
                "started_at": float(row["started_at"]),
                "finished_at": (
                    float(row["finished_at"]) if row["finished_at"] is not None else None
                ),
                "elapsed_seconds": round(
                    max(
                        0.0,
                        (
                            float(row["finished_at"])
                            if row["finished_at"] is not None
                            else now
                        )
                        - float(row["started_at"]),
                    ),
                    3,
                ),
            }
            for row in rows
        }

    def last_event_id(self, job_id: str) -> int:
        row = self._fetchone(
            "SELECT COALESCE(MAX(id), 0) AS id FROM events WHERE job_id = ?", (job_id,)
        )
        return int(row["id"]) if row is not None else 0

    def request_stop(self, job_id: str) -> None:
        self.update_job(job_id, stop_requested=True, state=JobState.STOPPING)  # type: ignore[attr-defined]
        self.event(job_id, "job.stop_requested")

    def clear_stop(self, job_id: str) -> None:
        self.update_job(  # type: ignore[attr-defined]
            job_id,
            stop_requested=False,
            state=JobState.QUEUED,
            error="",
        )
        self.event(job_id, "job.resumed")
