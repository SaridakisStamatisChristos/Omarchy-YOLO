from __future__ import annotations

from pathlib import Path

from omarchy_yolo.config import GitConfig
from omarchy_yolo.git import GitRepo


def test_worktree_commit_and_merge(git_repo: Path, tmp_path: Path) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    integration = tmp_path / "integration"
    worker = tmp_path / "worker"
    repo.ensure_existing_branch_worktree(integration, "yolo/test/integration", base)
    repo.ensure_worktree(worker, "yolo/test/T1", base)
    (worker / "feature.txt").write_text("works\n")
    repo.commit_all(worker, "feature", GitConfig())
    assert "feature.txt" in repo.changed_files(worker, base)
    repo.merge(integration, "yolo/test/T1")
    assert (integration / "feature.txt").read_text() == "works\n"
    assert repo.head(integration) != base


def test_orchestrator_commits_ignore_repository_git_hooks(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    hooks = git_repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text("#!/bin/sh\nexit 99\n")
    pre_commit.chmod(0o755)
    (git_repo / "safe.txt").write_text("safe\n")
    commit = repo.commit_all(git_repo, "internal commit", GitConfig())
    assert commit == repo.head(git_repo)
    assert repo.is_clean(git_repo)


def test_existing_branch_worktree_can_be_reattached_after_stale_registration(git_repo: Path, tmp_path: Path) -> None:
    import shutil
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    worker = tmp_path / "worker-recover"
    repo.ensure_worktree(worker, "yolo/recover/T1", base)
    (worker / "persisted.txt").write_text("kept\n")
    expected = repo.commit_all(worker, "persisted task progress", GitConfig())
    shutil.rmtree(worker)
    assert repo.branch_exists("yolo/recover/T1")
    repo.ensure_existing_branch_worktree(worker, "yolo/recover/T1", base)
    assert repo.head(worker) == expected
    assert (worker / "persisted.txt").read_text() == "kept\n"
