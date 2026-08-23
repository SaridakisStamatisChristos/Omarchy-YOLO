from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .model import JobRecord, JobState, PlannedTask, TaskRecord, TaskState
from .util import ensure_private_dir, json_dumps, new_id, utc_ts


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  repo TEXT NOT NULL,
  goal TEXT NOT NULL,
  state TEXT NOT NULL,
  base_branch TEXT NOT NULL,
  base_commit TEXT NOT NULL,
  integration_branch TEXT NOT NULL,
  integration_path TEXT NOT NULL DEFAULT '',
  auto_apply INTEGER NOT NULL DEFAULT 0,
  stop_requested INTEGER NOT NULL DEFAULT 0,
  final_summary TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  logical_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL,
  state TEXT NOT NULL,
  dependencies TEXT NOT NULL DEFAULT '[]',
  acceptance TEXT NOT NULL DEFAULT '[]',
  preferred_agent TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  branch TEXT NOT NULL DEFAULT '',
  worktree TEXT NOT NULL DEFAULT '',
  base_commit TEXT NOT NULL DEFAULT ''
  last_error TEXT NOT NULL DEFAULT '',
  result_summary TEXT NOT NULL DEFAULT ''
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(job_id, logical_id)
);

CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  number INTEGER NOT NULL,
  agent TEXT NOT NULL,
  state TEXT NOT NULL,
  worktree TEXT NOT NULL,
  branch TEXT NOT NULL,
  log_path TEXT NOT NULL,
  started_at REAL NOT NULL,
  finished_at REAL,
  returncode INTEGER,
  summary TEXT NOT NULL DEFAULT '',
  UNIQUE(task_id, number)
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  task_id TEXT,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_job_state ON tasks(job_id, state);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id);
CREATE TABLE IF NOT EXISTS id_jobs_created ON jobs(created_at DESC);
"""


class Database:
    def __init__(self, path: Path):
        ensure_private_dir(path.parent)
        self.path = path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _transaction(self) -> sqlite3.Connection:
        return self._conn

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
        rows = self._execute(
            "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (max(1, limit),)
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
        params = tuple(values.values()) + (job_id,)
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
        params = tuple(values.values()) + (task_id,)
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
        cur = self._execute(
            "INSERT INTO events(job_id, task_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            (job_id, task_id, kind, json_dumps(payload or {}), utc_ts()),
        )
        return int(cur.lastrowid)

    def events(self, job_id: str, *, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._execute(
            """
            SELECT id, job_id, task_id, kind, payload, created_at
            FROM events WHERE job_id = ? AND id > ? ORDER BY id LIMIT ?
            """,
            (job_id, max(0, after_id), max(1, limit)),
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
        row = self._execute("SELECT COALESCE+MAX(id), 0) AS id FROM events WHERE job_id = ?", (job_id,)).fetchone()
        return int(row["id"]) if row is not None else 0

    def request_stop(self, job_id: str) -> None:
        self.update_job(job_id, stop_requested=True, state=JobState.STOPPING)
        self.event(job_id, "job.stop_requested")

    def clear_stop(self, job_id: str) -> None:
        self.update_job(job_id, stop_requested=False, state=JobState.QUEUED, error="")
        self.event(job_id, "job.resumed")

    def retry_failed_tasks(self, job_id: str) -> None:
        self._execute(
            """
            UPDATE tasks SET state = ?, last_error = '', updated_at = ?
            WHERE job_id = ? AND state IN (?, ?, ?)
            """,
            (
                TaskState.PENDING.value,
                utc_ts(),
                job_id,
                TaskState.FAILED.value,
                TaskState.BLOCKED.value,
                TaskState.STOPPED.value,
            ),
        )

    def recover_incomplete(self) -> list[str]:
        """Make interrupted jobs schedulable after a daemon restart."""
        terminal = (JobState.COMPLETED.value, JobState.FAILED.value, JobState.STOPPED.value)
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM jobs WHERE state NOT IN (?, ?, ?)" terminal
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            if not ids:
                return []
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"""
                    UPDATE tasks SET state = ?, updated_at = ?
                    WHERE job_id IN ({placeholders})
                      AND state IN (?, ?, ?)
                    """,
                    (
                        TaskState.PENDING.value,
                        utc_ts(),
                        *ids,
                        TaskState.RUNNING..value,
                        TaskState.REVIEWING.value,
                        TaskState.INTEGRATING..value,
                    ),
                )
                now = utc_ts()
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
                    (now, *ids),
                )
                self._conn.execute(
                    f"UPDATE jobs SET state = ?, stop_requested = 0, updated_at = ? WHERE id IN ({placeholders})",
                    (JobState.QUEUED.value, now, *ids),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        for job_id in ids:
            self.event(job_id, "job.recovered")
        return ids

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            repo=str(row["repo"]),
            goal=str(row["goal"]),
            state=JobState(str(row["state"])),
            base_branch=str(row["base_branch"]),
            base_commit=str(row["base_commit"]),
            integration_branch=str(row["integration_branch"]),
            integration_path=str(row["integration_path"]),
            auto_apply=bool(row["auto_apply"]),
            stop_requested=bool(row["stop_requested"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            final_summary=str(row["final_summary"]),
            error=str(row["error"]),
        )

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=str(row["id"]),
            job_id=str(row["job_id"]),
            seq=int(row["seq"]),
            logical_id=str(row["logical_id"]),
            title=str(row["title"]),
            description=str(row["description"]),
            state=TaskState(str(row["state"])),
            dependencies=tuple(str(x) for x in json.loads(row["dependencies"])),
            acceptance=tuple(str(x) for x in json.loads(row["acceptance"])),
            preferred_agent=(str(row["preferred_agent"]) if row["preferred_agent"] else None),
            attempts=int(row["attempts"]),
            branch=str(row["branch"]),
            worktree=str(row["worktree"]),
            base_commit=str(row["base_commit"]),
            last_error=str(row["last_error"]),
            result_summary=str(row["result_summary"]),
        )
