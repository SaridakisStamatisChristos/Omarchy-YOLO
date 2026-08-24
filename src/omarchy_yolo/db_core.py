from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, cast

from .db_schema_contract import (
    validate_check_constraints,
    validate_named_indexes,
    validate_table_columns,
    validate_unique_constraints,
)
from .model import JobRecord, JobState, TaskRecord, TaskState
from .state_machine import validate_job_snapshot
from .util import YoloError, ensure_private_dir


CURRENT_SCHEMA_VERSION = 4
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
  updated_at REAL NOT NULL,
  accepted_commit TEXT NOT NULL DEFAULT '',
  acceptance_phase TEXT NOT NULL DEFAULT 'none'
    CHECK(acceptance_phase IN ('none', 'accepted', 'applying', 'published')),
  source_apply_intent TEXT NOT NULL DEFAULT 'not-requested'
    CHECK(source_apply_intent IN ('requested', 'not-requested')),
  source_apply_outcome TEXT NOT NULL DEFAULT ''
    CHECK(source_apply_outcome IN ('', 'applied', 'skipped', 'not-requested')),
  source_apply_reason TEXT NOT NULL DEFAULT ''
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

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_job_id_id ON tasks(job_id, id);

CREATE TABLE IF NOT EXISTS attempts (
  id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL,
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
  FOREIGN KEY(job_id, task_id) REFERENCES tasks(job_id, id) ON DELETE CASCADE,
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
  published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0, 1)),
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_job_state ON tasks(job_id, state);
CREATE INDEX IF NOT EXISTS idx_attempts_job_task ON attempts(job_id, task_id, number DESC);
CREATE INDEX IF NOT EXISTS idx_events_job_id ON events(job_id, id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at DESC);
"""

# Schema v1 was the original unversioned durable store. v2 added dossiers. v3
# made dossier publication explicit and attempt ownership relational. v4 adds a
# durable acceptance/apply journal so Git source application can be reconciled
# after a hard crash rather than pretending Git and SQLite share one transaction.
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
    2: (
        """
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
        )
        """,
        """
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
        )
        """,
        # Some v1 fixtures predate the event ledger entirely. v4 migration reads
        # completion/apply events, so materialize the canonical ledger before the
        # v3 -> v4 journal backfill rather than assuming a later SCHEMA pass did it.
        """
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          task_id TEXT,
          kind TEXT NOT NULL,
          payload TEXT NOT NULL DEFAULT '{}',
          created_at REAL NOT NULL
        )
        """,
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_job_id_id ON tasks(job_id, id)",
        "ALTER TABLE dossiers ADD COLUMN published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0, 1))",
        """
        UPDATE dossiers
        SET published = 1
        WHERE job_id IN (
          SELECT id FROM jobs WHERE state = 'completed' AND stop_requested = 0
        )
        """,
        """
        CREATE TABLE attempts_v3 (
          id TEXT PRIMARY KEY,
          job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
          task_id TEXT NOT NULL,
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
          FOREIGN KEY(job_id, task_id) REFERENCES tasks(job_id, id) ON DELETE CASCADE,
          UNIQUE(task_id, number)
        )
        """,
        """
        INSERT INTO attempts_v3(
          id, job_id, task_id, number, agent, state, worktree, branch,
          log_path, started_at, finished_at, returncode, summary
        )
        SELECT
          id, job_id, task_id, number, agent, state, worktree, branch,
          log_path, started_at, finished_at, returncode, summary
        FROM attempts
        """,
        "DROP TABLE attempts",
        "ALTER TABLE attempts_v3 RENAME TO attempts",
        "CREATE INDEX IF NOT EXISTS idx_attempts_job_task ON attempts(job_id, task_id, number DESC)",
    ),
    3: (
        "ALTER TABLE jobs ADD COLUMN accepted_commit TEXT NOT NULL DEFAULT ''",
        """
        ALTER TABLE jobs ADD COLUMN acceptance_phase TEXT NOT NULL DEFAULT 'none'
          CHECK(acceptance_phase IN ('none', 'accepted', 'applying', 'published'))
        """,
        """
        ALTER TABLE jobs ADD COLUMN source_apply_intent TEXT NOT NULL DEFAULT 'not-requested'
          CHECK(source_apply_intent IN ('requested', 'not-requested'))
        """,
        """
        ALTER TABLE jobs ADD COLUMN source_apply_outcome TEXT NOT NULL DEFAULT ''
          CHECK(source_apply_outcome IN ('', 'applied', 'skipped', 'not-requested'))
        """,
        "ALTER TABLE jobs ADD COLUMN source_apply_reason TEXT NOT NULL DEFAULT ''",
        """
        UPDATE jobs
        SET acceptance_phase = 'published',
            source_apply_intent = CASE WHEN auto_apply = 1 THEN 'requested' ELSE 'not-requested' END,
            source_apply_outcome = CASE
              WHEN EXISTS(
                SELECT 1 FROM events
                WHERE events.job_id = jobs.id AND events.kind = 'job.applied'
              ) THEN 'applied'
              WHEN EXISTS(
                SELECT 1 FROM events
                WHERE events.job_id = jobs.id AND events.kind = 'job.apply_skipped'
              ) THEN 'skipped'
              WHEN auto_apply = 0 THEN 'not-requested'
              ELSE ''
            END
        WHERE state = 'completed'
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
                    else:
                        # A current-version database must prove its structural
                        # contract before any idempotent CREATE INDEX can repair it.
                        self._validate_table_columns_contract()
                    self._conn.executescript(SCHEMA)
                    self._set_schema_version(CURRENT_SCHEMA_VERSION)
                self._validate_integrity_and_schema()
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

    def _validate_integrity_and_schema(self) -> None:
        check = self._conn.execute("PRAGMA quick_check").fetchone()
        if check is None or str(check[0]).lower() != "ok":
            detail = str(check[0]) if check is not None else "no result"
            raise sqlite3.DatabaseError(f"quick_check failed: {detail}")
        fk_rows = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_rows:
            raise sqlite3.DatabaseError("foreign_key_check found relational violations")
        self._validate_schema_contract()

    def _validate_table_columns_contract(self) -> None:
        validate_table_columns(self._conn)

    def _validate_schema_contract(self) -> None:
        validate_table_columns(self._conn)
        validate_named_indexes(self._conn)
        validate_unique_constraints(self._conn)
        validate_check_constraints(self._conn)

        task_fks = self._foreign_key_signatures("tasks")
        if ("jobs", "CASCADE", (("job_id", "id"),)) not in task_fks:
            raise sqlite3.DatabaseError("schema contract missing tasks.job_id -> jobs.id")

        attempt_fks = self._foreign_key_signatures("attempts")
        if ("jobs", "CASCADE", (("job_id", "id"),)) not in attempt_fks:
            raise sqlite3.DatabaseError("schema contract missing attempts.job_id -> jobs.id")
        if (
            "tasks",
            "CASCADE",
            (("job_id", "job_id"), ("task_id", "id")),
        ) not in attempt_fks:
            raise sqlite3.DatabaseError(
                "schema contract missing attempts(job_id, task_id) ownership foreign key"
            )

        event_fks = self._foreign_key_signatures("events")
        if ("jobs", "CASCADE", (("job_id", "id"),)) not in event_fks:
            raise sqlite3.DatabaseError("schema contract missing events.job_id -> jobs.id")

        dossier_fks = self._foreign_key_signatures("dossiers")
        if ("jobs", "CASCADE", (("job_id", "id"),)) not in dossier_fks:
            raise sqlite3.DatabaseError("schema contract missing dossiers.job_id -> jobs.id")

    def _foreign_key_signatures(
        self, table: str
    ) -> set[tuple[str, str, tuple[tuple[str, str], ...]]]:
        groups: dict[int, list[sqlite3.Row]] = {}
        for row in self._conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            groups.setdefault(int(row["id"]), []).append(row)
        signatures: set[tuple[str, str, tuple[tuple[str, str], ...]]] = set()
        for rows in groups.values():
            ordered = sorted(rows, key=lambda row: int(row["seq"]))
            first = ordered[0]
            signatures.add(
                (
                    str(first["table"]),
                    str(first["on_delete"]).upper(),
                    tuple((str(row["from"]), str(row["to"])) for row in ordered),
                )
            )
        return signatures

    def _snapshot_locked(
        self, job_id: str
    ) -> tuple[JobState, tuple[TaskState, ...], tuple[TaskState, ...], bool, str]:
        job = self._conn.execute(
            "SELECT state, stop_requested, acceptance_phase FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if job is None:
            raise KeyError(job_id)
        task_rows = self._conn.execute(
            "SELECT id, state FROM tasks WHERE job_id = ? ORDER BY seq",
            (job_id,),
        ).fetchall()
        running_attempt_rows = self._conn.execute(
            """
            SELECT t.state AS task_state
            FROM attempts AS a
            JOIN tasks AS t ON t.job_id = a.job_id AND t.id = a.task_id
            WHERE a.job_id = ? AND a.state = 'running'
            ORDER BY a.started_at, a.id
            """,
            (job_id,),
        ).fetchall()
        return (
            JobState(str(job["state"])),
            tuple(TaskState(str(row["state"])) for row in task_rows),
            tuple(TaskState(str(row["task_state"])) for row in running_attempt_rows),
            bool(job["stop_requested"]),
            str(job["acceptance_phase"]),
        )

    def _validate_snapshot_locked(self, job_id: str) -> None:
        state, tasks, running_owners, stop_requested, acceptance_phase = self._snapshot_locked(
            job_id
        )
        validate_job_snapshot(
            state,
            tasks,
            stop_requested=stop_requested,
            running_attempt_task_states=running_owners,
            acceptance_phase=acceptance_phase,
        )

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
            accepted_commit=str(row["accepted_commit"]),
            acceptance_phase=str(row["acceptance_phase"]),
            source_apply_intent=str(row["source_apply_intent"]),
            source_apply_outcome=str(row["source_apply_outcome"]),
            source_apply_reason=str(row["source_apply_reason"]),
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
