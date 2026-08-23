from __future__ import annotations

import os
import shutil
from pathlib import Path

from .config import SandboxConfig
from .util import YoloError


class Sandbox:
    def __init__(self, config: SandboxConfig):
        self.config = config

    def wrap(self, argv: list[str], cwd: Path) -> list[str]:
        backend = self.config.backend
        if backend == "native" or backend == "none":
            return argv
        if backend != "bwrap":
            raise YoloError(f"unknown sandbox backend: {backend}")
        executable = shutil.which("bwrap")
        if not executable:
            raise YoloError("sandbox backend 'bwrap' requested but bubblewrap is not installed")

        # Read-only host with a writable worktree and ephemeral temp directories. By default
        # HOME remains writable because several coding CLIs persist sessions/tokens there; set
        # read_only_home=true for a tighter boundary after authenticating/test-driving the CLIs.
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
            "--bind",
            str(cwd),
            str(cwd),
            "--chdir",
            str(cwd),
        ]
        home = Path.home()
        if not self.config.read_only_home and home.exists():
            wrapped.extend(["--bind", str(home), str(home)])
        if not self.config.network:
            wrapped.append("--unshare-net")
        wrapped.extend(["--", *argv])
        return wrapped

    def environment(self) -> dict[str, str]:
        env = os.environ.copy()
        env.setdefault("CI", "1")
        env["OMARCHY_YOLO"] = "1"
        return env
