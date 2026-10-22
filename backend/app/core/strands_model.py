"""Request-owned transport shared by probes and the separate Agent loop runtime."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from openai import AsyncOpenAI
from strands.models.openai import OpenAIModel

from app.core.config import Settings
from app.core.llm import resolve_llm_base_url


@dataclass
class TransportUsage:
    """Count actual HTTP attempts, including failed requests; no request bodies/keys."""

    attempts: int = 0
    usage: list[dict | None] = field(default_factory=list)

    async def on_request(self, request: httpx.Request) -> None:
        self.attempts += 1
        self.usage.append(None)

    async def on_response(self, response: httpx.Response) -> None:
        # P0 deliberately uses non-streaming, matching the existing provider configuration.
        await response.aread()
        if response.is_success:
            try:
                usage = response.json().get("usage")
            except (ValueError, AttributeError):
                return
            if isinstance(usage, dict):
                self.usage[-1] = {
                    key: usage.get(key) for key in ("prompt_tokens", "completion_tokens")
                }

    def summary(self) -> dict:
        known = [u for u in self.usage if u is not None and all(
            isinstance(u.get(k), int) for k in ("prompt_tokens", "completion_tokens")
        )]
        return {
            "http_attempts": self.attempts,
            "usage_missing_attempts": self.attempts - len(known),
            "known_input_tokens": sum(u["prompt_tokens"] for u in known),
            "known_output_tokens": sum(u["completion_tokens"] for u in known),
        }


@asynccontextmanager
async def openai_client(
    settings: Settings,
    usage: TransportUsage,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    client_class: type[httpx.AsyncClient] = httpx.AsyncClient,
) -> AsyncIterator[AsyncOpenAI]:
    """One client per invocation/event loop, explicitly closed on errors and cancellation."""
    if not settings.llm_api_key:
        raise ValueError("Model key is not configured")
    async with client_class(
        transport=transport, timeout=30,
        event_hooks={"request": [usage.on_request], "response": [usage.on_response]},
    ) as http_client:
        async with AsyncOpenAI(
            api_key=settings.llm_api_key, base_url=resolve_llm_base_url(settings),
            max_retries=0, timeout=30, http_client=http_client,
        ) as client:
            yield client


@asynccontextmanager
async def strands_model(
    settings: Settings, usage: TransportUsage, *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[OpenAIModel]:
    async with openai_client(settings, usage, transport=transport) as client:
        yield OpenAIModel(
            client=client, model_id=settings.llm_model, stream=False,
            params={"temperature": 0, **(
                {"extra_body": {"thinking": {"type": "disabled"}}}
                if settings.llm_provider == "deepseek" else {}
            )},
        )
