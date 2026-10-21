import json

from app.core.config import Settings
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage


def settings_at(path, mode="local"):
    return Settings(_env_file=None, llm_api_key="", agent_trace_mode=mode, agent_trace_dir=path)


def records(recording):
    content = (recording.directory / "trace.jsonl").read_text()
    return [json.loads(line) for line in content.splitlines()]


def events(recording, name):
    return [r["data"] for r in records(recording) if r.get("name") == name and r["kind"] == "event"]


def facet_model(query, responses):
    from tests.support.runtime import (
    ScriptedModel,
    goal,
    task,
)
    """Script only model outputs; use the real internal planner and native callbacks."""
    planner = ScriptedModel({
        "route_request": [AIMessage(content="", tool_calls=[{
            "id": "route", "name": "route_request", "args": {
                "rewritten_query": query, "goals": [goal(query)],
            },
        }])],
        "submit_execution_plan": [AIMessage(content="", tool_calls=[{
            "id": "plan", "name": "submit_execution_plan", "args": {
                "tasks": [{**task(query, tool="catalog_facets"), "goal_id": "goal_1",
                           "arguments": {"query": query, "limit": 1}}],
            },
        }])],
    })
    internal = FakeListChatModel(responses=responses)
    planner.ainvoke = internal.ainvoke
    return planner
