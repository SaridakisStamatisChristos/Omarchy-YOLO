from __future__ import annotations

from pathlib import Path

from omarchy_yolo.db import Database
from omarchy_yolo.model import JobState, PlannedTask, TaskState


def test_database_job_task_event_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    job = db.create_job(repo="/repo", goal="goal", base_branch="main", base_commit="abc", integration_branch="yolo/test/integration", auto_apply=False)
    assert job.state == JobState.QUEUED
    tasks = db.add_tasks(job.id, [PlannedTask("T1", "Title", "Description")])
    assert len(tasks) == 1
    db.update_task(tasks[0].id, state=TaskState.RUNNING, attempts=1)
    assert db.get_task(tasks[0].id).attempts == 1
    event_id = db.event(job.id, "test.event", {"x": 1}, task_id=tasks[0].id)
    events = db.events(job.id, after_id=event_id - 1)
    assert events[-1]["payload"] == {"x": 1}
    db.request_stop(job.id)
    assert db.get_job(job.id).stop_requested
    db.close()


def test_recovery_cancels_running_attempt_but_preserves_history(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    job = db.create_job(repo="/repo", goal="goal", base_branch="main", base_commit="abc", integration_branch="yolo/recover/integration", auto_apply=False)
    task = db.add_tasks(job.id, [PlannedTask("T1", "Title", "Description")])[0]
    db.update_job(job.id, state=JobState.RUNNING)
    db.update_task(task.id, state=TaskState.RUNNING, attempts=1)
    attempt_id = db.start_attempt(job_id=job.id, task_id=task.id, number=1, agent="fake", worktree="/tmp/wt", branch="yolo/t1", log_path="/tmp/a.log")
    assert db.recover_incomplete() == [job.id]
    assert db.get_job(job.id).state == JobState.QUEUED
    recovered_task = db.get_task(task.id)
    assert recovered_task.state == TaskState.PENDING
    assert recovered_task.attempts == 1
    row = db._execute("SELECT state, summary FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert row is not None
    assert row["state"] == "cancelled"
    assert "daemon restart" in row["summary"]
    db.close()
