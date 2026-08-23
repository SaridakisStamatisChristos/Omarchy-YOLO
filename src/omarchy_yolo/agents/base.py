from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Protocol

from ..config import AgentConfig, Config
from ..model import AgentResult
from ..process import ProcessRunner
from ..sandbox import Sandbox
from ..util import YoloError, truncate_utf8

MAX_PROMPT_ARG_BYTES = 120_000
_BUILTIN_REVIEW_AGENTS = frozenset({"codex", "claude", "opencode"})


class AgentLike(Protocol):
    name: str

    def available(self) -> bool: ...

    def supports_profile(self, execution_profile: str) -> bool: ...

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

    def _trusted_builtin_review_identity(self) -> bool:
        if self.name not in _BUILTIN_REVIEW_AGENTS or not self.config.command:
            return False
        return Path(self.config.command[0]).name == self.name

    def supports_profile(self, execution_profile: str) -> bool:
        if execution_profile != "review":
            return True
        if self.config.review_command:
            return bool(shutil.which(self.config.review_command[0]))
        return self._trusted_builtin_review_identity()

    @staticmethod
    def _strip_option_with_value(argv: list[str], option: str) -> list[str]:
        result: list[str] = []
        skip_next = False
        prefix = option + "="
        for token in argv:
            if skip_next:
                skip_next = False
                continue
            if token == option:
                skip_next = True
                continue
            if token.startswith(prefix):
                continue
            result.append(token)
        return result

    def command_for_profile(self, execution_profile: str) -> list[str]:
        argv = list(self.config.command)
        if execution_profile == "review":
            if self.config.review_command:
                return list(self.config.review_command)
            if not self._trusted_builtin_review_identity():
                raise YoloError(
                    f"agent '{self.name}' has no declared read-only review capability; "
                    "configure agents.<name>.review_command"
                )
            if self.name == "codex":
                argv = [
                    token
                    for token in argv
                    if token not in {"--dangerously-bypass-approvals-and-sandbox", "--full-auto"}
                ]
                argv = self._strip_option_with_value(argv, "--sandbox")
                argv.extend(["--sandbox", "read-only"])
                return argv
            if self.name == "claude":
                argv = [token for token in argv if token != "--dangerously-skip-permissions"]
                argv = self._strip_option_with_value(argv, "--permission-mode")
                argv.extend(["--permission-mode", "plan"])
                return argv
            if self.name == "opencode":
                return argv
            raise YoloError(
                f"agent '{self.name}' has no declared read-only review capability; "
                "configure agents.<name>.review_command"
            )

        if execution_profile != "danger-yolo" or self.name != "codex":
            return argv
        argv = [
            token
            for token in argv
            if token not in {"--full-auto", "--dangerously-bypass-approvals-and-sandbox"}
        ]
        argv = self._strip_option_with_value(argv, "--sandbox")
        argv.append("--dangerously-bypass-approvals-and-sandbox")
        return argv

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
        if not self.supports_profile(execution_profile):
            raise YoloError(
                f"agent '{self.name}' does not support execution profile '{execution_profile}'"
            )
        argv = self.sandbox.wrap(
            self.command_for_profile(execution_profile),
            cwd,
            execution_profile=execution_profile,
        )
        env = self.sandbox.environment()
        if extra_env:
            env.update(extra_env)
        # Safety policy wins over caller-supplied environment additions.
        env.update(self.environment_for_profile(execution_profile))
        bounded_prompt = truncate_utf8(
            prompt,
            MAX_PROMPT_ARG_BYTES,
            marker="\n...[prompt truncated by orchestrator]...\n",
        )
        raw = await self.runner.run(
            argv,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            log_path=log_path,
            env=env,
            prompt_arg=bounded_prompt,
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
                if isinstance(value, dict):
                    result = value.get("result")
                    if isinstance(result, str):
                        return result
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
                return truncate_utf8("\n".join(texts), 262_144)
        return stdout

    @staticmethod
    def _collect_text(value: object, target: list[str]) -> None:
        stack: list[object] = [value]
        visited = 0
        while stack and visited < 10_000:
            current = stack.pop()
            visited += 1
            if isinstance(current, dict):
                for key, child in current.items():
                    if key in {"text", "result", "content"} and isinstance(child, str):
                        target.append(child)
                    elif isinstance(child, (dict, list)):
                        stack.append(child)
            elif isinstance(current, list):
                stack.extend(reversed(current))
