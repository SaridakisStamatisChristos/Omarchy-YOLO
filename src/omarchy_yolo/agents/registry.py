from __future__ import annotations

from collections.abc import Iterable

from ..config import Config
from ..util import YoloError
from .base import AgentLike, CommandAgent


class AgentRegistry:
    def __init__(self, config: Config, overrides: dict[str, AgentLike] | None = None):
        self.config = config
        self._agents: dict[str, AgentLike] = {
            name: CommandAgent(name, agent_cfg, config) for name, agent_cfg in config.agents.items()
        }
        if overrides:
            self._agents.update(overrides)

    def get(self, name: str) -> AgentLike:
        try:
            return self._agents[name]
        except KeyError as exc:
            raise YoloError(f"unknown agent '{name}'") from exc

    @staticmethod
    def _supports(agent: AgentLike, execution_profile: str | None) -> bool:
        if execution_profile is None:
            return True
        return agent.supports_profile(execution_profile)

    def available(self, execution_profile: str | None = None) -> list[str]:
        return [
            name
            for name, agent in self._agents.items()
            if agent.available() and self._supports(agent, execution_profile)
        ]

    def first_available(
        self,
        preferred: Iterable[str],
        *,
        execution_profile: str | None = None,
    ) -> str:
        for name in preferred:
            agent = self._agents.get(name)
            if (
                agent is not None
                and agent.available()
                and self._supports(agent, execution_profile)
            ):
                return name
        available = self.available(execution_profile)
        if not available:
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
    ) -> str:
        agent = self._agents.get(configured)
        if (
            agent is not None
            and agent.available()
            and self._supports(agent, execution_profile)
        ):
            return configured
        return self.first_available(fallbacks, execution_profile=execution_profile)
