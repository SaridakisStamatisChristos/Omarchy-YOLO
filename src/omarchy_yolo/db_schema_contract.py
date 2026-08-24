from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    declared_type: str
    not_null: bool
    default_sql: str | None
    primary_key_ordinal: int = 0
    hidden: int = 0


EXPECTED_COLUMNS: dict[str, dict[str, ColumnSpec]] = {
    "jobs": {
        "id": ColumnSpec("TEXT", False, None, 1),
        "repo": ColumnSpec("TEXT", True, None),
        "goal": ColumnSpec("TEXT", True, None),
        "state": ColumnSpec("TEXT", True, None),
        "base_branch": ColumnSpec("TEXT", True, None),
        "base_commit": ColumnSpec("TEXT", True, None),
        "integration_branch": ColumnSpec("TEXT", True, None),
        "integration_path": ColumnSpec("TEXT", True, "''"),
        "auto_apply": ColumnSpec("INTEGER", True, "0"),
        "stop_requested": ColumnSpec("INTEGER", True, "0"),
        "final_summary": ColumnSpec("TEXT", True, "''"),
        "error": ColumnSpec("TEXT", True, "''"),
        "accepted_commit": ColumnSpec("TEXT", True, "''"),
        "acceptance_phase": ColumnSpec("TEXT", True, "'none'"),
        "source_apply_intent": ColumnSpec("TEXT", True, "'not-requested'"),
        "source_apply_outcome": ColumnSpec("TEXT", True, "''"),
        "source_apply_reason": ColumnSpec("TEXT", True, "''"),
        "created_at": ColumnSpec("REAL", True, None),
        "updated_at": ColumnSpec("REAL", True, None),
    },
    "tasks": {
        "id": ColumnSpec("TEXT", False, None, 1),
        "job_id": ColumnSpec("TEXT", True, None),
        "seq": ColumnSpec("INTEGER", True, None),
        "logical_id": ColumnSpec("TEXT", True, None),
        "title": ColumnSpec("TEXT", True, None),
        "description": ColumnSpec("TEXT", True, None),
        "state": ColumnSpec("TEXT", True, None),
        "dependencies": ColumnSpec("TEXT", True, "'[]'"),
        "acceptance": ColumnSpec("TEXT", True, "'[]'"),
        "preferred_agent": ColumnSpec("TEXT", False, None),
        "attempts": ColumnSpec("INTEGER", True, "0"),
        "branch": ColumnSpec("TEXT", True, "''"),
        "worktree": ColumnSpec("TEXT", True, "''"),
        "base_commit": ColumnSpec("TEXT", True, "''"),
        "last_error": ColumnSpec("TEXT", True, "''"),
        "result_summary": ColumnSpec("TEXT", True, "''"),
        "created_at": ColumnSpec("REAL", True, None),
        "updated_at": ColumnSpec("REAL", True, None),
    },
    "attempts": {
        "id": ColumnSpec("TEXT", False, None, 1),
        "job_id": ColumnSpec("TEXT", True, None),
        "task_id": ColumnSpec("TEXT", True, None),
        "number": ColumnSpec("INTEGER", True, None),
        "agent": ColumnSpec("TEXT", True, None),
        "state": ColumnSpec("TEXT", True, None),
        "worktree": ColumnSpec("TEXT", True, None),
        "branch": ColumnSpec("TEXT", True, None),
        "log_path": ColumnSpec("TEXT", True, None),
        "started_at": ColumnSpec("REAL", True, None),
        "finished_at": ColumnSpec("REAL", False, None),
        "returncode": ColumnSpec("INTEGER", False, None),
        "summary": ColumnSpec("TEXT", True, "''"),
    },
    "events": {
        "id": ColumnSpec("INTEGER", False, None, 1),
        "job_id": ColumnSpec("TEXT", True, None),
        "task_id": ColumnSpec("TEXT", False, None),
        "kind": ColumnSpec("TEXT", True, None),
        "payload": ColumnSpec("TEXT", True, "'{}'"),
        "created_at": ColumnSpec("REAL", True, None),
    },
    "dossiers": {
        "job_id": ColumnSpec("TEXT", False, None, 1),
        "schema_version": ColumnSpec("INTEGER", True, None),
        "sha256": ColumnSpec("TEXT", True, None),
        "content": ColumnSpec("TEXT", True, None),
        "published": ColumnSpec("INTEGER", True, "0"),
        "created_at": ColumnSpec("REAL", True, None),
    },
}

