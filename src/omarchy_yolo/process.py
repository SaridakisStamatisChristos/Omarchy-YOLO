from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from .model import AgentResult
from .util import (
    YoloError,
    ensure_private_dir,
    finish_before_cancel,
    open_private_binary,
    shell_join,
)

_LOG_TRUNCATION_MARKER = b"\n[omarchy-yolo: log output truncated]\n"
_PIPE_DRAIN_TIMEOUT_SECONDS = 2.0
_PROCESS_TERMINATION_GRACE_SECONDS = 5.0


async def _terminate_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the whole session even when its original leader already exited."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        if proc.returncode is None:
            await proc.wait()
        return

    deadline = time.monotonic() + _PROCESS_TERMINATION_GRACE_SECONDS
    if proc.returncode is None:
        try:
            await asyncio.wait_for(
                proc.wait(), timeout=_PROCESS_TERMINATION_GRACE_SECONDS
            )
        except TimeoutError:
            pass
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return
        await asyncio.sleep(0.01)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    if proc.returncode is None:
        await proc.wait()


async def _drain_process_pumps(
    proc: asyncio.subprocess.Process,
    tasks: list[asyncio.Task[None]],
) -> tuple[list[object], bool]:
    """Bound pipe draining and terminate descendants that outlive the leader."""
    _, pending = await asyncio.wait(tasks, timeout=_PIPE_DRAIN_TIMEOUT_SECONDS)
    drain_timed_out = bool(pending)
    if pending:
        await _terminate_process_group(proc)
        _, pending = await asyncio.wait(pending, timeout=_PIPE_DRAIN_TIMEOUT_SECONDS)
    for task in pending:
        task.cancel()
    return list(await asyncio.gather(*tasks, return_exceptions=True)), drain_timed_out


class ProcessRunner:
    def __init__(
        self,
        *,
        capture_limit_bytes: int = 2_000_000,
        log_limit_bytes: int = 64_000_000,
    ):
        self.capture_limit_bytes = max(1, capture_limit_bytes)
        self.log_limit_bytes = max(1, log_limit_bytes)

    async def run(
        self,
        argv: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
        env: dict[str, str] | None = None,
        prompt_arg: str | None = None,
    ) -> AgentResult:
        ensure_private_dir(log_path.parent)
        safe_cmd = shell_join(argv + (["<PROMPT>"] if prompt_arg is not None else []))
        header = f"$ {safe_cmd}\n".encode()
        # Prove the private log is writable before starting an untrusted child. If the
        # filesystem is full/read-only, fail without leaving a detached process behind.
        with open_private_binary(log_path) as fh:
            fh.write(header[: self.log_limit_bytes])
        logged_bytes = min(len(header), self.log_limit_bytes)

        command = [*argv]
        if prompt_arg is not None:
            command.append(prompt_arg)
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        stdout_buf = bytearray()
        stderr_buf = bytearray()
        lock = asyncio.Lock()
        log_truncated = False

        async def pump(stream: asyncio.StreamReader | None, target: bytearray, prefix: bytes) -> None:
            nonlocal logged_bytes, log_truncated
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
                    async with lock:
                        if logged_bytes < self.log_limit_bytes:
                            payload = prefix + chunk
                            if not chunk.endswith(b"\n"):
                                payload += b"\n"
                            remaining = self.log_limit_bytes - logged_bytes
                            payload_budget = max(
                                0, remaining - len(_LOG_TRUNCATION_MARKER)
                            )
                            truncated_now = len(payload) > payload_budget
                            payload = payload[:payload_budget]
                            with open_private_binary(log_path, append=True) as fh:
                                fh.write(payload)
                            logged_bytes += len(payload)
                            if truncated_now and not log_truncated:
                                marker = _LOG_TRUNCATION_MARKER[
                                    : self.log_limit_bytes - logged_bytes
                                ]
                                with open_private_binary(log_path, append=True) as fh:
                                    fh.write(marker)
                                logged_bytes += len(marker)
                                log_truncated = True
                        else:
                            log_truncated = True
            except (OSError, YoloError):
                await self._terminate_group(proc)
                raise

        out_task = asyncio.create_task(pump(proc.stdout, stdout_buf, b"[stdout] "))
        err_task = asyncio.create_task(pump(proc.stderr, stderr_buf, b"[stderr] "))
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
                _drain_process_pumps(proc, [out_task, err_task])
            )

        for pump_result in pump_results:
            if isinstance(pump_result, BaseException):
                raise pump_result
        if drain_timed_out:
            raise YoloError("agent output pipes did not close after command exit")

        rc = proc.returncode if proc.returncode is not None else 124
        if timed_out and rc == 0:
            rc = 124
        return AgentResult(
            returncode=rc,
            stdout=stdout_buf.decode("utf-8", errors="replace"),
            stderr=stderr_buf.decode("utf-8", errors="replace"),
            duration_seconds=time.monotonic() - started,
            command=tuple(argv),
        )

    @staticmethod
    async def _terminate_group(proc: asyncio.subprocess.Process) -> None:
        await _terminate_process_group(proc)
