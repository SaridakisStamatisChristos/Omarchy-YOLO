from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.config import GitConfig
from omarchy_yolo.git import GitRepo
from omarchy_yolo.review_source import build_review_chunks, changed_files
from omarchy_yolo.util import YoloError


def test_hierarchical_review_represents_every_changed_file(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    (git_repo / "alpha.txt").write_text("A" * 45_000 + "\nALPHA_TAIL\n")
    (git_repo / "beta.txt").write_text("B" * 45_000 + "\nBETA_TAIL\n")
    repo.commit_all(git_repo, "large candidate", GitConfig())

    manifest, chunks = build_review_chunks(
        repo,
        git_repo,
        base,
        max_files=10,
        chunk_bytes=30_000,
        chunk_files=1,
    )
    combined = "\n".join(chunk.text for chunk in chunks)
    assert manifest == ["alpha.txt", "beta.txt"]
    assert len(chunks) > 2
    assert "ALPHA_TAIL" in combined
    assert "BETA_TAIL" in combined
    assert {path for chunk in chunks for path in chunk.files} == set(manifest)


def test_review_file_limit_fails_closed(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    for index in range(4):
        (git_repo / f"file-{index}.txt").write_text(str(index))
    repo.commit_all(git_repo, "many files", GitConfig())

    with pytest.raises(YoloError, match="configured final-review maximum"):
        changed_files(repo, git_repo, base, max_files=3)


def test_single_long_line_is_split_without_losing_tail(git_repo: Path) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    (git_repo / "long.txt").write_text("x" * 120_000 + "TAIL_SENTINEL")
    repo.commit_all(git_repo, "long line", GitConfig())

    manifest, chunks = build_review_chunks(
        repo,
        git_repo,
        base,
        max_files=10,
        chunk_bytes=20_000,
        chunk_files=4,
    )
    assert manifest == ["long.txt"]
    assert len(chunks) >= 6
    assert "TAIL_SENTINEL" in "".join(chunk.text for chunk in chunks)
