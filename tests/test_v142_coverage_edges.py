from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omarchy_yolo.config import GitConfig
from omarchy_yolo.db import Database
from omarchy_yolo.git import GitRepo
from omarchy_yolo.model import JobState, PlannedTask, TaskState
from omarchy_yolo.state_machine import StateTransitionError, validate_job_snapshot


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _completed_candidate(
    db: Database,
    root: Path,
    *,
    suffix: str,
    auto_apply: bool = False,
) -> tuple[str, str]:
    job = db.create_job(
        repo=str(root / f"repo-{suffix}"),
        goal=f"v1.4.2 coverage {suffix}",
        base_branch="main",
        base_commit="a" * 40,
        integration_branch=f"yolo/{suffix}/integration",
        auto_apply=auto_apply,
    )
    task = db.add_tasks(
        job.id,
        [PlannedTask("T1", "Accepted task", "Exercise the durable trust boundary")],
    )[0]
    db.update_job(job.id, state=JobState.RUNNING)
    db.update_task(task.id, state=TaskState.RUNNING)
    db.update_task(task.id, state=TaskState.REVIEWING)
    db.update_task(task.id, state=TaskState.INTEGRATING)
    db.update_task(task.id, state=TaskState.COMPLETED, result_summary="accepted")
    return job.id, task.id


def _prepare_acceptance(
    db: Database,
    job_id: str,
    *,
    content: str = '{"accepted":true}',
    intent: str = "not-requested",
    summary: str = "accepted",
    accepted_commit: str = "b" * 40,
) -> str:
    digest = _digest(content)
    db.prepare_accepted_job(
        job_id,
        accepted_commit=accepted_commit,
        final_summary=summary,
        source_apply_intent=intent,
        dossier_schema_version=1,
        dossier_sha256=digest,
        dossier_content=content,
    )
    return digest


def test_state_machine_rejects_v142_cross_record_impossibilities() -> None:
    with pytest.raises(StateTransitionError, match="cannot retain a running attempt"):
        validate_job_snapshot(
            JobState.QUEUED,
            (TaskState.COMPLETED,),
            stop_requested=False,
            running_attempt_task_states=(TaskState.RUNNING,),
            acceptance_phase="none",
        )

    with pytest.raises(StateTransitionError, match="task that is not in flight"):
        validate_job_snapshot(
            JobState.RUNNING,
            (TaskState.COMPLETED,),
            stop_requested=False,
            running_attempt_task_states=(TaskState.COMPLETED,),
            acceptance_phase="none",
        )

    with pytest.raises(StateTransitionError, match="unknown acceptance phase"):
        validate_job_snapshot(
            JobState.RUNNING,
            (TaskState.COMPLETED,),
            stop_requested=False,
            acceptance_phase="corrupt",
        )

    with pytest.raises(StateTransitionError, match="non-completed task"):
        validate_job_snapshot(
            JobState.RUNNING,
            (TaskState.PENDING,),
            stop_requested=False,
            acceptance_phase="accepted",
        )

    with pytest.raises(StateTransitionError, match="cannot retain a running attempt"):
        validate_job_snapshot(
            JobState.RUNNING,
            (TaskState.COMPLETED,),
            stop_requested=False,
            running_attempt_task_states=(TaskState.RUNNING,),
            acceptance_phase="accepted",
        )

    with pytest.raises(StateTransitionError, match="requires running or stopping"):
        validate_job_snapshot(
            JobState.QUEUED,
            (TaskState.COMPLETED,),
            stop_requested=False,
            acceptance_phase="accepted",
        )

    with pytest.raises(StateTransitionError, match="published acceptance requires completed"):
        validate_job_snapshot(
            JobState.RUNNING,
            (TaskState.COMPLETED,),
            stop_requested=False,
            acceptance_phase="published",
        )

    with pytest.raises(StateTransitionError, match="requires published acceptance"):
        validate_job_snapshot(
            JobState.COMPLETED,
            (TaskState.COMPLETED,),
            stop_requested=False,
            acceptance_phase="none",
        )

    with pytest.raises(StateTransitionError, match="preserve stop_requested"):
        validate_job_snapshot(
            JobState.STOPPED,
            (TaskState.COMPLETED,),
            stop_requested=False,
            acceptance_phase="none",
        )

    with pytest.raises(StateTransitionError, match="cannot retain stop_requested"):
        validate_job_snapshot(
            JobState.COMPLETED,
            (TaskState.COMPLETED,),
            stop_requested=True,
            acceptance_phase="published",
        )


def test_recovery_guards_and_accepted_reconciliation_state(tmp_path: Path) -> None:
    db = Database(tmp_path / "recovery" / "state.sqlite3")
    try:
        assert db.recover_incomplete() == []

        running = db.create_job(
            repo=str(tmp_path / "running"),
            goal="running",
            base_branch="main",
            base_commit="a" * 40,
            integration_branch="yolo/running/integration",
            auto_apply=False,
        )
        db.update_job(running.id, state=JobState.RUNNING)
        with pytest.raises(StateTransitionError, match="while job is running"):
            db.retry_failed_tasks(running.id)

        with pytest.raises(ValueError, match="invalid settled task state"):
            db.settle_inflight(
                running.id,
                task_state=TaskState.COMPLETED,
                attempt_state="cancelled",
                summary="invalid",
            )
        with pytest.raises(ValueError, match="invalid settled attempt state"):
            db.settle_inflight(
                running.id,
                task_state=TaskState.PENDING,
                attempt_state="passed",
                summary="invalid",
            )
        with pytest.raises(KeyError):
            db.settle_inflight(
                "job_missing_recovery",
                task_state=TaskState.PENDING,
                attempt_state="cancelled",
                summary="missing",
            )
        with pytest.raises(KeyError):
            db.prepare_resume("job_missing_resume")

        accepted_id, _ = _completed_candidate(db, tmp_path, suffix="accepted-recovery")
        _prepare_acceptance(db, accepted_id)
        db._execute(
            "UPDATE jobs SET stop_requested = 1, error = 'crash-window' WHERE id = ?",
            (accepted_id,),
        )
        recovered = db.recover_incomplete()
        assert accepted_id in recovered
        accepted = db.get_job(accepted_id)
        assert accepted.state == JobState.RUNNING
        assert accepted.acceptance_phase == "accepted"
        assert not accepted.stop_requested
        assert accepted.error == ""
        assert any(
            event["kind"] == "job.recovered_acceptance"
            for event in db.events(accepted_id, limit=100)
        )

        with pytest.raises(StateTransitionError, match="after acceptance begins"):
            db.retry_failed_tasks(accepted_id)
        with pytest.raises(StateTransitionError, match="accepted job cannot be settled"):
            db.settle_inflight(
                accepted_id,
                task_state=TaskState.PENDING,
                attempt_state="cancelled",
                summary="must not regress",
            )
    finally:
        db.close()


