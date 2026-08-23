from __future__ import annotations

import errno
from pathlib import Path

import pytest

from omarchy_yolo.process import ProcessRunner


async def test_process_output_and_log_are_bounded(tmp_path: Path) -> None:
    runner = ProcessRunner(capture_limit_bytes=4096, log_limit_bytes=8192)
    result = await runner.run(
        ["python", "-c", "print('x' * 500000)"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=tmp_path / "process.log",
    )
    assert result.ok
    assert len(result.stdout.encode()) <= 4096
    assert (tmp_path / "process.log").stat().st_size < 9000


async def test_process_capture_keeps_final_answer(tmp_path: Path) -> None:
    runner = ProcessRunner(capture_limit_bytes=4096, log_limit_bytes=8192)
    result = await runner.run(
        ["python", "-c", "print('x' * 100000); print('{\\\"verdict\\\":\\\"pass\\\"}')"],
        cwd=tmp_path,
        timeout_seconds=10,
        log_path=tmp_path / "tail.log",
    )
    assert '"verdict":"pass"' in result.stdout
    assert len(result.stdout.encode()) <= 4096


async def test_log_open_failure_happens_before_child_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fail_open(*args: object, **kwargs: object) -> object:
        raise OSError(errno.ENOSPC, "No space left on device")

    async def should_not_spawn(*args: object, **kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("child process must not start when its durable log cannot be opened")

    monkeypatch.setattr("omarchy_yolo.process.open_private_binary", fail_open)
    monkeypatch.setattr("omarchy_yolo.process.asyncio.create_subprocess_exec", should_not_spawn)

    with pytest.raises(OSError, match="No space left on device"):
        await ProcessRunner().run(
            ["python", "-c", "print('should never run')"],
            cwd=tmp_path,
            timeout_seconds=10,
            log_path=tmp_path / "full.log",
        )
    assert not called
