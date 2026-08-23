from __future__ import annotations

from pathlib import Path

from .agents import AgentRegistry
from .model import ReviewResult, TaskRecord
from .prompts import FINAL_REVIEW_TEMPLATE, REVIEW_TEMPLATE
from .util import YoloError, extract_json_object


def format_gates(results: list[object]) -> str:
    lines: list[str] = []
    for result in results:
        command = getattr(result, "command", "?")
        returncode = getattr(result, "returncode", "?")
        timed_out = getattr(result, "timed_out", False)
        stderr = str(getattr(result, "stderr", ""))[-1500:]
        lines.append(f"- {command}: exit={returncode} timeout={timed_out}\n{stderr}")
    return "\n".join(lines) or "No explicit gates were run."


class Reviewer:
    def __init__(self, registry: AgentRegistry):
        self.registry = registry

    async def review_task(
        self,
        *,
        goal: str,
        task: TaskRecord,
        diff: str,
        gates: list[object],
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> ReviewResult:
        prompt = REVIEW_TEMPLATE.format(
            goal=goal,
            task=f"{task.logical_id} {task.title}\n{task.description}",
            gates=format_gates(gates),
            diff=diff,
        )
        return await self._run(prompt, cwd, agent_name, timeout_seconds, log_path)

    async def review_final(
        self,
        *,
        goal: str,
        diff: str,
        gates: list[object],
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> ReviewResult:
        prompt = FINAL_REVIEW_TEMPLATE.format(goal=goal, gates=format_gates(gates), diff=diff)
        return await self._run(prompt, cwd, agent_name, timeout_seconds, log_path)

    async def _run(
        self,
        prompt: str,
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> ReviewResult:
        result = await self.registry.get(agent_name).run(
            prompt,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
            execution_profile="review",
        )
        if not result.ok:
            raise YoloError(f"reviewer agent {agent_name} exited {result.returncode}: {result.stderr[-1000:]}")
        payload = extract_json_object(result.stdout)
        verdict = str(payload.get("verdict", "")).strip().lower()
        if verdict not in {"pass", "retry", "fail"}:
            raise YoloError(f"reviewer returned invalid verdict '{verdict}'")
        raw_findings = payload.get("findings", [])
        findings = tuple(str(x) for x in raw_findings) if isinstance(raw_findings, list) else ()
        return ReviewResult(verdict, str(payload.get("summary", "")).strip(), findings)
