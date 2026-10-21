"""Select exactly one orchestration implementation without importing the other."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings

if TYPE_CHECKING:
    from app.agent.legacy.runtime import AgentRuntime
    from app.agent.loop.runtime import AgentLoopRuntime


def runtime_type(name: str) -> type[AgentLoopRuntime] | type[AgentRuntime]:
    if name == "strands":
        from app.agent.loop.runtime import AgentLoopRuntime

        return AgentLoopRuntime
    if name == "legacy":
        from app.agent.legacy.runtime import AgentRuntime

        return AgentRuntime
    raise ValueError(f"Unsupported agent runtime: {name}")


def create_runtime(session: AsyncSession, settings: Settings) -> AgentLoopRuntime | AgentRuntime:
    return runtime_type(settings.agent_runtime)(session, settings)
