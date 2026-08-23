from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.config import GitConfig
from omarchy_yolo.db import Database
from omarchy_yolo.git import GitRepo, MergeConflict
from omarchy_yolo.util import YoloError


def test_corrupt_sqlite_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    path.write_bytes(b"not-a-sqlite-database\x00\xff" * 64)
    with pytest.raises(YoloError, match="database initialization/integrity check failed"):
        Database(path)


def test_interrupted_conflicting_merge_can_be_aborted_cleanly(
    git_repo: Path, tmp_path: Path
) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    integration = tmp_path / "integration"
    worker = tmp_path / "worker"
    repo.ensure_existing_branch_worktree(integration, "yolo/fault/integration", base)
    repo.ensure_worktree(worker, "yolo/fault/T1", base)

    (integration / "README.md").write_text("integration side\n")
    integration_head = repo.commit_all(integration, "integration change", GitConfig())
    (worker / "README.md").write_text("worker side\n")
    repo.commit_all(worker, "worker change", GitConfig())

    with pytest.raises(MergeConflict):
        repo.merge(integration, "yolo/fault/T1")
    assert repo.merge_in_progress(integration)
    assert repo.unresolved_files(integration) == ["README.md"]

    repo.abort_merge(integration)
    assert not repo.merge_in_progress(integration)
    assert repo.head(integration) == integration_head
    assert repo.is_clean(integration)
    assert (integration / "README.md").read_text() == "integration side\n"
