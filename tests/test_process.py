from __future__ import annotations

from pathlib import Path

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
