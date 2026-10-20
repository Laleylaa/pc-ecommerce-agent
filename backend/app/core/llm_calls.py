"""Model transport primitives shared by orchestration and tool planners."""

import asyncio
import math
import time
from datetime import UTC
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.core.loop_budget import model_stage
from app.core.model_usage import record_model_call


class ModelCallError(RuntimeError):
    """Safe terminal diagnostic; never eligible for output repair."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


class ModelDeadlineExceeded(TimeoutError):
    """Our shared deadline expired, distinct from a provider/HTTP timeout."""


def transport_failure_details(exc: BaseException) -> dict:
    """Classify wrapped failures without exposing request URLs, headers or bodies."""
    chain = []
    current = exc
    while current is not None and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    kind = "not_timeout"
    for error_type, label in (
        (ModelDeadlineExceeded, "local_deadline"),
        (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
        (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
        (APITimeoutError, "provider_unspecified"),
        ((TimeoutError, httpx.TimeoutException), "unspecified"),
    ):
        if any(isinstance(item, error_type) for item in chain):
            kind = label
            break
    return {"timeout_source": kind, "exception_types": [type(item).__name__ for item in chain]}


def retryable_transport_error(exc: Exception) -> bool:
    if isinstance(exc, APIStatusError):
        return exc.status_code in {408, 409, 429} or 500 <= exc.status_code < 600
    return isinstance(
        exc, (APIConnectionError, httpx.TimeoutException, httpx.NetworkError, TimeoutError)
    )


def transport_retry_delay(exc: Exception, default: float) -> float:
    if not isinstance(exc, APIStatusError):
        return default
    for header, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        value = exc.response.headers.get(header)
        if value is None:
            continue
        try:
            delay = float(value) / divisor
        except ValueError:
            if header != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                delay = max(0.0, date.timestamp() - time.time())
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return default


async def invoke_before(
    model: Any, messages: list, deadline: float, *, stage: str = "other",
) -> Any:
    """A single provider attempt, including rejection of cancellation-suppressing calls."""
    if asyncio.get_running_loop().time() >= deadline:
        raise ModelDeadlineExceeded("Model call budget exhausted")
    budget = asyncio.timeout_at(deadline)
    try:
        async with budget:
            record_model_call(stage)
            with model_stage(stage):
                response = await model.ainvoke(messages)
    except TimeoutError as exc:
        if budget.expired():
            raise ModelDeadlineExceeded("Model call budget exhausted") from exc
        raise
    if asyncio.get_running_loop().time() >= deadline:
        raise ModelDeadlineExceeded("Model returned after its deadline")
    return response
