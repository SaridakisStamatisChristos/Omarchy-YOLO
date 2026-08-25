from __future__ import annotations

import os
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
    )
    combined = "\n".join(chunk.text for chunk in chunks)
    assert manifest == ["alpha.txt", "beta.txt"]
    assert len(chunks) > 2
    assert "ALPHA_TAIL" in combined
    assert "BETA_TAIL" in combined
    assert {path for chunk in chunks for path in chunk.files} == set(manifest)
    assert all(len(chunk.files) <= 1 for chunk in chunks)


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
    )
    assert manifest == ["long.txt"]
    assert len(chunks) >= 6
    assert "TAIL_SENTINEL" in "".join(chunk.text for chunk in chunks)


def test_non_utf8_filename_round_trips_to_git_and_is_safely_rendered(
    git_repo: Path,
) -> None:
    repo = GitRepo.discover(git_repo)
    base = repo.head()
    raw_name = b"hostile-\xff.txt"
    actual_name = os.fsdecode(raw_name)
    (git_repo / actual_name).write_text("NON_UTF8_SENTINEL\n")
    repo.commit_all(git_repo, "non utf8 filename", GitConfig())

    actual_files = changed_files(repo, git_repo, base, max_files=10)
    assert len(actual_files) == 1
    assert os.fsencode(actual_files[0]) == raw_name

    manifest, chunks = build_review_chunks(
        repo,
        git_repo,
        base,
        max_files=10,
        chunk_bytes=20_000,
    )
    assert len(manifest) == 1
    assert "\\udcff" in manifest[0]
    combined = "\n".join(chunk.text for chunk in chunks)
    assert "NON_UTF8_SENTINEL" in combined
    assert manifest[0] in combined
