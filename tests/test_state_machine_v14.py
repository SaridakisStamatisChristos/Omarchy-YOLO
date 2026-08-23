from __future__ import annotations

import itertools

import pytest

from omarchy_yolo.model import AttemptState, JobState, TaskState
from omarchy_yolo.state_machine import (
    ATTEMPT_TRANSITIONS,
    JOB_TRANSITIONS,
    TASK_TRANSITIONS,
    StateTransitionError,
    validate_attempt_transition,
    validate_job_snapshot,
    validate_job_transition,
    validate_task_transition,
)


def test_reference_transition_tables_are_total_and_exhaustive() -> None:
    assert set(JOB_TRANSITIONS) == set(JobState)
    assert set(TASK_TRANSITIONS) == set(TaskState)
    assert set(ATTEMPT_TRANSITIONS) == set(AttemptState)

    for source, target in itertools.product(JobState, repeat=2):
        if target in JOB_TRANSITIONS[source]:
            validate_job_transition(source, target)
        else:
            with pytest.raises(StateTransitionError):
                validate_job_transition(source, target)

    for source, target in itertools.product(TaskState, repeat=2):
        if target in TASK_TRANSITIONS[source]:
            validate_task_transition(source, target)
        else:
            with pytest.raises(StateTransitionError):
                validate_task_transition(source, target)

    for source, target in itertools.product(AttemptState, repeat=2):
        if target in ATTEMPT_TRANSITIONS[source]:
            validate_attempt_transition(source, target)
        else:
            with pytest.raises(StateTransitionError):
                validate_attempt_transition(source, target)


def test_terminal_success_states_are_absorbing() -> None:
    assert JOB_TRANSITIONS[JobState.COMPLETED] == {JobState.COMPLETED}
    assert TASK_TRANSITIONS[TaskState.COMPLETED] == {TaskState.COMPLETED}
    assert ATTEMPT_TRANSITIONS[AttemptState.PASSED] == {AttemptState.PASSED}


def test_snapshot_model_rejects_impossible_terminal_combinations() -> None:
    validate_job_snapshot(
        JobState.COMPLETED,
        [TaskState.COMPLETED, TaskState.COMPLETED],
        stop_requested=False,
    )
    with pytest.raises(StateTransitionError, match="non-completed"):
        validate_job_snapshot(
            JobState.COMPLETED,
            [TaskState.COMPLETED, TaskState.PENDING],
            stop_requested=False,
        )
    with pytest.raises(StateTransitionError, match="in-flight"):
        validate_job_snapshot(
            JobState.QUEUED,
            [TaskState.RUNNING],
            stop_requested=False,
        )
    with pytest.raises(StateTransitionError, match="stop_requested"):
        validate_job_snapshot(JobState.STOPPED, [TaskState.STOPPED], stop_requested=False)
    with pytest.raises(StateTransitionError, match="cannot retain"):
        validate_job_snapshot(
            JobState.COMPLETED,
            [TaskState.COMPLETED],
            stop_requested=True,
        )


def test_recovery_paths_are_explicitly_represented() -> None:
    for source in (JobState.PLANNING, JobState.RUNNING, JobState.STOPPING):
        validate_job_transition(source, JobState.QUEUED)
    for source in (TaskState.RUNNING, TaskState.REVIEWING, TaskState.INTEGRATING):
        validate_task_transition(source, TaskState.PENDING)
        validate_task_transition(source, TaskState.STOPPED)
        validate_task_transition(source, TaskState.FAILED)
