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
    # Final review is the acceptance boundary. A stop request can race the
    # cancellation-shielded accepted-completion section after that boundary. If
    # source application/provenance has already begun, completion must be allowed
    # to win the race so an applied accepted release is never recorded as stopped.
    JobState.STOPPING: frozenset(
        {JobState.STOPPING, JobState.STOPPED, JobState.QUEUED, JobState.COMPLETED, JobState.FAILED}
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
ACCEPTANCE_PHASES = frozenset({"none", "accepted", "applying", "published"})


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
    running_attempt_task_states: Iterable[TaskState | str] = (),
    acceptance_phase: str | None = None,
) -> None:
    """Validate the durable orchestration graph at externally meaningful boundaries.

    ``running_attempt_task_states`` contains the current owning-task state for every
    attempt that is still durably RUNNING. Supplying it lets persistence mutations
    prove attempt/task/job consistency, not merely the job/task projection.

    ``acceptance_phase`` is optional for compatibility with model-only callers. The
    durable database always supplies it and therefore also proves that an accepted
    candidate cannot regress into ordinary scheduling and that COMPLETED implies a
    published acceptance record.
    """

    state = JobState(job_state)
    tasks = tuple(TaskState(item) for item in task_states)
    running_owners = tuple(TaskState(item) for item in running_attempt_task_states)
    inflight = any(item in INFLIGHT_TASK_STATES for item in tasks)

    if state == JobState.COMPLETED and any(item != TaskState.COMPLETED for item in tasks):
        raise StateTransitionError("completed job contains a non-completed task")
    if state in {JobState.QUEUED, JobState.STOPPED, JobState.FAILED} and inflight:
        raise StateTransitionError(f"{state.value} job contains an in-flight task")
    if state == JobState.STOPPED and not stop_requested:
        raise StateTransitionError("stopped job must preserve stop_requested")
    if state == JobState.COMPLETED and stop_requested:
        raise StateTransitionError("completed job cannot retain stop_requested")

    if running_owners:
        if state not in {JobState.RUNNING, JobState.STOPPING}:
            raise StateTransitionError(
                f"{state.value} job cannot retain a running attempt"
            )
        if any(owner not in INFLIGHT_TASK_STATES for owner in running_owners):
            raise StateTransitionError(
                "running attempt belongs to a task that is not in flight"
            )

    if acceptance_phase is None:
        return
    phase = str(acceptance_phase)
    if phase not in ACCEPTANCE_PHASES:
        raise StateTransitionError(f"unknown acceptance phase: {phase}")

    if phase in {"accepted", "applying", "published"}:
        if any(item != TaskState.COMPLETED for item in tasks):
            raise StateTransitionError(
                f"acceptance phase {phase} contains a non-completed task"
            )
        if running_owners:
            raise StateTransitionError(
                f"acceptance phase {phase} cannot retain a running attempt"
            )

    if phase in {"accepted", "applying"} and state not in {
        JobState.RUNNING,
        JobState.STOPPING,
    }:
        raise StateTransitionError(
            f"acceptance phase {phase} requires running or stopping job state"
        )
    if phase == "published" and state != JobState.COMPLETED:
        raise StateTransitionError("published acceptance requires completed job state")
    if state == JobState.COMPLETED and phase != "published":
        raise StateTransitionError("completed job requires published acceptance phase")
