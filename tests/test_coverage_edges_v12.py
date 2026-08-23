from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omarchy_yolo.db import Database
from omarchy_yolo.runtime import ResourceCoordinator
from omarchy_yolo.util import YoloError


def test_database_refuses_symlink_path(tmp_path: Path) -> None:
    target = tmp_path / "target.db"
    target.write_bytes(b"")
    link = tmp_path / "state.db"
    link.symlink_to(target)
    with pytest.raises(YoloError, match="refusing symlink"):
        Database(link)


def test_resource_coordinator_rejects_nonpositive_capacity() -> None:
    with pytest.raises(ValueError, match="positive"):
        ResourceCoordinator(0)


async def test_cancelled_waiter_releases_worker_waiting_accounting() -> None:
    coordinator = ResourceCoordinator(1)
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holder() -> None:
        async with coordinator.worker_slot():
            entered.set()
            await release.wait()

    first = asyncio.create_task(holder())
    await entered.wait()

    async def waiter() -> None:
        async with coordinator.worker_slot():
            raise AssertionError("cancelled waiter must never acquire the slot")

    second = asyncio.create_task(waiter())
    for _ in range(100):
        if coordinator.snapshot()["workers_waiting"] == 1:
            break
        await asyncio.sleep(0)
    assert coordinator.snapshot()["workers_waiting"] == 1
    second.cancel()
    with pytest.raises(asyncio.CancelledError):
        await second
    assert coordinator.snapshot()["workers_waiting"] == 0
    release.set()
    await first
