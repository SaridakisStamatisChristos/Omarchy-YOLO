from __future__ import annotations

from pathlib import Path

from omarchy_yolo.gates import GateRunner, detect_gate_commands


def test_detect_python_gates(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    (tmp_path / "tests").mkdir()
    commands = detect_gate_commands(tmp_path)
    assert "python -m pytest -q" in commands


async def test_gate_runner_stops_on_failure(tmp_path: Path) -> None:
    results = await GateRunner().run(("printf ok", "exit 7", "printf never"), cwd=tmp_path, timeout_seconds=10, log_path=tmp_path / "gates.log")
    assert len(results) == 2
    assert results[0].ok
    assert not results[1].ok


async def test_gate_cancellation_terminates_process_group(tmp_path: Path) -> None:
    import asyncio
    import os
    pidfile = tmp_path / "gate.pid"
    runner = GateRunner()
    handle = asyncio.create_task(runner.run((f"echo $$ > {pidfile}; sleep 30",), cwd=tmp_path, timeout_seconds=60, log_path=tmp_path / "cancel.log"))
    for _ in range(100):
        if pidfile.exists(): break
        await asyncio.sleep(0.01)
    assert pidfile.exists()
    pid = int(pidfile.read_text().strip())
    handle.cancel()
    try:
        await handle
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("gate run did not propagate cancellation")
    for _ in range(100):
        try: os.kill(pid, 0)
        except ProcessLookupError: break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError(f"cancelled gate process {pid} is still alive")
