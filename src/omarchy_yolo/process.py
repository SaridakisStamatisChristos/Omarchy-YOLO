from __future__ import annotations

import asyncio
import os
import signal
import time
from pathlib import Path

from .model import AgentResult
from .util import ensure_private_dir, shell_join


class ProcessRunner:
    def __init__(self, *, capture_limit_bytes: int = 2_000_000):
        self.capture_limit_bytes = capture_limit_bytes

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

        async def pump(stream: asyncio.StreamReader | None, target: bytearray, prefix: bytes) -> None:
            if stream is None:
                return
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                if len(target) < self.capture_limit_bytes:
                    remaining = self.capture_limit_bytes - len(target)
                    target.extend(chunk[:remaining])
                async with lock:
                    with log_path.open("ab") as fh:
                        fh.write(prefix)
                        fh.write(chunk)
                        if not chunk.endswith(b"\n"):
                            fh.write(b"\n")

        with log_path.open("wb") as fh:
            safe_cmd = shell_join(argv + (["<PROMPT>"] if prompt_arg is not None else []))
            fh.write(f"$ {safe_cmd}\n".encode())

        out_task = asyncio.create_task(pump(proc.stdout, stdout_buf, b"[stdout] "))
        err_task = asyncio.create_task(pump(proc.stderr, stderr_buf, b"[stderr] "))
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout_seconds)
        except TimeoutError:
            timed_out = True
            await self._terminate_group(proc)
        except asyncio.CancelledError:
            await self._terminate_group(proc)
            raise
        finally:
            await asyncio.gather(out_task, err_task, return_exceptions=True)

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
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except TimeoutError:
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        await proc.wait()
