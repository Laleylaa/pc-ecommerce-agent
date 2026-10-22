"""A small deterministic P4 demonstration, shared by offline checks and local UI smoke."""

import json

from agent_loop.support.domain_tools import make_catalog
from agent_loop.support.loop_runtime import (
    calls,
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.core.loop_budget import model_stage
from app.tools.contracts import BoundTool, ToolCatalog
from langchain_core.messages import HumanMessage

MESSAGE = "P4 合成演示：查 SKU 9101 测试键盘当前价格，两次失败后补查"
ANSWER = "补查成功：P1 测试键盘 9101 当前价格为 299 元。"


def scenario(monkeypatch, *, enabled=True):
    catalog = make_catalog(monkeypatch)
    original = catalog.get_by_llm_name("catalog_search")
    attempts = []

    def factory(model):
        async def search(request):
            attempts.append(request.query)
            with model_stage("tool_planning"):
                await model.ainvoke([HumanMessage(content=json.dumps({
                    "query": request.query, "synthetic": True,
                }, ensure_ascii=False))])
            if len(attempts) <= 2:
                raise OSError(f"synthetic temporary failure {len(attempts)}")
            return await original.handler(request)

        return ToolCatalog([
            BoundTool(b.contract, search if b.contract.llm_name == "catalog_search" else b.handler)
            for b in catalog.list_bound_tools()
        ])

    def checked_answer(payload):
        facts = json.loads(payload["messages"][1]["content"])
        successes = [e for e in facts["evidence"] if e["result"]["ok"]]
        assert successes[-1]["result"]["output"]["products"][0]["price"] == "299"
        return final_answer(ANSWER)(payload)

    registration = register(goal(MESSAGE))["calls"]
    first = calls(*registration, ("catalog_search", {"query": "SKU 9101"}))
    invalid = calls(*registration, ("catalog_search", {"query": "SKU 9101", "limit": "bad"}))
    runtime, provider, repo = runtime_fixture(monkeypatch, [
        invalid, first, {"content": '{"plan":"synthetic attempt 1"}'},
        calls(("catalog_search", {"query": "SKU 9101"})),
        {"content": '{"plan":"synthetic attempt 2"}'},
        calls(("catalog_search", {"query": "SKU 9101", "limit": 1})),
        {"content": '{"plan":"synthetic supplemental lookup"}'},
        finish(), checked_answer,
    ], catalog_factory=factory)
    runtime.settings = runtime.settings.model_copy(update={"loop_trace_enabled": enabled})
    return runtime, provider, repo, attempts
