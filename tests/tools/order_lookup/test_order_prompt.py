import json

import pytest
from app.tools.prompts.templates import load_prompt


@pytest.mark.contract
@pytest.mark.asyncio
@pytest.mark.parametrize("malformed_first", [False, True])
async def test_order_planner_renders_plan_review_and_optional_repair(malformed_first):
    from types import SimpleNamespace

    from app.tools.orders import LLMOrderQueryPlanner
    from app.tools.schemas import OrderLookupInput

    plan = {"selection": "all", "operation": "list", "requested_fields": ["order_summary"],
            "filters": {}, "unresolved_requirements": []}
    responses = (["not JSON"] if malformed_first else []) + [
        json.dumps(plan), '{"verdict":"approve"}',
    ]

    class Model:
        def __init__(self):
            self.calls = []

        async def ainvoke(self, messages):
            self.calls.append(messages)
            return SimpleNamespace(content=responses.pop(0))

    model = Model()
    result = await LLMOrderQueryPlanner(model).plan(
        OrderLookupInput(user_id=7, query="查一下我的订单"),
    )
    assert result.operation == "list"
    assert result.requested_fields == ["order_summary"]
    assert len(model.calls) == (3 if malformed_first else 2)
    assert model.calls[0][0].content.startswith("# Role and task")
    assert model.calls[-1][0].content.startswith("# 角色与任务")
    if malformed_first:
        feedback = json.loads(model.calls[1][-1].content)
        assert feedback["repair_instruction"] == load_prompt("shared/repair_plan.md")
