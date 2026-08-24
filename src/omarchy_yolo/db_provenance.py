from __future__ import annotations

import hashlib
import json
from typing import Any

from .db_core import DatabaseCore
from .model import JobState, TaskState
from .state_machine import StateTransitionError, validate_job_snapshot, validate_job_transition
from .util import json_dumps, utc_ts


def _digest_matches(content: str, expected: str) -> bool:
    actual = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return len(expected) == 64 and actual == expected


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
        """Persist a candidate dossier privately before acceptance begins."""
        if schema_version < 1:
            raise ValueError("dossier schema_version must be positive")
        if not _digest_matches(content, sha256):
            raise StateTransitionError("refusing dossier whose SHA-256 does not match its content")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._conn.execute(
                    "SELECT state, acceptance_phase FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                phase = str(job["acceptance_phase"])
                if JobState(str(job["state"])) == JobState.COMPLETED or phase != "none":
                    raise StateTransitionError(
                        f"cannot replace dossier after acceptance begins ({phase})"
                    )
                self._stage_dossier_locked(
                    job_id,
                    schema_version=schema_version,
                    sha256=sha256,
                    content=content,
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def _stage_dossier_locked(
        self,
        job_id: str,
        *,
        schema_version: int,
        sha256: str,
        content: str,
    ) -> None:
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

    # Compatibility name retained for callers from v1.4.0.
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

    def prepare_accepted_job(
        self,
        job_id: str,
        *,
        accepted_commit: str,
        final_summary: str,
        source_apply_intent: str,
        dossier_schema_version: int,
        dossier_sha256: str,
        dossier_content: str,
    ) -> None:
        """Durably journal acceptance before any source-branch Git side effect.

        The accepted commit, apply intent and staged dossier become durable in the
        same transaction. A hard crash after this point is therefore recoverable:
        startup can resume from ``accepted``/``applying`` without re-planning or
        re-running semantic acceptance.
        """
        if source_apply_intent not in {"requested", "not-requested"}:
            raise ValueError(f"invalid source apply intent: {source_apply_intent}")
        if not accepted_commit or len(accepted_commit) > 128:
            raise ValueError("accepted_commit must be a non-empty bounded Git object id")
        if dossier_schema_version < 1:
            raise ValueError("dossier schema_version must be positive")
        if not _digest_matches(dossier_content, dossier_sha256):
            raise StateTransitionError("refusing accepted dossier with invalid SHA-256")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._conn.execute(
                    """
                    SELECT state, acceptance_phase, accepted_commit,
                           source_apply_intent, final_summary
                    FROM jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                current = JobState(str(job["state"]))
                phase = str(job["acceptance_phase"])
                if phase in {"accepted", "applying"}:
                    dossier = self._conn.execute(
                        "SELECT schema_version, sha256, content FROM dossiers WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()
                    same = (
                        str(job["accepted_commit"]) == accepted_commit
                        and str(job["source_apply_intent"]) == source_apply_intent
                        and str(job["final_summary"]) == final_summary
                        and dossier is not None
                        and int(dossier["schema_version"]) == dossier_schema_version
                        and str(dossier["sha256"]) == dossier_sha256
                        and str(dossier["content"]) == dossier_content
                    )
                    if same:
                        self._conn.execute("COMMIT")
                        return
                    raise StateTransitionError(
                        "accepted job metadata changed across idempotent preparation"
                    )
                if phase != "none":
                    raise StateTransitionError(
                        f"cannot prepare acceptance from phase {phase}"
                    )
                if current not in {JobState.RUNNING, JobState.STOPPING}:
                    raise StateTransitionError(
                        f"acceptance requires running/stopping job, got {current.value}"
                    )
                validate_job_transition(current, JobState.COMPLETED)

                _, task_states, running_owners, _, _ = self._snapshot_locked(job_id)
                validate_job_snapshot(
                    current,
                    task_states,
                    stop_requested=False,
                    running_attempt_task_states=running_owners,
                    acceptance_phase="accepted",
                )
                self._stage_dossier_locked(
                    job_id,
                    schema_version=dossier_schema_version,
                    sha256=dossier_sha256,
                    content=dossier_content,
                )
                now = utc_ts()
                cur = self._conn.execute(
                    """
                    UPDATE jobs
                    SET accepted_commit = ?, acceptance_phase = 'accepted',
                        source_apply_intent = ?, source_apply_outcome = '',
                        source_apply_reason = '', final_summary = ?, error = '',
                        stop_requested = 0, updated_at = ?
                    WHERE id = ? AND acceptance_phase = 'none' AND state = ?
                    """,
                    (
                        accepted_commit,
                        source_apply_intent,
                        final_summary,
                        now,
                        job_id,
                        current.value,
                    ),
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale job state while journaling acceptance for {job_id}"
                    )
                self._conn.execute(
                    "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        "job.accepted",
                        json_dumps(
                            {
                                "accepted_commit": accepted_commit,
                                "source_apply_intent": source_apply_intent,
                                "dossier_sha256": dossier_sha256,
                            }
                        ),
                        now,
                    ),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def mark_apply_started(self, job_id: str, *, accepted_commit: str) -> None:
        """Persist APPLYING before touching the user's source branch."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    """
                    SELECT acceptance_phase, accepted_commit, source_apply_intent
                    FROM jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if row is None:
                    raise KeyError(job_id)
                phase = str(row["acceptance_phase"])
                if str(row["accepted_commit"]) != accepted_commit:
                    raise StateTransitionError("accepted commit changed before source application")
                if str(row["source_apply_intent"]) != "requested":
                    raise StateTransitionError("source application was not requested")
                if phase == "applying":
                    self._conn.execute("COMMIT")
                    return
                if phase != "accepted":
                    raise StateTransitionError(
                        f"source application requires accepted phase, got {phase}"
                    )
                now = utc_ts()
                cur = self._conn.execute(
                    """
                    UPDATE jobs SET acceptance_phase = 'applying', updated_at = ?
                    WHERE id = ? AND acceptance_phase = 'accepted'
                    """,
                    (now, job_id),
                )
                if cur.rowcount != 1:
                    raise StateTransitionError(
                        f"stale acceptance phase while starting source apply for {job_id}"
                    )
                self._conn.execute(
                    "INSERT INTO events(job_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                    (
                        job_id,
                        "job.apply_started",
                        json_dumps({"accepted_commit": accepted_commit}),
                        now,
                    ),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

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
        """Atomically publish dossier, apply outcome and durable COMPLETED state."""
        if source_apply_outcome not in {"applied", "skipped", "not-requested"}:
            raise ValueError(f"invalid source apply outcome: {source_apply_outcome}")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job = self._conn.execute(
                    """
                    SELECT state, stop_requested, base_branch, integration_branch,
                           acceptance_phase, accepted_commit, source_apply_intent,
                           source_apply_outcome
                    FROM jobs WHERE id = ?
                    """,
                    (job_id,),
                ).fetchone()
                if job is None:
                    raise KeyError(job_id)
                current = JobState(str(job["state"]))
                phase = str(job["acceptance_phase"])

                dossier = self._conn.execute(
                    """
                    SELECT schema_version, sha256, content, published
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
                if not _digest_matches(str(dossier["content"]), dossier_sha256):
                    raise StateTransitionError("staged dossier content failed SHA-256 verification")

                if current == JobState.COMPLETED:
                    if bool(dossier["published"]) and phase == "published":
                        if str(job["source_apply_outcome"]) != source_apply_outcome:
                            raise StateTransitionError(
                                "completed job source-apply outcome changed across idempotent publication"
                            )
                        self._conn.execute("COMMIT")
                        return
                    raise StateTransitionError("completed job contains an unpublished acceptance")

                intent = str(job["source_apply_intent"])
                if intent == "not-requested":
                    if phase != "accepted" or source_apply_outcome != "not-requested":
                        raise StateTransitionError(
                            "non-applied acceptance must publish from accepted/not-requested state"
                        )
                elif intent == "requested":
                    if phase != "applying" or source_apply_outcome not in {"applied", "skipped"}:
                        raise StateTransitionError(
                            "requested source application must publish from applying with applied/skipped outcome"
                        )
                else:
                    raise StateTransitionError(f"invalid durable source apply intent: {intent}")
                if not str(job["accepted_commit"]):
                    raise StateTransitionError("accepted job is missing accepted_commit")

                validate_job_transition(current, JobState.COMPLETED)
                _, task_states, running_owners, _, _ = self._snapshot_locked(job_id)
                validate_job_snapshot(
                    JobState.COMPLETED,
                    task_states,
                    stop_requested=False,
                    running_attempt_task_states=running_owners,
                    acceptance_phase="published",
                )

                now = utc_ts()
                cur = self._conn.execute(
                    """
                    UPDATE jobs
                    SET state = ?, stop_requested = 0, final_summary = ?, error = '',
                        acceptance_phase = 'published', source_apply_outcome = ?,
                        source_apply_reason = ?, updated_at = ?
                    WHERE id = ? AND state = ? AND acceptance_phase = ?
                    """,
                    (
                        JobState.COMPLETED.value,
                        final_summary,
                        source_apply_outcome,
                        source_apply_reason[-4_000:],
                        now,
                        job_id,
                        current.value,
                        phase,
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
                                "accepted_commit": str(job["accepted_commit"]),
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
                            json_dumps(
                                {
                                    "branch": str(job["base_branch"]),
                                    "accepted_commit": str(job["accepted_commit"]),
                                }
                            ),
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
                                "accepted_commit": str(job["accepted_commit"]),
                                "source_apply_outcome": source_apply_outcome,
                            }
                        ),
                        now,
                    ),
                )
                self._validate_snapshot_locked(job_id)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
