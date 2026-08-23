from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Protocol

from ..config import AgentConfig, Config
from ..model import AgentResult
from ..process import ProcessRunner
from ..sandbox import Sandbox
from ..util import YoloError


class AgentLike(Protocol):
    name: str

    def available(self) -> bool: ...

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
        execution_profile: str,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult: ...


class CommandAgent:
    def __init__(
        self,
        name: str,
        config: AgentConfig,
        global_config: Config,
        runner: ProcessRunner | None = None,
    ):
        self.name = name
        self.config = config
        self.global_config = global_config
        self.runner = runner or ProcessRunner()
        self.sandbox = Sandbox(global_config.sandbox)

    def available(self) -> bool:
        return bool(self.config.enabled and self.config.command and shutil.which(self.config.command[0]))

    def command_for_profile(self, execution_profile: str) -> list[str]:
        argv = list(self.config.command)
        if execution_profile == "review":
            if self.name == "codex":
                filtered: list[str] = []
                skip_next = False
                for index, token in enumerate(argv):
                    if skip_next:
                        skip_next = False
                        continue
                    if token == "--dangerously-bypass-approvals-and-sandbox":
                        continue
                    if token == "--sandbox" and index + 1 < len(argv):
                        skip_next = True
                        continue
                    filtered.append(token)
                filtered.extend(["--sandbox", "read-only"])
                return filtered
            if self.name == "claude":
                filtered = [token for token in argv if token != "--dangerously-skip-permissions"]
                if "--permission-mode" not in filtered:
                    filtered.extend(["--permission-mode", "plan"])
                return filtered
            return argv

        if execution_profile != "danger-yolo" or self.name != "codex":
            return argv
        # Codex's configured default is bounded workspace full-auto. `danger-yolo` explicitly
        # selects its no-sandbox/no-approval switch. Other agents' default Omarchy-style commands
        # already use their unattended modes.
        filtered = []
        skip_next = False
        for index, token in enumerate(argv):
            if skip_next:
                skip_next = False
                continue
            if token == "--full-auto":
                continue
            if token == "--sandbox" and index + 1 < len(argv):
                skip_next = True
                continue
            filtered.append(token)
        if "--dangerously-bypass-approvals-and-sandbox" not in filtered:
            filtered.append("--dangerously-bypass-approvals-and-sandbox")
        return filtered

    def environment_for_profile(self, execution_profile: str) -> dict[str, str]:
        if execution_profile != "review" or self.name != "opencode":
            return {}
        policy = {
            "permission": {
                "edit": "deny",
                "bash": "deny",
                "webfetch": "deny",
                "websearch": "deny",
                "external_directory": "deny",
            }
        }
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(policy, separators=(",", ":"))}

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path,
        timeout_seconds: int,
        log_path: Path,
        execution_profile: str,
        extra_env: dict[str, str] | None = None,
    ) -> AgentResult:
        if not self.available():
            raise YoloError(f"agent '{self.name}' is not installed or disabled")
        argv = self.sandbox.wrap(self.command_for_profile(execution_profile), cwd)
        env = self.sandbox.environment()
        env.update(self.environment_for_profile(execution_profile))
        if extra_env:
            env.update(extra_env)
        raw = await self.runner.run(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
            env=env,
            prompt_arg=prompt,
        )
        return AgentResult(
            returncode=raw.returncode,
            stdout=self._normalize_stdout(raw.stdout),
            stderr=raw.stderr,
            duration_seconds=raw.duration_seconds,
            command=raw.command,
        )

    def _normalize_stdout(self, stdout: str) -> str:
        if self.name == "claude":
            try:
                value = json.loads(stdout)
                if isinstance(value, dict) and isinstance(value.get("result"), str):
                    return value["result"]
            except json.JSONDecodeError:
                return stdout
        if self.name == "opencode":
            texts: list[str] = []
            for line in stdout.splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._collect_text(value, texts)
            if texts:
                return "\n".join(texts)
        return stdout

    @classmethod
    def _collect_text(cls, value: object, target: list[str]) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"text", "result", "content"} and isinstance(child, str):
                    target.append(child)
                else:
                    cls._collect_text(child, target)
        elif isinstance(value, list):
            for child in value:
                cls._collect_text(child, target)
