from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omarchy_yolo.config import Config, EngineConfig, GateConfig, GitConfig
from omarchy_yolo.db import Database
from omarchy_yolo.git import GitRepo
from omarchy_yolo.model import GateResult, PlannedTask, ReviewResult
from omarchy_yolo.orchestrator import Orchestrator
from omarchy_yolo.review_source import ReviewChunk
from omarchy_yolo.util import YoloError


class SemanticReviewerStub:
    def __init__(self, *, reject_first_chunk: bool = False) -> None:
        self.reject_first_chunk = reject_first_chunk
        self.chunk_calls = 0
        self.file_calls = 0
        self.global_calls = 0

    async def review_final_chunk(self, **_: Any) -> ReviewResult:
        self.chunk_calls += 1
        if self.reject_first_chunk and self.chunk_calls == 1:
            return ReviewResult("retry", "shard exposes a fixable defect", ("repair shard",))
        return ReviewResult(
            "pass",
            f"shard {self.chunk_calls} preserves the file contract",
            (),
        )

    async def review_file_synthesis(self, **_: Any) -> ReviewResult:
        self.file_calls += 1
        return ReviewResult("pass", "whole-file API and state transitions are coherent", ())

    async def review_final_synthesis(self, **_: Any) -> ReviewResult:
        self.global_calls += 1
        return ReviewResult("pass", "cross-file release contracts are coherent", ())


def _job_and_integration(
    git_repo: Path,
    tmp_path: Path,
    *,
    engine: EngineConfig | None = None,
) -> tuple[Config, Database, GitRepo, str, Path, str]:
    config = Config(
        state_dir=tmp_path / "state",
        config_path=tmp_path / "config.toml",
        engine=engine or EngineConfig(cleanup_worktrees=False),
        gates=GateConfig(commands=("git diff --check",), final_commands=("git diff --check",)),
    )
    db = Database(config.db_path)
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    job = db.create_job(
        repo=str(repo.root),
        goal="change the candidate safely",
        base_branch=branch,
        base_commit=base,
        integration_branch="yolo/v12/integration",
        auto_apply=False,
    )
    integration = tmp_path / "integration"
    repo.ensure_existing_branch_worktree(integration, job.integration_branch, base)
    db.update_job(job.id, integration_path=str(integration))
    return config, db, repo, job.id, integration, base


async def test_hierarchical_review_synthesizes_multishard_file(
    git_repo: Path, tmp_path: Path
) -> None:
    engine = EngineConfig(
        cleanup_worktrees=False,
        final_review_chunk_bytes=20_000,
        final_review_chunk_files=8,
        final_review_max_files=20,
    )
    config, db, repo, job_id, integration, _ = _job_and_integration(
        git_repo, tmp_path, engine=engine
    )
    (integration / "large.py").write_text("value = '" + ("x" * 90_000) + "'\n")
    repo.commit_all(integration, "large semantic change", GitConfig())

    orchestrator = Orchestrator(config, db)
    reviewer = SemanticReviewerStub()
    orchestrator.reviewer = reviewer  # type: ignore[assignment]
    result = await orchestrator._hierarchical_final_review(
        job_id,
        repo,
        integration,
        [],
        "semantic",
        0,
    )
    assert result.passed
    assert reviewer.chunk_calls >= 4
    assert reviewer.file_calls == 1
    assert reviewer.global_calls == 1
    kinds = [event["kind"] for event in db.events(job_id, limit=100)]
    assert "final.review_file" in kinds
    assert "final.review_synthesis" in kinds
    db.close()


async def test_hierarchical_review_stops_before_synthesis_on_shard_defect(
    git_repo: Path, tmp_path: Path
) -> None:
    config, db, repo, job_id, integration, _ = _job_and_integration(git_repo, tmp_path)
    (integration / "change.py").write_text("VALUE = 1\n")
    repo.commit_all(integration, "change", GitConfig())
    orchestrator = Orchestrator(config, db)
    reviewer = SemanticReviewerStub(reject_first_chunk=True)
    orchestrator.reviewer = reviewer  # type: ignore[assignment]
    result = await orchestrator._hierarchical_final_review(
        job_id, repo, integration, [], "semantic", 0
    )
    assert result.verdict == "retry"
    assert "repair shard" in result.findings[0]
    assert reviewer.file_calls == 0
    assert reviewer.global_calls == 0
    db.close()


