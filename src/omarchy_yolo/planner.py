from __future__ import annotations

import re
from pathlib import Path

from .agents import AgentRegistry
from .model import PlannedTask
from .prompts import PLANNER_TEMPLATE
from .util import YoloError, extract_json_object


_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")


class Planner:
    def __init__(self, registry: AgentRegistry, *, max_tasks: int):
        self.registry = registry
        self.max_tasks = max_tasks

    async def plan(
        self,
        goal: str,
        *,
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> tuple[str, list[PlannedTask]]:
        result = await self.registry.get(agent_name).run(
            PLANNER_TEMPLATE.format(goal=goal),
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
            execution_profile="review",
        )
        if not result.ok:
            raise YoloError(f"planner agent {agent_name} exited {result.returncode}: {result.stderr[-1000:]}")
        payload = extract_json_object(result.stdout)
        summary = str(payload.get("summary", "")).strip()
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list):
            raise YoloError("planner JSON is missing a tasks array")
        tasks = [PlannedTask.from_dict(x) for x in raw_tasks if isinstance(x, dict)]
        self.validate(tasks)
        return summary, tasks

    def validate(self, tasks: list[PlannedTask]) -> None:
        if not tasks:
            raise YoloError("planner returned zero tasks")
        if len(tasks) > self.max_tasks:
            raise YoloError(f"planner returned {len(tasks)} tasks; configured maximum is {self.max_tasks}")
        ids = [task.logical_id for task in tasks]
        if any(not _ID_RE.fullmatch(task_id) for task_id in ids):
            raise YoloError("planner task IDs must be short identifiers such as T1 or parser_fix")
        if len(set(ids)) != len(ids):
            raise YoloError("planner returned duplicate task IDs")
        known = set(ids)
        for task in tasks:
            if not task.title or not task.description:
                raise YoloError(f"task {task.logical_id} is missing title/description")
            missing = set(task.depends_on) - known
            if missing:
                raise YoloError(f"task {task.logical_id} depends on unknown tasks: {sorted(missing)}")
            if task.logical_id in task.depends_on:
                raise YoloError(f"task {task.logical_id} depends on itself")
            if task.risk not in {"low", "medium", "high"}:
                raise YoloError(f"task {task.logical_id} has invalid risk '{task.risk}'")

        graph = {task.logical_id: set(task.depends_on) for task in tasks}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str) -> None:
            if node in visited:
                return
            if node in visiting:
                raise YoloError("planner task graph contains a dependency cycle")
            visiting.add(node)
            for dep in graph[node]:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node)
