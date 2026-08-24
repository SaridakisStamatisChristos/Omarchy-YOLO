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
        if execution_profile not in {"review", "gate", "yolo-worktree", "danger-yolo"}:
            raise YoloError(f"unknown sandbox execution profile: {execution_profile}")
        backend = self.config.backend
        if backend == "native" or backend == "none":
            if self.config.hostile_repo_mode:
                raise YoloError("hostile repository mode requires the bwrap sandbox backend")
            return argv
        if backend != "bwrap":
            raise YoloError(f"unknown sandbox backend: {backend}")
        executable = shutil.which("bwrap")
        if not executable:
            raise YoloError("sandbox backend 'bwrap' requested but bubblewrap is not installed")

        review_mode = execution_profile == "review"
        gate_mode = execution_profile == "gate"
        cwd_bind = "--ro-bind" if review_mode else "--bind"
        home = Path.home().resolve()
        resolved_cwd = cwd.resolve()
        wrapped = [
            executable,
            "--die-with-parent",
            "--new-session",
            "--unshare-pid",
            "--proc",
            "/proc",
        ]
        if self.config.hostile_repo_mode:
            wrapped.extend(["--unshare-ipc", "--unshare-uts", "--dev", "/dev"])
        else:
            wrapped.extend(["--dev-bind", "/dev", "/dev"])
        wrapped.extend([
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/tmp",
        ])
        if self.config.hostile_repo_mode:
            masked_roots: list[Path] = []
            if home.exists():
                wrapped.extend(["--tmpfs", str(home)])
                masked_roots.append(home)
            runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
            if runtime.exists() and runtime != home:
                try:
                    runtime.relative_to(home)
                except ValueError:
                    wrapped.extend(["--tmpfs", str(runtime)])
                    masked_roots.append(runtime)
            created: set[Path] = set()
            for masked_root in masked_roots:
                try:
                    relative_cwd = resolved_cwd.relative_to(masked_root)
                except ValueError:
                    continue
                candidate = masked_root
                for part in relative_cwd.parts:
                    candidate /= part
                    if candidate not in created:
                        wrapped.extend(["--dir", str(candidate)])
                        created.add(candidate)
            if home.exists() and not gate_mode:
                for relative in self.config.writable_home_paths:
                    candidate = (home / relative).resolve()
                    try:
                        candidate.relative_to(home)
                    except ValueError as exc:
                        raise YoloError(
                            f"sandbox writable path escapes HOME: {relative}"
                        ) from exc
                    if not candidate.exists():
                        continue
                    if not candidate.is_dir():
                        raise YoloError(
                            "hostile repository HOME exceptions must be directories: "
                            f"{relative}"
                        )
                    parent = home
                    for part in candidate.relative_to(home).parts:
                        parent /= part
                        if parent not in created:
                            wrapped.extend(["--dir", str(parent)])
                            created.add(parent)
                    bind_mode = "--ro-bind" if review_mode else "--bind"
                    wrapped.extend([bind_mode, str(candidate), str(candidate)])
        wrapped.extend([
            cwd_bind,
            str(resolved_cwd),
            str(resolved_cwd),
            "--chdir",
            str(resolved_cwd),
        ])
        if (
            home.exists()
            and not self.config.hostile_repo_mode
            and not review_mode
            and not gate_mode
        ):
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
        if not self.config.network_for(execution_profile):
            wrapped.append("--unshare-net")
        wrapped.extend(["--", *argv])
        return wrapped

    def environment(self, execution_profile: str = "yolo-worktree") -> dict[str, str]:
        if execution_profile not in {"review", "gate", "yolo-worktree", "danger-yolo"}:
            raise YoloError(f"unknown sandbox execution profile: {execution_profile}")
        if self.config.hostile_repo_mode:
            safe_names = {
                "AR",
                "CC",
                "COLORTERM",
                "CXX",
                "HOME",
                "LANG",
                "LANGUAGE",
                "LD",
                "LOGNAME",
                "MAKEFLAGS",
                "NINJA_STATUS",
                "PATH",
                "PYTHONHOME",
                "PYTHONPATH",
                "RUST_BACKTRACE",
                "SHELL",
                "TERM",
                "USER",
                "VIRTUAL_ENV",
                *(
                    self.config.gate_env_allowlist
                    if execution_profile == "gate"
                    else self.config.agent_env_allowlist
                ),
            }
            env = {
                key: value
                for key, value in os.environ.items()
                if key in safe_names or key.startswith("LC_")
            }
        else:
            env = os.environ.copy()
        env.setdefault("CI", "1")
        env["OMARCHY_YOLO"] = "1"
        return env
