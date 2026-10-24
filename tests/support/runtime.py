"""Small scenario suite: scripted LLMs, real graph and search tool, no external I/O."""


from collections import defaultdict
from types import SimpleNamespace

from app.agent.legacy.runtime import AgentRuntime
from app.core.config import Settings
from app.repositories.catalog_search import CatalogRecord
from app.schemas.catalog import ProductCard
from app.schemas.catalog_search import SearchPlan
from app.schemas.chat import ChatRequest
from app.schemas.context import PreparedTurn, WorkingMemoryV2
from app.services.context import _next_working_memory
from app.tools.catalog.compatibility import CatalogToolService
from app.tools.catalog.service import CatalogSearchService, within_scope
from app.tools.contracts import BoundTool, execute_bound_tool
from langchain_core.messages import AIMessage


def message(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": name}])


class ScriptedModel:
    def __init__(self, scripts):
        self.scripts = scripts
        self.calls = defaultdict(list)

    def bind_tools(self, tools, **kwargs):
        names = [t["function"]["name"] for t in tools]
        role = names[0]
        parent = self

        class Bound:
            async def ainvoke(self, messages):
                parent.calls[role].append(messages)
                outputs = parent.scripts.get(role, [])
                assert outputs, f"unexpected model call: {role}"
                return outputs.pop(0)

        return Bound()


class MemoryProducts:
    def __init__(self):
        self.rows = [
            CatalogRecord(
                product=ProductCard(
                    sku_id=i,
                    spu_id=i,
                    title=f"测试键盘{i}",
                    spu_title=f"测试键盘{i}",
                    brand="测试",
                    category="键盘",
                    price=price,
                    stock=10,
                    specs={
                        "color": "白色", "connection_type": "蓝牙",
                        "style": "75%" if i != 3 else "全尺寸",
                    },
                ),
                active=True,
            )
            for i, price in [(1, 200), (2, 250), (3, 500)]
        ]

    async def load(self, category, scope):
        return [
            r
            for r in self.rows
            if within_scope(r, scope) and (not category or r.product.category == category)
        ]


class FixedSearchPlans:
    def __init__(self, plans):
        self.plans = plans

    async def plan(self, request):
        assert self.plans, "unexpected search planner invocation"
        return SearchPlan.model_validate(self.plans.pop(0))


class IsolatedExecutor:
    supports_concurrent_calls = True

    def __init__(self, plans):
        self.calls = []
        self.search = CatalogSearchService(MemoryProducts(), FixedSearchPlans(plans))

    async def execute(self, contract, arguments, runtime_context):
        self.calls.append((contract.name, arguments, runtime_context))
        if contract.name == "catalog_search":
            handler = self.search.search
        else:
            assert contract.name == "catalog_facets"
            handler = CatalogToolService(None).facets
        return await execute_bound_tool(BoundTool(contract, handler), arguments, runtime_context)


class FakeContext:
    def __init__(self, memory=None):
        self.memory = WorkingMemoryV2.model_validate(memory or {})
        self.completed = 0

    async def prepare_turn(self, user_id, conversation_id, text):
        return PreparedTurn(
            user_id=user_id,
            conversation_id=1,
            user_message_id=1,
            run_id=1,
            message=text,
            history=[],
            memory=[],
            working_memory=self.memory,
        )

    async def complete_turn(self, prepared, state):
        self.completed += 1
        return SimpleNamespace(
            working_memory=_next_working_memory(self.memory, state), memory_changes=[]
        )


def goal(query, identity="goal_1", disposition="tool_planning", refs=None):
    return {
        "id": identity,
        "query": query,
        "source_text": query,
        "disposition": disposition,
        "context_refs": refs or [],
        "missing_information": ["办公时更在意的特点"] if disposition == "clarification" else [],
    }


def task(query, identity="task_1", tool="catalog_search", **extra):
    return {"id": identity, "tool_name": tool, "arguments": {"query": query}, **extra}


def setup_runtime(
    monkeypatch, text, goals, tasks, plans=None, memory=None, bad_route=None, bad_tasks=None
):
    route = message("route_request", {"rewritten_query": text, "goals": goals})
    execution = message(
        "submit_execution_plan",
        {
            "tasks": [{**t, "goal_id": g["goal_id"]} for g in tasks for t in g.get("tasks", [])],
            "blocked_goals": [
                {"goal_id": g["goal_id"], "blocked_reason": g["blocked_reason"]}
                for g in tasks
                if g.get("blocked_reason")
            ],
        },
    )
    scripts = {
        "route_request": ([bad_route] if bad_route else []) + [route],
        "submit_execution_plan": ([bad_tasks] if bad_tasks else []) + [execution],
        "summarize_goals": [message("summarize_goals", {
            "response": "我是商城客服助手。", "covered_goal_ids": [g["id"] for g in goals],
        })],
    }
    model = ScriptedModel(scripts)
    executor = IsolatedExecutor(plans or [])
    context = FakeContext(memory)
    runtime = AgentRuntime(
        None,
        Settings(llm_api_key=""),
        router_model=model,
        chat_model=model,
        tool_executor=executor,
        context_service=context,
    )

    class Audit:
        def __init__(self, session):
            assert session is None

        async def add_tool_call(self, *args):
            pass

    monkeypatch.setattr("app.agent.legacy.runtime.ConversationRepository", Audit)

    async def facet_records(self, category, scope):
        assert self.session is None
        return await executor.search.repository.load(category, scope)

    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load", facet_records,
    )
    return runtime, model, executor, context


async def through_tools(runtime, text, waves=1):
    state = runtime._initial_state(ChatRequest(message=text), 1)
    visited = []
    stream = runtime._build_graph().astream(state, stream_mode="updates")
    try:
        async for update in stream:
            for name, values in update.items():
                visited.append(name)
                state.update(values)
                if name == "update_subquery_ledger" and state["tool_wave_count"] == waves:
                    return state, visited
    finally:
        await stream.aclose()
    raise AssertionError(
        f"did not reach tool-return boundary: {visited}; {state.get('planning_error')}"
    )


def keyboard_plan(price=300):
    return {
        "category": "键盘",
        "category_evidence": "键盘",
        "conditions": [
            {"key": "price", "operator": "lte", "values": [str(price)], "evidence": str(price)},
            {"key": "color", "values": ["白色"], "evidence": "白色"},
            {"key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙"},
        ],
    }
