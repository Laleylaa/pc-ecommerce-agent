"""Scripted HTTP provider and transactional in-memory dependencies for P2 only."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
from agent_loop.support.domain_tools import make_catalog
from agent_loop.support.strands_provider import SETTINGS
from app.agent.loop.runtime import AgentLoopRuntime
from app.schemas.context import WorkingMemoryV2
from app.services.context import ConversationContextService


def goal(source, *, goal_id="g1", disposition="tool_planning", **kwargs):
    return {
        "id": goal_id,
        "source_text": source,
        "query": source,
        "disposition": disposition,
        **kwargs,
    }


def calls(*specs):
    """Build explicit SDK envelopes; a third tuple item selects a non-default goal."""
    result = []
    for spec in specs:
        name, arguments = spec[:2]
        if name not in {"register_goals", "query_complete", "activate_goal"} and not (
            "goal_id" in arguments and "arguments" in arguments
        ):
            arguments = {"goal_id": spec[2] if len(spec) == 3 else "g1", "arguments": arguments}
        result.append((name, arguments))
    return {"calls": result}


def register(*goals):
    return calls(
        ("register_goals", {"registration": {"goals": list(goals)}})
    )


def observations(payload):
    found = {}
    for message in payload["messages"]:
        if message["role"] != "tool":
            continue
        content = message["content"]
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and "evidence_id" in data:
            found[data["evidence_id"]] = data
    # Later attempts replace earlier evidence of the same logical operation.
    active = {item["invocation"]["operation_id"]: item for item in found.values()}
    return list(active.values())


def finish():
    def response(payload):
        # No copying of goal statuses or evidence IDs from observations.
        return calls(("query_complete", {"completion": {"reason": "已有材料交给回答阶段"}}))

    return response


def final_answer(text="已根据本次查询结果回答。"):
    def response(payload):
        assert "tools" not in payload
        facts = json.loads(payload["messages"][1]["content"])
        return {
            "content": json.dumps(
                {
                    "response": text,
                    "covered_goal_ids": [g["id"] for g in facts["goals"]],
                    "evidence_ids": [r["evidence_id"] for r in facts["evidence"]],
                },
                ensure_ascii=False,
            )
        }

    return response


class LoopProvider:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        assert payload["model"] == "probe-model"
        assert payload["stream"] is False
        index = len(self.requests) - 1
        if index >= len(self.steps):
            raise AssertionError("unexpected extra model request")
        step = self.steps[index]
        if callable(step):
            step = step(payload)
        if isinstance(step, httpx.Response):
            return step
        tool_calls = [
            {
                "id": step.get("tool_use_ids", {}).get(i, f"call_{index}_{i}"),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for i, (name, args) in enumerate(step.get("calls", []))
        ]
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "object": "chat.completion",
                "created": 0,
                "model": "probe",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": step.get("content", ""),
                            "tool_calls": tool_calls,
                        },
                        "finish_reason": "tool_calls" if tool_calls else "stop",
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            },
        )


class MemorySession:
    def __init__(self):
        self.commits = self.rollbacks = 0
        self.repository = None

    async def commit(self):
        self.commits += 1
        self.repository.saved = deepcopy(self.repository.data)

    async def rollback(self):
        self.rollbacks += 1
        self.repository.data = deepcopy(self.repository.saved)

    async def get(self, model, identity):
        return SimpleNamespace(id=identity)


class MemoryRepository:
    def __init__(self, session, *, memory=None, working_memory=None):
        self.session = session
        session.repository = self
        self.data = {
            "messages": [],
            "memory": memory or [],
            "working_memory": working_memory or WorkingMemoryV2().model_dump(mode="json"),
            "audit": None,
            "failures": [],
        }
        self.saved = deepcopy(self.data)
        self.fail_complete = False

    async def get_or_create(self, user_id, conversation_id):
        if user_id != 41 or conversation_id not in {None, 7}:
            raise ValueError("conversation_not_owned")
        return SimpleNamespace(id=7)

    async def list_recent_messages(self, conversation_id, limit):
        return list(self.data["messages"])

    async def count_complete_turns(self, conversation_id):
        return sum(m.role == "assistant" for m in self.data["messages"])

    async def get_working_memory(self, conversation_id):
        return deepcopy(self.data["working_memory"])

    async def list_memory(self, user_id):
        assert user_id == 41
        return [SimpleNamespace(**row) for row in self.data["memory"]]

    async def add_message(self, conversation_id, role, content, metadata=None):
        result = SimpleNamespace(
            id=len(self.data["messages"]) + 1, role=role, content=content, metadata=metadata
        )
        self.data["messages"].append(result)
        return result

    async def start_run(self, conversation_id):
        return SimpleNamespace(id=8)

    async def update_working_memory(self, conversation_id, memory):
        self.data["working_memory"] = deepcopy(memory)
        if self.fail_complete:
            raise RuntimeError("synthetic persistence failure")

    async def upsert_memory(self, user_id, key, value, confidence, **kwargs):
        assert user_id == 41
        item = {
            "id": 12,
            "key": key,
            "value": value,
            "confidence": confidence,
            **{k: kwargs[k] for k in ("scope", "fact_type", "value_json")},
        }
        self.data["memory"].append(item)
        return SimpleNamespace(memory=SimpleNamespace(**item), created=True)

    async def mark_memory_used(self, user_id, ids):
        assert user_id == 41

    async def finish_run(self, run, intent, audit):
        self.data["audit"] = deepcopy(audit)

    async def fail_run(self, run_id, intent, audit, error):
        self.data["failures"].append({"audit": audit, "error": error})


def runtime_fixture(
    monkeypatch,
    steps,
    *,
    working_memory=None,
    memory=None,
    catalog_factory=None,
    budget_factory=None,
):
    session = MemorySession()
    repository = MemoryRepository(session, memory=memory, working_memory=working_memory)
    context = ConversationContextService(session, SETTINGS, repository=repository)
    provider = LoopProvider(steps)
    catalog = make_catalog(monkeypatch)
    runtime = AgentLoopRuntime(
        session,
        SETTINGS,
        context_service=context,
        catalog_factory=catalog_factory or (lambda text: catalog),
        transport=httpx.MockTransport(provider),
        **({"budget_factory": budget_factory} if budget_factory else {}),
    )
    return runtime, provider, repository
