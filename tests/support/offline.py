"""Fail closed before collection; mocks remain allowed, external IO does not.

The guard is process-wide (not a fixture that disappears between tests). The runner
also installs it in child Python processes via sitecustomize. Shell tests must use
reviewed stub executables. This is a guard for reviewed code, not a security sandbox.
"""

import os
import socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def denied(*args, **kwargs):
    raise AssertionError("Offline checks forbid network and real database access")


# Do this before importing application code, during collection as well as execution.
socket.socket.connect = denied
socket.socket.connect_ex = denied
socket.create_connection = denied
socket.getaddrinfo = denied
socket.socket.sendto = denied

# Never consume developer credentials inherited from the parent shell.
for name in list(os.environ):
    if any(token in name.upper() for token in ("API_KEY", "TOKEN", "SECRET")) or name in {
        "DATABASE_URL", "LLM_BASE_URL", "AGENT_RUNTIME", "AGENT_TRACE_MODE",
        "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING",
    }:
        os.environ.pop(name, None)
os.environ.update(
    LLM_API_KEY="", TAVILY_API_KEY="", LANGSMITH_API_KEY="",
    AGENT_TRACE_MODE="off", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
    DATABASE_URL="postgresql+asyncpg://offline:offline@invalid/offline",
)

from pydantic_settings.sources import DotEnvSettingsSource  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine  # noqa: E402

# Retain Settings.model_config and synthetic temp dotenv tests, but never read the
# repository's real dotenv (including explicitly supplied paths).
_read_env_files = DotEnvSettingsSource._read_env_files


def synthetic_env_only(self):
    paths = self.env_file
    paths = [paths] if isinstance(paths, (str, os.PathLike)) else list(paths or [])
    if any(Path(p).resolve() == ROOT / ".env" for p in paths):
        return {}
    return _read_env_files(self)


DotEnvSettingsSource._read_env_files = synthetic_env_only
AsyncEngine.connect = denied
AsyncEngine.raw_connection = denied
Engine.connect = denied
Engine.raw_connection = denied

import asyncpg  # noqa: E402

asyncpg.connect = denied
asyncpg.create_pool = denied
