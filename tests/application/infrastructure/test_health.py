from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.api.routers import health as module
from app.core.config import Settings


@pytest.mark.asyncio
async def test_disabled_optional_services_are_not_contacted(monkeypatch):
    monkeypatch.setattr(module.redis, "from_url", Mock(side_effect=AssertionError("Redis called")))
    monkeypatch.setattr(
        module.httpx, "AsyncClient", Mock(side_effect=AssertionError("HTTP called")),
    )
    result = await module.health(
        SimpleNamespace(execute=AsyncMock()),
        Settings(_env_file=None, redis_enabled=False, chroma_enabled=False),
    )
    assert result == {"status": "ok", "checks": {
        "postgres": "ok", "redis": "disabled", "chroma": "disabled",
    }}


@pytest.mark.asyncio
async def test_enabled_service_failure_is_reported(monkeypatch):
    monkeypatch.setattr(module.redis, "from_url", Mock(side_effect=OSError("unavailable")))
    result = await module.health(
        SimpleNamespace(execute=AsyncMock()),
        Settings(_env_file=None, redis_enabled=True, chroma_enabled=False),
    )
    assert result["status"] == "degraded"
    assert result["checks"]["redis"].startswith("error:")
