"""One owned client for query, tool planners and tool-free answer; SDK retries disabled."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from time import monotonic

import httpx
from langchain_core.messages import AIMessage
from strands.models.openai import OpenAIModel

from app.core.config import Settings
from app.core.llm_calls import retryable_transport_error
from app.core.loop_budget import LoopBudget, find_loop_limit, model_stage
from app.core.strands_model import TransportUsage, openai_client


class LoopUsage(TransportUsage):
    def __init__(self, budget: LoopBudget):
        super().__init__()
        self.budget = budget
        self.stages: list[str] = []
        self.attempt_details: list[dict] = []

    async def on_request(self, request: httpx.Request) -> None:
        self.stages.append(self.budget.charge_model())
        self.attempt_details.append(
            {
                "stage": self.stages[-1],
                "started": monotonic(),
                "elapsed_seconds": None,
                "status_code": None,
            }
        )
        # Inner planner retries cannot renew an HTTP timeout beyond the shared deadline.
        remaining = max(0.001, self.budget.deadline() - asyncio.get_running_loop().time())
        request.extensions["timeout"] = dict.fromkeys(
            ("connect", "read", "write", "pool"),
            min(30, remaining),
        )
        await super().on_request(request)

    async def on_response(self, response: httpx.Response) -> None:
        await super().on_response(response)
        item = self.attempt_details[-1]
        item["elapsed_seconds"] = round(monotonic() - item["started"], 3)
        item["status_code"] = response.status_code

    def summary(self) -> dict:
        attempts = [
            {
                "stage": item["stage"],
                "status_code": item["status_code"],
                "elapsed_seconds": item["elapsed_seconds"],
                "usage": usage,
                "usage_status": "known"
                if usage is not None
                and all(
                    isinstance(usage.get(k), int) for k in ("prompt_tokens", "completion_tokens")
                )
                else "unknown",
            }
            for item, usage in zip(self.attempt_details, self.usage, strict=True)
        ]
        return {**super().summary(), "attempt_stages": list(self.stages), "attempts": attempts}


class QueryModel(OpenAIModel):
    def __init__(self, *, budget: LoopBudget, **kwargs):
        super().__init__(**kwargs)
        self.budget = budget
        self.tool_spec_filter: Callable[[list[dict]], list[dict]] | None = None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.tool_spec_filter is not None:
            tool_specs = self.tool_spec_filter(tool_specs or [])
        with model_stage("query"):
            while True:
                self.budget.check()
                emitted = False
                try:
                    async with asyncio.timeout_at(self.budget.query_deadline):
                        async for event in super().stream(
                            messages, tool_specs, system_prompt, **kwargs
                        ):
                            emitted = True
                            yield event
                    return
                except Exception as exc:
                    if limit := find_loop_limit(exc):
                        raise limit from exc
                    if emitted or not retryable_transport_error(exc):
                        raise
                    if self.budget.query_transport_retries >= 1:
                        raise
                    self.budget.query_transport_retries += 1


class TextModel:
    """Minimal existing planner interface; no bind_tools and no shared query messages."""

    def __init__(self, client, settings: Settings, budget: LoopBudget):
        self.client, self.settings, self.budget = client, settings, budget

    async def ainvoke(self, messages) -> AIMessage:
        self.budget.check()
        serialized = []
        for message in messages:
            if message.type not in {"system", "human", "ai"}:
                raise ValueError("text_model_disallows_tool_messages")
            serialized.append(
                {
                    "role": {"system": "system", "human": "user", "ai": "assistant"}[message.type],
                    "content": message.content,
                }
            )
        try:
            async with asyncio.timeout_at(self.budget.deadline()):
                response = await self.client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=serialized,
                    temperature=0,
                    stream=False,
                    **(
                        {"extra_body": {"thinking": {"type": "disabled"}}}
                        if self.settings.llm_provider == "deepseek"
                        else {}
                    ),
                )
        except Exception as exc:
            if limit := find_loop_limit(exc):
                raise limit from exc
            raise
        message = response.choices[0].message
        if message.tool_calls:
            raise ValueError("text_model_returned_tool_calls")
        return AIMessage(content=message.content or "")


@asynccontextmanager
async def loop_models(
    settings: Settings,
    budget: LoopBudget,
    *,
    transport=None,
) -> AsyncIterator[tuple[QueryModel, TextModel, LoopUsage]]:
    usage = LoopUsage(budget)
    async with openai_client(
        settings, usage, transport=transport,
    ) as client:
        query = QueryModel(
            budget=budget,
            client=client,
            model_id=settings.llm_model,
            stream=False,
            params={
                "temperature": 0,
                **(
                    {"extra_body": {"thinking": {"type": "disabled"}}}
                    if settings.llm_provider == "deepseek"
                    else {}
                ),
            },
        )
        yield query, TextModel(client, settings, budget), usage
