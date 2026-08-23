from __future__ import annotations

import pytest

from omarchy_yolo.model import PlannedTask
from omarchy_yolo.planner import Planner
from omarchy_yolo.util import YoloError


class DummyRegistry: pass

def planner() -> Planner: return Planner(DummyRegistry(), max_tasks=8)  # type: ignore[arg-type]

def test_validate_accepts_dag() -> None:
    planner().validate([PlannedTask("T1", "First", "Do first"), PlannedTask("T2", "Second", "Do second", depends_on=("T1",))])

def test_validate_rejects_cycle() -> None:
    tasks=[PlannedTask("T1","First","Do first",depends_on=("T2",)),PlannedTask("T2","Second","Do second",depends_on=("T1",))]
    with pytest.raises(YoloError, match="cycle"): planner().validate(tasks)

def test_validate_rejects_missing_dependency() -> None:
    with pytest.raises(YoloError, match="unknown"): planner().validate([PlannedTask("T1","First","Do first",depends_on=("NOPE",))])
