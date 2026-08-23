from __future__ import annotations

from pathlib import Path

from omarchy_yolo.agents.base import CommandAgent
from omarchy_yolo.config import AgentConfig, Config


def cfg(tmp_path: Path) -> Config:
    return Config(state_dir=tmp_path, config_path=tmp_path / "config.toml")


def test_codex_danger_profile(tmp_path: Path) -> None:
    agent = CommandAgent("codex", AgentConfig(command=("codex", "exec", "--full-auto", "--sandbox", "workspace-write")), cfg(tmp_path))
    argv = agent.command_for_profile("danger-yolo")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "workspace-write" not in argv


def test_claude_review_profile_is_plan_mode(tmp_path: Path) -> None:
    agent = CommandAgent("claude", AgentConfig(command=("claude", "-p", "--dangerously-skip-permissions", "--output-format", "json")), cfg(tmp_path))
    argv = agent.command_for_profile("review")
    assert "--dangerously-skip-permissions" not in argv
    assert argv[-2:] == ["--permission-mode", "plan"]


def test_opencode_review_profile_denies_mutation_and_external_access(tmp_path: Path) -> None:
    agent = CommandAgent("opencode", AgentConfig(command=("opencode", "run", "--format", "json", "--auto")), cfg(tmp_path))
    policy = agent.environment_for_profile("review")
    assert "OPENCODE_CONFIG_CONTENT" in policy
    import json
    payload = json.loads(policy["OPENCODE_CONFIG_CONTENT"])
    permission = payload["permission"]
    assert permission["edit"] == "deny"
    assert permission["bash"] == "deny"
    assert permission["external_directory"] == "deny"
    assert agent.command_for_profile("review")[-1] == "--auto"
