from __future__ import annotations

from pathlib import Path

from .agents import AgentRegistry
from .model import ReviewResult, TaskRecord
from .prompts import FINAL_REVIEW_TEMPLATE, REVIEW_TEMPLATE
from .util import YoloError, extract_json_object, truncate_utf8

MAX_REVIEW_SUMMARY_CHARS = 8_000
MAX_REVIEW_FINDINGS = 64
MAX_REVIEW_FINDING_CHARS = 4_000
MAX_GATE_SUMMARY_BYTES = 16_000


def format_gates(results: list[object]) -> str:
    lines: list[str] = []
    for result in results:
        command = getattr(result, "command", "?")
        returncode = getattr(result, "returncode", "?")
        timed_out = getattr(result, "timed_out", False)
        stderr = str(getattr(result, "stderr", ""))[-1500:]
        lines.append(f"- {command}: exit={returncode} timeout={timed_out}\n{stderr}")
    combined = "\n".join(lines) or "No explicit gates were run."
    return truncate_utf8(combined, MAX_GATE_SUMMARY_BYTES)


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
            raise YoloError(
                f"reviewer agent {agent_name} exited {result.returncode}: {result.stderr[-1000:]}"
            )
        payload = extract_json_object(result.stdout)
        verdict_raw = payload.get("verdict", "")
        summary_raw = payload.get("summary", "")
        raw_findings = payload.get("findings", [])
        if not isinstance(verdict_raw, str) or not isinstance(summary_raw, str):
            raise YoloError("reviewer verdict and summary must be strings")
        verdict = verdict_raw.strip().lower()
        if verdict not in {"pass", "retry", "fail"}:
            raise YoloError(f"reviewer returned invalid verdict '{verdict}'")
        summary = summary_raw.strip()
        if len(summary) > MAX_REVIEW_SUMMARY_CHARS:
            raise YoloError("reviewer summary is too large")
        if not isinstance(raw_findings, list):
            raise YoloError("reviewer findings must be an array")
        if len(raw_findings) > MAX_REVIEW_FINDINGS:
            raise YoloError("reviewer returned too many findings")
        findings: list[str] = []
        for item in raw_findings:
            if not isinstance(item, str):
                raise YoloError("reviewer findings must contain only strings")
            finding = item.strip()
            if len(finding) > MAX_REVIEW_FINDING_CHARS:
                raise YoloError("reviewer finding is too large")
            findings.append(finding)
        if verdict == "pass" and any(findings):
            raise YoloError("reviewer returned pass with material findings")
        return ReviewResult(verdict, summary, tuple(findings))
