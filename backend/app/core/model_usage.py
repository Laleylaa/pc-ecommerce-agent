"""Request-local model attempt counts, shared with concurrent child tasks."""

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_counts: ContextVar[Counter[str] | None] = ContextVar("model_call_counts", default=None)


@contextmanager
def collect_model_calls() -> Iterator[None]:
    token = _counts.set(Counter())
    try:
        yield
    finally:
        _counts.reset(token)


def record_model_call(stage: str) -> None:
    if (counts := _counts.get()) is not None:
        counts[stage] += 1


def model_call_counts() -> dict[str, int] | None:
    counts = _counts.get()
    return dict(counts) if counts is not None else None