def test_provenance_guards_and_idempotent_apply_journal(tmp_path: Path) -> None:
    db = Database(tmp_path / "provenance" / "state.sqlite3")
    try:
        content = '{"accepted":true}'
        digest = _digest(content)

        with pytest.raises(KeyError):
            db.stage_dossier(
                "job_missing_stage",
                schema_version=1,
                sha256=digest,
                content=content,
            )

        requested_id, _ = _completed_candidate(
            db,
            tmp_path,
            suffix="requested",
            auto_apply=True,
        )

        with pytest.raises(ValueError, match="source apply intent"):
            db.prepare_accepted_job(
                requested_id,
                accepted_commit="b" * 40,
                final_summary="accepted",
                source_apply_intent="invalid",
                dossier_schema_version=1,
                dossier_sha256=digest,
                dossier_content=content,
            )
        with pytest.raises(ValueError, match="accepted_commit"):
            db.prepare_accepted_job(
                requested_id,
                accepted_commit="",
                final_summary="accepted",
                source_apply_intent="requested",
                dossier_schema_version=1,
                dossier_sha256=digest,
                dossier_content=content,
            )
        with pytest.raises(ValueError, match="schema_version"):
            db.prepare_accepted_job(
                requested_id,
                accepted_commit="b" * 40,
                final_summary="accepted",
                source_apply_intent="requested",
                dossier_schema_version=0,
                dossier_sha256=digest,
                dossier_content=content,
            )
        with pytest.raises(StateTransitionError, match="invalid SHA-256"):
            db.prepare_accepted_job(
                requested_id,
                accepted_commit="b" * 40,
                final_summary="accepted",
                source_apply_intent="requested",
                dossier_schema_version=1,
                dossier_sha256="0" * 64,
                dossier_content=content,
            )
        with pytest.raises(KeyError):
            db.prepare_accepted_job(
                "job_missing_prepare",
                accepted_commit="b" * 40,
                final_summary="accepted",
                source_apply_intent="requested",
                dossier_schema_version=1,
                dossier_sha256=digest,
                dossier_content=content,
            )

        _prepare_acceptance(
            db,
            requested_id,
            content=content,
            intent="requested",
            summary="accepted",
        )
        # Exact replay is intentionally idempotent.
        _prepare_acceptance(
            db,
            requested_id,
            content=content,
            intent="requested",
            summary="accepted",
        )
        with pytest.raises(StateTransitionError, match="metadata changed"):
            _prepare_acceptance(
                db,
                requested_id,
                content=content,
                intent="requested",
                summary="changed",
            )

        with pytest.raises(StateTransitionError, match="accepted commit changed"):
            db.mark_apply_started(requested_id, accepted_commit="c" * 40)
        db.mark_apply_started(requested_id, accepted_commit="b" * 40)
        db.mark_apply_started(requested_id, accepted_commit="b" * 40)

        not_requested_id, _ = _completed_candidate(
            db,
            tmp_path,
            suffix="not-requested",
        )
        _prepare_acceptance(db, not_requested_id, content=content)
        with pytest.raises(StateTransitionError, match="was not requested"):
            db.mark_apply_started(not_requested_id, accepted_commit="b" * 40)

        with pytest.raises(KeyError):
            db.mark_apply_started("job_missing_apply", accepted_commit="b" * 40)
    finally:
        db.close()


def test_git_reconcile_fast_forward_is_idempotent(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    repo = GitRepo.discover(git_repo)
    base_branch, base_commit = repo.preflight(require_clean=True)
    integration_branch = "yolo/v142/reconcile"
    integration = tmp_path / "integration-v142"
    repo.ensure_existing_branch_worktree(integration, integration_branch, base_commit)
    (integration / "accepted-v142.txt").write_text("accepted\n")
    accepted_commit = repo.commit_all(
        integration,
        "accepted v1.4.2 candidate",
        GitConfig(),
    )

    outcome, reason = repo.reconcile_fast_forward_source(
        integration_branch,
        base_branch,
        base_commit,
        accepted_commit,
    )
    assert outcome == "applied"
    assert "fast-forwarded" in reason
    assert repo.head() == accepted_commit

    outcome, reason = repo.reconcile_fast_forward_source(
        integration_branch,
        base_branch,
        base_commit,
        accepted_commit,
    )
    assert outcome == "applied"
    assert "already at accepted commit" in reason

    outcome, reason = repo.reconcile_fast_forward_source(
        integration_branch,
        "HEAD",
        base_commit,
        accepted_commit,
    )
    assert outcome == "skipped"
    assert "detached" in reason
