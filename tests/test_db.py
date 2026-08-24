from __future__ import annotations

from pathlib import Path

from omarchy_yolo.db import Database
from omarchy_yolo.model import JobState, PlannedTask, TaskState


def test_database_job_task_event_lifecycle(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    job = db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/test/integration",
        auto_apply=False,
    )
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
    job = db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/recover/integration",
        auto_apply=False,
    )
    task = db.add_tasks(job.id, [PlannedTask("T1", "Title", "Description")])[0]
    db.update_job(job.id, state=JobState.RUNNING)
    db.update_task(task.id, state=TaskState.RUNNING, attempts=1)
    attempt_id = db.start_attempt(
        job_id=job.id,
        task_id=task.id,
        number=1,
        agent="fake",
        worktree="/tmp/wt",
        branch="yolo/t1",
        log_path="/tmp/a.log",
    )

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


def test_recovery_preserves_explicit_stop(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    job = db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/stop/integration",
        auto_apply=False,
    )
    task = db.add_tasks(job.id, [PlannedTask("T1", "Title", "Description")])[0]
    db.update_job(job.id, state=JobState.STOPPING, stop_requested=True)
    db.update_task(task.id, state=TaskState.RUNNING, attempts=1)
    attempt_id = db.start_attempt(
        job_id=job.id,
        task_id=task.id,
        number=1,
        agent="fake",
        worktree="/tmp/wt",
        branch="yolo/t1",
        log_path="/tmp/a.log",
    )

    assert db.recover_incomplete() == []
    recovered = db.get_job(job.id)
    assert recovered.state == JobState.STOPPED
    assert recovered.stop_requested
    assert db.get_task(task.id).state == TaskState.STOPPED
    row = db._execute("SELECT state FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert row is not None and row["state"] == "cancelled"
    db.close()


def test_prepare_resume_closes_stale_attempt_and_preserves_attempt_number(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    job = db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/resume/integration",
        auto_apply=False,
    )
    task = db.add_tasks(job.id, [PlannedTask("T1", "Title", "Description")])[0]
    db.update_job(job.id, state=JobState.FAILED, error="boom")

    # Recovery tests intentionally emulate a stale pre-v1.4.1 durable snapshot.
    # The guarded public mutation API must reject FAILED + in-flight task, so seed
    # that historical state directly and verify prepare_resume repairs it atomically.
    db._execute(
        "UPDATE tasks SET state = ?, attempts = ?, last_error = ? WHERE id = ?",
        (TaskState.REVIEWING.value, 3, "useful context", task.id),
    )
    attempt_id = db.start_attempt(
        job_id=job.id,
        task_id=task.id,
        number=3,
        agent="fake",
        worktree="/tmp/wt",
        branch="yolo/t1",
        log_path="/tmp/a.log",
    )

    db.prepare_resume(job.id)
    resumed = db.get_job(job.id)
    assert resumed.state == JobState.QUEUED
    assert not resumed.stop_requested
    task_after = db.get_task(task.id)
    assert task_after.state == TaskState.PENDING
    assert task_after.attempts == 3
    assert task_after.last_error == "useful context"
    row = db._execute("SELECT state FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert row is not None and row["state"] == "cancelled"
    db.close()


def test_list_limits_are_capped(tmp_path: Path) -> None:
    from omarchy_yolo.db import MAX_EVENT_LIST_LIMIT, MAX_JOB_LIST_LIMIT

    db = Database(tmp_path / "state.db")
    job = db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/limits/integration",
        auto_apply=False,
    )
    for i in range(MAX_EVENT_LIST_LIMIT + 5):
        db.event(job.id, "test", {"i": i})
    assert len(db.events(job.id, limit=10**9)) == MAX_EVENT_LIST_LIMIT
    assert len(db.list_jobs(limit=10**9)) <= MAX_JOB_LIST_LIMIT
    db.close()


def test_event_payload_is_bounded_at_rest(tmp_path: Path) -> None:
    from omarchy_yolo.db import MAX_EVENT_PAYLOAD_CHARS

    db = Database(tmp_path / "state.db")
    job = db.create_job(
        repo="/repo",
        goal="goal",
        base_branch="main",
        base_commit="abc",
        integration_branch="yolo/event/integration",
        auto_apply=False,
    )
    db.event(job.id, "huge", {"text": "x" * 100_000})
    row = db._execute("SELECT payload FROM events WHERE kind = 'huge'").fetchone()
    assert row is not None
    assert len(row["payload"]) <= MAX_EVENT_PAYLOAD_CHARS
    payload = __import__("json").loads(row["payload"])
    assert payload["truncated"] is True
    db.close()
