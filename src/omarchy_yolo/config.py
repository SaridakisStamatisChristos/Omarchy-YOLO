from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import xdg_config_home, xdg_state_home


@dataclass(slots=True)
class EngineConfig:
    max_parallel: int = 4
    max_attempts: int = 3
    max_final_cycles: int = 2
    max_tasks: int = 12
    agent_timeout_seconds: int = 3600
    gate_timeout_seconds: int = 1800
    planner_agent: str = "codex"
    reviewer_agent: str = "claude"
    integrator_agent: str = "codex"
    worker_agents: tuple[str, ...] = ("codex", "claude", "opencode")
    auto_apply: bool = False
    cleanup_worktrees: bool = True
    execution_profile: str = "yolo-worktree"


@dataclass(slots=True)
class GitConfig:
    require_clean_repo: bool = True
    branch_prefix: str = "yolo"
    commit_name: str = "Omarchy YOLO"
    commit_email: str = "omarchy-yolo@localhost"


@dataclass(slots=True)
class GateConfig:
    commands: tuple[str, ...] = ()
    final_commands: tuple[str, ...] = ()


@dataclass(slots=True)
class SandboxConfig:
    backend: str = "native"
    network: bool = True
    read_only_home: bool = False


@dataclass(slots=True)
class AgentConfig:
    enabled: bool = True
    command: tuple[str, ...] = ()


DEFAULT_AGENT_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "exec", "--full-auto", "--sandbox", "workspace-write"),
    "claude": ("claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"),
    "opencode": (
        "opencode",
        "run",
        "--format",
        "json",
        "--auto",
    ),
    "gemini": ("gemini", "-p"),
}


@dataclass(slots=True)
class Config:
    state_dir: Path
    config_path: Path
    engine: EngineConfig = field(default_factory=EngineConfig)
    git: GitConfig = field(default_factory=GitConfig)
    gates: GateConfig = field(default_factory=GateConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def worktrees_dir(self) -> Path:
        return self.state_dir / "worktrees"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"


def _section(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key, {})
    return value if isinstance(value, dict) else {}


def _tuple_str(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not isinstance(value, list):
        return default
    return tuple(str(x) for x in value)


def load_config(path: Path | None = None) -> Config:
    config_path = path or Path(
        os.environ.get("OMARCHY_YOLO_CONFIG", xdg_config_home() / "omarchy-yolo/config.toml")
    )
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)

    state_dir = Path(
        os.environ.get("OMARCHY_YOLO_STATE_DIR", xdg_state_home() / "omarchy-yolo")
    ).expanduser()

    e = _section(data, "engine")
    engine = EngineConfig(
        max_parallel=max(1, int(e.get("max_parallel", 4))),
        max_attempts=max(1, int(e.get("max_attempts", 3))),
        max_final_cycles=max(0, int(e.get("max_final_cycles", 2))),
        max_tasks=max(1, int(e.get("max_tasks", 12))),
        agent_timeout_seconds=max(1, int(e.get("agent_timeout_seconds", 3600))),
        gate_timeout_seconds=max(1, int(e.get("gate_timeout_seconds", 1800))),
        planner_agent=str(e.get("planner_agent", "codex")),
        reviewer_agent=str(e.get("reviewer_agent", "claude")),
        integrator_agent=str(e.get("integrator_agent", "codex")),
        worker_agents=_tuple_str(e.get("worker_agents"), ("codex", "claude", "opencode")),
        auto_apply=bool(e.get("auto_apply", False)),
        cleanup_worktrees=bool(e.get("cleanup_worktrees", True)),
        execution_profile=str(e.get("execution_profile", "yolo-worktree")),
    )

    g = _section(data, "git")
    git = GitConfig(
        require_clean_repo=bool(g.get("require_clean_repo", True)),
        branch_prefix=str(g.get("branch_prefix", "yolo")),
        commit_name=str(g.get("commit_name", "Omarchy YOLO")),
        commit_email=str(g.get("commit_email", "omarchy-yolo@localhost")),
    )

    gates_section = _section(data, "gates")
    gates = GateConfig(
        commands=_tuple_str(gates_section.get("commands")),
        final_commands=_tuple_str(gates_section.get("final_commands")),
    )

    s = _section(data, "sandbox")
    sandbox = SandboxConfig(
        backend=str(s.get("backend", "native")),
        network=bool(s.get("network", True)),
        read_only_home=bool(s.get("read_only_home", False)),
    )

    agents_section = _section(data, "agents")
    agents: dict[str, AgentConfig] = {}
    for name, default_cmd in DEFAULT_AGENT_COMMANDS.items():
        raw = agents_section.get(name, {})
        if not isinstance(raw, dict):
            raw = {}
        agents[name] = AgentConfig(
            enabled=bool(raw.get("enabled", name != "gemini")),
            command=_tuple_str(raw.get("command"), default_cmd),
        )
    for name, raw in agents_section.items():
        if name in agents or not isinstance(raw, dict):
            continue
        agents[name] = AgentConfig(
            enabled=bool(raw.get("enabled", True)),
            command=_tuple_str(raw.get("command")),
        )

    return Config(
        state_dir=state_dir,
        config_path=config_path,
        engine=engine,
        git=git,
        gates=gates,
        sandbox=sandbox,
        agents=agents,
    )
