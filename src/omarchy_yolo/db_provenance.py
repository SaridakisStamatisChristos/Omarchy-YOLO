from __future__ import annotations

import json
from typing import Any

from .db_core import DatabaseCore
from .util import utc_ts


class ProvenanceMixin(DatabaseCore):
    def provenance_snapshot(self, job_id: str) -> dict[str, Any]:
        task_rows = self._fetchall(
            """
            SELECT id, seq, logical_id, title, state, dependencies, acceptance,
                   preferred_agent, attempts, branch, base_commit, last_error,
                   result_summary, created_at, updated_at
            FROM tasks WHERE job_id = ? ORDER BY seq
            """,
            (job_id,),
        )
        attempt_rows = self._fetchall(
            """
            SELECT id, task_id, number, agent, state, branch, log_path,
                   started_at, finished_at, returncode, summary
            FROM attempts WHERE job_id = ? ORDER BY task_id, number
            """,
            (job_id,),
        )
        event_rows = self._fetchall(
            """
            SELECT id, task_id, kind, payload, created_at
            FROM events WHERE job_id = ? ORDER BY id
            """,
            (job_id,),
        )
        return {
            "tasks": [
                {
                    "id": str(row["id"]),
                    "seq": int(row["seq"]),
                    "logical_id": str(row["logical_id"]),
                    "title": str(row["title"]),
                    "state": str(row["state"]),
                    "dependencies": list(json.loads(row["dependencies"])),
                    "acceptance": list(json.loads(row["acceptance"])),
                    "preferred_agent": (
                        str(row["preferred_agent"])
                        if row["preferred_agent"] is not None
                        else None
                    ),
                    "attempts": int(row["attempts"]),
                    "branch": str(row["branch"]),
                    "base_commit": str(row["base_commit"]),
                    "last_error": str(row["last_error"]),
                    "result_summary": str(row["result_summary"]),
                    "created_at": float(row["created_at"]),
                    "updated_at": float(row["updated_at"]),
                }
                for row in task_rows
            ],
            "attempts": [
                {
                    "id": str(row["id"]),
                    "task_id": str(row["task_id"]),
                    "number": int(row["number"]),
                    "agent": str(row["agent"]),
                    "state": str(row["state"]),
                    "branch": str(row["branch"]),
                    "log_path": str(row["log_path"]),
                    "started_at": float(row["started_at"]),
                    "finished_at": (
                        float(row["finished_at"])
                        if row["finished_at"] is not None
                        else None
                    ),
                    "returncode": (
                        int(row["returncode"])
                        if row["returncode"] is not None
                        else None
                    ),
                    "summary": str(row["summary"]),
                }
                for row in attempt_rows
            ],
            "events": [
                {
                    "id": int(row["id"]),
                    "task_id": str(row["task_id"]) if row["task_id"] is not None else None,
                    "kind": str(row["kind"]),
                    "payload": json.loads(row["payload"]),
                    "created_at": float(row["created_at"]),
                }
                for row in event_rows
            ],
        }

    def store_dossier(
        self,
        job_id: str,
        *,
        schema_version: int,
        sha256: str,
        content: str,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO dossiers(job_id, schema_version, sha256, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                  schema_version = excluded.schema_version,
                  sha256 = excluded.sha256,
                  content = excluded.content,
                  created_at = excluded.created_at
                """,
                (job_id, schema_version, sha256, content, utc_ts()),
            )

    def get_dossier(self, job_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            "SELECT schema_version, sha256, content, created_at FROM dossiers WHERE job_id = ?",
            (job_id,),
        )
        if row is None:
            return None
        return {
            "schema_version": int(row["schema_version"]),
            "sha256": str(row["sha256"]),
            "content": str(row["content"]),
            "created_at": float(row["created_at"]),
        }
