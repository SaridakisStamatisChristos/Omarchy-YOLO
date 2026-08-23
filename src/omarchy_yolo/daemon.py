from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import os
import signal
from dataclasses import asdict
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .agents import AgentRegistry
from .config import Config, load_config
from .db import MAX_EVENT_LIST_LIMIT, MAX_JOB_LIST_LIMIT, Database
from .git import GitRepo
from .model import JobRecord, JobState, TaskState
from .orchestrator import Orchestrator
from .rpc import MAX_RPC_MESSAGE_BYTES
from .runtime import ResourceCoordinator
from .util import YoloError, current_uid, ensure_private_dir, new_id, xdg_runtime_dir

MAX_GOAL_CHARS = 16_000
MAX_REPO_PATH_CHARS = 4_096


class YoloDaemon:
    def __init__(self, config: Config):
        if current_uid() == 0:
            raise YoloError("omarchy-yolo refuses to run as root")
        self.config = config
        ensure_private_dir(config.state_dir)
        ensure_private_dir(config.worktrees_dir)
        ensure_private_dir(config.logs_dir)
        self.db = Database(config.db_path)
        self.registry = AgentRegistry(config)
        self.coordinator = ResourceCoordinator(config.engine.max_global_workers)
        self.runners: dict[str, asyncio.Task[None]] = {}
        runtime = ensure_private_dir(xdg_runtime_dir())
        self.socket_path = runtime / "omarchy-yolo.sock"
        self.lock_path = runtime / "omarchy-yolo.lock"
        self._lock_handle: TextIO | None = None
        self.server: asyncio.AbstractServer | None = None
        self._stopping = asyncio.Event()

    async def start(self) -> None:
        self._acquire_singleton_lock()
        try:
            if self.socket_path.exists() or self.socket_path.is_symlink():
                self.socket_path.unlink()
            self.server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self.socket_path),
                limit=MAX_RPC_MESSAGE_BYTES + 1,
            )
        except Exception:
            self._release_singleton_lock()
            raise
        self.socket_path.chmod(0o600)
        for job_id in self.db.recover_incomplete():
            self._spawn(job_id)

    async def serve(self) -> None:
        await self.start()
        assert self.server is not None
        async with self.server:
            await self._stopping.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        active = [task for task in self.runners.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        self.runners.clear()
        self.db.close()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        self._release_singleton_lock()

    def _acquire_singleton_lock(self) -> None:
        if self._lock_handle is not None:
            return
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise YoloError("another omarchy-yolo daemon is already running") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        self._lock_handle = handle

    def _release_singleton_lock(self) -> None:
        handle = self._lock_handle
        if handle is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._lock_handle = None

    def request_shutdown(self) -> None:
        self._stopping.set()

    def _spawn(self, job_id: str) -> None:
        current = self.runners.get(job_id)
        if current is not None and not current.done():
            return
        orchestrator = Orchestrator(
            self.config,
            self.db,
            registry=self.registry,
            coordinator=self.coordinator,
        )
        task = asyncio.create_task(orchestrator.run_job(job_id), name=f"job:{job_id}")
        self.runners[job_id] = task

        def consume(done: asyncio.Task[None]) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.result()
            if self.runners.get(job_id) is done:
                self.runners.pop(job_id, None)

        task.add_done_callback(consume)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id: object | None = None
        try:
            try:
                raw = await asyncio.wait_for(reader.readline(), timeout=30)
            except ValueError as exc:
                raise YoloError("RPC request exceeds the maximum message size") from exc
            if not raw:
                return
            if len(raw) > MAX_RPC_MESSAGE_BYTES:
                raise YoloError("RPC request exceeds the maximum message size")
            request = json.loads(raw)
            if not isinstance(request, dict):
                raise YoloError("RPC request must be an object")
            request_id = request.get("id")
            method = request.get("method")
            if not isinstance(method, str) or not method:
                raise YoloError("RPC method must be a non-empty string")
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise YoloError("RPC params must be an object")
            try:
                result = await self.dispatch(method, params)
                response = {"id": request_id, "ok": True, "result": result}
            except Exception as exc:
                response = {"id": request_id, "ok": False, "error": str(exc)}
            encoded = (json.dumps(response, default=str, separators=(",", ":")) + "\n").encode()
            if len(encoded) > MAX_RPC_MESSAGE_BYTES:
                encoded = (
                    json.dumps(
                        {"id": request_id, "ok": False, "error": "RPC response exceeds the maximum message size"},
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode()
            writer.write(encoded)
            await writer.drain()
        except (json.JSONDecodeError, TimeoutError, YoloError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    @staticmethod
    def _bounded_int(value: object, *, default: int, minimum: int, maximum: int, name: str) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            raise YoloError(f"{name} must be an integer")
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = int(value)
            except ValueError as exc:
                raise YoloError(f"{name} must be an integer") from exc
        else:
            raise YoloError(f"{name} must be an integer")
        return min(maximum, max(minimum, parsed))

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"version": __version__, "pid": os.getpid()}
        if method == "runtime":
            return self.coordinator.snapshot()
        if method == "submit":
            return await self._submit(params)
        if method == "status":
            return self._status(params.get("job_id"))
        if method == "jobs":
            limit = self._bounded_int(
                params.get("limit"), default=50, minimum=1, maximum=MAX_JOB_LIST_LIMIT, name="limit"
            )
            return [self._job_list_item(job) for job in self.db.list_jobs(limit)]
        if method == "events":
            job_id = str(params["job_id"])
            after_id = self._bounded_int(
                params.get("after_id"), default=0, minimum=0, maximum=2**63 - 1, name="after_id"
            )
            limit = self._bounded_int(
                params.get("limit"), default=200, minimum=1, maximum=MAX_EVENT_LIST_LIMIT, name="limit"
            )
            return self._events_bounded(
                self.db.events(job_id, after_id=after_id, limit=limit)
            )
        if method == "stop":
            job_id = str(params["job_id"])
            job = self.db.get_job(job_id)
            if job.state == JobState.COMPLETED:
                raise YoloError("completed jobs are immutable")
            if job.state == JobState.STOPPED:
                return self._status(job_id)
            if job.state == JobState.FAILED:
                raise YoloError("failed job is already inactive; use resume to retry it")
            if job.state != JobState.STOPPING:
                self.db.request_stop(job_id)
            runner = self.runners.get(job_id)
            if runner is not None and not runner.done():
                runner.cancel()
            else:
                self.db.settle_inflight(
                    job_id,
                    task_state=TaskState.STOPPED,
                    attempt_state="cancelled",
                    summary="stopped by user request",
                )
                self.db.update_job(job_id, state=JobState.STOPPED)
                self.db.event(job_id, "job.stopped")
            return self._status(job_id)
        if method == "resume":
            job_id = str(params["job_id"])
            job = self.db.get_job(job_id)
            if job.state == JobState.COMPLETED:
                raise YoloError("completed jobs are immutable; submit a new goal instead")
            runner = self.runners.get(job_id)
            if job.state in {JobState.RUNNING, JobState.PLANNING}:
                raise YoloError("job is already active")
            if job.state == JobState.STOPPING and runner is not None and not runner.done():
                try:
                    await asyncio.wait_for(asyncio.shield(runner), timeout=30)
                except asyncio.CancelledError:
                    pass
                except TimeoutError as exc:
                    raise YoloError("job is still stopping; retry resume shortly") from exc
                except Exception:
                    pass
                job = self.db.get_job(job_id)
            elif runner is not None and not runner.done():
                raise YoloError("job still has an active runner")

            if job.state == JobState.QUEUED:
                self._spawn(job_id)
                return self._status(job_id)
            if job.state not in {JobState.FAILED, JobState.STOPPED, JobState.STOPPING}:
                raise YoloError(f"job in state '{job.state}' cannot be resumed")
            self.db.prepare_resume(job_id)
            self._spawn(job_id)
            return self._status(job_id)
        if method == "agents":
            return {
                "available": self.registry.available(),
                "review_available": self.registry.available("review"),
                "configured": {
                    name: {
                        "enabled": cfg.enabled,
                        "command": list(cfg.command),
                        "review_command": list(cfg.review_command),
                    }
                    for name, cfg in self.config.agents.items()
                },
            }
        raise YoloError(f"unknown RPC method: {method}")

    @staticmethod
    def _job_list_item(job: JobRecord) -> dict[str, Any]:
        data = asdict(job)
        data["goal"] = str(data.get("goal", ""))[:500]
        data["final_summary"] = str(data.get("final_summary", ""))[:1000]
        data["error"] = str(data.get("error", ""))[:1000]
        return data

    @staticmethod
    def _events_bounded(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        budget = MAX_RPC_MESSAGE_BYTES - 65_536
        used = 2
        for event in events:
            size = len(json.dumps(event, default=str, separators=(",", ":")).encode()) + 1
            if result and used + size > budget:
                break
            if size > budget:
                continue
            result.append(event)
            used += size
        return result

    async def _submit(self, params: dict[str, Any]) -> dict[str, Any]:
        goal = str(params.get("goal", "")).strip()
        if not goal:
            raise YoloError("goal cannot be empty")
        if len(goal) > MAX_GOAL_CHARS:
            raise YoloError(f"goal exceeds {MAX_GOAL_CHARS} characters")
        repo_value = str(params.get("repo", "."))
        if len(repo_value) > MAX_REPO_PATH_CHARS:
            raise YoloError("repository path is too long")
        repo = GitRepo.discover(Path(repo_value).expanduser())
        async with self.coordinator.repo_lock(repo.root):
            base_branch, base_commit = await asyncio.to_thread(
                repo.preflight, require_clean=self.config.git.require_clean_repo
            )
        branch_tag = new_id("run")
        integration_branch = f"{self.config.git.branch_prefix}/{branch_tag}/integration"
        raw_auto_apply = params.get("auto_apply")
        if raw_auto_apply is not None and not isinstance(raw_auto_apply, bool):
            raise YoloError("auto_apply must be true, false, or null")
        auto_apply = self.config.engine.auto_apply if raw_auto_apply is None else raw_auto_apply
        job = self.db.create_job(
            repo=str(repo.root),
            goal=goal,
            base_branch=base_branch,
            base_commit=base_commit,
            integration_branch=integration_branch,
            auto_apply=auto_apply,
        )
        self._spawn(job.id)
        return self._status(job.id)

    def _status(self, job_id: object | None) -> dict[str, Any]:
        job: JobRecord | None
        if job_id:
            job = self.db.get_job(str(job_id))
        else:
            job = self.db.latest_job()
        runtime = self.coordinator.snapshot()
        if job is None:
            return {"job": None, "tasks": [], "counts": {}, "runtime": runtime}
        tasks = self.db.list_tasks(job.id)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        task_items = [
            {
                "id": task.id,
                "job_id": task.job_id,
                "seq": task.seq,
                "logical_id": task.logical_id,
                "title": task.title[:240],
                "state": task.state,
                "preferred_agent": task.preferred_agent,
                "attempts": task.attempts,
                "branch": task.branch,
                "worktree": task.worktree,
                "last_error": task.last_error[-2_000:],
                "result_summary": task.result_summary[-2_000:],
            }
            for task in tasks
        ]
        last_events = self._events_bounded(
            self.db.events(
                job.id,
                after_id=max(0, self.db.last_event_id(job.id) - 12),
                limit=12,
            )
        )
        job_item = asdict(job)
        job_item["goal"] = job.goal[:MAX_GOAL_CHARS]
        job_item["final_summary"] = job.final_summary[-8_000:]
        job_item["error"] = job.error[-8_000:]
        return {
            "job": job_item,
            "tasks": task_items,
            "counts": counts,
            "last_events": last_events,
            "runtime": runtime,
        }


async def run_daemon(config: Config | None = None) -> None:
    daemon = YoloDaemon(config or load_config())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.request_shutdown)
    await daemon.serve()
