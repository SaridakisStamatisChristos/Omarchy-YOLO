from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omarchy_yolo.agents import AgentRegistry
from omarchy_yolo.config import Config, EngineConfig, GateConfig
from omarchy_yolo.db import Database
from omarchy_yolo.model import AgentResult, GateResult, JobState, PlannedTask, ReviewResult, TaskState
from omarchy_yolo.orchestrator import Orchestrator, TaskExecutionError
from omarchy_yolo.util import YoloError


class RoleAgent:
    name = "fake"

    def available(self) -> bool:
        return True

    def supports_profile(self, execution_profile: str) -> bool:
        return True

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
        return AgentResult(0, "ok", "", 0.01, ("fake",))


def make_orchestrator(tmp_path: Path, *, max_final_cycles: int = 1) -> tuple[Orchestrator, Database, str]:
    config = Config(
        state_dir=tmp_path / "state",
        config_path=tmp_path / "config.toml",
        engine=EngineConfig(
            max_parallel=2,
            max_global_workers=2,
            max_attempts=2,
            max_final_cycles=max_final_cycles,
            planner_agent="fake",
            reviewer_agent="fake",
            integrator_agent="fake",
            worker_agents=("fake",),
            cleanup_worktrees=False,
        ),
        gates=GateConfig(commands=("true",), final_commands=("true",)),
    )
    db = Database(config.db_path)
    job = db.create_job(
        repo=str(tmp_path / "repo"),
        goal="exercise orchestration",
        base_branch="main",
        base_commit="a" * 40,
        integration_branch="yolo/v14/integration",
        auto_apply=False,
    )
    registry = AgentRegistry(config, overrides={"fake": RoleAgent()})
    return Orchestrator(config, db, registry=registry), db, job.id


def advance_task(db: Database, task_id: str, target: TaskState) -> None:
    path = [
        TaskState.RUNNING,
        TaskState.REVIEWING,
        TaskState.INTEGRATING,
        TaskState.COMPLETED,
    ]
    current = db.get_task(task_id).state
    if current == target:
        return
    for state in path:
        if db.get_task(task_id).state == state:
            continue
        db.update_task(task_id, state=state)
        if state == target:
            return


async def test_execute_dag_returns_when_every_task_completed(tmp_path: Path) -> None:
    orch, db, job_id = make_orchestrator(tmp_path)
    task = db.add_tasks(job_id, [PlannedTask("T1", "Done", "Already done")])[0]
    advance_task(db, task.id, TaskState.COMPLETED)
    db.update_job(job_id, state=JobState.RUNNING)
    await orch._execute_dag(job_id, object(), tmp_path)  # type: ignore[arg-type]
    db.close()


async def test_execute_dag_marks_unschedulable_tasks_blocked(tmp_path: Path) -> None:
    orch, db, job_id = make_orchestrator(tmp_path)
    task = db.add_tasks(
        job_id,
        [PlannedTask("T1", "Blocked", "Needs missing dependency", depends_on=("T0",))],
    )[0]
    db.update_job(job_id, state=JobState.RUNNING)
    with pytest.raises(TaskExecutionError, match="stalled"):
        await orch._execute_dag(job_id, object(), tmp_path)  # type: ignore[arg-type]
    assert db.get_task(task.id).state == TaskState.BLOCKED
    db.close()


async def test_plan_job_success_and_fallback_are_both_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db, job_id = make_orchestrator(tmp_path)

    async def successful_plan(*_: Any, **__: Any) -> tuple[str, list[PlannedTask]]:
        return "planned", [PlannedTask("T1", "One", "Do one thing")]

    monkeypatch.setattr(orch.planner, "plan", successful_plan)
    await orch._plan_job(job_id, tmp_path)
    assert [task.logical_id for task in db.list_tasks(job_id)] == ["T1"]
    assert any(event["kind"] == "planner.completed" for event in db.events(job_id, limit=100))
    db.close()

    orch2, db2, job_id2 = make_orchestrator(tmp_path / "fallback")

    async def broken_plan(*_: Any, **__: Any) -> tuple[str, list[PlannedTask]]:
        raise RuntimeError("synthetic planner outage")

    monkeypatch.setattr(orch2.planner, "plan", broken_plan)
    await orch2._plan_job(job_id2, tmp_path)
    tasks = db2.list_tasks(job_id2)
    assert len(tasks) == 1 and tasks[0].logical_id == "T1"
    assert any(event["kind"] == "planner.fallback" for event in db2.events(job_id2, limit=100))
    db2.close()


