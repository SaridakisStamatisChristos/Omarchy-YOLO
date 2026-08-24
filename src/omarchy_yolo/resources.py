from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .util import YoloError


@dataclass(slots=True, frozen=True)
class ResourcePolicy:
    """Build an optional systemd/cgroups-v2 execution envelope for child work.

    Omarchy is systemd-based, so transient user scopes provide hard aggregate
    process-tree controls without adding a long-running helper or privileged daemon.
    The policy is deliberately opt-in: environments without a user systemd manager
    keep the existing bounded-process behavior rather than failing unexpectedly.
    """

    backend: str = "none"
    memory_max_mib: int = 0
    memory_high_mib: int = 0
    tasks_max: int = 0
    cpu_quota_percent: int = 0
    io_weight: int = 0

    @property
    def enabled(self) -> bool:
        return self.backend != "none"

    def _properties(self) -> list[str]:
        properties: list[str] = []
        if self.memory_high_mib:
            properties.append(f"MemoryHigh={self.memory_high_mib}M")
        if self.memory_max_mib:
            properties.append(f"MemoryMax={self.memory_max_mib}M")
        if self.tasks_max:
            properties.append(f"TasksMax={self.tasks_max}")
        if self.cpu_quota_percent:
            properties.append(f"CPUQuota={self.cpu_quota_percent}%")
        if self.io_weight:
            properties.append(f"IOWeight={self.io_weight}")
        return properties

    def _systemd_run(self) -> str:
        if self.backend != "systemd":
            raise YoloError(f"unknown resource-control backend: {self.backend}")
        systemd_run = shutil.which("systemd-run")
        if systemd_run is None:
            raise YoloError("resources.backend='systemd' requires systemd-run on PATH")
        return systemd_run

    def wrap(self, argv: list[str]) -> list[str]:
        if not self.enabled:
            return list(argv)
        systemd_run = self._systemd_run()
        wrapped = [
            systemd_run,
            "--user",
            "--scope",
            "--quiet",
            "--collect",
        ]
        for prop in self._properties():
            wrapped.extend(["--property", prop])
        wrapped.append("--")
        wrapped.extend(argv)
        return wrapped

    def probe(self, *, timeout_seconds: int = 10) -> tuple[bool, str]:
        """Live-check the selected enforcement backend for `yolo doctor`."""
        if not self.enabled:
            return True, "disabled"
        try:
            systemd_run = self._systemd_run()
        except YoloError as exc:
            return False, str(exc)
        true_executable = shutil.which("true") or "/usr/bin/true"
        try:
            result = subprocess.run(
                [
                    systemd_run,
                    "--user",
                    "--scope",
                    "--quiet",
                    "--collect",
                    "--",
                    true_executable,
                ],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(1, min(30, timeout_seconds)),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"systemd user-scope probe failed: {exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return False, f"systemd user-scope unavailable: {detail or f'rc={result.returncode}'}"
        return True, systemd_run

    def contract(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "memory_max_mib": self.memory_max_mib,
            "memory_high_mib": self.memory_high_mib,
            "tasks_max": self.tasks_max,
            "cpu_quota_percent": self.cpu_quota_percent,
            "io_weight": self.io_weight,
        }
