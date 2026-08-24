from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.db import Database
from omarchy_yolo.model import PlannedTask


def test_db_mutation_guards_fail_closed_on_unknown_empty_and_missing_records(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "state.sqlite3")
    try:
        job = db.create_job(
            repo="/repo",
            goal="guard mutations",
            base_branch="main",
            base_commit="a" * 40,
            integration_branch="yolo/guards/integration",
            auto_apply=False,
        )
        task = db.add_tasks(job.id, [PlannedTask("T1", "Guard", "Exercise guards")])[0]

        with pytest.raises(ValueError, match="unsupported job fields"):
            db.update_job(job.id, unknown_field=True)
        db.update_job(job.id)
        with pytest.raises(KeyError):
            db.update_job("job_missing0000", error="missing")

        with pytest.raises(ValueError, match="unsupported task fields"):
            db.update_task(task.id, unknown_field=True)
        db.update_task(task.id)
        with pytest.raises(KeyError):
            db.update_task("task_missing000", last_error="missing")

        with pytest.raises(KeyError):
            db.start_attempt(
                job_id=job.id,
                task_id="task_missing000",
                number=1,
                agent="fake",
                worktree="/tmp/missing",
                branch="yolo/missing",
                log_path="/tmp/missing.log",
            )
        with pytest.raises(KeyError):
            db.finish_attempt(
                "attempt_missing0",
                state="failed",
                returncode=1,
                summary="missing",
            )
    finally:
        db.close()