def test_review_failure_combines_retry_and_fail_semantics() -> None:
    assert Orchestrator._review_failure(summary="x", entries=[]) is None
    retry = Orchestrator._review_failure(
        summary="retry",
        entries=[("one", ReviewResult("retry", "repair this", ()))],
    )
    assert retry is not None and retry.verdict == "retry"
    failed = Orchestrator._review_failure(
        summary="fail",
        entries=[
            ("clean", ReviewResult("pass", "clean", ())),
            ("bad", ReviewResult("fail", "bad", ("material defect",))),
        ],
    )
    assert failed is not None and failed.verdict == "fail"
    assert "bad: material defect" in failed.findings


async def test_repair_integration_commits_success_and_records_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db, job_id = make_orchestrator(tmp_path)
    commits: list[str] = []

    class Repo:
        def commit_all(self, cwd: Path, message: str, config: object) -> str:
            commits.append(message)
            return "b" * 40

    repaired = await orch._repair_integration(
        job_id,
        Repo(),  # type: ignore[arg-type]
        tmp_path,
        "synthetic integration problem",
    )
    assert repaired
    assert commits == ["yolo: integration repair"]
    assert any(event["kind"] == "integration.repaired" for event in db.events(job_id, limit=100))
    db.close()


async def test_resolve_merge_conflict_finishes_when_index_is_already_clean(tmp_path: Path) -> None:
    orch, db, job_id = make_orchestrator(tmp_path)
    task = db.add_tasks(job_id, [PlannedTask("T1", "Merge", "Merge it")])[0]
    finished: list[bool] = []

    class Repo:
        def unresolved_files(self, cwd: Path) -> list[str]:
            return []

        def finish_merge(self, cwd: Path, config: object) -> None:
            finished.append(True)

    assert await orch._resolve_merge_conflict(
        job_id,
        task,
        Repo(),  # type: ignore[arg-type]
        tmp_path,
        "synthetic conflict",
    )
    assert finished == [True]
    db.close()


async def test_finalize_success_and_terminal_gate_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db, job_id = make_orchestrator(tmp_path, max_final_cycles=0)

    async def good_gates(*_: Any, **__: Any) -> list[GateResult]:
        return [GateResult("true", 0, "", "", 0.01, False)]

    async def clean_review(*_: Any, **__: Any) -> ReviewResult:
        return ReviewResult("pass", "semantic release clean", ())

    monkeypatch.setattr(orch, "_run_gates", good_gates)
    monkeypatch.setattr(orch, "_hierarchical_final_review", clean_review)
    assert (
        await orch._finalize(job_id, object(), tmp_path)  # type: ignore[arg-type]
        == "semantic release clean"
    )

    async def bad_gates(*_: Any, **__: Any) -> list[GateResult]:
        return [GateResult("false", 1, "", "broken", 0.01, False)]

    monkeypatch.setattr(orch, "_run_gates", bad_gates)
    with pytest.raises(YoloError, match="Final repository gates failed"):
        await orch._finalize(job_id, object(), tmp_path)  # type: ignore[arg-type]
    db.close()


async def test_review_task_resilient_returns_retry_when_every_reviewer_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch, db, job_id = make_orchestrator(tmp_path)
    task = db.add_tasks(job_id, [PlannedTask("T1", "Review", "Review me")])[0]
    db.update_task(task.id, base_commit="a" * 40)

    class Repo:
        def diff(self, cwd: Path, base: str) -> str:
            return "diff --git a/a b/a\n"

    async def broken_review(*_: Any, **__: Any) -> ReviewResult:
        raise RuntimeError("review endpoint down")

    monkeypatch.setattr(orch.reviewer, "review_task", broken_review)
    review = await orch._review_task_resilient(
        job_id,
        db.get_task(task.id),
        "goal",
        Repo(),  # type: ignore[arg-type]
        tmp_path,
        [],
        1,
    )
    assert review.verdict == "retry"
    assert review.summary == "review infrastructure failed"
    assert review.findings
    db.close()
