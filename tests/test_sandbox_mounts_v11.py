from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.config import SandboxConfig
from omarchy_yolo.sandbox import Sandbox


def _mount_triplet(argv: list[str], path: Path) -> list[str]:
    value = str(path)
    indices = [index for index, token in enumerate(argv) if token == value]
    assert len(indices) >= 2
    first = indices[0]
    return argv[first - 1 : first + 2]


def test_bubblewrap_review_mounts_repository_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omarchy_yolo.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    argv = Sandbox(SandboxConfig(backend="bwrap")).wrap(
        ["reviewer"],
        tmp_path,
        execution_profile="review",
    )
    assert _mount_triplet(argv, tmp_path) == ["--ro-bind", str(tmp_path), str(tmp_path)]


def test_bubblewrap_worker_keeps_repository_writable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omarchy_yolo.sandbox.shutil.which", lambda _: "/usr/bin/bwrap")
    argv = Sandbox(SandboxConfig(backend="bwrap", read_only_home=True)).wrap(
        ["worker"],
        tmp_path,
        execution_profile="yolo-worktree",
    )
    assert _mount_triplet(argv, tmp_path) == ["--bind", str(tmp_path), str(tmp_path)]
