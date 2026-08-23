from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from .git import GitError, GitRepo, MergeConflict
from .model import ReviewResult, TaskRecord, TaskState
from .prompts import INTEGRATION_REPAIR_TEMPLATE
from .reviewer import format_gates
from .util import YoloError
from .gates import detect_gate_commands


class IntegrationMixin:
    async def _integrate_task(
        self,
        job_id: str,
        task: TaskRecord,
        repo: GitRepo,
        integration_path: Path,
    ) -> tuple[bool, str]:
        async with self._merge_lock:
            self.db.update_task(task.id, state=TaskState.INTEGRATING)
            pre_merge = await asyncio.to_thread(repo.head, integration_path)
            try:
                try:
                    await asyncio.to_thread(repo.merge, integration_path, task.branch)
                except MergeConflict as exc:
                    self.db.event(job_id, "integration.conflict", {"error": str(exc)}, task_id=task.id)
                    resolved = await self._resolve_merge_conflict(job_id, task, repo, integration_path, str(exc))
                    if not resolved:
                        await asyncio.to_thread(repo.abort_merge, integration_path)
                        return False, "Integration merge conflict could not be resolved autonomously."

                integration_gates = await self._run_gates(
                    job_id, task, integration_path, final=False, suffix="integration"
                )
                if not all(result.ok for result in integration_gates):
                    issue = "Post-merge integration gates failed:\n" + format_gates(integration_gates)
                    repaired = await self._repair_integration(job_id, repo, integration_path, issue, task.id)
                    if repaired:
                        integration_gates = await self._run_gates(
                            job_id, task, integration_path, final=False, suffix="integration-repair"
                        )
                    if not repaired or not all(result.ok for result in integration_gates):
                        await asyncio.to_thread(repo.reset_hard, integration_path, pre_merge)
                        self.db.event(job_id, "integration.rolled_back", {"task": task.logical_id}, task_id=task.id)
                        return False, issue

                self.db.event(
                    job_id,
                    "integration.completed",
                    {"task": task.logical_id, "head": await asyncio.to_thread(repo.head, integration_path)},
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
            log_path = self.config.logs_dir / job_id / task.logical_id / f"merge-resolve-{cycle}-{agent_name}.log"
            result = await self.registry.get(agent_name).run(
                INTEGRATION_REPAIR_TEMPLATE.format(goal=self.db.get_job(job_id).goal, issue=issue),
                cwd=integration_path,
                timeout_seconds=self.config.engine.agent_timeout_seconds,
                log_path=log_path,
                execution_profile=self.config.engine.execution_profile,
            )
            if result.ok:
                try:
                    await asyncio.to_thread(repo.finish_merge, integration_path, self.config.git)
                    if not await asyncio.to_thread(repo.unresolved_files, integration_path):
                        self.db.event(job_id, "integration.conflict_resolved", {"agent": agent_name}, task_id=task.id)
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
                agent_name = self.registry.choose_role(candidate, self.config.engine.worker_agents)
                if agent_name in actual_seen:
                    continue
                actual_seen.add(agent_name)
                log_path = self.config.logs_dir / job_id / "integration" / f"repair-{cycle}-{agent_name}.log"
                result = await self.registry.get(agent_name).run(
                    INTEGRATION_REPAIR_TEMPLATE.format(goal=self.db.get_job(job_id).goal, issue=issue),
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
                self.db.event(job_id, "integration.repaired", {"agent": agent_name}, task_id=task_id)
                return True
            except Exception as exc:
                self.db.event(job_id, "integration.repair_error", {"error": str(exc)}, task_id=task_id)
        return False

    async def _run_gates(
        self,
        job_id: str,
        task: TaskRecord | None,
        cwd: Path,
        *,
        final: bool,
        suffix: str = "worker",
    ) -> list[object]:
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
                "commands": [getattr(result, "command", "") for result in results],
                "ok": all(getattr(result, "ok", False) for result in results),
            },
            task_id=task.id if task else None,
        )
        return list(results)

    async def _finalize(self, job_id: str, repo: GitRepo, integration_path: Path) -> str:
        job = self.db.get_job(job_id)
        reviewer_name = self.registry.choose_role(
            self.config.engine.reviewer_agent, self.config.engine.worker_agents
        )
        last_review = ReviewResult("retry", "not reviewed", ())

        for cycle in range(0, self.config.engine.max_final_cycles + 1):
            gates = await self._run_gates(job_id, None, integration_path, final=True, suffix=f"cycle-{cycle}")
            if not all(getattr(result, "ok", False) for result in gates):
                issue = "Final repository gates failed:\n" + format_gates(gates)
                if cycle >= self.config.engine.max_final_cycles:
                    raise YoloError(issue)
                repaired = await self._repair_integration(job_id, repo, integration_path, issue)
                if not repaired:
                    raise YoloError("final gates failed and no integration agent could repair them")
                continue

            diff = await asyncio.to_thread(repo.diff, integration_path, job.base_commit, max_bytes=400_000)
            log_path = self.config.logs_dir / job_id / "final" / f"review-{cycle}-{reviewer_name}.log"
            try:
                last_review = await self.reviewer.review_final(
                    goal=job.goal,
                    diff=diff,
                    gates=gates,
                    cwd=integration_path,
                    agent_name=reviewer_name,
                    timeout_seconds=self.config.engine.agent_timeout_seconds,
                    log_path=log_path,
                )
            except Exception as exc:
                last_review = ReviewResult("retry", "final reviewer failed", (str(exc),))

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
            repaired = await self._repair_integration(job_id, repo, integration_path, issue)
            if not repaired:
                raise YoloError("final audit requested repairs but no integration agent could complete them")

        raise YoloError("finalization exhausted without a passing audit")

    async def _cleanup_completed(self, repo: GitRepo, job_id: str, integration_path: Path) -> None:
        tasks = self.db.list_tasks(job_id)
        for task in tasks:
            if task.worktree:
                await asyncio.to_thread(repo.remove_worktree, Path(task.worktree), force=True)
            if task.branch:
                await asyncio.to_thread(repo.delete_branch, task.branch)
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
