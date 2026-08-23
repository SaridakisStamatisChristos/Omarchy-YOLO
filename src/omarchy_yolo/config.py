from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import YoloError, xdg_config_home, xdg_state_home


@dataclass(slots=True)
class EngineConfig:
    max_parallel: int = 4
    max_global_workers: int = 4
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
    final_review_chunk_bytes: int = 60_000
    final_review_chunk_files: int = 8
    final_review_max_files: int = 512
    final_review_allow_binary: bool = False


@dataclass(slots=True)
class GitConfig:
    require_clean_repo: bool = True
    branch_prefix: str = "yolo"
    commit_name: str = "Omarchy YOLO"
    commit_email: str = "omarchy-yolo@localhost"
    command_timeout_seconds: int = 120
    allow_repository_commands: bool = True


@dataclass(slots=True)
class GateConfig:
    commands: tuple[str, ...] = ()
    final_commands: tuple[str, ...] = ()


@dataclass(slots=True)
class SandboxConfig:
    backend: str = "native"
    network: bool = True
    read_only_home: bool = False
    writable_home_paths: tuple[str, ...] = ()
    hostile_repo_mode: bool = False
    gate_env_allowlist: tuple[str, ...] = ()
    agent_env_allowlist: tuple[str, ...] = ()


@dataclass(slots=True)
class AgentConfig:
    enabled: bool = True
    command: tuple[str, ...] = ()
    review_command: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


DEFAULT_AGENT_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "exec", "--full-auto", "--sandbox", "workspace-write"),
    "claude": ("claude", "-p", "--dangerously-skip-permissions", "--output-format", "json"),
    "opencode": ("opencode", "run", "--format", "json", "--auto"),
    "gemini": ("gemini", "-p"),
}

_BRANCH_PREFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,63}$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_AGENT_ROLES = frozenset({"worker", "planner", "reviewer", "integrator"})


