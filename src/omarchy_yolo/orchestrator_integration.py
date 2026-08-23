from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from .gates import detect_gate_commands
from .git import GitError, GitRepo, MergeConflict
from .model import GateResult, ReviewResult, TaskRecord, TaskState
from .prompts import INTEGRATION_REPAIR_TEMPLATE
from .review_source import build_review_chunks
from .reviewer import format_gates
from .util import YoloError

if TYPE_CHECKING:
    from .agents import AgentRegistry
    from .config import Config
    from .db import Database
    from .gates import GateRunner
    from .reviewer import Reviewer
    from .runtime import ResourceCoordinator


class IntegrationMixin:
    if TYPE_CHECKING:
        config: Config
        db: Database
        registry: AgentRegistry
        reviewer: Reviewer
        gates: GateRunner
        coordinator: ResourceCoordinator
        _merge_lock: asyncio.Lock
        _active: set[asyncio.Task[bool]]

    async def _integrate_task(
        self,
        job_id: str,
        task: TaskRecord,
        repo: GitRepo,
        integration_path: Path,
    ) -> tuple[bool, str]:
        async with self._merge_lock:
            async with self.coordinator.repo_lock(repo.root):
                self.db.update_task(task.id, state=TaskState.INTEGRATING)
                pre_merge = await asyncio.to_thread(repo.head, integration_path)
                try:
                    try:
                        await asyncio.to_thread(repo.merge, integration_path, task.branch)
                    except MergeConflict as exc:
                        self.db.event(
                            job_id,
                            "integration.conflict",
                            {"error": str(exc)},
                            task_id=task.id,
                        )
                        resolved = await self._resolve_merge_conflict(
                            job_id, task, repo, integration_path, str(exc)
                        )
                        if not resolved:
                            await asyncio.to_thread(repo.abort_merge, integration_path)
                            return False, "Integration merge conflict could not be resolved autonomously."

                    integration_gates = await self._run_gates(
                        job_id, task, integration_path, final=False, suffix="integration"
                    )
                    if not all(result.ok for result in integration_gates):
                        issue = "Post-merge integration gates failed:\n" + format_gates(
                            integration_gates
                        )
                        repaired = await self._repair_integration(
                            job_id, repo, integration_path, issue, task.id
                        )
                        if repaired:
                            integration_gates = await self._run_gates(
                                job_id,
                                task,
                                integration_path,
                                final=False,
                                suffix="integration-repair",
                            )
                        if not repaired or not all(result.ok for result in integration_gates):
                            await asyncio.to_thread(repo.reset_hard, integration_path, pre_merge)
                            self.db.event(
                                job_id,
                                "integration.rolled_back",
                                {"task": task.logical_id},
                                task_id=task.id,
                            )
                            return False, issue

                    self.db.event(
                        job_id,
                        "integration.completed",
                        {
                            "task": task.logical_id,
                            "head": await asyncio.to_thread(repo.head, integration_path),
                        },
                        task_id=task.id,
                    )
                    return True, ""
                except Exception:
                    await asyncio.to_thread(repo.abort_merge, integration_path)
                    if await asyncio.to_thread(repo.head, integration_path) != pre_merge:
                        await asyncio.to_thread(repo.reset_hard, integration_path, pre_merge)
                    raise

    async def _resolve_merge_conflict(
        self,
        job_id: str,
        task: TaskRecord,
        repo: GitRepo,
        integration_path: Path,
        original_error: str,
    ) -> bool:
        for cycle in range(1, 3):
            unresolved = await asyncio.to_thread(repo.unresolved_files, integration_path)
            if not unresolved:
                try:
                    await asyncio.to_thread(repo.finish_merge, integration_path, self.config.git)
                    return True
                except GitError:
                    pass
            issue = (
                f"A merge of task {task.logical_id} ({task.title}) is in progress.\n"
                f"Original error: {original_error}\nUnresolved files: {unresolved}\n"
                "Resolve the merge while preserving both the task intent and already integrated work."
            )
            agent_name = self.registry.choose_role(
                self.config.engine.integrator_agent, self.config.engine.worker_agents
            )
            log_path = (
                self.config.logs_dir
                / job_id
                / task.logical_id
                / f"merge-resolve-{cycle}-{agent_name}.log"
            )
            result = await self.registry.get(agent_name).run(
                INTEGRATION_REPAIR_TEMPLATE.format(
                    goal=self.db.get_job(job_id).goal,
                    issue=issue,
                ),
                cwd=integration_path,
                timeout_seconds=self.config.engine.agent_timeout_seconds,
                log_path=log_path,
                execution_profile=self.config.engine.execution_profile,
            )
            if result.ok:
                try:
                    await asyncio.to_thread(repo.finish_merge, integration_path, self.config.git)
                    if not await asyncio.to_thread(repo.unresolved_files, integration_path):
                        self.db.event(
                            job_id,
                            "integration.conflict_resolved",
                            {"agent": agent_name},
                            task_id=task.id,
                        )
                        return True
                except GitError:
                    pass
        return False

    async def _repair_integration(
        self,
        job_id: str,
        repo: GitRepo,
        integration_path: Path,
        issue: str,
        task_id: str | None = None,
    ) -> bool:
        candidates = [self.config.engine.integrator_agent, *self.config.engine.worker_agents]
        actual_seen: set[str] = set()
        for cycle, candidate in enumerate(candidates, start=1):
            if len(actual_seen) >= 3:
                break
            try:
                agent_name = self.registry.choose_role(
                    candidate, self.config.engine.worker_agents
                )
                if agent_name in actual_seen:
                    continue
                actual_seen.add(agent_name)
                log_path = (
                    self.config.logs_dir
                    / job_id
                    / "integration"
                    / f"repair-{cycle}-{agent_name}.log"
                )
                result = await self.registry.get(agent_name).run(
                    INTEGRATION_REPAIR_TEMPLATE.format(
                        goal=self.db.get_job(job_id).goal,
                        issue=issue,
                    ),
                    cwd=integration_path,
                    timeout_seconds=self.config.engine.agent_timeout_seconds,
                    log_path=log_path,
                    execution_profile=self.config.engine.execution_profile,
                )
                if not result.ok:
                    continue
                await asyncio.to_thread(
                    repo.commit_all,
                    integration_path,
                    "yolo: integration repair",
                    self.config.git,
                )
                self.db.event(
                    job_id,
                    "integration.repaired",
                    {"agent": agent_name},
                    task_id=task_id,
                )
                return True
            except Exception as exc:
                self.db.event(
                    job_id,
                    "integration.repair_error",
                    {"error": str(exc)},
                    task_id=task_id,
                )
        return False

    async def _run_gates(
        self,
        job_id: str,
        task: TaskRecord | None,
        cwd: Path,
        *,
        final: bool,
        suffix: str = "worker",
    ) -> list[GateResult]:
        configured = self.config.gates.final_commands if final else self.config.gates.commands
        commands = configured or detect_gate_commands(cwd)
        label = "final" if final else (task.logical_id if task else "job")
        log_path = self.config.logs_dir / job_id / label / f"gates-{suffix}.log"
        results = await self.gates.run(
            commands,
            cwd=cwd,
            timeout_seconds=self.config.engine.gate_timeout_seconds,
            log_path=log_path,
        )
        self.db.event(
            job_id,
            "gates.completed",
            {
                "scope": label,
                "commands": [result.command for result in results],
                "ok": all(result.ok for result in results),
            },
            task_id=task.id if task else None,
        )
        return list(results)

    @staticmethod
    def _review_failure(
        *,
        summary: str,
        entries: list[tuple[str, ReviewResult]],
    ) -> ReviewResult | None:
        findings: list[str] = []
        saw_fail = False
        for label, review in entries:
            if review.passed:
                continue
            saw_fail = saw_fail or review.verdict == "fail"
            for finding in review.findings or (review.summary,):
                if finding:
                    findings.append(f"{label}: {finding}")
        if not findings:
            return None
        bounded = findings[:64]
        if len(findings) > len(bounded):
            bounded.append(f"{len(findings) - len(bounded)} additional findings omitted")
        return ReviewResult("fail" if saw_fail else "retry", summary, tuple(bounded))

    async def _hierarchical_final_review(
        self,
        job_id: str,
        repo: GitRepo,
        integration_path: Path,
        gates: list[GateResult],
        reviewer_name: str,
        cycle: int,
    ) -> ReviewResult:
        job = self.db.get_job(job_id)
        manifest, chunks = await asyncio.to_thread(
            build_review_chunks,
            repo,
            integration_path,
            job.base_commit,
            max_files=self.config.engine.final_review_max_files,
            chunk_bytes=self.config.engine.final_review_chunk_bytes,
            chunk_files=self.config.engine.final_review_chunk_files,
            allow_binary=self.config.engine.final_review_allow_binary,
        )
        self.db.event(
            job_id,
            "final.review_manifest",
            {"cycle": cycle, "files": len(manifest), "chunks": len(chunks)},
        )

        chunk_reviews_by_file: dict[str, list[ReviewResult]] = {path: [] for path in manifest}
        chunk_results: list[tuple[str, ReviewResult]] = []
        for chunk in chunks:
            log_path = (
                self.config.logs_dir
                / job_id
                / "final"
                / f"review-{cycle}-chunk-{chunk.index}-{reviewer_name}.log"
            )
            review = await self.reviewer.review_final_chunk(
                goal=job.goal,
                diff=chunk.text,
                gates=gates,
                files=chunk.files,
                chunk_index=chunk.index,
                chunk_total=len(chunks),
                cwd=integration_path,
                agent_name=reviewer_name,
                timeout_seconds=self.config.engine.agent_timeout_seconds,
                log_path=log_path,
            )
            self.db.event(
                job_id,
                "final.review_chunk",
                {
                    "cycle": cycle,
                    "chunk": chunk.index,
                    "chunks": len(chunks),
                    "files": list(chunk.files),
                    "verdict": review.verdict,
                    "summary": review.summary,
                    "findings": list(review.findings),
                },
            )
            label = f"chunk {chunk.index}"
            chunk_results.append((label, review))
            for file_path in chunk.files:
                if file_path not in chunk_reviews_by_file:
                    raise YoloError(
                        f"final-review shard referenced a file outside the manifest: {file_path}"
                    )
                chunk_reviews_by_file[file_path].append(review)

        shard_failure = self._review_failure(
            summary="hierarchical shard audit found material defects",
            entries=chunk_results,
        )
        if shard_failure is not None:
            return shard_failure

        file_reviews: list[tuple[str, ReviewResult]] = []
        for file_path in manifest:
            reviews = chunk_reviews_by_file.get(file_path, [])
            if not reviews:
                return ReviewResult(
                    "fail",
                    "hierarchical review lost changed-file coverage",
                    (f"no reviewed shard represented {file_path}",),
                )
            if len(reviews) == 1:
                file_review = reviews[0]
                synthesized = False
            else:
                synthesis_log = (
                    self.config.logs_dir
                    / job_id
                    / "final"
                    / f"review-{cycle}-file-{len(file_reviews) + 1}-{reviewer_name}.log"
                )
                file_review = await self.reviewer.review_file_synthesis(
                    goal=job.goal,
                    gates=gates,
                    file_path=file_path,
                    chunk_reviews=reviews,
                    cwd=integration_path,
                    agent_name=reviewer_name,
                    timeout_seconds=self.config.engine.agent_timeout_seconds,
                    log_path=synthesis_log,
                )
                synthesized = True
            file_reviews.append((file_path, file_review))
            self.db.event(
                job_id,
                "final.review_file",
                {
                    "cycle": cycle,
                    "file": file_path,
                    "shards": len(reviews),
                    "synthesized": synthesized,
                    "verdict": file_review.verdict,
                    "summary": file_review.summary,
                    "findings": list(file_review.findings),
                },
            )

        file_failure = self._review_failure(
            summary="file-level semantic synthesis found material defects",
            entries=file_reviews,
        )
        if file_failure is not None:
            return file_failure

        synthesis_log = (
            self.config.logs_dir
            / job_id
            / "final"
            / f"review-{cycle}-synthesis-{reviewer_name}.log"
        )
        synthesis = await self.reviewer.review_final_synthesis(
            goal=job.goal,
            gates=gates,
            manifest=manifest,
            file_reviews=file_reviews,
            cwd=integration_path,
            agent_name=reviewer_name,
            timeout_seconds=self.config.engine.agent_timeout_seconds,
            log_path=synthesis_log,
        )
        self.db.event(
            job_id,
            "final.review_synthesis",
            {
                "cycle": cycle,
                "verdict": synthesis.verdict,
                "summary": synthesis.summary,
                "findings": list(synthesis.findings),
            },
        )
        return synthesis

    async def _finalize(self, job_id: str, repo: GitRepo, integration_path: Path) -> str:
        reviewer_name = self.registry.choose_role(
            self.config.engine.reviewer_agent,
            self.config.engine.worker_agents,
            execution_profile="review",
        )
        last_review = ReviewResult("retry", "not reviewed", ())

        for cycle in range(0, self.config.engine.max_final_cycles + 1):
            gates = await self._run_gates(
                job_id,
                None,
                integration_path,
                final=True,
                suffix=f"cycle-{cycle}",
            )
            if not all(result.ok for result in gates):
                issue = "Final repository gates failed:\n" + format_gates(gates)
                if cycle >= self.config.engine.max_final_cycles:
                    raise YoloError(issue)
                repaired = await self._repair_integration(
                    job_id, repo, integration_path, issue
                )
                if not repaired:
                    raise YoloError(
                        "final gates failed and no integration agent could repair them"
                    )
                continue

            last_review = await self._hierarchical_final_review(
                job_id,
                repo,
                integration_path,
                gates,
                reviewer_name,
                cycle,
            )
            self.db.event(
                job_id,
                "final.reviewed",
                {
                    "cycle": cycle,
                    "verdict": last_review.verdict,
                    "summary": last_review.summary,
                    "findings": list(last_review.findings),
                },
            )
            if last_review.passed:
                return last_review.summary or "All tasks, gates, and final review passed."
            if cycle >= self.config.engine.max_final_cycles:
                raise YoloError(
                    "final audit did not pass: "
                    + ("; ".join(last_review.findings) or last_review.summary)
                )
            issue = "Final audit findings:\n" + "\n".join(last_review.findings)
            if last_review.summary:
                issue += "\nSummary: " + last_review.summary
            repaired = await self._repair_integration(
                job_id, repo, integration_path, issue
            )
            if not repaired:
                raise YoloError(
                    "final audit requested repairs but no integration agent could complete them"
                )

        raise YoloError("finalization exhausted without a passing audit")

    async def _cleanup_completed(
        self,
        repo: GitRepo,
        job_id: str,
        integration_path: Path,
    ) -> None:
        async with self.coordinator.repo_lock(repo.root):
            tasks = self.db.list_tasks(job_id)
            for task in tasks:
                if task.worktree:
                    worktree = Path(task.worktree)
                    if worktree.exists() or worktree.is_symlink():
                        await asyncio.to_thread(repo.remove_worktree, worktree, force=True)
                if task.branch:
                    await asyncio.to_thread(repo.delete_branch, task.branch)
            if integration_path.exists() or integration_path.is_symlink():
                await asyncio.to_thread(repo.remove_worktree, integration_path, force=True)
            root = self.config.worktrees_dir / job_id
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)

    async def _cancel_active(self) -> None:
        active = [task for task in self._active if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
