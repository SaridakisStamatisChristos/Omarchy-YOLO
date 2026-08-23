from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .agents import AgentRegistry
from .model import PlannedTask
from .prompts import PLANNER_TEMPLATE
from .util import YoloError, extract_json_object

_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,31}$")
MAX_SUMMARY_CHARS = 4_000
MAX_TITLE_CHARS = 240
MAX_DESCRIPTION_CHARS = 8_000
MAX_ACCEPTANCE_ITEMS = 16
MAX_ACCEPTANCE_CHARS = 1_000
MAX_AGENT_NAME_CHARS = 64


def _string_list(
    value: Any,
    *,
    field: str,
    max_items: int,
    max_item_chars: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise YoloError(f"planner field '{field}' must be an array")
    if len(value) > max_items:
        raise YoloError(f"planner field '{field}' has too many items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise YoloError(f"planner field '{field}' must contain only strings")
        text = item.strip()
        if len(text) > max_item_chars:
            raise YoloError(f"planner field '{field}' contains an oversized item")
        result.append(text)
    return result


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
            raise YoloError(
                f"planner agent {agent_name} exited {result.returncode}: {result.stderr[-1000:]}"
            )
        payload = extract_json_object(result.stdout)
        summary_raw = payload.get("summary", "")
        if not isinstance(summary_raw, str):
            raise YoloError("planner summary must be a string")
        summary = summary_raw.strip()
        if len(summary) > MAX_SUMMARY_CHARS:
            raise YoloError("planner summary is too large")
        raw_tasks = payload.get("tasks")
        if not isinstance(raw_tasks, list):
            raise YoloError("planner JSON is missing a tasks array")
        if len(raw_tasks) > self.max_tasks:
            raise YoloError(
                f"planner returned {len(raw_tasks)} tasks; configured maximum is {self.max_tasks}"
            )
        tasks: list[PlannedTask] = []
        for raw in raw_tasks:
            if not isinstance(raw, dict):
                raise YoloError("planner tasks must be objects")
            logical_id = raw.get("id", "")
            title = raw.get("title", "")
            description = raw.get("description", "")
            if not all(isinstance(x, str) for x in (logical_id, title, description)):
                raise YoloError("planner task id/title/description must be strings")
            depends_on = _string_list(
                raw.get("depends_on", []),
                field="depends_on",
                max_items=self.max_tasks,
                max_item_chars=32,
            )
            acceptance = _string_list(
                raw.get("acceptance", []),
                field="acceptance",
                max_items=MAX_ACCEPTANCE_ITEMS,
                max_item_chars=MAX_ACCEPTANCE_CHARS,
            )
            preferred_raw = raw.get("preferred_agent")
            if preferred_raw not in (None, "") and not isinstance(preferred_raw, str):
                raise YoloError("planner preferred_agent must be a string or null")
            preferred = preferred_raw.strip() if isinstance(preferred_raw, str) else None
            if preferred and (
                len(preferred) > MAX_AGENT_NAME_CHARS
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", preferred)
            ):
                raise YoloError("planner preferred_agent is not a safe agent identifier")
            risk_raw = raw.get("risk", "medium")
            if not isinstance(risk_raw, str):
                raise YoloError("planner risk must be a string")
            tasks.append(
                PlannedTask(
                    logical_id=logical_id.strip(),
                    title=title.strip(),
                    description=description.strip(),
                    depends_on=tuple(depends_on),
                    acceptance=tuple(acceptance),
                    preferred_agent=preferred or None,
                    risk=risk_raw.strip().lower() or "medium",
                )
            )
        self.validate(tasks)
        return summary, tasks

    def validate(self, tasks: list[PlannedTask]) -> None:
        if not tasks:
            raise YoloError("planner returned zero tasks")
        if len(tasks) > self.max_tasks:
            raise YoloError(
                f"planner returned {len(tasks)} tasks; configured maximum is {self.max_tasks}"
            )
        ids = [task.logical_id for task in tasks]
        if any(not _ID_RE.fullmatch(task_id) for task_id in ids):
            raise YoloError("planner task IDs must be short identifiers such as T1 or parser_fix")
        if len(set(ids)) != len(ids):
            raise YoloError("planner returned duplicate task IDs")
        known = set(ids)
        for task in tasks:
            if not task.title or not task.description:
                raise YoloError(f"task {task.logical_id} is missing title/description")
            if len(task.title) > MAX_TITLE_CHARS:
                raise YoloError(f"task {task.logical_id} title is too long")
            if len(task.description) > MAX_DESCRIPTION_CHARS:
                raise YoloError(f"task {task.logical_id} description is too long")
            if len(task.acceptance) > MAX_ACCEPTANCE_ITEMS:
                raise YoloError(f"task {task.logical_id} has too many acceptance criteria")
            if any(len(item) > MAX_ACCEPTANCE_CHARS for item in task.acceptance):
                raise YoloError(f"task {task.logical_id} has an oversized acceptance criterion")
            if len(set(task.depends_on)) != len(task.depends_on):
                raise YoloError(f"task {task.logical_id} has duplicate dependencies")
            missing = set(task.depends_on) - known
            if missing:
                raise YoloError(
                    f"task {task.logical_id} depends on unknown tasks: {sorted(missing)}"
                )
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
