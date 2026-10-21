"""Customer-facing Markdown survives the answer envelope and stored chat response."""

import pytest
from agent_loop.support.loop_runtime import (
    final_answer,
    finish,
    goal,
    register,
    runtime_fixture,
)
from app.schemas.chat import ChatRequest

pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]


@pytest.mark.parametrize("body", [
    "你好，我是小帕瓦，店铺的智能助手。",
    "我可以帮你：\n\n- **选外设**：查询商品信息。\n- **查订单**：查看你的订单。",
    "| 服务 | 内容 |\n| --- | --- |\n| 商品查询 | 价格与规格 |\n| 订单查询 | 订单与物流 |",
])
async def test_markdown_answer_is_preserved_without_control_envelope(monkeypatch, body):
    message = "请介绍一下你的服务范围"

    def respond(payload):
        assert "tools" not in payload
        assert "小帕瓦" in payload["messages"][0]["content"]
        assert "不把“PC外设商城”当作对外名称" in payload["messages"][0]["content"]
        return final_answer(body)(payload)

    runtime, provider, repo = runtime_fixture(monkeypatch, [
        register(goal(message, disposition="direct_response")),
        finish(),
        respond,
    ])
    response = await runtime.run(ChatRequest(message=message), 41)
    assert response.answer == body
    assert repo.data["messages"][-1].content == body
    assert "covered_goal_ids" not in response.answer
    assert response.execution_details.tool_calls == []
    assert len(provider.requests) == 3
    assert repo.data["audit"]["agent_loop"]["answer_mode"] == "model"
