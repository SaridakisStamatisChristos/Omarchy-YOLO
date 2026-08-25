from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from omarchy_yolo.config import Config, GitConfig, load_config
from omarchy_yolo.db import Database
from omarchy_yolo.git import CommandOutput, GitError, GitRepo
from omarchy_yolo.model import JobState, PlannedTask, TaskState
from omarchy_yolo.orchestrator import Orchestrator
from omarchy_yolo.state_machine import StateTransitionError, validate_job_snapshot
from omarchy_yolo.util import YoloError


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


def test_attempt_lifecycle_has_one_owner_and_one_terminal_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "attempt-lifecycle" / "state.sqlite3")
    try:
        job = db.create_job(
            repo=str(tmp_path / "repo-attempt-lifecycle"),
            goal="close attempt lifecycle",
            base_branch="main",
            base_commit="a" * 40,
            integration_branch="yolo/attempt-lifecycle/integration",
            auto_apply=False,
        )
        task = db.add_tasks(
            job.id,
            [PlannedTask("T1", "Attempt", "Prove single ownership")],
        )[0]
        db.update_job(job.id, state=JobState.RUNNING)
        db.update_task(task.id, state=TaskState.RUNNING, attempts=1)
        attempt = db.start_attempt(
            job_id=job.id,
            task_id=task.id,
            number=1,
            agent="fake",
            worktree="/tmp/attempt-1",
            branch="yolo/attempt-1",
            log_path="/tmp/attempt-1.log",
        )

        with pytest.raises(StateTransitionError, match="terminal attempt state"):
            db.finish_attempt(attempt, state="running", returncode=None, summary="not done")
        with pytest.raises(StateTransitionError, match="already has running attempt"):
            db.start_attempt(
                job_id=job.id,
                task_id=task.id,
                number=2,
                agent="fake",
                worktree="/tmp/attempt-2",
                branch="yolo/attempt-2",
                log_path="/tmp/attempt-2.log",
            )

        db.finish_attempt(attempt, state="passed", returncode=0, summary="accepted")
        with pytest.raises(StateTransitionError, match="already finished"):
            db.finish_attempt(attempt, state="failed", returncode=1, summary="rewrite")
    finally:
        db.close()


def test_accepted_job_metadata_is_frozen_but_stop_race_remains_modeled(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "accepted-immutability" / "state.sqlite3")
    try:
        job_id, _ = _completed_candidate(db, tmp_path, suffix="accepted-immutability")
        _prepare_acceptance(db, job_id)

        with pytest.raises(StateTransitionError, match="metadata is immutable"):
            db.update_job(job_id, final_summary="rewritten after acceptance")
        with pytest.raises(StateTransitionError, match="metadata is immutable"):
            db.update_job(job_id, integration_path="/tmp/replaced")

        db.update_job(job_id, state=JobState.STOPPING, stop_requested=True)
        accepted = db.get_job(job_id)
        assert accepted.state == JobState.STOPPING
        assert accepted.stop_requested
        assert accepted.final_summary == "accepted"
    finally:
        db.close()


async def test_staged_dossier_is_verified_before_source_application(tmp_path: Path) -> None:
    class ApplyProbe:
        def __init__(self, root: Path):
            self.root = root
            self.called = False

        def reconcile_fast_forward_source(self, *_args: str) -> tuple[str, str]:
            self.called = True
            return "applied", "must not run"

    cfg = Config(
        state_dir=tmp_path / "dossier-integrity" / "state",
        config_path=tmp_path / "dossier-integrity" / "config.toml",
    )
    db = Database(cfg.db_path)
    try:
        job_id, _ = _completed_candidate(
            db,
            tmp_path,
            suffix="dossier-integrity",
            auto_apply=True,
        )
        _prepare_acceptance(db, job_id, intent="requested")
        db._execute(
            "UPDATE dossiers SET content = ? WHERE job_id = ?",
            ('{"accepted":false}', job_id),
        )
        repo_root = tmp_path / "dossier-integrity" / "repo"
        repo_root.mkdir(parents=True)
        repo = ApplyProbe(repo_root)
        orchestrator = Orchestrator(cfg, db)

        with pytest.raises(YoloError, match="SHA-256 verification"):
            await orchestrator._resume_accepted_job(db.get_job(job_id), repo)  # type: ignore[arg-type]

        assert not repo.called
        assert db.get_job(job_id).acceptance_phase == "accepted"
    finally:
        db.close()


