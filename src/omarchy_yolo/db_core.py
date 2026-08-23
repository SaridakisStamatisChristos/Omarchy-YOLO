from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, cast

from .model import JobRecord, JobState, TaskRecord, TaskState
from .util import YoloError, ensure_private_dir


CURRENT_SCHEMA_VERSION = 2
LEGACY_SCHEMA_VERSION = 1

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA trusted_schema=OFF",
)

SCHEMA = """
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

CREATE TABLE IF NOT EXISTS dossiers (
  job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  schema_version INTEGER NOT NULL,
  sha256 TEXT NOT NULL,
  content TEXT NOT NULL,
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_job_state ON tasks(job_id, state);
CREATE INDEX IF NOT EXISTS idx_attempts_job_task ON attempts(job_id, task_id, number DESC);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS dossiers (
          job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
          schema_version INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          content TEXT NOT NULL,
          created_at REAL NOT NULL
        )
        """,
    ),
}

MAX_JOB_LIST_LIMIT = 200
MAX_EVENT_LIST_LIMIT = 100
MAX_EVENT_PAYLOAD_CHARS = 8_000


class DatabaseCore:
    def __init__(self, path: Path):
        ensure_private_dir(path.parent)
        self.path = path
        if path.is_symlink():
            raise YoloError(f"refusing symlink for database path: {path}")
        existed = path.exists()
        self._lock = threading.RLock()
        self.last_migration_backup: Path | None = None
        try:
            self._conn = sqlite3.connect(
                path,
                check_same_thread=False,
                isolation_level=None,
            )
            self._conn.row_factory = sqlite3.Row
            os.chmod(path, 0o600)
            with self._lock:
                for pragma in _PRAGMAS:
                    self._conn.execute(pragma)
                has_schema = self._has_table("jobs")
                if not existed or not has_schema:
                    self._conn.executescript(SCHEMA)
                    self._set_schema_version(CURRENT_SCHEMA_VERSION)
                else:
                    version = self._schema_version()
                    if version == 0:
                        version = LEGACY_SCHEMA_VERSION
                    if version > CURRENT_SCHEMA_VERSION:
                        raise sqlite3.DatabaseError(
                            f"database schema v{version} is newer than supported v{CURRENT_SCHEMA_VERSION}"
                        )
                    if version < CURRENT_SCHEMA_VERSION:
                        self.last_migration_backup = self._backup_before_migration(version)
                        self._migrate(version)
                    self._conn.executescript(SCHEMA)
                    self._set_schema_version(CURRENT_SCHEMA_VERSION)
                check = self._conn.execute("PRAGMA quick_check").fetchone()
                if check is None or str(check[0]).lower() != "ok":
                    detail = str(check[0]) if check is not None else "no result"
                    raise sqlite3.DatabaseError(f"quick_check failed: {detail}")
        except (sqlite3.DatabaseError, OSError) as exc:
            connection = getattr(self, "_conn", None)
            if connection is not None:
                connection.close()
            backup = (
                f"; pre-migration backup: {self.last_migration_backup}"
                if self.last_migration_backup is not None
                else ""
            )
            raise YoloError(
                f"database initialization/integrity check failed: {exc}{backup}"
            ) from exc

    def _has_table(self, name: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        return row is not None

    def _schema_version(self) -> int:
        row = self._conn.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    def schema_version(self) -> int:
        with self._lock:
            return self._schema_version()

    def _set_schema_version(self, version: int) -> None:
        self._conn.execute(f"PRAGMA user_version = {int(version)}")

    def _backup_before_migration(self, version: int) -> Path:
        backup_path = self.path.with_name(
            f"{self.path.name}.pre-v{version}-to-v{CURRENT_SCHEMA_VERSION}-{time.time_ns()}.bak"
        )
        if backup_path.exists() or backup_path.is_symlink():
            raise sqlite3.DatabaseError(f"refusing existing migration backup path: {backup_path}")
        backup = sqlite3.connect(backup_path)
        try:
            self._conn.backup(backup)
        finally:
            backup.close()
        os.chmod(backup_path, 0o600)
        return backup_path

    def _migrate(self, version: int) -> None:
        current = version
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            while current < CURRENT_SCHEMA_VERSION:
                statements = _MIGRATIONS.get(current)
                if statements is None:
                    raise sqlite3.DatabaseError(
                        f"no migration path from schema v{current} to v{current + 1}"
                    )
                for statement in statements:
                    self._conn.execute(statement)
                current += 1
                self._set_schema_version(current)
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return cast(sqlite3.Row | None, self._conn.execute(sql, params).fetchone())

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
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
        )
