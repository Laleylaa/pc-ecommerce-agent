"""Regressions for the earlier audit, with scripted models and fake audit I/O."""


from app.schemas.chat import ChatRequest

from tests.support.runtime import goal, setup_runtime, task


async def invoke(runtime, text):
    return await runtime._build_graph().ainvoke(
        runtime._initial_state(ChatRequest(message=text), 1)
    )


def single(monkeypatch, text="查询键盘"):
    return setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [{"goal_id": "goal_1", "tasks": [task(text)]}],
        [{"category": "键盘", "category_evidence": "键盘"}],
    )
