from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from pathlib import Path

from .model import GateResult
from .util import ensure_private_dir


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
            package = json.loads(package_json.read_text())
            scripts = package.get("scripts", {}) if isinstance(package, dict) else {}
            test_script = scripts.get("test", "") if isinstance(scripts, dict) else ""
            if test_script and "no test specified" not in str(test_script):
                manager = "pnpm" if (repo / "pnpm-lock.yaml").exists() else "npm"
                if (repo / "yarn.lock").exists():
                    manager = "yarn"
                commands.append(f"{manager} test")
        except (OSError, json.JSONDecodeError):
            pass

    if (repo / "Cargo.toml").exists():
        commands.append("cargo test --all-targets --all-features")
    if (repo / "go.mod").exists():
        commands.append("go test ./...")
    makefile = repo / "Makefile"
    if makefile.exists():
        try:
            content = makefile.read_text(errors="ignore")
            if re.search(r"(?m)^test\s*:", content) and "make test" not in commands:
                commands.append("make test")
        except OSError:
            pass
    if (repo / "build").is_dir() and (repo / "build/CTestTestfile.cmake").exists():
        commands.append("ctest --test-dir build --output-on-failure")

    return tuple(dict.fromkeys(commands))


class GateRunner:
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
        for command in commands:
            started = time.monotonic()
            env = os.environ.copy()
            env.setdefault("CI", "1")
            proc = await asyncio.create_subprocess_exec(
                "bash",
                "-lc",
                command,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
            timed_out = False
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout_seconds
                )
            except TimeoutError:
                timed_out = True
                stdout_b, stderr_b = await self._terminate_group(proc)
            except asyncio.CancelledError:
                await self._terminate_group(proc)
                raise
            result = GateResult(
                command=command,
                returncode=proc.returncode if proc.returncode is not None else 124,
                stdout=stdout_b.decode(errors="replace")[-200_000:],
                stderr=stderr_b.decode(errors="replace")[-200_000:],
                duration_seconds=time.monotonic() - started,
                timed_out=timed_out,
            )
            with log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n$ {command}\n")
                fh.write(result.stdout)
                if result.stderr:
                    fh.write("\n[stderr]\n" + result.stderr)
                fh.write(f"\n[exit={result.returncode} timeout={result.timed_out}]\n")
            results.append(result)
            if not result.ok:
                break
        return results

    @staticmethod
    async def _terminate_group(
        proc: asyncio.subprocess.Process,
    ) -> tuple[bytes, bytes]:
        if proc.returncode is not None:
            return await proc.communicate()
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return await proc.communicate()
        try:
            return await asyncio.wait_for(proc.communicate(), timeout=5)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return await proc.communicate()