async def test_hierarchical_review_fails_if_manifest_file_loses_all_shards(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, db, repo, job_id, integration, _ = _job_and_integration(git_repo, tmp_path)
    orchestrator = Orchestrator(config, db)
    orchestrator.reviewer = SemanticReviewerStub()  # type: ignore[assignment]

    def broken_builder(*_: Any, **__: Any) -> tuple[list[str], list[ReviewChunk]]:
        return ["lost.py"], []

    monkeypatch.setattr(
        "omarchy_yolo.orchestrator_integration.build_review_chunks", broken_builder
    )
    result = await orchestrator._hierarchical_final_review(
        job_id, repo, integration, [], "semantic", 0
    )
    assert result.verdict == "fail"
    assert "lost.py" in result.findings[0]
    db.close()


async def test_finalize_repairs_failed_gate_then_passes(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = EngineConfig(max_final_cycles=1, cleanup_worktrees=False)
    config, db, repo, job_id, integration, _ = _job_and_integration(
        git_repo, tmp_path, engine=engine
    )
    orchestrator = Orchestrator(config, db)
    monkeypatch.setattr(orchestrator.registry, "choose_role", lambda *args, **kwargs: "semantic")
    calls = 0
    repairs = 0

    async def gates(*_: Any, **__: Any) -> list[GateResult]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return [GateResult("false", 1, "", "broken", 0.01, False)]
        return [GateResult("true", 0, "", "", 0.01, False)]

    async def repair(*_: Any, **__: Any) -> bool:
        nonlocal repairs
        repairs += 1
        return True

    async def review(*_: Any, **__: Any) -> ReviewResult:
        return ReviewResult("pass", "release coherent after gate repair", ())

    monkeypatch.setattr(orchestrator, "_run_gates", gates)
    monkeypatch.setattr(orchestrator, "_repair_integration", repair)
    monkeypatch.setattr(orchestrator, "_hierarchical_final_review", review)
    summary = await orchestrator._finalize(job_id, repo, integration)
    assert summary == "release coherent after gate repair"
    assert calls == 2
    assert repairs == 1
    db.close()


async def test_finalize_repairs_audit_retry_then_passes(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = EngineConfig(max_final_cycles=1, cleanup_worktrees=False)
    config, db, repo, job_id, integration, _ = _job_and_integration(
        git_repo, tmp_path, engine=engine
    )
    orchestrator = Orchestrator(config, db)
    monkeypatch.setattr(orchestrator.registry, "choose_role", lambda *args, **kwargs: "semantic")
    review_calls = 0
    repair_issues: list[str] = []

    async def gates(*_: Any, **__: Any) -> list[GateResult]:
        return [GateResult("true", 0, "", "", 0.01, False)]

    async def review(*_: Any, **__: Any) -> ReviewResult:
        nonlocal review_calls
        review_calls += 1
        if review_calls == 1:
            return ReviewResult("retry", "caller contract mismatch", ("fix caller",))
        return ReviewResult("pass", "release coherent after audit repair", ())

    async def repair(*args: Any, **__: Any) -> bool:
        repair_issues.append(str(args[3]))
        return True

    monkeypatch.setattr(orchestrator, "_run_gates", gates)
    monkeypatch.setattr(orchestrator, "_hierarchical_final_review", review)
    monkeypatch.setattr(orchestrator, "_repair_integration", repair)
    summary = await orchestrator._finalize(job_id, repo, integration)
    assert summary == "release coherent after audit repair"
    assert review_calls == 2
    assert "fix caller" in repair_issues[0]
    db.close()


async def test_integrate_task_rolls_back_when_postmerge_gates_fail(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, db, repo, job_id, integration, base = _job_and_integration(git_repo, tmp_path)
    worker = tmp_path / "worker"
    worker_branch = "yolo/v12/T1"
    repo.ensure_worktree(worker, worker_branch, base)
    (worker / "feature.txt").write_text("feature\n")
    repo.commit_all(worker, "worker feature", GitConfig())
    task = db.add_tasks(job_id, [PlannedTask("T1", "Feature", "Add feature")])[0]
    db.update_task(
        task.id,
        branch=worker_branch,
        worktree=str(worker),
        base_commit=base,
    )
    db.update_task(task.id, state=TaskState.RUNNING)
    db.update_task(task.id, state=TaskState.REVIEWING)
    task = db.get_task(task.id)
    orchestrator = Orchestrator(config, db)

    async def bad_gates(*_: Any, **__: Any) -> list[GateResult]:
        return [GateResult("false", 1, "", "integration broken", 0.01, False)]

    async def no_repair(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(orchestrator, "_run_gates", bad_gates)
    monkeypatch.setattr(orchestrator, "_repair_integration", no_repair)
    integrated, feedback = await orchestrator._integrate_task(
        job_id, task, repo, integration
    )
    assert not integrated
    assert "Post-merge integration gates failed" in feedback
    assert repo.head(integration) == base
    assert not (integration / "feature.txt").exists()
    assert any(
        event["kind"] == "integration.rolled_back"
        for event in db.events(job_id, limit=100)
    )
    db.close()


async def test_finalize_refuses_unrepairable_final_gate(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = EngineConfig(max_final_cycles=1, cleanup_worktrees=False)
    config, db, repo, job_id, integration, _ = _job_and_integration(
        git_repo, tmp_path, engine=engine
    )
    orchestrator = Orchestrator(config, db)
    monkeypatch.setattr(orchestrator.registry, "choose_role", lambda *args, **kwargs: "semantic")

    async def bad_gates(*_: Any, **__: Any) -> list[GateResult]:
        return [GateResult("false", 1, "", "still broken", 0.01, False)]

    async def no_repair(*_: Any, **__: Any) -> bool:
        return False

    monkeypatch.setattr(orchestrator, "_run_gates", bad_gates)
    monkeypatch.setattr(orchestrator, "_repair_integration", no_repair)
    with pytest.raises(YoloError, match="no integration agent"):
        await orchestrator._finalize(job_id, repo, integration)
    db.close()
