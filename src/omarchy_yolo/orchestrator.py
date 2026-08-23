from __future__ import annotations

import asyncio
from pathlib import Path

from .agents import AgentRegistry
from .config import Config
from .db import Database
from .gates import GateRunner
from .git import GitError, GitRepo
from .model import AgentRole, JobRecord, JobState, PlannedTask, TaskState
from .omarchy import notify
from .orchestrator_integration import IntegrationMixin
from .orchestrator_task import TaskExecutionMixin
from .planner import Planner
from .reviewer import Reviewer
from .runtime import ResourceCoordinator
from .sandbox import Sandbox
from .util import YoloError, atomic_to_thread, ensure_private_dir, finish_before_cancel


class TaskExecutionError(YoloError):
    pass


class Orchestrator(TaskExecutionMixin, IntegrationMixin):
    def __init__(
        self,
        config: Config,
        db: Database,
        *,
        registry: AgentRegistry | None = None,
        gate_runner: GateRunner | None = None,
        coordinator: ResourceCoordinator | None = None,
    ):
        self.config = config
        self.db = db
        self.registry = registry or AgentRegistry(config)
        self.gates = gate_runner or GateRunner(sandbox=Sandbox(config.sandbox))
        self.planner = Planner(self.registry, max_tasks=config.engine.max_tasks)
        self.reviewer = Reviewer(self.registry)
        self.coordinator = coordinator or ResourceCoordinator(config.engine.max_global_workers)
        self._merge_lock = asyncio.Lock()
        self._active: set[asyncio.Task[bool]] = set()

    async def run_job(self, job_id: str) -> None:
        job = self.db.get_job(job_id)
        repo = GitRepo(
            Path(job.repo),
            command_timeout_seconds=self.config.git.command_timeout_seconds,
            allow_repository_commands=self.config.git.allow_repository_commands,
        )
        job_root = ensure_private_dir(self.config.worktrees_dir / job_id)
        integration_path = job_root / "integration"
        try:
            async with self.coordinator.repo_lock(repo.root):
                await atomic_to_thread(
                    repo.ensure_existing_branch_worktree,
                    integration_path,
                    job.integration_branch,
                    job.base_commit,
                )
                if await asyncio.to_thread(repo.merge_in_progress, integration_path):
                    await atomic_to_thread(repo.abort_merge, integration_path)
                    self.db.event(job_id, "integration.recovered_merge_abort")
                if not await asyncio.to_thread(repo.is_clean, integration_path):
                    head = await asyncio.to_thread(repo.head, integration_path)
                    await atomic_to_thread(repo.reset_hard, integration_path, head)
                    self.db.event(job_id, "integration.recovered_dirty_reset", {"head": head})
            self.db.update_job(job_id, integration_path=str(integration_path))
            self.db.event(
                job_id,
                "integration.ready",
                {"path": str(integration_path), "branch": job.integration_branch},
            )

            if not self.db.list_tasks(job_id):
                async with self.coordinator.worker_slot():
                    await self._plan_job(job_id, integration_path)

            self.db.update_job(job_id, state=JobState.RUNNING)
            self.db.event(job_id, "job.running")
            await self._execute_dag(job_id, repo, integration_path)

            if self.db.get_job(job_id).stop_requested:
                raise asyncio.CancelledError

            async with self.coordinator.worker_slot():
                final_summary = await self._finalize(job_id, repo, integration_path)
            job = self.db.get_job(job_id)
            # Final review is the acceptance boundary. If cancellation arrives while
            # optional source application is in flight, finish application/refusal and
            # persist completion together so an applied source can never be re-queued.
            await finish_before_cancel(
                self._complete_accepted_job(job, repo, final_summary)
            )

            if self.config.engine.cleanup_worktrees:
                try:
                    await self._cleanup_completed(repo, job_id, integration_path)
                except asyncio.CancelledError:
                    self.db.event(job_id, "job.cleanup_interrupted")
                    raise
                except Exception as exc:
                    self.db.event(
                        job_id,
                        "job.cleanup_failed",
                        {"error": str(exc)[-4_000:]},
                    )
        except asyncio.CancelledError:
            await self._cancel_active()
            current = self.db.get_job(job_id)
            if current.state == JobState.COMPLETED:
                raise
            if current.stop_requested:
                self.db.settle_inflight(
                    job_id,
                    task_state=TaskState.STOPPED,
                    attempt_state="cancelled",
                    summary="stopped by user request",
                )
                self.db.update_job(job_id, state=JobState.STOPPED)
                self.db.event(job_id, "job.stopped")
                notify("YOLO stopped", Path(current.repo).name)
            else:
                self.db.settle_inflight(
                    job_id,
                    task_state=TaskState.PENDING,
                    attempt_state="cancelled",
                    summary="orchestrator interrupted",
                )
                self.db.update_job(job_id, state=JobState.QUEUED)
                self.db.event(job_id, "job.interrupted")
            raise
        except Exception as exc:
            await self._cancel_active()
            self.db.settle_inflight(
                job_id,
                task_state=TaskState.FAILED,
                attempt_state="failed",
                summary=f"orchestrator failure: {exc}",
            )
            self.db.update_job(job_id, state=JobState.FAILED, error=str(exc))
            self.db.event(job_id, "job.failed", {"error": str(exc)})
            notify("YOLO failed", f"{Path(job.repo).name}: {str(exc)[:180]}")
            raise

    async def _complete_accepted_job(
        self,
        job: JobRecord,
        repo: GitRepo,
        final_summary: str,
    ) -> None:
        accepted = self.db.get_job(job.id)
        if accepted.auto_apply:
            try:
                async with self.coordinator.repo_lock(repo.root):
                    await atomic_to_thread(
                        repo.fast_forward_source,
                        accepted.integration_branch,
                        accepted.base_branch,
                        accepted.base_commit,
                    )
                self.db.event(accepted.id, "job.applied", {"branch": accepted.base_branch})
            except GitError as exc:
                self.db.event(accepted.id, "job.apply_skipped", {"reason": str(exc)})

        self.db.update_job(
            accepted.id,
            state=JobState.COMPLETED,
            stop_requested=False,
            final_summary=final_summary,
            error="",
        )
        self.db.event(
            accepted.id,
            "job.completed",
            {"summary": final_summary, "branch": accepted.integration_branch},
        )
        notify(
            "YOLO completed",
            f"{Path(accepted.repo).name}: {accepted.integration_branch}",
        )

    async def _plan_job(self, job_id: str, integration_path: Path) -> None:
        job = self.db.get_job(job_id)
        self.db.update_job(job_id, state=JobState.PLANNING)
        self.db.event(job_id, "planner.started")
        planner_name = self.registry.choose_role(
            self.config.engine.planner_agent,
            self.config.engine.worker_agents,
            role=AgentRole.PLANNER,
        )
        log_path = self.config.logs_dir / job_id / "planner.log"
        try:
            summary, tasks = await self.planner.plan(
                job.goal,
                cwd=integration_path,
                agent_name=planner_name,
                timeout_seconds=self.config.engine.agent_timeout_seconds,
                log_path=log_path,
            )
            self.db.event(
                job_id,
                "planner.completed",
                {"agent": planner_name, "summary": summary, "tasks": len(tasks)},
            )
        except Exception as exc:
            tasks = [
                PlannedTask(
                    logical_id="T1",
                    title="Implement the requested goal",
                    description=job.goal,
                    acceptance=("The requested goal is implemented and repository checks pass.",),
                    risk="high",
                )
            ]
            self.db.event(job_id, "planner.fallback", {"error": str(exc)})
        self.db.add_tasks(job_id, tasks)

    async def _execute_dag(self, job_id: str, repo: GitRepo, integration_path: Path) -> None:
        while True:
            job = self.db.get_job(job_id)
            if job.stop_requested:
                raise asyncio.CancelledError
            tasks = self.db.list_tasks(job_id)
            failures = [task for task in tasks if task.state == TaskState.FAILED]
            if failures:
                raise TaskExecutionError(
                    "task(s) failed: " + ", ".join(f"{t.logical_id}: {t.last_error}" for t in failures)
                )
            if tasks and all(task.state == TaskState.COMPLETED for task in tasks):
                return

            completed = {task.logical_id for task in tasks if task.state == TaskState.COMPLETED}
            active_ids = {
                task.get_name() for task in self._active if not task.done() and task.get_name()
            }
            ready = [
                task
                for task in tasks
                if task.state == TaskState.PENDING
                and set(task.dependencies).issubset(completed)
                and task.id not in active_ids
            ]

            capacity = self.config.engine.max_parallel - len([t for t in self._active if not t.done()])
            for task_record in ready[: max(0, capacity)]:
                handle = asyncio.create_task(
                    self._run_task(job_id, task_record.id, repo, integration_path),
                    name=task_record.id,
                )
                self._active.add(handle)
                handle.add_done_callback(self._active.discard)

            running = [task for task in self._active if not task.done()]
            if not running:
                remaining = [task for task in tasks if task.state != TaskState.COMPLETED]
                if remaining:
                    for task in remaining:
                        self.db.update_task(
                            task.id,
                            state=TaskState.BLOCKED,
                            last_error="dependency graph made no schedulable progress",
                        )
                    raise TaskExecutionError("task graph stalled with blocked tasks")
                return

            done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for handle in done:
                try:
                    ok = handle.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise TaskExecutionError(str(exc)) from exc
                if not ok:
                    task_id = handle.get_name()
                    failed = self.db.get_task(task_id)
                    raise TaskExecutionError(f"{failed.logical_id} failed: {failed.last_error}")
