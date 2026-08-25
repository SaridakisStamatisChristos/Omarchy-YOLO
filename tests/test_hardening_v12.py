from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

import omarchy_yolo.gates as gates_module
from omarchy_yolo.agents import AgentRegistry
from omarchy_yolo.config import Config, EngineConfig, GateConfig, GitConfig, SandboxConfig
from omarchy_yolo.db import Database
from omarchy_yolo.gates import GateRunner
from omarchy_yolo.git import GitRepo
from omarchy_yolo.model import AgentResult, JobState, ReviewResult, TaskState
from omarchy_yolo.orchestrator import Orchestrator
from omarchy_yolo.review_source import build_review_chunks
from omarchy_yolo.reviewer import Reviewer
from omarchy_yolo.sandbox import Sandbox
from omarchy_yolo.util import YoloError


async def test_gate_midstream_log_failure_terminates_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_open = gates_module.open_private_binary
    append_calls = 0

    def flaky_open(path: Path, *, append: bool = False):
        nonlocal append_calls
        if append:
            append_calls += 1
            if append_calls >= 2:
                raise YoloError("synthetic ENOSPC")
        return real_open(path, append=append)

    monkeypatch.setattr(gates_module, "open_private_binary", flaky_open)
    pidfile = tmp_path / "gate.pid"
    command = f"echo $$ > {pidfile}; printf gate-output; sleep 30"
    with pytest.raises(YoloError, match="synthetic ENOSPC"):
        await GateRunner().run(
            (command,),
            cwd=tmp_path,
            timeout_seconds=60,
            log_path=tmp_path / "gates.log",
        )
    assert pidfile.exists()
    pid = int(pidfile.read_text().strip())
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"gate child {pid} survived logging failure")


def test_bwrap_gate_profile_is_repo_writable_but_home_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omarchy_yolo.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    sandbox = Sandbox(SandboxConfig(backend="bwrap", network=False, read_only_home=False))
    argv = sandbox.wrap(["bash", "-lc", "true"], tmp_path, execution_profile="gate")
    cwd_index = argv.index(str(tmp_path))
    assert argv[cwd_index - 1] == "--bind"
    assert str(Path.home().resolve()) not in argv
    assert "--unshare-net" in argv


