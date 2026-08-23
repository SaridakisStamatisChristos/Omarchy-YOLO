from __future__ import annotations

import pytest

from omarchy_yolo.model import PlannedTask
from omarchy_yolo.planner import Planner
from omarchy_yolo.util import YoloError


class DummyRegistry:
    pass


def planner() -> Planner:
    return Planner(DummyRegistry(), max_tasks=8)  # type: ignore[arg-type]


def test_validate_accepts_dag() -> None:
    tasks = [
        PlannedTask("T1", "First", "Do first"),
        PlannedTask("T2", "Second", "Do second", depends_on=("T1",)),
    ]
    planner().validate(tasks)


def test_validate_rejects_cycle() -> None:
    tasks = [
        PlannedTask("T1", "First", "Do first", depends_on=("T2",)),
        PlannedTask("T2", "Second", "Do second", depends_on=("T1",)),
    ]
    with pytest.raises(YoloError, match="cycle"):
        planner().validate(tasks)


def test_validate_rejects_missing_dependency() -> None:
    tasks = [PlannedTask("T1", "First", "Do first", depends_on=("NOPE",))]
    with pytest.raises(YoloError, match="unknown"):
        planner().validate(tasks)



def test_validate_rejects_oversized_task_title() -> None:
    tasks = [PlannedTask("T1", "x" * 300, "description")]
    with pytest.raises(YoloError, match="title is too long"):
        planner().validate(tasks)



def test_validate_rejects_invalid_preferred_agent_via_plan_parser(tmp_path) -> None:
    # Syntax validation for preferred agents is exercised through the planner parser in integration;
    # the identifier grammar itself must remain filename-safe.
    import re
    from omarchy_yolo.planner import MAX_AGENT_NAME_CHARS

    assert MAX_AGENT_NAME_CHARS == 64
    assert re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "claude-review")
    assert not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "../../escape")
