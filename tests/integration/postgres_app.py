"""Explicit disposable PostgreSQL test app. Never reads the configured business DB URL."""

import os

import httpx
from agent_loop.support.loop_runtime import (
    LoopProvider,
    calls,
    final_answer,
    finish,
    goal,
    register,
)
from app.agent.loop.runtime import AgentLoopRuntime
from app.api.routers import after_sales, auth, catalog, chat, conversations, memories, orders
from app.core.config import Settings, get_settings
from app.core.database import get_session
from app.tools.contracts import BoundTool, ToolCatalog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from evals.support.goal_tool_fixtures import knowledge_service, web_search_service


def isolated_url():
    raw = os.environ.get("AGENT_LOOP_TEST_DATABASE_URL", "")
    if not raw:
        raise ValueError("Explicit isolated database required")
    url = make_url(raw)
    if (
        url.host not in {"127.0.0.1", "localhost"}
        or url.database != "agent_loop_p3"
        or url.port != 15439
    ):
        raise ValueError("Only the disposable loopback agent_loop_p3 database on 15439 is allowed")
    return raw


class ScriptedRuntime(AgentLoopRuntime):
    """Real domain SQL, context and persistence; deterministic model responses only."""

    def _catalog(self, model):
        catalog = super()._catalog(model)
        replacements = {
            "policy_search": knowledge_service().search_policy,
            "knowledge_search": web_search_service().search,
        }
        return ToolCatalog(
            [
                BoundTool(b.contract, replacements.get(b.contract.llm_name, b.handler))
                for b in catalog.list_bound_tools()
            ]
        )

    async def run(self, request, user_id):
        message = request.message
        if message == "查 SKU 9101 的价格":
            steps = [
                register(goal(message)),
                calls(("catalog_search", {"query": "SKU 9101"})),
                finish(),
                final_answer("测试键盘白色版当前价格299元。"),
            ]
        elif message == "查订单 8201":
            steps = [
                register(goal(message)),
                calls(("order_lookup", {"order_id": 8201})),
                finish(),
                final_answer("已查询当前账号可访问的订单。"),
            ]
        elif message == "说明退货配件要求":
            steps = [
                register(goal(message)),
                calls(("policy_search", {"query": "键盘退货配件"})),
                finish(),
                final_answer("按测试政策保留全部配件，具体需人工审核。"),
            ]
        else:
            steps = [
                register(goal(message, disposition="direct_response")),
                finish(),
                final_answer("已收到本次说明。"),
            ]
        self.transport = httpx.MockTransport(LoopProvider(steps))
        return await super().run(request, user_id)


def create_app():
    url = isolated_url()
    settings = Settings(
        _env_file=None,
        database_url=url,
        agent_runtime="strands",
        llm_api_key="fixture-key",
        llm_model="probe-model",
        auth_token_secret="isolated-p3-test-secret-not-for-real-users",
    )
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def sessions():
        async with factory() as session:
            yield session

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5174"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )
    for router in (auth, chat, conversations, memories, after_sales, catalog, orders):
        app.include_router(router.router, prefix="/api")
    app.dependency_overrides[get_session] = sessions
    app.dependency_overrides[get_settings] = lambda: settings
    # This module is used only by the explicit disposable validation process.
    chat.create_runtime = lambda session, settings: ScriptedRuntime(session, settings)
    app.state.engine = engine
    app.state.sessions = factory
    return app
