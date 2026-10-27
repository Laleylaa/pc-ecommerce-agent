"""Construct the single supported Agent Loop runtime."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.loop.runtime import AgentLoopRuntime
from app.core.config import Settings


def create_runtime(session: AsyncSession, settings: Settings) -> AgentLoopRuntime:
    return AgentLoopRuntime(session, settings)
