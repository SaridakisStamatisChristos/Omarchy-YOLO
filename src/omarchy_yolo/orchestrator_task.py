from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from .git import GitRepo
from .model import GateResult, ReviewResult, TaskRecord, TaskState
from .prompts import worker_prompt
from .reviewer import format_gates
from .util import YoloError, slug

if TYPE_CHECKING:
    from .agents import AgentRegistry
    from .config import Config
    from .db import Database
    from .reviewer import Reviewer


class TaskExecutionMixin:
    if TYPE_CHECKING:
        config: Config
        db: Database
        registry: AgentRegistry
        reviewer: Reviewer
        _merge_lock: asyncio.Lock

        async def _run_gates(
            self,
            job_id: str,
            task: TaskRecord | None,
            cwd: Path,
            *,
            final: bool,
            suffix: str = "worker",
        ) -> list[GateResult]: ...

        async def _integrate_task(
            self,
            job_id: str,
            task: TaskRecord,
            repo: GitRepo,
            integration_path: Path,
        ) -> tuple[bool, str]: ...

    async def _run_task(
        self,
        job_id: str,
        task_id: str,
        repo: GitRepo,
        integration_path: Path,
    ) -> bool:
        task = self.db.get_task(task_id)
        job = self.db.get_job(job_id)
        retry_context = task.last_error

        stored_worktree = Path(task.worktree) if task.worktree else None
        branch_exists = False
        if task.branch:
            branch_exists = await asyncio.to_thread(repo.branch_exists, task.branch)
        if (
            stored_worktree is not None
            and task.branch
            and stored_worktree.exists()
            and (stored_worktree / ".git").exists()
        ):
            worktree = stored_worktree
            branch = task.branch
            base_commit = task.base_commit
        elif task.branch and branch_exists:
            # Recovery path: keep durable commits on the task branch even if the worktree directory
            # vanished or its registration went stale. Reattaching is safer than recreating the
            # branch from integration and silently discarding progress.
            branch = task.branch
            base_commit = task.base_commit or await asyncio.to_thread(repo.head, integration_path)
            worktree = stored_worktree or (
                self.config.worktrees_dir / job_id / f"task-{task.seq}-{slug(task.logical_id)}"
            )
            async with self._merge_lock:
                await asyncio.to_thread(
                    repo.ensure_existing_branch_worktree, worktree, branch, base_commit
                )
            self.db.update_task(task_id, worktree=str(worktree), base_commit=base_commit)
            self.db.event(
                job_id,
                "task.worktree_recovered",
                {"branch": branch, "path": str(worktree), "base": base_commit},
                task_id=task_id,
            )
        else:
            async with self._merge_lock:
                base_commit = await asyncio.to_thread(repo.head, integration_path)
                branch = f"{self.config.git.branch_prefix}/{job_id}/{slug(task.logical_id)}"
                worktree = self.config.worktrees_dir / job_id / f"task-{task.seq}-{slug(task.logical_id)}"
                await asyncio.to_thread(repo.ensure_worktree, worktree, branch, base_commit)
            self.db.update_task(
                task_id,
                branch=branch,
                worktree=str(worktree),
                base_commit=base_commit,
            )
            self.db.event(
                job_id,
                "task.worktree_created",
                {"branch": branch, "path": str(worktree), "base": base_commit},
                task_id=task_id,
            )

        # `max_attempts` is a budget per scheduling run, not a lifetime cap. This lets an
        # explicitly resumed failed task make fresh progress without deleting prior attempt history.
        # Attempt numbers remain globally monotonic per task, satisfying the DB uniqueness invariant.
        first_attempt = task.attempts + 1
        last_attempt = task.attempts + self.config.engine.max_attempts
        for attempt_number in range(first_attempt, last_attempt + 1):
            if self.db.get_job(job_id).stop_requested:
                raise asyncio.CancelledError
            task = self.db.get_task(task_id)
            agent_name = self._choose_worker(task, attempt_number)
            log_path = self.config.logs_dir / job_id / task.logical_id / f"attempt-{attempt_number}-{agent_name}.log"
            self.db.update_task(task_id, state=TaskState.RUNNING, attempts=attempt_number, last_error="")
            attempt_id = self.db.start_attempt(
                job_id=job_id,
                task_id=task_id,
                number=attempt_number,
                agent=agent_name,
                worktree=str(worktree),
                branch=branch,
                log_path=str(log_path),
            )
            self.db.event(
                job_id,
                "task.attempt_started",
                {"attempt": attempt_number, "agent": agent_name},
                task_id=task_id,
            )

            prompt = worker_prompt(job.goal, task, retry_context)
            try:
                result = await self.registry.get(agent_name).run(
                    prompt,
                    cwd=worktree,
                    timeout_seconds=self.config.engine.agent_timeout_seconds,
                    log_path=log_path,
                    execution_profile=self.config.engine.execution_profile,
                    extra_env={
                        "OMARCHY_YOLO_JOB_ID": job_id,
                        "OMARCHY_YOLO_TASK_ID": task_id,
                        "OMARCHY_YOLO_ATTEMPT": str(attempt_number),
                    },
                )
            except asyncio.CancelledError:
                self.db.finish_attempt(attempt_id, state="cancelled", returncode=None, summary="cancelled")
                raise
            except Exception as exc:
                retry_context = f"Agent launch failed: {exc}"
                self.db.finish_attempt(attempt_id, state="failed", returncode=None, summary=retry_context)
                self.db.update_task(task_id, state=TaskState.PENDING, last_error=retry_context)
                self.db.event(job_id, "task.agent_error", {"error": str(exc)}, task_id=task_id)
                continue

            if not result.ok:
                retry_context = (
                    f"Worker exited with {result.returncode}.\nSTDERR:\n{result.stderr[-5000:]}\n"
                    f"STDOUT:\n{result.stdout[-5000:]}"
                )
                self.db.finish_attempt(
                    attempt_id,
                    state="failed",
                    returncode=result.returncode,
                    summary=retry_context[-4000:],
                )
                self.db.update_task(task_id, state=TaskState.PENDING, last_error=retry_context[-8000:])
                self.db.event(
                    job_id,
                    "task.worker_failed",
                    {"attempt": attempt_number, "agent": agent_name, "returncode": result.returncode},
                    task_id=task_id,
                )
                continue

            await asyncio.to_thread(
                repo.commit_all,
                worktree,
                f"yolo({task.logical_id}): {task.title}",
                self.config.git,
            )
            gate_results = await self._run_gates(job_id, task, worktree, final=False)
            if not all(result.ok for result in gate_results):
                retry_context = "Repository gates failed:\n" + format_gates(gate_results)
                self.db.finish_attempt(
                    attempt_id,
                    state="failed",
                    returncode=1,
                    summary=retry_context[-4000:],
                )
                self.db.update_task(task_id, state=TaskState.PENDING, last_error=retry_context[-8000:])
                self.db.event(job_id, "task.gates_failed", {"attempt": attempt_number}, task_id=task_id)
                continue

            self.db.update_task(task_id, state=TaskState.REVIEWING)
            review = await self._review_task_resilient(
                job_id, self.db.get_task(task_id), job.goal, repo, worktree, gate_results, attempt_number
            )
            if not review.passed:
                retry_context = "Reviewer rejected the attempt:\n" + "\n".join(review.findings)
                if review.summary:
                    retry_context += "\nSummary: " + review.summary
                self.db.finish_attempt(attempt_id, state="failed", returncode=0, summary=retry_context[-4000:])
                self.db.update_task(task_id, state=TaskState.PENDING, last_error=retry_context[-8000:])
                self.db.event(
                    job_id,
                    "task.review_rejected",
                    {"verdict": review.verdict, "findings": list(review.findings)},
                    task_id=task_id,
                )
                continue

            integrated, integration_feedback = await self._integrate_task(
                job_id, self.db.get_task(task_id), repo, integration_path
            )
            if not integrated:
                retry_context = integration_feedback
                self.db.finish_attempt(attempt_id, state="failed", returncode=1, summary=retry_context[-4000:])
                self.db.update_task(task_id, state=TaskState.PENDING, last_error=retry_context[-8000:])
                continue

            self.db.finish_attempt(
                attempt_id,
                state="passed",
                returncode=0,
                summary=review.summary or "passed gates and review",
            )
            self.db.update_task(
                task_id,
                state=TaskState.COMPLETED,
                result_summary=review.summary,
                last_error="",
            )
            self.db.event(
                job_id,
                "task.completed",
                {"attempt": attempt_number, "agent": agent_name, "summary": review.summary},
                task_id=task_id,
            )
            if self.config.engine.cleanup_worktrees:
                await asyncio.to_thread(repo.remove_worktree, worktree, force=True)
                await asyncio.to_thread(repo.delete_branch, branch)
            return True

        task = self.db.get_task(task_id)
        error = task.last_error or (
            f"exhausted attempt budget {first_attempt}-{last_attempt} "
            f"({self.config.engine.max_attempts} attempts this run)"
        )
        self.db.update_task(task_id, state=TaskState.FAILED, last_error=error)
        self.db.event(job_id, "task.failed", {"error": error}, task_id=task_id)
        return False

    def _choose_worker(self, task: TaskRecord, attempt_number: int) -> str:
        if task.preferred_agent:
            try:
                if self.registry.get(task.preferred_agent).available():
                    return task.preferred_agent
            except YoloError:
                pass
        available = [
            name
            for name in self.config.engine.worker_agents
            if name in self.registry.available()
        ]
        if not available:
            return self.registry.first_available(self.config.engine.worker_agents)
        index = (task.seq + attempt_number - 2) % len(available)
        return available[index]

    async def _review_task_resilient(
        self,
        job_id: str,
        task: TaskRecord,
        goal: str,
        repo: GitRepo,
        worktree: Path,
        gates: list[GateResult],
        attempt_number: int,
    ) -> ReviewResult:
        diff = await asyncio.to_thread(repo.diff, worktree, task.base_commit)
        candidates = [self.config.engine.reviewer_agent, *self.config.engine.worker_agents]
        actual_tried: set[str] = set()
        errors: list[str] = []
        for candidate in candidates:
            try:
                agent_name = self.registry.choose_role(candidate, self.config.engine.worker_agents)
                if agent_name in actual_tried:
                    continue
                actual_tried.add(agent_name)
                log_path = self.config.logs_dir / job_id / task.logical_id / f"review-{attempt_number}-{agent_name}.log"
                review = await self.reviewer.review_task(
                    goal=goal,
                    task=task,
                    diff=diff,
                    gates=gates,
                    cwd=worktree,
                    agent_name=agent_name,
                    timeout_seconds=self.config.engine.agent_timeout_seconds,
                    log_path=log_path,
                )
                self.db.event(
                    job_id,
                    "task.reviewed",
                    {"agent": agent_name, "verdict": review.verdict, "findings": list(review.findings)},
                    task_id=task.id,
                )
                return review
            except Exception as exc:
                errors.append(f"{candidate}: {exc}")
        return ReviewResult("retry", "review infrastructure failed", tuple(errors))
