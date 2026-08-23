from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .model import JobRecord, JobState, TaskRecord, TaskState
from .util import YoloError, ensure_private_dir


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;
PRAGMA busy_timeout=5000;
PRAGMA trusted_schema=OFF;

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
  base_commit TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  result_summary TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

MAX_JOB_LIST_LIMIT = 200
MAX_EVENT_LIST_LIMIT = 100
MAX_EVENT_PAYLOAD_CHARS = 8_000


class DatabaseCore:
    def __init__(self, path: Path):
        ensure_private_dir(path.parent)
        self.path = path
        if path.is_symlink():
            raise YoloError(f"refusing symlink for database path: {path}")
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                path,
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            os.chmod(path, 0o600)
            with self._lock:
                self._conn.executescript(SCHEMA)
                check = self._conn.execute("PRAGMA quick_check").fetchone()
                if check is None or str(check[0]).lower() != "ok":
                    detail = str(check[0]) if check is not None else "no result"
                    raise sqlite3.DatabaseError(f"quick_check failed: {detail}")
        except (sqlite3.DatabaseError, OSError) as exc:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                connection.close()
            raise YoloError(f"database initialization/integrity check failed: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

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
