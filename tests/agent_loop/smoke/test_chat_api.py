"""P3 real FastAPI routing into the Strands runtime, isolated persistence/provider."""

from types import SimpleNamespace

import httpx
import pytest
from app.api.dependencies import get_current_user
from app.api.routers import chat
from app.core.config import Settings, get_settings
from app.core.database import get_session
from fastapi import FastAPI
from pydantic import ValidationError

from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)

pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]


def api(settings, user=41):
    app = FastAPI()
    app.include_router(chat.router, prefix="/api")
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = lambda: None
    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=user)
    return app


@pytest.mark.parametrize("selected", ["legacy", "strands"])
async def test_api_selects_only_one_runtime_and_persists_once(monkeypatch, selected):
    message = "查 SKU 9101 的价格"
    runtime, provider, repo = runtime_fixture(
        monkeypatch,
        [
            register(goal(message)),
            calls(("catalog_search", {"query": "SKU 9101"})),
            finish(),
            final_answer(),
        ],
    )

    def forbidden(*args):
        raise AssertionError("unselected runtime must never run")

    monkeypatch.setattr(
        chat, "AgentLoopRuntime" if selected == "strands" else "AgentRuntime", lambda *args: runtime
    )
    monkeypatch.setattr(
        chat, "AgentRuntime" if selected == "strands" else "AgentLoopRuntime", forbidden
    )
    settings = runtime.settings.model_copy(update={"agent_runtime": selected})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api(settings)), base_url="http://test"
    ) as client:
        response = await client.post("/api/chat", json={"message": message})
    assert response.status_code == 200
    assert response.json()["products"][0]["sku_id"] == 9101
    assert repo.session.commits == 2 and len(repo.data["messages"]) == 2
    assert len(provider.requests) == 4


async def test_default_remains_legacy_and_invalid_switch_is_rejected():
    assert Settings(_env_file=None).agent_runtime == "legacy"
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_runtime="both")


async def test_missing_authentication_rejected_before_any_runtime(monkeypatch):
    def forbidden(*args):
        raise AssertionError("unauthenticated runtime call")

    monkeypatch.setattr(chat, "AgentRuntime", forbidden)
    monkeypatch.setattr(chat, "AgentLoopRuntime", forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=api(Settings(_env_file=None, agent_runtime="strands"), user=None),
        ),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/chat", json={"message": "查订单"})
    assert response.status_code == 401