@dataclass(slots=True)
class Config:
    state_dir: Path
    config_path: Path
    engine: EngineConfig = field(default_factory=EngineConfig)
    git: GitConfig = field(default_factory=GitConfig)
    gates: GateConfig = field(default_factory=GateConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sandbox.hostile_repo_mode and self.sandbox.backend != "bwrap":
            raise YoloError("sandbox.hostile_repo_mode requires sandbox.backend='bwrap'")
        if self.sandbox.hostile_repo_mode and self.git.allow_repository_commands:
            raise YoloError(
                "sandbox.hostile_repo_mode requires git.allow_repository_commands=false"
            )

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


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise YoloError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise YoloError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _strict_bool(value: Any, *, default: bool, name: str) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise YoloError(f"{name} must be true or false")
    return value


def _agent_name(value: Any, *, field_name: str) -> str:
    name = str(value).strip()
    if not _AGENT_NAME_RE.fullmatch(name):
        raise YoloError(f"{field_name} must be a safe agent identifier")
    return name


def _agent_names(value: Any, *, default: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    names = _tuple_str(value, default)
    if not names:
        raise YoloError(f"{field_name} must contain at least one agent")
    return tuple(_agent_name(name, field_name=field_name) for name in names)


def _branch_prefix(value: Any) -> str:
    prefix = str(value).strip()
    invalid = (
        not _BRANCH_PREFIX_RE.fullmatch(prefix)
        or ".." in prefix
        or "//" in prefix
        or prefix.endswith(("/", ".", ".lock"))
        or "@{" in prefix
    )
    if invalid:
        raise YoloError("git.branch_prefix is not a safe Git ref prefix")
    return prefix


def _identity_value(value: Any, *, field_name: str, maximum: int) -> str:
    identity = str(value).strip()
    if not identity:
        raise YoloError(f"{field_name} cannot be empty")
    if any(char in identity for char in ("\x00", "\r", "\n")):
        raise YoloError(f"{field_name} cannot contain control line breaks")
    if len(identity) > maximum:
        raise YoloError(f"{field_name} cannot exceed {maximum} characters")
    return identity


def _writable_home_paths(value: Any) -> tuple[str, ...]:
    paths = _tuple_str(value)
    clean: list[str] = []
    for raw in paths:
        candidate = raw.strip().strip("/")
        if not candidate or candidate in {".", ".."} or candidate.startswith("../") or "/../" in candidate:
            raise YoloError("sandbox.writable_home_paths must contain relative paths inside HOME")
        clean.append(candidate)
    return tuple(clean)


def _environment_names(value: Any, *, field_name: str) -> tuple[str, ...]:
    names = _tuple_str(value)
    clean: list[str] = []
    for raw in names:
        name = raw.strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise YoloError(f"{field_name} contains an invalid environment variable name")
        if name not in clean:
            clean.append(name)
    return tuple(clean)


def _agent_roles(value: Any, *, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise YoloError(f"{field_name} must be an array of role names")
    roles = tuple(str(item) for item in value)
    clean: list[str] = []
    for raw in roles:
        role = raw.strip().lower()
        if role not in _AGENT_ROLES:
            raise YoloError(
                f"{field_name} contains invalid role {role!r}; expected one of {sorted(_AGENT_ROLES)}"
            )
        if role not in clean:
            clean.append(role)
    return tuple(clean)


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
    execution_profile = str(e.get("execution_profile", "yolo-worktree"))
    if execution_profile not in {"yolo-worktree", "danger-yolo"}:
        raise YoloError("engine.execution_profile must be 'yolo-worktree' or 'danger-yolo'")
    engine = EngineConfig(
        max_parallel=_bounded_int(e.get("max_parallel", 4), default=4, minimum=1, maximum=64, name="engine.max_parallel"),
        max_global_workers=_bounded_int(
            e.get("max_global_workers", 4),
            default=4,
            minimum=1,
            maximum=128,
            name="engine.max_global_workers",
        ),
        max_attempts=_bounded_int(e.get("max_attempts", 3), default=3, minimum=1, maximum=20, name="engine.max_attempts"),
        max_final_cycles=_bounded_int(e.get("max_final_cycles", 2), default=2, minimum=0, maximum=20, name="engine.max_final_cycles"),
        max_tasks=_bounded_int(e.get("max_tasks", 12), default=12, minimum=1, maximum=256, name="engine.max_tasks"),
        agent_timeout_seconds=_bounded_int(e.get("agent_timeout_seconds", 3600), default=3600, minimum=1, maximum=86_400, name="engine.agent_timeout_seconds"),
        gate_timeout_seconds=_bounded_int(e.get("gate_timeout_seconds", 1800), default=1800, minimum=1, maximum=86_400, name="engine.gate_timeout_seconds"),
        planner_agent=_agent_name(e.get("planner_agent", "codex"), field_name="engine.planner_agent"),
        reviewer_agent=_agent_name(e.get("reviewer_agent", "claude"), field_name="engine.reviewer_agent"),
        integrator_agent=_agent_name(e.get("integrator_agent", "codex"), field_name="engine.integrator_agent"),
        worker_agents=_agent_names(
            e.get("worker_agents"),
            default=("codex", "claude", "opencode"),
            field_name="engine.worker_agents",
        ),
        auto_apply=_strict_bool(e.get("auto_apply"), default=False, name="engine.auto_apply"),
        cleanup_worktrees=_strict_bool(e.get("cleanup_worktrees"), default=True, name="engine.cleanup_worktrees"),
        execution_profile=execution_profile,
        final_review_chunk_bytes=_bounded_int(
            e.get("final_review_chunk_bytes", 60_000),
            default=60_000,
            minimum=8_000,
            maximum=120_000,
            name="engine.final_review_chunk_bytes",
        ),
        final_review_chunk_files=_bounded_int(
            e.get("final_review_chunk_files", 8),
            default=8,
            minimum=1,
            maximum=64,
            name="engine.final_review_chunk_files",
        ),
        final_review_max_files=_bounded_int(
            e.get("final_review_max_files", 512),
            default=512,
            minimum=1,
            maximum=4096,
            name="engine.final_review_max_files",
        ),
        final_review_allow_binary=_strict_bool(
            e.get("final_review_allow_binary"),
            default=False,
            name="engine.final_review_allow_binary",
        ),
    )

    s = _section(data, "sandbox")
    backend = str(s.get("backend", "native"))
    if backend not in {"native", "none", "bwrap"}:
        raise YoloError("sandbox.backend must be 'native', 'none', or 'bwrap'")
    hostile_repo_mode = _strict_bool(
        s.get("hostile_repo_mode"),
        default=False,
        name="sandbox.hostile_repo_mode",
    )
    if hostile_repo_mode and backend != "bwrap":
        raise YoloError("sandbox.hostile_repo_mode requires sandbox.backend='bwrap'")
    sandbox = SandboxConfig(
        backend=backend,
        network=_strict_bool(s.get("network"), default=True, name="sandbox.network"),
        read_only_home=_strict_bool(s.get("read_only_home"), default=False, name="sandbox.read_only_home"),
        writable_home_paths=_writable_home_paths(s.get("writable_home_paths")),
        hostile_repo_mode=hostile_repo_mode,
        gate_env_allowlist=_environment_names(
            s.get("gate_env_allowlist"), field_name="sandbox.gate_env_allowlist"
        ),
        agent_env_allowlist=_environment_names(
            s.get("agent_env_allowlist"), field_name="sandbox.agent_env_allowlist"
        ),
    )

    g = _section(data, "git")
    allow_repository_commands = _strict_bool(
        g.get("allow_repository_commands"),
        default=not hostile_repo_mode,
        name="git.allow_repository_commands",
    )
    if hostile_repo_mode and allow_repository_commands:
        raise YoloError(
            "sandbox.hostile_repo_mode requires git.allow_repository_commands=false"
        )
    git = GitConfig(
        require_clean_repo=_strict_bool(g.get("require_clean_repo"), default=True, name="git.require_clean_repo"),
        branch_prefix=_branch_prefix(g.get("branch_prefix", "yolo")),
        commit_name=_identity_value(
            g.get("commit_name", "Omarchy YOLO"),
            field_name="git.commit_name",
            maximum=200,
        ),
        commit_email=_identity_value(
            g.get("commit_email", "omarchy-yolo@localhost"),
            field_name="git.commit_email",
            maximum=320,
        ),
        command_timeout_seconds=_bounded_int(
            g.get("command_timeout_seconds", 120),
            default=120,
            minimum=5,
            maximum=900,
            name="git.command_timeout_seconds",
        ),
        allow_repository_commands=allow_repository_commands,
    )

    gates_section = _section(data, "gates")
    gates = GateConfig(
        commands=_tuple_str(gates_section.get("commands")),
        final_commands=_tuple_str(gates_section.get("final_commands")),
    )

    agents_section = _section(data, "agents")
    agents: dict[str, AgentConfig] = {}
    for name, default_cmd in DEFAULT_AGENT_COMMANDS.items():
        raw = agents_section.get(name, {})
        if not isinstance(raw, dict):
            raw = {}
        agents[name] = AgentConfig(
            enabled=_strict_bool(raw.get("enabled"), default=name != "gemini", name=f"agents.{name}.enabled"),
            command=_tuple_str(raw.get("command"), default_cmd),
            review_command=_tuple_str(raw.get("review_command")),
            roles=_agent_roles(raw.get("roles"), field_name=f"agents.{name}.roles"),
        )
    for name, raw in agents_section.items():
        safe_name = _agent_name(name, field_name=f"agents.{name}")
        if safe_name in agents or not isinstance(raw, dict):
            continue
        agents[safe_name] = AgentConfig(
            enabled=_strict_bool(
                raw.get("enabled"), default=True, name=f"agents.{safe_name}.enabled"
            ),
            command=_tuple_str(raw.get("command")),
            review_command=_tuple_str(raw.get("review_command")),
            roles=_agent_roles(raw.get("roles"), field_name=f"agents.{safe_name}.roles"),
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
