from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.agents import AgentRegistry
from omarchy_yolo.agents.base import CommandAgent
from omarchy_yolo.config import AgentConfig, Config
from omarchy_yolo.db import Database
from omarchy_yolo.git import GitRepo
from omarchy_yolo.model import AgentResult, JobState, PlannedTask, TaskState
from omarchy_yolo.orchestrator import Orchestrator
from omarchy_yolo.reviewer import Reviewer
from omarchy_yolo.util import YoloError


class EmptyNonPassReviewer:
    name = "reviewer"

    def available(self) -> bool:
        return True

    def supports_profile(self, execution_profile: str) -> bool:
        return execution_profile == "review"

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
        execution_profile: str,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        return AgentResult(
            0,
            '{"verdict":"retry","summary":"","findings":[]}',
            "",
            0.01,
            ("reviewer",),
        )


async def test_nonpass_review_requires_actionable_detail(tmp_path: Path) -> None:
    config = Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml")
    registry = AgentRegistry(config, overrides={"reviewer": EmptyNonPassReviewer()})
    reviewer = Reviewer(registry)

    with pytest.raises(YoloError, match="non-pass verdict"):
        await reviewer.review_final(
            goal="goal",
            diff="diff",
            gates=[],
            cwd=tmp_path,
            agent_name="reviewer",
            timeout_seconds=5,
            log_path=tmp_path / "review.log",
        )


def test_builtin_name_does_not_spoof_read_only_review_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omarchy_yolo.agents.base.shutil.which",
        lambda executable: f"/usr/bin/{Path(executable).name}",
    )
    config = Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml")
    spoofed = CommandAgent(
        "codex",
        AgentConfig(command=("python", "-V")),
        config,
    )
    assert spoofed.available()
    assert not spoofed.supports_profile("review")
    with pytest.raises(YoloError, match="no declared read-only review capability"):
        spoofed.command_for_profile("review")

    explicit = CommandAgent(
        "codex",
        AgentConfig(
            command=("python", "worker"),
            review_command=("python", "review"),
        ),
        config,
    )
    assert explicit.supports_profile("review")
    assert explicit.command_for_profile("review") == ["python", "review"]


def test_missing_custom_review_executable_is_not_advertised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def which(executable: str) -> str | None:
        return None if executable == "missing-review" else f"/usr/bin/{executable}"

    monkeypatch.setattr("omarchy_yolo.agents.base.shutil.which", which)
    config = Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml")
    agent = CommandAgent(
        "custom",
        AgentConfig(
            command=("python",),
            review_command=("missing-review",),
        ),
        config,
    )
    assert agent.available()
    assert not agent.supports_profile("review")


async def test_cleanup_failure_cannot_rewrite_completed_release(
    git_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = tmp_path / "state"
    config = Config(state_dir=state, config_path=tmp_path / "config.toml")
    db = Database(config.db_path)
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    job = db.create_job(
        repo=str(repo.root),
        goal="already satisfied",
        base_branch=branch,
        base_commit=base,
        integration_branch="yolo/cleanup-test/integration",
        auto_apply=False,
    )
    task = db.add_tasks(job.id, [PlannedTask("T1", "Done", "Already completed")])[0]
    db.update_task(task.id, state=TaskState.RUNNING)
    db.update_task(task.id, state=TaskState.REVIEWING)
    db.update_task(task.id, state=TaskState.INTEGRATING)
    db.update_task(task.id, state=TaskState.COMPLETED, result_summary="done")

    orchestrator = Orchestrator(config, db)

    async def finalize(job_id: str, repo_arg: GitRepo, integration_path: Path) -> str:
        assert job_id == job.id
        assert repo_arg.root == repo.root
        assert integration_path.name == "integration"
        return "release passed"

    async def cleanup(repo_arg: GitRepo, job_id: str, integration_path: Path) -> None:
        raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(orchestrator, "_finalize", finalize)
    monkeypatch.setattr(orchestrator, "_cleanup_completed", cleanup)

    await orchestrator.run_job(job.id)

    finished = db.get_job(job.id)
    assert finished.state == JobState.COMPLETED
    assert finished.final_summary == "release passed"
    events = db.events(job.id, limit=100)
    assert any(event["kind"] == "job.cleanup_failed" for event in events)
    assert not any(event["kind"] == "job.failed" for event in events)
    db.close()