def test_review_shards_never_mix_files(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    (git_repo / "a.py").write_text("A = 1\n")
    (git_repo / "b.py").write_text("B = 2\n")
    repo.commit_all(git_repo, "two files", GitConfig())

    manifest, chunks = build_review_chunks(
        repo,
        git_repo,
        base,
        max_files=10,
        chunk_bytes=60_000,
    )
    assert manifest == ["a.py", "b.py"]
    assert len(chunks) == 2
    assert [chunk.files for chunk in chunks] == [("a.py",), ("b.py",)]


def test_binary_review_fails_closed_unless_explicitly_allowed(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    (git_repo / "artifact.bin").write_bytes(b"\x00\xff\x00binary" * 200)
    repo.commit_all(git_repo, "binary", GitConfig())

    with pytest.raises(YoloError, match="binary change cannot be semantically reviewed"):
        build_review_chunks(
            repo,
            git_repo,
            base,
            max_files=10,
            chunk_bytes=60_000,
        )

    manifest, chunks = build_review_chunks(
        repo,
        git_repo,
        base,
        max_files=10,
        chunk_bytes=60_000,
        allow_binary=True,
    )
    assert manifest == ["artifact.bin"]
    assert "Binary content review explicitly allowed" in chunks[0].text
    assert "RAW:" in chunks[0].text


class SemanticReviewAgent:
    name = "semantic"

    def __init__(self) -> None:
        self.prompts: list[str] = []

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
        self.prompts.append(prompt)
        return AgentResult(
            0,
            json.dumps(
                {
                    "verdict": "pass",
                    "summary": "API contract remains coherent across reviewed components",
                    "findings": [],
                }
            ),
            "",
            0.01,
            ("semantic",),
        )


async def test_file_then_global_semantic_synthesis(tmp_path: Path) -> None:
    agent = SemanticReviewAgent()
    config = Config(state_dir=tmp_path / "state", config_path=tmp_path / "config.toml")
    reviewer = Reviewer(AgentRegistry(config, overrides={"semantic": agent}))
    shard_reviews = [
        ReviewResult("pass", "public function now returns Optional[T]", ()),
        ReviewResult("pass", "implementation handles missing value internally", ()),
    ]
    file_review = await reviewer.review_file_synthesis(
        goal="change API safely",
        gates=[],
        file_path="api.py",
        chunk_reviews=shard_reviews,
        cwd=tmp_path,
        agent_name="semantic",
        timeout_seconds=5,
        log_path=tmp_path / "file.log",
    )
    assert file_review.passed
    assert "public function now returns Optional[T]" in agent.prompts[-1]

    global_review = await reviewer.review_final_synthesis(
        goal="change API safely",
        gates=[],
        manifest=["api.py", "caller.py"],
        file_reviews=[
            ("api.py", file_review),
            ("caller.py", ReviewResult("pass", "caller handles Optional[T]", ())),
        ],
        cwd=tmp_path,
        agent_name="semantic",
        timeout_seconds=5,
        log_path=tmp_path / "global.log",
    )
    assert global_review.passed
    assert "caller.py: caller handles Optional[T]" in agent.prompts[-1]


class EndToEndAgent:
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
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(prompt)
        if "planning controller" in prompt:
            stdout = json.dumps(
                {
                    "summary": "one task",
                    "tasks": [
                        {
                            "id": "T1",
                            "title": "Create result",
                            "description": "Create result.txt.",
                            "depends_on": [],
                            "acceptance": ["result exists"],
                            "preferred_agent": "fake",
                            "risk": "low",
                        }
                    ],
                }
            )
        elif "TASK T1" in prompt:
            (cwd / "result.txt").write_text("done\n")
            stdout = "implemented"
        elif "adversarial senior code reviewer" in prompt or "final release audit" in prompt:
            stdout = json.dumps(
                {
                    "verdict": "pass",
                    "summary": "result contract is coherent and isolated",
                    "findings": [],
                }
            )
        else:
            stdout = json.dumps(
                {"verdict": "pass", "summary": "coherent release", "findings": []}
            )
        return AgentResult(0, stdout, "", 0.01, ("fake",))


async def test_task_cleanup_failure_is_housekeeping_only(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = Config(
        state_dir=tmp_path / "state",
        config_path=tmp_path / "config.toml",
        engine=EngineConfig(
            max_parallel=1,
            max_global_workers=1,
            max_attempts=1,
            max_final_cycles=0,
            planner_agent="fake",
            reviewer_agent="fake",
            integrator_agent="fake",
            worker_agents=("fake",),
            cleanup_worktrees=True,
        ),
        gates=GateConfig(
            commands=("git status --porcelain=v1 >/dev/null",),
            final_commands=("git status --porcelain=v1 >/dev/null",),
        ),
    )
    db = Database(config.db_path)
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    job = db.create_job(
        repo=str(repo.root),
        goal="Create result.txt",
        base_branch=branch,
        base_commit=base,
        integration_branch="yolo/v12-cleanup/integration",
        auto_apply=False,
    )
    registry = AgentRegistry(config, overrides={"fake": EndToEndAgent()})
    original_remove = GitRepo.remove_worktree

    def flaky_remove(self: GitRepo, path: Path, *, force: bool = True) -> None:
        if path.name.startswith("task-"):
            raise OSError("synthetic cleanup failure")
        original_remove(self, path, force=force)

    monkeypatch.setattr(GitRepo, "remove_worktree", flaky_remove)
    await Orchestrator(config, db, registry=registry).run_job(job.id)

    assert db.get_job(job.id).state == JobState.COMPLETED
    assert db.list_tasks(job.id)[0].state == TaskState.COMPLETED
    events = db.events(job.id, limit=100)
    assert any(event["kind"] == "task.cleanup_failed" for event in events)
    assert not any(event["kind"] == "job.failed" for event in events)
    db.close()
