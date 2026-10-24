"""Input -> production graph -> real tool planners/output; scripted models, no external I/O."""


import json

from app.schemas.chat import ChatRequest
from app.tools.catalog.compatibility import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog.planner import CatalogSearchPlanner
from app.tools.catalog.service import CatalogSearchService
from app.tools.contracts import BoundTool, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner, OrderToolService

from tests.support.runtime import goal, setup_runtime, task
from tests.support.tool_planning import Model, Repository


async def chain(
    monkeypatch, query, tool, responses, rows=(), *, arguments=None, memory=None,
    refs=None, bindings=None, session=None,
):
    planned = task(query, tool=tool)
    planned["arguments"].update(arguments or {})
    planned["bindings"] = bindings or []
    runtime, orchestrator, executor, context = setup_runtime(
        monkeypatch, query, [goal(query, refs=refs)],
        [{"goal_id": "goal_1", "tasks": [planned]}], memory=memory,
    )
    model, repository, calls = Model(*responses), Repository(rows), []
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    handlers = {
        "catalog_search": CatalogSearchService(repository, CatalogSearchPlanner(model)).search,
        "catalog_facets": CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets,
        "order_lookup": OrderToolService(session, planner=LLMOrderQueryPlanner(model)).lookup,
    }

    async def execute(contract, arguments, runtime_context):
        calls.append((arguments, runtime_context))
        return await execute_bound_tool(
            BoundTool(contract, handlers[contract.name]), arguments, runtime_context,
        )

    executor.execute = execute
    state = runtime._initial_state(ChatRequest(message=query), 7)
    stream = runtime._build_graph().astream(state, stream_mode="updates")
    try:
        async for update in stream:
            for name, values in update.items():
                state.update(values)
                if name == "execute_tool_wave":
                    assert len(orchestrator.calls["route_request"]) == 1
                    assert len(orchestrator.calls["submit_execution_plan"]) == 1
                    assert not orchestrator.calls["summarize_goals"] and context.completed == 0
                    assert calls[0][0]["query"] == query
                    assert calls[0][1]["user_id"] == 7
                    return state["tool_results"][-1]["execution"], model, repository, calls
    finally:
        await stream.aclose()
    raise AssertionError(f"No tool output: {state.get('planning_error')}")


def feedback(model):
    return json.loads(model.calls[1][-1].content)["validation_feedback"]["errors"][0]
