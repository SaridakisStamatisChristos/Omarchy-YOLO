from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.config import load_config
from omarchy_yolo.resources import ResourcePolicy
from omarchy_yolo.util import YoloError


def test_systemd_resource_policy_wraps_entire_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omarchy_yolo.resources.shutil.which",
        lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
    )
    policy = ResourcePolicy(
        backend="systemd",
        memory_high_mib=1536,
        memory_max_mib=2048,
        tasks_max=256,
        cpu_quota_percent=300,
        io_weight=100,
    )
    wrapped = policy.wrap(["bash", "-lc", "python -m pytest"])
    assert wrapped[:5] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
    ]
    assert "MemoryHigh=1536M" in wrapped
    assert "MemoryMax=2048M" in wrapped
    assert "TasksMax=256" in wrapped
    assert "CPUQuota=300%" in wrapped
    assert "IOWeight=100" in wrapped
    assert wrapped[-4:] == ["--", "bash", "-lc", "python -m pytest"]


def test_resource_policy_fails_closed_when_systemd_run_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omarchy_yolo.resources.shutil.which", lambda _: None)
    with pytest.raises(YoloError, match="requires systemd-run"):
        ResourcePolicy(backend="systemd", memory_max_mib=1024).wrap(["true"])


def test_resource_config_round_trips_from_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[resources]
backend = "systemd"
memory_high_mib = 1536
memory_max_mib = 2048
tasks_max = 256
cpu_quota_percent = 300
io_weight = 100
""",
        encoding="utf-8",
    )
    cfg = load_config(config_path)
    assert cfg.resources.backend == "systemd"
    assert cfg.resources.memory_high_mib == 1536
    assert cfg.resources.memory_max_mib == 2048
    assert cfg.resources.tasks_max == 256
    assert cfg.resources.cpu_quota_percent == 300
    assert cfg.resources.io_weight == 100


def test_resource_config_rejects_invalid_memory_order(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
[resources]
backend = "systemd"
memory_high_mib = 4096
memory_max_mib = 2048
""",
        encoding="utf-8",
    )
    with pytest.raises(YoloError, match="cannot exceed"):
        load_config(config_path)
