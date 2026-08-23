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

from .agents import AgentRegistry
from .config import Config, load_config
from .db import Database
from .git import GitRepo
from .model import JobState
from .orchestrator import Orchestrator
from .util import YoloError, ensure_private_dir, new_id, xdg_runtime_dir


class YoloDaemon:
    def __init__(self, config: Config):
        if os.geteuid() == 0:
            raise YoloError("omarchy-yolo refuses to run as root")
        self.config = config
        ensure_private_dir(config.state_dir)
        ensure_private_dir(config.worktrees_dir)
        ensure_private_dir(config.logs_dir)
        self.db = Database(config.db_path)
        self.registry = AgentRegistry(config)
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
            if self.socket_path.exists():
                self.socket_path.unlink()
            self.server = await asyncio.start_unix_server(self._handle_client, path=str(self.socket_path))
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
        active = [task for task in self.runners.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
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
        orchestrator = Orchestrator(self.config, self.db, registry=self.registry)
        task = asyncio.create_task(orchestrator.run_job(job_id), name=f"job:{job_id}")
        self.runners[job_id] = task

        def consume(done: asyncio.Task[None]) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                done.result()

        task.add_done_callback(consume)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=30)
            if not raw:
                return
            request = json.loads(raw)
            request_id = request.get("id")
            method = str(request.get("method", ""))
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise YoloError("RPC params must be an object")
            try:
                result = await self.dispatch(method, params)
                response = {"id": request_id, "ok": True, "result": result}
            except Exception as exc:
                response = {"id": request_id, "ok": False, "error": str(exc)}
            writer.write((json.dumps(response, default=str, separators=(",", ":")) + "\n").encode())
            await writer.drain()
        except (json.JSONDecodeError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        if method == "ping":
            return {"version": "1.0.0", "pid": os.getpid()}
        if method == "submit":
            return await self._submit(params)
        if method == "status":
            return self._status(params.get("job_id"))
        if method == "jobs":
            return [asdict(job) for job in self.db.list_jobs(int(params.get("limit", 50)))]
        if method == "events":
            job_id = str(params["job_id"])
            return self.db.events(job_id, after_id=int(params.get("after_id", 0)), limit=int(params.get("limit", 200)))
        if method == "stop":
            job_id = str(params["job_id"])
            self.db.request_stop(job_id)
            runner = self.runners.get(job_id)
            if runner is not None and not runner.done():
                runner.cancel()
            return self._status(job_id)
        if method == "resume":
            job_id = str(params["job_id"])
            job = self.db.get_job(job_id)
            if job.state == JobState.COMPLETED:
                raise YoloError("completed jobs are immutable; submit a new goal instead")
            self.db.retry_failed_tasks(job_id)
            self.db.clear_stop(job_id)
            self._spawn(job_id)
            return self._status(job_id)
        if method == "agents":
            return {"available": self.registry.available(), "configured": {name: {"enabled": cfg.enabled, "command": list(cfg.command)} for name, cfg in self.config.agents.items()}}
        if method == "shutdown":
            self.request_shutdown()
            return {"ok": True}
        raise YoloError(f"unknown RPC method: {method}")

    async def _submit(self, params: dict[str, Any]) -> dict[str, Any]:
        goal = str(params.get("goal", "")).strip()
        if not goal:
            raise YoloError("goal cannot be empty")
        repo = GitRepo.discover(Path(str(params.get("repo", "."))).expanduser())
        base_branch, base_commit = await asyncio.to_thread(repo.preflight, require_clean=self.config.git.require_clean_repo)
        branch_tag = new_id("run")
        integration_branch = f"{self.config.git.branch_prefix}/{branch_tag}/integration"
        raw_auto_apply = params.get("auto_apply")
        auto_apply = self.config.engine.auto_apply if raw_auto_apply is None else bool(raw_auto_apply)
        job = self.db.create_job(repo=str(repo.root), goal=goal, base_branch=base_branch, base_commit=base_commit, integration_branch=integration_branch, auto_apply=auto_apply)
        self._spawn(job.id)
        return self._status(job.id)

    def _status(self, job_id: object | None) -> dict[str, Any]:
        if job_id:
            job = self.db.get_job(str(job_id))
        else:
            job = self.db.latest_job()
            if job is None:
                return {"job": None, "tasks": [], "counts": {}}
        tasks = self.db.list_tasks(job.id)
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.state.value] = counts.get(task.state.value, 0) + 1
        return {"job": asdict(job), "tasks": [asdict(task) for task in tasks], "counts": counts, "last_events": self.db.events(job.id, after_id=max(0, self.db.last_event_id(job.id) - 12), limit=12)}


async def run_daemon(config: Config | None = None) -> None:
    daemon = YoloDaemon(config or load_config())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, daemon.request_shutdown)
    await daemon.serve()
