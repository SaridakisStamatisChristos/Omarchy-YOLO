from __future__ import annotations

from collections.abc import Iterable

from .model import AttemptState, JobState, TaskState
from .util import YoloError


class StateTransitionError(YoloError):
    """Raised when durable orchestration state would violate the reference model."""


JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset(
        {JobState.QUEUED, JobState.PLANNING, JobState.RUNNING, JobState.STOPPING, JobState.FAILED}
    ),
    JobState.PLANNING: frozenset(
        {JobState.PLANNING, JobState.RUNNING, JobState.QUEUED, JobState.STOPPING, JobState.FAILED}
    ),
    JobState.RUNNING: frozenset(
        {JobState.RUNNING, JobState.QUEUED, JobState.STOPPING, JobState.COMPLETED, JobState.FAILED}
    ),
    JobState.STOPPING: frozenset(
        {JobState.STOPPING, JobState.STOPPED, JobState.QUEUED, JobState.FAILED}
    ),
    JobState.STOPPED: frozenset({JobState.STOPPED, JobState.QUEUED}),
    JobState.FAILED: frozenset({JobState.FAILED, JobState.QUEUED}),
    JobState.COMPLETED: frozenset({JobState.COMPLETED}),
}

TASK_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.PENDING, TaskState.RUNNING, TaskState.BLOCKED, TaskState.STOPPED, TaskState.FAILED}
    ),
    TaskState.RUNNING: frozenset(
        {TaskState.RUNNING, TaskState.REVIEWING, TaskState.PENDING, TaskState.FAILED, TaskState.STOPPED}
    ),
    TaskState.REVIEWING: frozenset(
        {
            TaskState.REVIEWING,
            TaskState.INTEGRATING,
            TaskState.PENDING,
            TaskState.FAILED,
            TaskState.STOPPED,
        }
    ),
    TaskState.INTEGRATING: frozenset(
        {
            TaskState.INTEGRATING,
            TaskState.PENDING,
            TaskState.COMPLETED,
            TaskState.FAILED,
            TaskState.STOPPED,
        }
    ),
    TaskState.COMPLETED: frozenset({TaskState.COMPLETED}),
    TaskState.FAILED: frozenset({TaskState.FAILED, TaskState.PENDING}),
    TaskState.BLOCKED: frozenset(
        {TaskState.BLOCKED, TaskState.PENDING, TaskState.FAILED, TaskState.STOPPED}
    ),
    TaskState.STOPPED: frozenset({TaskState.STOPPED, TaskState.PENDING}),
}

ATTEMPT_TRANSITIONS: dict[AttemptState, frozenset[AttemptState]] = {
    AttemptState.RUNNING: frozenset(
        {AttemptState.RUNNING, AttemptState.PASSED, AttemptState.FAILED, AttemptState.CANCELLED}
    ),
    AttemptState.PASSED: frozenset({AttemptState.PASSED}),
    AttemptState.FAILED: frozenset({AttemptState.FAILED}),
    AttemptState.CANCELLED: frozenset({AttemptState.CANCELLED}),
}

INFLIGHT_TASK_STATES = frozenset(
    {TaskState.RUNNING, TaskState.REVIEWING, TaskState.INTEGRATING}
)
TERMINAL_JOB_STATES = frozenset({JobState.STOPPED, JobState.COMPLETED, JobState.FAILED})


def validate_job_transition(current: JobState | str, target: JobState | str) -> None:
    source = JobState(current)
    destination = JobState(target)
    if destination not in JOB_TRANSITIONS[source]:
        raise StateTransitionError(
            f"illegal job state transition: {source.value} -> {destination.value}"
        )


def validate_task_transition(current: TaskState | str, target: TaskState | str) -> None:
    source = TaskState(current)
    destination = TaskState(target)
    if destination not in TASK_TRANSITIONS[source]:
        raise StateTransitionError(
            f"illegal task state transition: {source.value} -> {destination.value}"
        )


def validate_attempt_transition(
    current: AttemptState | str, target: AttemptState | str
) -> None:
    source = AttemptState(current)
    destination = AttemptState(target)
    if destination not in ATTEMPT_TRANSITIONS[source]:
        raise StateTransitionError(
            f"illegal attempt state transition: {source.value} -> {destination.value}"
        )


def validate_job_snapshot(
    job_state: JobState | str,
    task_states: Iterable[TaskState | str],
    *,
    stop_requested: bool,
) -> None:
    """Validate cross-record invariants at externally meaningful boundaries.

    This is intentionally stricter than transition validation and is used by tests,
    recovery checks, and provenance generation. Intermediate statements inside one
    SQLite transaction do not need to satisfy it until that transaction commits.
    """

    state = JobState(job_state)
    tasks = tuple(TaskState(item) for item in task_states)
    inflight = any(item in INFLIGHT_TASK_STATES for item in tasks)

    if state == JobState.COMPLETED and any(item != TaskState.COMPLETED for item in tasks):
        raise StateTransitionError("completed job contains a non-completed task")
    if state in {JobState.QUEUED, JobState.STOPPED, JobState.FAILED} and inflight:
        raise StateTransitionError(f"{state.value} job contains an in-flight task")
    if state == JobState.STOPPED and not stop_requested:
        raise StateTransitionError("stopped job must preserve stop_requested")
    if state == JobState.COMPLETED and stop_requested:
        raise StateTransitionError("completed job cannot retain stop_requested")
