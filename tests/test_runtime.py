from __future__ import annotations

import asyncio
from pathlib import Path

from omarchy_yolo.runtime import ResourceCoordinator


async def test_worker_slots_are_global_and_bounded() -> None:
    coordinator = ResourceCoordinator(2)
    active = 0
    peak = 0
    release = asyncio.Event()

    async def worker() -> None:
        nonlocal active, peak
        async with coordinator.worker_slot():
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(5)]
    for _ in range(100):
        if coordinator.snapshot()["workers_active"] == 2:
            break
        await asyncio.sleep(0)
    snapshot = coordinator.snapshot()
    assert snapshot["workers_active"] == 2
    assert snapshot["workers_waiting"] == 3
    assert peak == 2
    release.set()
    await asyncio.gather(*tasks)
    assert coordinator.snapshot()["workers_active"] == 0


async def test_same_repository_lock_serializes_callers(tmp_path: Path) -> None:
    coordinator = ResourceCoordinator(4)
    repo = tmp_path / "repo"
    repo.mkdir()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with coordinator.repo_lock(repo):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-exit")

    async def second() -> None:
        await first_entered.wait()
        async with coordinator.repo_lock(repo):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert order == ["first-enter"]
    assert coordinator.snapshot()["repository_waiters"] == 1
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-exit", "second-enter"]


async def test_different_repository_locks_can_run_concurrently(tmp_path: Path) -> None:
    coordinator = ResourceCoordinator(4)
    repos = [tmp_path / "a", tmp_path / "b"]
    for repo in repos:
        repo.mkdir()
    entered: set[str] = set()
    both = asyncio.Event()

    async def hold(repo: Path) -> None:
        async with coordinator.repo_lock(repo):
            entered.add(repo.name)
            if len(entered) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), timeout=1)

    await asyncio.gather(*(hold(repo) for repo in repos))
    assert entered == {"a", "b"}