EXPECTED_INDEXES: dict[str, tuple[str, tuple[str, ...], bool]] = {
    "idx_tasks_job_state": ("tasks", ("job_id", "state"), False),
    "idx_tasks_job_id_id": ("tasks", ("job_id", "id"), True),
    "idx_attempts_job_task": ("attempts", ("job_id", "task_id", "number"), False),
    "idx_events_job_id": ("events", ("job_id", "id"), False),
    "idx_jobs_created": ("jobs", ("created_at",), False),
}

EXPECTED_UNIQUE_CONSTRAINTS: dict[str, frozenset[tuple[str, ...]]] = {
    "tasks": frozenset({("job_id", "logical_id")}),
    "attempts": frozenset({("task_id", "number")}),
}

EXPECTED_CHECK_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "jobs": (
        re.compile(
            r"check\s*\(\s*acceptance_phase\s+in\s*\(\s*'none'\s*,\s*'accepted'\s*,\s*'applying'\s*,\s*'published'\s*\)\s*\)",
            re.IGNORECASE,
        ),
        re.compile(
            r"check\s*\(\s*source_apply_intent\s+in\s*\(\s*'requested'\s*,\s*'not-requested'\s*\)\s*\)",
            re.IGNORECASE,
        ),
        re.compile(
            r"check\s*\(\s*source_apply_outcome\s+in\s*\(\s*''\s*,\s*'applied'\s*,\s*'skipped'\s*,\s*'not-requested'\s*\)\s*\)",
            re.IGNORECASE,
        ),
    ),
    "dossiers": (
        re.compile(
            r"check\s*\(\s*published\s+in\s*\(\s*0\s*,\s*1\s*\)\s*\)",
            re.IGNORECASE,
        ),
    ),
}


def _index_columns(conn: sqlite3.Connection, name: str) -> tuple[str, ...]:
    return tuple(
        str(row["name"])
        for row in conn.execute(f"PRAGMA index_info({name})").fetchall()
    )


def validate_table_columns(conn: sqlite3.Connection) -> None:
    for table, expected in EXPECTED_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_xinfo({table})").fetchall()
        actual_names = tuple(str(row["name"]) for row in rows)
        if len(actual_names) != len(expected) or set(actual_names) != set(expected):
            raise sqlite3.DatabaseError(
                f"schema contract mismatch for {table}: expected columns {tuple(expected)}, got {actual_names}"
            )
        by_name = {str(row["name"]): row for row in rows}
        for name, spec in expected.items():
            row = by_name[name]
            actual = ColumnSpec(
                declared_type=str(row["type"]).upper(),
                not_null=bool(row["notnull"]),
                default_sql=(str(row["dflt_value"]) if row["dflt_value"] is not None else None),
                primary_key_ordinal=int(row["pk"]),
                hidden=int(row["hidden"]),
            )
            if actual != spec:
                raise sqlite3.DatabaseError(
                    f"schema contract mismatch for {table}.{name}: expected {spec}, got {actual}"
                )


def validate_named_indexes(conn: sqlite3.Connection) -> None:
    for name, (table, expected_columns, expected_unique) in EXPECTED_INDEXES.items():
        index_rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
        match = next((row for row in index_rows if str(row["name"]) == name), None)
        if match is None:
            raise sqlite3.DatabaseError(f"schema contract missing index {name}")
        actual_unique = bool(match["unique"])
        columns = _index_columns(conn, name)
        if columns != expected_columns or actual_unique != expected_unique:
            raise sqlite3.DatabaseError(
                f"schema contract mismatch for index {name}: columns={columns}, unique={actual_unique}"
            )


def validate_unique_constraints(conn: sqlite3.Connection) -> None:
    for table, expected_constraints in EXPECTED_UNIQUE_CONSTRAINTS.items():
        found: set[tuple[str, ...]] = set()
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not bool(row["unique"]):
                continue
            if str(row["origin"]) != "u":
                continue
            found.add(_index_columns(conn, str(row["name"])))
        missing = expected_constraints - found
        if missing:
            raise sqlite3.DatabaseError(
                f"schema contract missing UNIQUE constraint(s) for {table}: {sorted(missing)}"
            )


def validate_check_constraints(conn: sqlite3.Connection) -> None:
    for table, patterns in EXPECTED_CHECK_PATTERNS.items():
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None or row["sql"] is None:
            raise sqlite3.DatabaseError(f"schema contract missing table definition for {table}")
        sql = str(row["sql"])
        for pattern in patterns:
            if pattern.search(sql) is None:
                raise sqlite3.DatabaseError(
                    f"schema contract missing required CHECK constraint for {table}"
                )
