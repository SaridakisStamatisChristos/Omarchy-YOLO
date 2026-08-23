from __future__ import annotations

from pathlib import Path

import pytest

from omarchy_yolo.agents.base import CommandAgent
from omarchy_yolo.config import AgentConfig, Config
from omarchy_yolo.util import YoloError


def cfg(tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path, config_path=tmp_path / "config.toml")


def test_codex_danger_profile(tmp_path: Path) -> None:
    agent = CommandAgent(
        "codex",
        AgentConfig(command=("codex", "exec", "--full-auto", "--sandbox", "workspace-write")),
        cfg(tmp_path),
    )
    argv = agent.command_for_profile("danger-yolo")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "workspace-write" not in argv


def test_claude_review_profile_is_plan_mode(tmp_path: Path) -> None:
    agent = CommandAgent(
        "claude",
        AgentConfig(command=("claude", "-p", "--dangerously-skip-permissions", "--output-format", "json")),
        cfg(tmp_path),
    )
    argv = agent.command_for_profile("review")
    assert agent.supports_profile("review")
    assert "--dangerously-skip-permissions" not in argv
    assert argv[-2:] == ["--permission-mode", "plan"]


def test_opencode_review_profile_denies_mutation_and_external_access(tmp_path: Path) -> None:
    agent = CommandAgent(
        "opencode",
        AgentConfig(command=("opencode", "run", "--format", "json", "--auto")),
        cfg(tmp_path),
    )
    policy = agent.environment_for_profile("review")
    assert "OPENCODE_CONFIG_CONTENT" in policy
    import json

    payload = json.loads(policy["OPENCODE_CONFIG_CONTENT"])
    permission = payload["permission"]
    assert permission["edit"] == "deny"
    assert permission["bash"] == "deny"
    assert permission["external_directory"] == "deny"
    assert agent.command_for_profile("review")[-1] == "--auto"


def test_codex_review_overrides_equals_style_sandbox_and_full_auto(tmp_path: Path) -> None:
    agent = CommandAgent(
        "codex",
        AgentConfig(command=("codex", "exec", "--full-auto", "--sandbox=workspace-write")),
        cfg(tmp_path),
    )
    argv = agent.command_for_profile("review")
    assert "--full-auto" not in argv
    assert "--sandbox=workspace-write" not in argv
    assert argv[-2:] == ["--sandbox", "read-only"]


def test_claude_review_overrides_existing_bypass_permission_mode(tmp_path: Path) -> None:
    agent = CommandAgent(
        "claude",
        AgentConfig(command=("claude", "-p", "--permission-mode", "bypassPermissions")),
        cfg(tmp_path),
    )
    argv = agent.command_for_profile("review")
    assert "bypassPermissions" not in argv
    assert argv[-2:] == ["--permission-mode", "plan"]


def test_custom_agent_is_not_implicitly_trusted_for_review(tmp_path: Path) -> None:
    agent = CommandAgent(
        "custom",
        AgentConfig(command=("custom-agent", "--yolo")),
        cfg(tmp_path),
    )
    assert not agent.supports_profile("review")
    with pytest.raises(YoloError, match="no declared read-only review capability"):
        agent.command_for_profile("review")


def test_custom_agent_can_declare_separate_review_command(tmp_path: Path) -> None:
    agent = CommandAgent(
        "custom",
        AgentConfig(
            command=("custom-agent", "--write"),
            review_command=("custom-agent", "--read-only"),
        ),
        cfg(tmp_path),
    )
    assert agent.supports_profile("review")
    assert agent.command_for_profile("review") == ["custom-agent", "--read-only"]