def test_hostile_repository_preset_is_coherent_and_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "hostile.toml"
    path.write_text('[safety]\npreset = "hostile-repo"\n')
    loaded = load_config(path)
    assert loaded.safety_preset == "hostile-repo"
    assert loaded.sandbox.backend == "bwrap"
    assert loaded.sandbox.hostile_repo_mode
    assert loaded.sandbox.read_only_home
    assert loaded.sandbox.network_for("review") is False
    assert loaded.sandbox.network_for("gate") is False
    assert loaded.git.allow_repository_commands is False
    assert loaded.trust_posture()[0] == "hostile-repo"

    conflict = tmp_path / "hostile-conflict.toml"
    conflict.write_text(
        '[safety]\npreset = "hostile-repo"\n[sandbox]\nreview_network = true\n'
    )
    with pytest.raises(YoloError, match="conflicts with sandbox.review_network"):
        load_config(conflict)


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

    (git_repo / "user-follow-up.txt").write_text("follow-up\n")
    follow_up = repo.commit_all(git_repo, "user follow-up", GitConfig())
    outcome, reason = repo.reconcile_fast_forward_source(
        integration_branch,
        base_branch,
        base_commit,
        accepted_commit,
    )
    assert outcome == "applied"
    assert "already contains accepted commit" in reason
    assert repo.head() == follow_up

    outcome, reason = repo.reconcile_fast_forward_source(
        integration_branch,
        "HEAD",
        base_commit,
        accepted_commit,
    )
    assert outcome == "skipped"
    assert "detached" in reason


def test_git_reconcile_merges_journaled_commit_not_racing_branch(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = GitRepo.discover(git_repo)
    base_branch, base_commit = repo.preflight(require_clean=True)
    integration_branch = "yolo/v142/racing-ref"
    integration = tmp_path / "integration-v142-racing-ref"
    repo.ensure_existing_branch_worktree(integration, integration_branch, base_commit)
    (integration / "accepted.txt").write_text("accepted\n")
    accepted_commit = repo.commit_all(integration, "accepted candidate", GitConfig())
    (integration / "unaccepted.txt").write_text("unaccepted\n")
    unaccepted_commit = repo.commit_all(integration, "unaccepted descendant", GitConfig())
    repo.run("reset", "--hard", accepted_commit, cwd=integration)

    original_run = repo.run
    ref_moved = False

    def racing_run(*args: str, **kwargs: object):
        nonlocal ref_moved
        if not ref_moved and "merge" in args and "--ff-only" in args:
            original_run(
                "update-ref",
                f"refs/heads/{integration_branch}",
                unaccepted_commit,
                accepted_commit,
            )
            ref_moved = True
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "run", racing_run)
    outcome, _ = repo.reconcile_fast_forward_source(
        integration_branch,
        base_branch,
        base_commit,
        accepted_commit,
    )

    assert ref_moved
    assert outcome == "applied"
    assert repo.head() == accepted_commit


def test_git_reconcile_skips_clean_source_moved_to_sibling(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    repo = GitRepo.discover(git_repo)
    base_branch, base_commit = repo.preflight(require_clean=True)
    integration_branch = "yolo/v142/source-sibling"
    integration = tmp_path / "integration-v142-source-sibling"
    repo.ensure_existing_branch_worktree(integration, integration_branch, base_commit)
    (integration / "accepted.txt").write_text("accepted\n")
    accepted_commit = repo.commit_all(integration, "accepted candidate", GitConfig())

    (git_repo / "user-change.txt").write_text("user change\n")
    moved_commit = repo.commit_all(git_repo, "user source change", GitConfig())
    outcome, reason = repo.reconcile_fast_forward_source(
        integration_branch,
        base_branch,
        base_commit,
        accepted_commit,
    )

    assert outcome == "skipped"
    assert "moved since job creation" in reason
    assert repo.head() == moved_commit


@pytest.mark.parametrize(
    ("ancestry_output", "message"),
    [
        (CommandOutput(1, "", "", timed_out=True), "timed out"),
        (CommandOutput(1, "", "", output_truncated=True), "oversized output"),
        (CommandOutput(2, "", "fatal: synthetic ancestry failure"), "synthetic ancestry"),
    ],
)
def test_git_reconcile_fails_closed_when_ancestry_cannot_be_proved(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestry_output: CommandOutput,
    message: str,
) -> None:
    repo = GitRepo.discover(git_repo)
    base_branch, base_commit = repo.preflight(require_clean=True)
    integration_branch = "yolo/v142/ancestry-error"
    integration = tmp_path / "integration-v142-ancestry-error"
    repo.ensure_existing_branch_worktree(integration, integration_branch, base_commit)
    (integration / "accepted.txt").write_text("accepted\n")
    accepted_commit = repo.commit_all(integration, "accepted candidate", GitConfig())
    (git_repo / "user-change.txt").write_text("user change\n")
    moved_commit = repo.commit_all(git_repo, "user source change", GitConfig())

    original_run = repo.run

    def intercept_run(*args: str, **kwargs: object):
        if args[:2] == ("merge-base", "--is-ancestor"):
            return ancestry_output
        return original_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(repo, "run", intercept_run)
    with pytest.raises(GitError, match=message):
        repo.reconcile_fast_forward_source(
            integration_branch,
            base_branch,
            base_commit,
            accepted_commit,
        )
    assert repo.head() == moved_commit
