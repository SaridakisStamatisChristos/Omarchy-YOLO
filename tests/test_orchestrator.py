from __future__ import annotations

import json
from pathlib import Path

from omarchy_yolo.agents import AgentRegistry
from omarchy_yolo.config import Config, EngineConfig, GateConfig, GitConfig
from omarchy_yolo.db import Database
from omarchy_yolo.git import GitRepo
from omarchy_yolo.model import AgentResult, JobState
from omarchy_yolo.orchestrator import Orchestrator


class FakeAgent:
    name = "fake"
    def available(self) -> bool: return True
    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: int, log_path: Path, execution_profile: str, extra_env: dict[str, str] | None = None) -> AgentResult:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(prompt)
        if "planning controller" in prompt:
            stdout = json.dumps({"summary":"one task","tasks":[{"id":"T1","title":"Create result","description":"Create result.txt containing autonomous.","depends_on":[],"acceptance":["result.txt exists"],"preferred_agent":"fake","risk":"low"}]})
        elif "adversarial senior code reviewer" in prompt or "final release auditor" in prompt:
            stdout = json.dumps({"verdict":"pass","summary":"clean","findings":[]})
        elif "TASK T1" in prompt:
            (cwd / "result.txt").write_text("autonomous\n")
            stdout = "implemented"
        else: stdout = "ok"
        return AgentResult(0, stdout, "", 0.01, ("fake",))


async def test_orchestrator_end_to_end(git_repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state"
    config = Config(state_dir=state, config_path=tmp_path / "config.toml", engine=EngineConfig(max_parallel=2,max_attempts=2,max_final_cycles=1,planner_agent="fake",reviewer_agent="fake",integrator_agent="fake",worker_agents=("fake",),cleanup_worktrees=False), git=GitConfig(require_clean_repo=True), gates=GateConfig(commands=("git status --porcelain=v1 >/dev/null",), final_commands=("git status --porcelain=v1 >/dev/null",)))
    db = Database(config.db_path)
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    job = db.create_job(repo=str(repo.root), goal="Create result.txt", base_branch=branch, base_commit=base, integration_branch="yolo/test/integration", auto_apply=False)
    registry = AgentRegistry(config, overrides={"fake": FakeAgent()})
    await Orchestrator(config, db, registry=registry).run_job(job.id)
    finished = db.get_job(job.id)
    assert finished.state == JobState.COMPLETED
    assert repo.head() == base
    assert repo.branch_exists(finished.integration_branch)
    assert (Path(finished.integration_path) / "result.txt").read_text() == "autonomous\n"
    assert db.list_tasks(job.id)[0].state.value == "completed"
    db.close()


class FailOnceWorker(FakeAgent):
    def __init__(self) -> None: self.worker_calls = 0
    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: int, log_path: Path, execution_profile: str, extra_env: dict[str, str] | None = None) -> AgentResult:
        if "TASK T1" in prompt:
            self.worker_calls += 1
            if self.worker_calls == 1: return AgentResult(1, "", "synthetic failure", 0.01, ("fake",))
        return await super().run(prompt, cwd=cwd, timeout_seconds=timeout_seconds, log_path=log_path, execution_profile=execution_profile, extra_env=extra_env)


async def test_resume_gets_fresh_attempt_budget_without_erasing_attempt_numbers(git_repo: Path, tmp_path: Path) -> None:
    state = tmp_path / "state-resume"
    config = Config(state_dir=state, config_path=tmp_path / "config.toml", engine=EngineConfig(max_parallel=1,max_attempts=1,max_final_cycles=0,planner_agent="fake",reviewer_agent="fake",integrator_agent="fake",worker_agents=("fake",),cleanup_worktrees=False), git=GitConfig(require_clean_repo=True), gates=GateConfig(commands=("git status --porcelain=v1 >/dev/null",), final_commands=("git status --porcelain=v1 >/dev/null",)))
    db = Database(config.db_path)
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    job = db.create_job(repo=str(repo.root), goal="Create result.txt", base_branch=branch, base_commit=base, integration_branch="yolo/test-resume/integration", auto_apply=False)
    fake = FailOnceWorker(); registry = AgentRegistry(config, overrides={"fake": fake}); orchestrator = Orchestrator(config, db, registry=registry)
    try: await orchestrator.run_job(job.id)
    except Exception: pass
    assert db.get_job(job.id).state == JobState.FAILED
    task = db.list_tasks(job.id)[0]; assert task.attempts == 1
    db.retry_failed_tasks(job.id); db.clear_stop(job.id)
    await Orchestrator(config, db, registry=registry).run_job(job.id)
    task = db.list_tasks(job.id)[0]
    assert task.attempts == 2
    assert task.state.value == "completed"
    rows = db._execute("SELECT number FROM attempts WHERE task_id = ? ORDER BY number", (task.id,)).fetchall()
    assert [row["number"] for row in rows] == [1, 2]
    db.close()
