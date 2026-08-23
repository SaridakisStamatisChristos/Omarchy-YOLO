from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import SandboxConfig
from .util import YoloError


class Sandbox:
    def __init__(self, config: SandboxConfig):
        self.config = config

    def wrap(self, argv: list[str], cwd: Path, *, execution_profile: str) -> list[str]:
        backend = self.config.backend
        if backend == "native" or backend == "none":
            return argv
        if backend != "bwrap":
            raise YoloError(f"unknown sandbox backend: {backend}")
        executable = shutil.which("bwrap")
        if not executable:
            raise YoloError("sandbox backend 'bwrap' requested but bubblewrap is not installed")

        # The host root is read-only. Workers may optionally receive a writable HOME or a
        # narrowly-scoped set of HOME subpaths. Planner/reviewer processes are always forced
        # to a read-only HOME and their repository cwd is mounted read-only as an OS-level
        # backstop to the CLI-specific review/plan permission profile.
        review_mode = execution_profile == "review"
        cwd_bind = "--ro-bind" if review_mode else "--bind"
        wrapped = [
            executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--proc",
            "/proc",
            "--dev-bind",
            "/dev",
            "/dev",
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
            cwd_bind,
            str(cwd),
            str(cwd),
            "--chdir",
            str(cwd),
        ]
        home = Path.home().resolve()
        if home.exists() and not review_mode:
            if not self.config.read_only_home:
                wrapped.extend(["--bind", str(home), str(home)])
            else:
                for relative in self.config.writable_home_paths:
                    candidate = (home / relative).resolve()
                    try:
                        candidate.relative_to(home)
                    except ValueError as exc:
                        raise YoloError(
                            f"sandbox writable path escapes HOME: {relative}"
                        ) from exc
                    if candidate.exists():
                        wrapped.extend(["--bind", str(candidate), str(candidate)])
        if not self.config.network:
            wrapped.append("--unshare-net")
        wrapped.extend(["--", *argv])
        return wrapped

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env["OMARCHY_YOLO"] = "1"
        return env
