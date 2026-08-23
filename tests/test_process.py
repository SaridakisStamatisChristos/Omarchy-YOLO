from __future__ import annotations

import errno
import time
from pathlib import Path
from typing import BinaryIO

import pytest

from omarchy_yolo.process import ProcessRunner
from omarchy_yolo.util import YoloError


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


async def test_wrapped_log_failure_terminates_child_and_is_propagated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omarchy_yolo import process as process_module

    real_open = process_module.open_private_binary

    def fail_appends(path: Path, *, append: bool = False) -> BinaryIO:
        if append:
            raise YoloError("cannot safely open private file: synthetic ENOSPC")
        return real_open(path, append=append)

    monkeypatch.setattr(process_module, "open_private_binary", fail_appends)
    started = time.monotonic()
    with pytest.raises(YoloError, match="synthetic ENOSPC"):
        await ProcessRunner().run(
            [
                "python",
                "-c",
                "import time; print('trigger', flush=True); time.sleep(30)",
            ],
            cwd=tmp_path,
            timeout_seconds=20,
            log_path=tmp_path / "midstream.log",
        )
    assert time.monotonic() - started < 10
