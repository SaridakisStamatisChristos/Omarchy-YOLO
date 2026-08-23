from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class JobState(StrEnum):
    QUEUED = "queued"
    PLANNING = "planning"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    REVIEWING = "reviewing"
    INTEGRATING = "integrating"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    STOPPED = "stopped"


class AttemptState(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True, frozen=True)
class PlannedTask:
    logical_id: str
    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    acceptance: tuple[str, ...] = ()
    preferred_agent: str | None = None
    risk: str = "medium"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PlannedTask":
        return cls(
            logical_id=str(value.get("id", "")).strip(),
            title=str(value.get("title", "")).strip(),
            description=str(value.get("description", "")).strip(),
            depends_on=tuple(str(x) for x in value.get("depends_on", []) or []),
            acceptance=tuple(str(x) for x in value.get("acceptance", []) or []),
            preferred_agent=(
                str(value["preferred_agent"]).strip()
                if value.get("preferred_agent") not in (None, "")
                else None
            ),
            risk=str(value.get("risk", "medium")).strip().lower() or "medium",
        )


@dataclass(slots=True, frozen=True)
class ReviewResult:
    verdict: str
    summary: str
    findings: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


@dataclass(slots=True, frozen=True)
class AgentResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    command: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass(slots=True, frozen=True)
class GateResult:
    command: str
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


@dataclass(slots=True)
class JobRecord:
    id: str
    repo: str
    goal: str
    state: JobState
    base_branch: str
    base_commit: str
    integration_branch: str
    integration_path: str
    auto_apply: bool
    stop_requested: bool
    created_at: float
    updated_at: float
    final_summary: str = ""
    error: str = ""


@dataclass(slots=True)
class TaskRecord:
    id: str
    job_id: str
    seq: int
    logical_id: str
    title: str
    description: str
    state: TaskState
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    acceptance: tuple[str, ...] = field(default_factory=tuple)
    preferred_agent: str | None = None
    attempts: int = 0
    branch: str = ""
    worktree: str = ""
    base_commit: str = ""
    last_error: str = ""
    result_summary: str = ""
