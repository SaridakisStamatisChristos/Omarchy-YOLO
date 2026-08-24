from __future__ import annotations

import json
from typing import Any

from .db_core import DatabaseCore
from .model import JobState, TaskState
from .state_machine import StateTransitionError, validate_job_snapshot, validate_job_transition
from .util import json_dumps, utc_ts


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

    def stage_dossier(
        self,
        job_id: str,
        *,
        schema_version: int,
        sha256: str,
        content: str,
    ) -> None:
        """Persist an accepted candidate dossier without making it externally visible."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._conn.execute(
                    "SELECT state FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                if JobState(str(job["state"])) == JobState.COMPLETED:
                    raise StateTransitionError("cannot replace dossier for a completed job")
                self._conn.execute(
                    """
                    INSERT INTO dossiers(
                      job_id, schema_version, sha256, content, published, created_at
                    ) VALUES (?, ?, ?, ?, 0, ?)
                    ON CONFLICT(job_id) DO UPDATE SET
                      schema_version = excluded.schema_version,
                      sha256 = excluded.sha256,
                      content = excluded.content,
                      published = 0,
                      created_at = excluded.created_at
                    """,
                    (job_id, schema_version, sha256, content, utc_ts()),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # Compatibility name retained for callers from v1.4.0. A store is now a
    # private stage; only publish_completed_job can make it externally visible.
    def store_dossier(
        self,
        job_id: str,
        *,
        schema_version: int,
        sha256: str,
        content: str,
    ) -> None:
        self.stage_dossier(
            job_id,
            schema_version=schema_version,
            sha256=sha256,
            content=content,
        )

    def get_staged_dossier(self, job_id: str) -> dict[str, Any] | None:
        row = self._fetchone(
            """
            SELECT schema_version, sha256, content, published, created_at
            FROM dossiers WHERE job_id = ?
            """,
            (job_id,),
        )
        if row is None:
            return None
        return {
            "schema_version": int(row["schema_version"]),
            "sha256": str(row["sha256"]),
            "content": str(row["content"]),
            "published": bool(row["published"]),
            "created_at": float(row["created_at"]),
        }

    def get_dossier(self, job_id: str) -> dict[str, Any] | None:
        """Return only a durably published dossier."""
        row = self._fetchone(
            """
            SELECT schema_version, sha256, content, created_at
            FROM dossiers WHERE job_id = ? AND published = 1
            """,
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

    def publish_completed_job(
        self,
        job_id: str,
        *,
        dossier_schema_version: int,
        dossier_sha256: str,
        final_summary: str,
        source_apply_outcome: str,
        source_apply_reason: str = "",
    ) -> None:
        """Atomically publish the dossier and durable completion state.

        The staged dossier remains invisible until this transaction validates the
        complete task snapshot, clears stop_requested, transitions the job to
        completed, marks the dossier published, and records the acceptance events.
        """
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._conn.execute(
                    """
                    SELECT state, stop_requested, base_branch, integration_branch
                    FROM jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                current = JobState(str(job["state"]))

                dossier = self._conn.execute(
                    """
                    SELECT schema_version, sha256, published
                    FROM dossiers WHERE job_id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if dossier is None:
                    raise StateTransitionError("cannot publish completion without a staged dossier")
                if int(dossier["schema_version"]) != dossier_schema_version:
                    raise StateTransitionError("staged dossier schema version changed before publication")
                if str(dossier["sha256"]) != dossier_sha256:
                    raise StateTransitionError("staged dossier digest changed before publication")

                if current == JobState.COMPLETED:
                    if bool(dossier["published"]):
                        self._conn.execute("COMMIT")
                        return
                    raise StateTransitionError("completed job contains an unpublished dossier")

                validate_job_transition(current, JobState.COMPLETED)
                task_rows = self._conn.execute(
                    "SELECT state FROM tasks WHERE job_id = ? ORDER BY seq", (job_id,)
                ).fetchall()
                task_states = tuple(TaskState(str(row["state"])) for row in task_rows)
                validate_job_snapshot(
                    JobState.COMPLETED,
                    task_states,
                    stop_requested=False,
                )
                running_attempt = self._conn.execute(
                    "SELECT 1 FROM attempts WHERE job_id = ? AND state = 'running' LIMIT 1",
                    (job_id,),
                ).fetchone()
                if running_attempt is not None:
                    raise StateTransitionError("completed job cannot retain a running attempt")

                now = utc_ts()
                cur = self._conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, stop_requested = 0, final_summary = ?, error = '', updated_at = ?
                    WHERE id = ? AND state = ?
                    """,
                    (
                        JobState.COMPLETED.value,
                        final_summary,
                        now,
                        job_id,
                        current.value,
                    ),
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale job state while publishing completion for {job_id}"
                    )
                cur = self._conn.execute(
                    """
                    UPDATE dossiers SET published = 1
                    WHERE job_id = ? AND published = 0 AND sha256 = ?
                    """,
                    (job_id, dossier_sha256),
                )
                if cur.rowcount != 1:
                    raise StateTransitionError("staged dossier could not be published atomically")

                self._conn.execute(
                    "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        "job.dossier_created",
                        json_dumps(
                            {
                                "sha256": dossier_sha256,
                                "schema_version": dossier_schema_version,
                            }
                        ),
                        now,
                    ),
                )
                if source_apply_outcome == "applied":
                    self._conn.execute(
                        "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                        (
                            job_id,
                            "job.applied",
                            json_dumps({"branch": str(job["base_branch"])}),
                            now,
                        ),
                    )
                elif source_apply_outcome == "skipped":
                    self._conn.execute(
                        "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                        (
                            job_id,
                            "job.apply_skipped",
                            json_dumps({"reason": source_apply_reason[-4_000:]}),
                            now,
                        ),
                    )
                elif source_apply_outcome != "not-requested":
                    raise ValueError(f"invalid source apply outcome: {source_apply_outcome}")

                self._conn.execute(
                    "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        "job.completed",
                        json_dumps(
                            {
                                "summary": final_summary,
                                "branch": str(job["integration_branch"]),
                                "dossier_sha256": dossier_sha256,
                                "source_apply_outcome": source_apply_outcome,
                            }
                        ),
                        now,
                    ),
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
