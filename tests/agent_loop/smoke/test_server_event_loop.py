"""Formal chat route must work under the server's event loop and clock origin."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from app.api.dependencies import get_current_user
from app.api.routers import chat
from app.core.config import get_settings
from app.core.database import get_session
from fastapi import FastAPI

from agent_loop.support.event_loops import LOOP_KINDS, new_loop
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)

pytestmark = pytest.mark.smoke


@pytest.mark.parametrize("loop_kind", LOOP_KINDS)
@pytest.mark.parametrize("kind", ["greeting", "product"])
def test_chat_route_with_server_clock(monkeypatch, loop_kind, kind):
    async def scenario():
        message = "你好，你能做什么" if kind == "greeting" else "查 SKU 9101 的价格"
        if kind == "greeting":
            steps = [
                calls(
                    *register(goal(message, disposition="direct_response"))["calls"],
                    ("query_complete", {"completion": {"reason": "No queries needed"}}),
                ),
                final_answer("可以查询商品和本人订单。"),
            ]
        else:
            steps = [
                calls(*register(goal(message))["calls"], ("catalog_search", {"query": message})),
                finish(),
                final_answer("此版本299元。"),
            ]
        runtime, provider, repo = runtime_fixture(monkeypatch, steps)
        prepare = runtime.context.prepare_turn

        async def yielding_prepare(*args):
            # A real database operation yields; an all-synchronous memory fake hid this bug.
            await asyncio.sleep(0.001)
            return await prepare(*args)

        monkeypatch.setattr(runtime.context, "prepare_turn", yielding_prepare)
        monkeypatch.setattr(chat, "AgentLoopRuntime", lambda *args: runtime)
        app = FastAPI()
        app.include_router(chat.router, prefix="/api")
        app.dependency_overrides[get_session] = lambda: None
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=41)
        app.dependency_overrides[get_settings] = lambda: runtime.settings.model_copy(
            update={"agent_runtime": "strands"},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            response = await client.post("/api/chat", json={"message": message})
        assert response.status_code == 200, response.text
        assert len(provider.requests) == (2 if kind == "greeting" else 3)
        assert "tools" not in provider.requests[-1]
        audit = repo.data["audit"]["agent_loop"]
        assert audit["stop_reason"] == "completed" and audit["answer_mode"] == "model"
        assert 0 <= audit["timings"]["prepare_seconds"] < 5
        assert repo.session.commits == 2 and len(repo.data["messages"]) == 2
        if kind == "product":
            assert response.json()["products"][0]["sku_id"] == 9101
            assert audit["tool_attempts"] == 1

    with asyncio.Runner(loop_factory=lambda: new_loop(loop_kind)) as runner:
        runner.run(scenario())
