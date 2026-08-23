from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path

import pytest

from omarchy_yolo.config import GitConfig
from omarchy_yolo.gates import GateRunner, detect_gate_commands
from omarchy_yolo.git import GitError, GitRepo
from omarchy_yolo.process import ProcessRunner


def test_git_preflight_and_fast_forward_refusal_modes(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    assert branch == "main"
    assert repo.can_fast_forward_source(branch, base) == (True, "ok")

    (git_repo / "dirty.txt").write_text("dirty\n")
    with pytest.raises(GitError, match="uncommitted changes"):
        repo.preflight(require_clean=True)
    ok, reason = repo.can_fast_forward_source(branch, base)
    assert not ok and "dirty" in reason
    (git_repo / "dirty.txt").unlink()

    repo.run("checkout", "-b", "other")
    ok, reason = repo.can_fast_forward_source(branch, base)
    assert not ok and "not main" in reason
    repo.run("checkout", "main")

    (git_repo / "moved.txt").write_text("moved\n")
    repo.commit_all(git_repo, "move source", GitConfig())
    ok, reason = repo.can_fast_forward_source(branch, base)
    assert not ok and "moved" in reason

    repo.run("checkout", "--detach")
    ok, reason = repo.can_fast_forward_source("HEAD", repo.head())
    assert not ok and "detached" in reason


def test_git_rejects_invalid_branch_symlink_and_foreign_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    repo = GitRepo.discover(git_repo)
    with pytest.raises(GitError, match="invalid Git branch"):
        repo.branch_exists("bad..branch")

    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "worker-link"
    symlink.symlink_to(target, target_is_directory=True)
    with pytest.raises(GitError, match="symlink"):
        repo.ensure_worktree(symlink, "yolo/symlink/T1", repo.head())

    foreign = tmp_path / "foreign"
    foreign.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(foreign)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(foreign), "config", "user.name", "Foreign"], check=True)
    subprocess.run(["git", "-C", str(foreign), "config", "user.email", "foreign@example.invalid"], check=True)
    (foreign / "x").write_text("x")
    subprocess.run(["git", "-C", str(foreign), "add", "x"], check=True)
    subprocess.run(["git", "-C", str(foreign), "commit", "-m", "initial"], check=True, capture_output=True)
    with pytest.raises(GitError, match="different repository"):
        repo.assert_worktree(foreign)


def test_git_reset_diff_truncation_missing_cleanup_and_finish_dirty(
    git_repo: Path, tmp_path: Path
) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    worker = tmp_path / "worker"
    repo.ensure_worktree(worker, "yolo/git-extra/T1", base)

    (worker / "huge.txt").write_text("x" * 120_000)
    repo.commit_all(worker, "large", GitConfig())
    diff = repo.diff(worker, base, max_bytes=2_000)
    assert "...[diff truncated]..." in diff

    (worker / "untracked.txt").write_text("remove me")
    repo.reset_hard(worker, repo.head(worker))
    assert not (worker / "untracked.txt").exists()

    (worker / "dirty-after.txt").write_text("finish me")
    repo.finish_merge(worker, GitConfig())
    assert repo.is_clean(worker)
    assert (worker / "dirty-after.txt").exists()

    repo.remove_worktree(worker)
    repo.remove_worktree(worker)  # missing-path prune path is idempotent
    repo.delete_branch("yolo/git-extra/T1")
    assert not repo.branch_exists("yolo/git-extra/T1")
    repo.abort_merge(git_repo)  # no merge is a no-op


def test_git_fast_forward_source_success(git_repo: Path, tmp_path: Path) -> None:
    repo = GitRepo.discover(git_repo)
    branch, base = repo.preflight(require_clean=True)
    integration = tmp_path / "integration"
    repo.ensure_existing_branch_worktree(integration, "yolo/ff/integration", base)
    (integration / "released.txt").write_text("release\n")
    expected = repo.commit_all(integration, "release", GitConfig())
    repo.fast_forward_source("yolo/ff/integration", branch, base)
    assert repo.head() == expected
    assert (git_repo / "released.txt").read_text() == "release\n"


def test_detect_gate_matrix_and_malformed_package(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"scripts":{"test":"node test.js"}}')
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n")
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\nversion='0.1.0'\n")
    (tmp_path / "go.mod").write_text("module example.invalid/x\n")
    (tmp_path / "Makefile").write_text("test:\n\t@true\n")
    build = tmp_path / "build"
    build.mkdir()
    (build / "CTestTestfile.cmake").write_text("# smoke\n")
    commands = detect_gate_commands(tmp_path)
    assert "pnpm test" in commands
    assert "cargo test --all-targets --all-features" in commands
    assert "go test ./..." in commands
    assert "make test" in commands
    assert "ctest --test-dir build --output-on-failure" in commands

    (tmp_path / "package.json").write_text("{broken")
    (tmp_path / "Cargo.toml").unlink()
    (tmp_path / "go.mod").unlink()
    (tmp_path / "Makefile").unlink()
    (build / "CTestTestfile.cmake").unlink()
    commands = detect_gate_commands(tmp_path)
    assert commands == ("git diff --check",)


async def test_gate_timeout_kills_process_group(tmp_path: Path) -> None:
    pidfile = tmp_path / "timeout.pid"
    result = await GateRunner().run(
        (f"echo $$ > {pidfile}; sleep 30",),
        cwd=tmp_path,
        timeout_seconds=1,
        log_path=tmp_path / "timeout.log",
    )
    assert len(result) == 1
    assert result[0].timed_out
    assert not result[0].ok
    pid = int(pidfile.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_gate_log_truncation_marker(tmp_path: Path) -> None:
    log = tmp_path / "truncated.log"
    runner = GateRunner(capture_limit_bytes=1024, log_limit_bytes=512)
    results = await runner.run(
        ("python -c 'print(\"x\" * 20000)'", "printf done"),
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=log,
    )
    assert results[0].ok
    assert len(results[0].stdout.encode()) <= 1024
    # The log stays bounded even when subsequent commands attempt additional writes.
    assert log.stat().st_size < 1024


async def test_process_timeout_and_cancellation(tmp_path: Path) -> None:
    timeout_result = await ProcessRunner().run(
        ["python", "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        timeout_seconds=1,
        log_path=tmp_path / "process-timeout.log",
    )
    assert timeout_result.returncode != 0

    pidfile = tmp_path / "process-cancel.pid"
    code = (
        "import os,time,pathlib; "
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    handle = asyncio.create_task(
        ProcessRunner().run(
            ["python", "-c", code],
            cwd=tmp_path,
            timeout_seconds=60,
            log_path=tmp_path / "process-cancel.log",
        )
    )
    for _ in range(100):
        if pidfile.exists():
            break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    pid = int(pidfile.read_text())
    handle.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handle
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
