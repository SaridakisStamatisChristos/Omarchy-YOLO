from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .db_core import MAX_EVENT_LIST_LIMIT, MAX_EVENT_PAYLOAD_CHARS, MAX_JOB_LIST_LIMIT, DatabaseCore
from .model import JobRecord, JobState, PlannedTask, TaskRecord, TaskState
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
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        row = self._execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._job_from_row(row)

    def latest_job(self) -> JobRecord | None:
        row = self._execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        bounded = min(MAX_JOB_LIST_LIMIT, max(1, limit))
        rows = self._execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (bounded,)
        ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def update_job(self, job_id: str, **fields: Any) -> None:
        allowed = {
            "state",
            "integration_path",
            "stop_requested",
            "final_summary",
            "error",
            "auto_apply",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported job fields: {sorted(unknown)}")
        if not fields:
            return
        values: dict[str, Any] = dict(fields)
        if isinstance(values.get("state"), JobState):
            values["state"] = values["state"].value
        for key in ("stop_requested", "auto_apply"):
            if key in values:
                values[key] = int(bool(values[key]))
        values["updated_at"] = utc_ts()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = (*values.values(), job_id)
        self._execute(f"UPDATE jobs SET {assignments} WHERE id = ?", params)

    def add_tasks(self, job_id: str, planned: Iterable[PlannedTask]) -> list[TaskRecord]:
        now = utc_ts()
        rows: list[tuple[Any, ...]] = []
        for seq, task in enumerate(planned, start=1):
            rows.append(
                (
                    new_id("task"),
                    job_id,
                    seq,
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
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    """
                    INSERT INTO tasks(
                      id, job_id, seq, logical_id, title, description, state,
                      dependencies, acceptance, preferred_agent, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.list_tasks(job_id)

    def list_tasks(self, job_id: str) -> list[TaskRecord]:
        rows = self._execute(
            "SELECT * FROM tasks WHERE job_id = ? ORDER BY seq", (job_id,)
        ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def get_task(self, task_id: str) -> TaskRecord:
        row = self._execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def get_task_by_logical_id(self, job_id: str, logical_id: str) -> TaskRecord:
        row = self._execute(
            "SELECT * FROM tasks WHERE job_id = ? AND logical_id = ?", (job_id, logical_id)
        ).fetchone()
        if row is None:
            raise KeyError((job_id, logical_id))
        return self._task_from_row(row)

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "state",
            "attempts",
            "branch",
            "worktree",
            "base_commit",
            "last_error",
            "result_summary",
            "preferred_agent",
        }
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unsupported task fields: {sorted(unknown)}")
        if not fields:
            return
        values: dict[str, Any] = dict(fields)
        if isinstance(values.get("state"), TaskState):
            values["state"] = values["state"].value
        values["updated_at"] = utc_ts()
        assignments = ", ".join(f"{key} = ?" for key in values)
        params = (*values.values(), task_id)
        self._execute(f"UPDATE tasks SET {assignments} WHERE id = ?", params)

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
        self._execute(
            """
            INSERT INTO attempts(
              id, job_id, task_id, number, agent, state, worktree, branch, log_path, started_at
            ) VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
            """,
            (attempt_id, job_id, task_id, number, agent, worktree, branch, log_path, utc_ts()),
        )
        return attempt_id

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        returncode: int | None,
        summary: str,
    ) -> None:
        self._execute(
            """
            UPDATE attempts
            SET state = ?, returncode = ?, summary = ?, finished_at = ?
            WHERE id = ?
            """,
            (state, returncode, summary, utc_ts(), attempt_id),
        )

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
        rows = self._execute(
            """
            SELECT id, job_id, task_id, kind, payload, created_at
            FROM events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?
            """,
            (job_id, max(0, after_id), bounded),
        ).fetchall()
        return [
            {
                "id": int(row["id"]),
                "job_id": row["job_id"],
                "task_id": row["task_id"],
                "kind": row["kind"],
                "payload": json.loads(row["payload"]),
                "created_at": float(row["created_at"]),
            }
            for row in rows
        ]

    def last_event_id(self, job_id: str) -> int:
        row = self._execute("SELECT COALESCE(MAX(id), 0) AS id FROM events WHERE job_id = ?", (job_id,)).fetchone()
        return int(row["id"]) if row is not None else 0

    def request_stop(self, job_id: str) -> None:
        self.update_job(job_id, stop_requested=True, state=JobState.STOPPING)
        self.event(job_id, "job.stop_requested")

    def clear_stop(self, job_id: str) -> None:
        self.update_job(job_id, stop_requested=False, state=JobState.QUEUED, error="")
        self.event(job_id, "job.resumed")

