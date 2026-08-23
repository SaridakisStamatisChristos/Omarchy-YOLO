from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.config import load_config
from omarchy_yolo.util import YoloError


def test_config_rejects_unsafe_agent_name(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[agents."../../escape"]\nenabled = true\ncommand = ["echo"]\n')
    with pytest.raises(YoloError, match="safe agent identifier"):
        load_config(config)


def test_config_rejects_unbounded_parallelism(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[engine]\nmax_parallel = 1000000\n")
    with pytest.raises(YoloError, match="between 1 and 64"):
        load_config(config)


def test_config_rejects_unbounded_global_workers(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("[engine]\nmax_global_workers = 1000000\n")
    with pytest.raises(YoloError, match="between 1 and 128"):
        load_config(config)


def test_config_rejects_unsafe_branch_prefix(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[git]\nbranch_prefix = "../oops"\n')
    with pytest.raises(YoloError, match="safe Git ref prefix"):
        load_config(config)


def test_config_rejects_unknown_execution_profile(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[engine]\nexecution_profile = "mystery"\n')
    with pytest.raises(YoloError, match="execution_profile"):
        load_config(config)


def test_config_rejects_writable_home_escape(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[sandbox]\nwritable_home_paths = ["../escape"]\n')
    with pytest.raises(YoloError, match="relative paths inside HOME"):
        load_config(config)


def test_custom_review_command_is_loaded(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        '[agents.custom]\ncommand = ["custom", "--write"]\n'
        'review_command = ["custom", "--read-only"]\n'
    )
    loaded = load_config(config)
    assert loaded.agents["custom"].review_command == ("custom", "--read-only")
