"""Scripted provider responses shared by SDK smoke and contract checks."""

import json

import httpx
from app.core.config import Settings

SETTINGS = Settings(_env_file=None, llm_api_key="synthetic", llm_model="probe-model")


def call(name, args):
    return {"id": name, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args) if isinstance(args, dict) else args,
    }}


FIRST = call("probe_lookup", {"key": "first"})
SECOND = call("probe_lookup", {"key": "second"})
FINISH = call("query_complete", {"completion": {
    "goal_id": "probe", "evidence_ids": ["e1", "e2"],
}})


def response(calls, *, usage=True):
    return httpx.Response(200, json={
        "id": "synthetic", "object": "chat.completion", "created": 0, "model": "probe",
        "choices": [{"index": 0, "message": {
            "role": "assistant", "content": "" if calls else "unsupported free text",
            "tool_calls": calls,
        }, "finish_reason": "tool_calls" if calls else "stop"}],
        **({"usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
           if usage else {}),
    })


class Provider:
    def __init__(self, batches, *, usage=True):
        self.batches = iter(batches)
        self.requests = []
        self.usage = usage

    def __call__(self, request):
        payload = json.loads(request.content)
        self.requests.append(payload)
        assert payload["model"] == "probe-model"
        assert payload["stream"] is False
        assert payload["thinking"] == {"type": "disabled"}
        return response(next(self.batches), usage=self.usage)
