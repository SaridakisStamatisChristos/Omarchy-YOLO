from __future__ import annotations

from pathlib import Path

from .agents import AgentRegistry
from .model import GateResult, ReviewResult, TaskRecord
from .prompts import (
    FILE_SYNTHESIS_TEMPLATE,
    FINAL_REVIEW_TEMPLATE,
    FINAL_SYNTHESIS_TEMPLATE,
    REVIEW_TEMPLATE,
)
from .util import YoloError, extract_json_object, truncate_utf8

MAX_REVIEW_SUMMARY_CHARS = 8_000
MAX_REVIEW_FINDINGS = 64
MAX_REVIEW_FINDING_CHARS = 4_000
MAX_GATE_SUMMARY_BYTES = 16_000
MAX_SYNTHESIS_INPUT_BYTES = 80_000


def format_gates(results: list[GateResult]) -> str:
    lines: list[str] = []
    for result in results:
        stderr = result.stderr[-1500:]
        lines.append(
            f"- {result.command}: exit={result.returncode} timeout={result.timed_out}\n{stderr}"
        )
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
        gates: list[GateResult],
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
        gates: list[GateResult],
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> ReviewResult:
        return await self.review_final_chunk(
            goal=goal,
            diff=diff,
            gates=gates,
            files=("candidate",),
            chunk_index=1,
            chunk_total=1,
            cwd=cwd,
            agent_name=agent_name,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
        )

    async def review_final_chunk(
        self,
        *,
        goal: str,
        diff: str,
        gates: list[GateResult],
        files: tuple[str, ...],
        chunk_index: int,
        chunk_total: int,
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> ReviewResult:
        prompt = FINAL_REVIEW_TEMPLATE.format(
            goal=goal,
            gates=format_gates(gates),
            chunk_index=chunk_index,
            chunk_total=chunk_total,
            files="\n".join(f"- {path}" for path in files) or "- none",
            diff=diff,
        )
        return await self._run(prompt, cwd, agent_name, timeout_seconds, log_path)

    async def review_file_synthesis(
        self,
        *,
        goal: str,
        gates: list[GateResult],
        file_path: str,
        chunk_reviews: list[ReviewResult],
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
    ) -> ReviewResult:
        summary_text = "\n".join(
            f"- shard {index}: {review.summary}"
            for index, review in enumerate(chunk_reviews, start=1)
        )
        if len(file_path.encode("utf-8")) > MAX_SYNTHESIS_INPUT_BYTES:
            raise YoloError("file path is too large for complete file synthesis review")
        if len(summary_text.encode("utf-8")) > MAX_SYNTHESIS_INPUT_BYTES:
            raise YoloError("file shard summaries are too large for complete synthesis review")
        prompt = FILE_SYNTHESIS_TEMPLATE.format(
            goal=goal,
            gates=format_gates(gates),
            file_path=file_path,
            chunk_summaries=summary_text or "- no shard summaries",
        )
        return await self._run(prompt, cwd, agent_name, timeout_seconds, log_path)

    async def review_final_synthesis(
        self,
        *,
        goal: str,
        gates: list[GateResult],
        manifest: list[str],
        cwd: Path,
        agent_name: str,
        timeout_seconds: int,
        log_path: Path,
        file_reviews: list[tuple[str, ReviewResult]] | None = None,
        chunk_reviews: list[ReviewResult] | None = None,
    ) -> ReviewResult:
        manifest_text = "\n".join(f"- {path}" for path in manifest) or "- no changed files"
        if len(manifest_text.encode("utf-8")) > MAX_SYNTHESIS_INPUT_BYTES:
            raise YoloError("changed-file manifest is too large for complete synthesis review")

        # Backward compatibility for v1.1 callers. The orchestrator itself never uses
        # this path in v1.2; it supplies semantically synthesized file reports.
        effective_file_reviews = file_reviews
        if effective_file_reviews is None:
            legacy = chunk_reviews or []
            if len(legacy) == len(manifest):
                effective_file_reviews = list(zip(manifest, legacy, strict=True))
            else:
                effective_file_reviews = [
                    (f"legacy-shard-{index}", review)
                    for index, review in enumerate(legacy, start=1)
                ]

        summary_text = "\n".join(
            f"- {path}: {review.summary}" for path, review in effective_file_reviews
        ) or "- no changed-file reports"
        if len(summary_text.encode("utf-8")) > MAX_SYNTHESIS_INPUT_BYTES:
            raise YoloError("file semantic reports are too large for complete synthesis review")
        prompt = FINAL_SYNTHESIS_TEMPLATE.format(
            goal=goal,
            gates=format_gates(gates),
            manifest=manifest_text,
            file_summaries=summary_text,
        )
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
        if verdict == "pass":
            if findings:
                raise YoloError("reviewer returned pass with material findings")
            if not summary:
                raise YoloError("reviewer pass verdict must include a semantic summary")
        elif not summary and not any(findings):
            raise YoloError("reviewer non-pass verdict must include a summary or finding")
        return ReviewResult(verdict, summary, tuple(findings))
