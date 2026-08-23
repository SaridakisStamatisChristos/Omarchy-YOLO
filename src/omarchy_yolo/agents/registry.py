from __future__ import annotations

from collections.abc import Iterable

from ..config import Config
from ..model import AgentRole
from ..process import ProcessRunner
from ..util import YoloError
from .base import AgentLike, CommandAgent


class AgentRegistry:
    def __init__(self, config: Config, overrides: dict[str, AgentLike] | None = None):
        self.config = config
        self._agents: dict[str, AgentLike] = {
            name: CommandAgent(
                name,
                agent_cfg,
                config,
                runner=ProcessRunner(resource_policy=config.resources),
            )
            for name, agent_cfg in config.agents.items()
        }
        if overrides:
            self._agents.update(overrides)

    def get(self, name: str) -> AgentLike:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise YoloError(f"unknown agent '{name}'") from exc

    @staticmethod
    def _supports(
        agent: AgentLike,
        execution_profile: str | None,
        role: AgentRole | None,
    ) -> bool:
        if role is not None:
            supports_role = getattr(agent, "supports_role", None)
            if callable(supports_role):
                role_supported = bool(supports_role(role))
            else:
                fallback_profile = (
                    "review"
                    if role in {AgentRole.PLANNER, AgentRole.REVIEWER}
                    else "yolo-worktree"
                )
                role_supported = agent.supports_profile(fallback_profile)
            return role_supported and (
                execution_profile is None or agent.supports_profile(execution_profile)
            )
        return execution_profile is None or agent.supports_profile(execution_profile)

    @staticmethod
    def _role(value: AgentRole | str | None) -> AgentRole | None:
        if value is None:
            return None
        try:
            return AgentRole(value)
        except ValueError as exc:
            raise YoloError(f"unknown agent role: {value}") from exc

    def available(
        self,
        execution_profile: str | None = None,
        *,
        role: AgentRole | str | None = None,
    ) -> list[str]:
        normalized_role = self._role(role)
        return [
            name
            for name, agent in self._agents.items()
            if agent.available() and self._supports(agent, execution_profile, normalized_role)
        ]

    def first_available(
        self,
        preferred: Iterable[str],
        *,
        execution_profile: str | None = None,
        role: AgentRole | str | None = None,
    ) -> str:
        normalized_role = self._role(role)
        for name in preferred:
            agent = self._agents.get(name)
            if (
                agent is not None
                and agent.available()
                and self._supports(agent, execution_profile, normalized_role)
            ):
                return name
        available = self.available(execution_profile, role=normalized_role)
        if not available:
            if normalized_role is not None:
                raise YoloError(
                    f"no configured agent satisfies the {normalized_role.value!r} capability contract"
                )
            if execution_profile == "review":
                raise YoloError("no configured agent has a declared read-only review capability")
            raise YoloError("no configured coding agent CLI is available")
        return available[0]

    def choose_role(
        self,
        configured: str,
        fallbacks: Iterable[str],
        *,
        execution_profile: str | None = None,
        role: AgentRole | str | None = None,
    ) -> str:
        normalized_role = self._role(role)
        agent = self._agents.get(configured)
        if (
            agent is not None
            and agent.available()
            and self._supports(agent, execution_profile, normalized_role)
        ):
            return configured
        return self.first_available(
            fallbacks,
            execution_profile=execution_profile,
            role=normalized_role,
        )

    def contracts(self) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for name, agent in self._agents.items():
            contract = getattr(agent, "contract", None)
            if callable(contract):
                result[name] = dict(contract())
                continue
            roles = [
                role.value
                for role in AgentRole
                if self._supports(agent, None, role)
            ]
            result[name] = {
                "roles": roles,
                "executable": "",
                "review_executable": "",
                "review_capability": "override",
            }
        return result
