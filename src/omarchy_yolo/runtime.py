from __future__ import annotations

import asyncio
import time
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
        self._worker_acquisitions = 0
        self._worker_cancelled_waits = 0
        self._worker_wait_seconds = 0.0
        self._worker_busy_seconds = 0.0
        self._worker_peak_active = 0
        self._repo_acquisitions = 0
        self._repo_cancelled_waits = 0
        self._repo_wait_seconds = 0.0
        self._repo_busy_seconds = 0.0

    @asynccontextmanager
    async def worker_slot(self) -> AsyncIterator[None]:
        queued_at = time.monotonic()
        self._waiting_workers += 1
        acquired = False
        try:
            await self._worker_slots.acquire()
            acquired = True
            self._waiting_workers -= 1
            self._worker_wait_seconds += time.monotonic() - queued_at
            self._worker_acquisitions += 1
            self._active_workers += 1
            self._worker_peak_active = max(self._worker_peak_active, self._active_workers)
            active_at = time.monotonic()
            try:
                yield
            finally:
                self._worker_busy_seconds += time.monotonic() - active_at
                self._active_workers -= 1
                self._worker_slots.release()
        finally:
            if not acquired:
                self._waiting_workers -= 1
                self._worker_wait_seconds += time.monotonic() - queued_at
                self._worker_cancelled_waits += 1

    @staticmethod
    def _repo_key(repo_root: Path) -> str:
        return str(repo_root.expanduser().resolve())

    @asynccontextmanager
    async def repo_lock(self, repo_root: Path) -> AsyncIterator[None]:
        key = self._repo_key(repo_root)
        lock = self._repo_locks.setdefault(key, asyncio.Lock())
        self._repo_waiters[key] = self._repo_waiters.get(key, 0) + 1
        queued_at = time.monotonic()
        acquired = False
        try:
            await lock.acquire()
            acquired = True
            self._repo_waiters[key] -= 1
            self._repo_wait_seconds += time.monotonic() - queued_at
            self._repo_acquisitions += 1
            self._repo_active.add(key)
            active_at = time.monotonic()
            try:
                yield
            finally:
                self._repo_busy_seconds += time.monotonic() - active_at
                self._repo_active.discard(key)
                lock.release()
        finally:
            if not acquired:
                self._repo_waiters[key] -= 1
                self._repo_wait_seconds += time.monotonic() - queued_at
                self._repo_cancelled_waits += 1
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
            "worker_acquisitions_total": self._worker_acquisitions,
            "worker_cancelled_waits_total": self._worker_cancelled_waits,
            "worker_wait_seconds_total": round(self._worker_wait_seconds, 3),
            "worker_busy_seconds_total": round(self._worker_busy_seconds, 3),
            "worker_peak_active": self._worker_peak_active,
            "repository_acquisitions_total": self._repo_acquisitions,
            "repository_cancelled_waits_total": self._repo_cancelled_waits,
            "repository_wait_seconds_total": round(self._repo_wait_seconds, 3),
            "repository_busy_seconds_total": round(self._repo_busy_seconds, 3),
        }
