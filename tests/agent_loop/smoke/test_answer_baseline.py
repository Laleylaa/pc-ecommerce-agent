"""The P0 baseline must exercise final answering without calling persistence."""

import pytest
from app.core.config import Settings
from app.core.model_usage import collect_model_calls
from langchain_core.messages import AIMessage

from evals.agent.eval_agent_loop_baseline import MeteredModel
from evals.agent.eval_goal_tool_flow import evaluate


class DirectModel:
    def bind_tools(self, definitions, **kwargs):
        name = definitions[0]["function"]["name"]

        class Bound:
            async def ainvoke(self, messages):
                outputs = {
                    "route_request": {
                        "rewritten_query": "解释刷新率", "goals": [{
                            "id": "goal_1", "query": "解释刷新率", "source_text": "解释刷新率",
                            "disposition": "direct_response",
                        }],
                    },
                    "summarize_goals": {"response": "刷新率表示屏幕每秒更新的次数。",
                                        "covered_goal_ids": ["goal_1"]},
                }
                return AIMessage(content="", tool_calls=[
                    {"id": name, "name": name, "args": outputs[name]},
                ])
        return Bound()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_full_answer_baseline_uses_formal_summary_and_memory_without_database():
    meter = MeteredModel(DirectModel())
    with collect_model_calls():
        result = await evaluate(
            {"id": "direct", "message": "解释刷新率", "dispositions": ["direct_response"],
             "tools": []}, Settings(_env_file=None, llm_api_key=""),
            model=meter, full_answer=True,
        )
    assert result["answer"] == "刷新率表示屏幕每秒更新的次数。"
    assert result["visited"][-1] == "persist_turn"
    assert result["planning_failures"] == result["information_issues"] == []
    assert len(meter.attempts) == 2
    assert all(a["stage"] != "unknown" for a in meter.attempts)
    assert all(a["usage"] is None for a in meter.attempts)
