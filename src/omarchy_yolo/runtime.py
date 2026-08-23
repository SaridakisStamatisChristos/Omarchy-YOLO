from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any


class ResourceCoordinator:
    """Coordinate scarce worker capacity and repository topology across all jobs.

    A daemon owns exactly one coordinator and passes it to every orchestrator. The
    worker semaphore prevents multiple jobs from multiplying machine load, while
    per-repository locks serialize operations that mutate shared Git metadata.
    """

    def __init__(self, max_workers: int):
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = max_workers
        self._worker_slots = asyncio.Semaphore(max_workers)
        self._active_workers = 0
        self._waiting_workers = 0
        self._repo_locks: dict[str, asyncio.Lock] = {}
        self._repo_waiters: dict[str, int] = {}
        self._repo_active: set[str] = set()

    @asynccontextmanager
    async def worker_slot(self) -> AsyncIterator[None]:
        self._waiting_workers += 1
        acquired = False
        try:
            await self._worker_slots.acquire()
            acquired = True
            self._waiting_workers -= 1
            self._active_workers += 1
            try:
                yield
            finally:
                self._active_workers -= 1
                self._worker_slots.release()
        finally:
            if not acquired:
                self._waiting_workers -= 1

    @staticmethod
    def _repo_key(repo_root: Path) -> str:
        return str(repo_root.expanduser().resolve())

    @asynccontextmanager
    async def repo_lock(self, repo_root: Path) -> AsyncIterator[None]:
        key = self._repo_key(repo_root)
        lock = self._repo_locks.setdefault(key, asyncio.Lock())
        self._repo_waiters[key] = self._repo_waiters.get(key, 0) + 1
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            self._repo_waiters[key] -= 1
            self._repo_active.add(key)
            try:
                yield
            finally:
                self._repo_active.discard(key)
                lock.release()
        finally:
            if not acquired:
                self._repo_waiters[key] -= 1
            if self._repo_waiters.get(key) == 0:
                self._repo_waiters.pop(key, None)
                if not lock.locked():
                    self._repo_locks.pop(key, None)

    def snapshot(self) -> dict[str, Any]:
        repo_waiters = sum(self._repo_waiters.values())
        return {
            "worker_capacity": self.max_workers,
            "workers_active": self._active_workers,
            "workers_waiting": self._waiting_workers,
            "repositories_active": len(self._repo_active),
            "repository_waiters": repo_waiters,
        }
