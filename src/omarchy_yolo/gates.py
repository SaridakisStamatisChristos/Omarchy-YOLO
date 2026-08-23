from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from .model import GateResult
from .process import _drain_process_pumps, _terminate_process_group
from .resources import ResourcePolicy
from .sandbox import Sandbox
from .util import (
    YoloError,
    ensure_private_dir,
    finish_before_cancel,
    open_private_binary,
    read_text_bounded,
)


CAPTURE_LIMIT_BYTES = 200_000
LOG_LIMIT_BYTES = 64_000_000
_LOG_TRUNCATION_MARKER = b"\n[omarchy-yolo: gate log truncated]\n"


def detect_gate_commands(repo: Path) -> tuple[str, ...]:
    commands: list[str] = ["git diff --check"]

    pyproject = repo / "pyproject.toml"
    if pyproject.exists() and (repo / "tests").exists():
        commands.append("python -m pytest -q")
    elif (repo / "pytest.ini").exists() or (repo / "tox.ini").exists():
        commands.append("python -m pytest -q")

    package_json = repo / "package.json"
    if package_json.exists():
        try:
            package = json.loads(read_text_bounded(package_json, max_bytes=2_000_000))
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            test_script = scripts.get("test", "") if isinstance(scripts, dict) else ""
            if test_script and "no test specified" not in str(test_script):
                manager = "pnpm" if (repo / "pnpm-lock.yaml").exists() else "npm"
                if (repo / "yarn.lock").exists():
                    manager = "yarn"
                commands.append(f"{manager} test")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, YoloError):
            pass

    if (repo / "Cargo.toml").exists():
        commands.append("cargo test --all-targets --all-features")
    if (repo / "go.mod").exists():
        commands.append("go test ./...")
    makefile = repo / "Makefile"
    if makefile.exists():
        try:
            content = read_text_bounded(makefile, max_bytes=1_000_000, errors="ignore")
            if re.search(r"(?m)^test\s*:", content) and "make test" not in commands:
                commands.append("make test")
        except (OSError, YoloError):
            pass
    if (repo / "build").is_dir() and (repo / "build/CTestTestfile.cmake").exists():
        commands.append("ctest --test-dir build --output-on-failure")

    return tuple(dict.fromkeys(commands))


class GateRunner:
    def __init__(
        self,
        *,
        capture_limit_bytes: int = CAPTURE_LIMIT_BYTES,
        log_limit_bytes: int = LOG_LIMIT_BYTES,
        sandbox: Sandbox | None = None,
        resource_policy: ResourcePolicy | None = None,
    ):
        self.capture_limit_bytes = max(1, capture_limit_bytes)
        self.log_limit_bytes = max(1, log_limit_bytes)
        self.sandbox = sandbox
        self.resource_policy = resource_policy or ResourcePolicy()

    async def run(
        self,
        commands: tuple[str, ...],
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
    ) -> list[GateResult]:
        results: list[GateResult] = []
        ensure_private_dir(log_path.parent)
        with open_private_binary(log_path):
            pass
        logged_bytes = 0
        log_truncated = False
        log_lock = asyncio.Lock()

        async def write_log(data: bytes) -> None:
            nonlocal logged_bytes, log_truncated
            async with log_lock:
                if logged_bytes < self.log_limit_bytes:
                    remaining = self.log_limit_bytes - logged_bytes
                    payload_budget = max(0, remaining - len(_LOG_TRUNCATION_MARKER))
                    truncated_now = len(data) > payload_budget
                    payload = data[:payload_budget]
                    with open_private_binary(log_path, append=True) as fh:
                        fh.write(payload)
                    logged_bytes += len(payload)
                    if truncated_now and not log_truncated:
                        marker = _LOG_TRUNCATION_MARKER[: self.log_limit_bytes - logged_bytes]
                        with open_private_binary(log_path, append=True) as fh:
                            fh.write(marker)
                        logged_bytes += len(marker)
                        log_truncated = True
                else:
                    log_truncated = True

        async def pump(
            stream: asyncio.StreamReader | None,
            target: bytearray,
            process: asyncio.subprocess.Process,
        ) -> None:
            if stream is None:
                return
            try:
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        break
                    target.extend(chunk)
                    if len(target) > self.capture_limit_bytes:
                        del target[: len(target) - self.capture_limit_bytes]
                    await write_log(chunk)
            except (OSError, YoloError):
                await self._terminate_group(process)
                raise

        for command in commands:
            started = time.monotonic()
            await write_log(f"\n$ {command}\n".encode())
            command_argv = ["bash", "-lc", command]
            if self.sandbox is not None:
                command_argv = self.sandbox.wrap(
                    command_argv,
                    cwd,
                    execution_profile="gate",
                )
                env = self.sandbox.environment("gate")
            else:
                env = os.environ.copy()
            command_argv = self.resource_policy.wrap(command_argv)
            env.setdefault("CI", "1")
            proc = await asyncio.create_subprocess_exec(
                *command_argv,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            stdout_b = bytearray()
            stderr_b = bytearray()
            stdout_task = asyncio.create_task(pump(proc.stdout, stdout_b, proc))
            stderr_task = asyncio.create_task(pump(proc.stderr, stderr_b, proc))
            timed_out = False
            pump_results: list[object] = []
            try:
                await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
            except TimeoutError:
                timed_out = True
                await self._terminate_group(proc)
            except asyncio.CancelledError:
                await self._terminate_group(proc)
                raise
            finally:
                pump_results, drain_timed_out = await finish_before_cancel(
                    _drain_process_pumps(proc, [stdout_task, stderr_task])
                )

            for pump_result in pump_results:
                if isinstance(pump_result, BaseException):
                    raise pump_result
            if drain_timed_out:
                raise YoloError("gate output pipes did not close after command exit")

            result = GateResult(
                command=command,
                returncode=proc.returncode if proc.returncode is not None else 124,
                stdout=stdout_b.decode(errors="replace"),
                stderr=stderr_b.decode(errors="replace"),
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
            )
            await write_log(
                f"\n[exit={result.returncode} timeout={result.timed_out}]\n".encode()
            )
            results.append(result)
            if not result.ok:
                break
        return results

    @staticmethod
    async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
        await _terminate_process_group(proc)
