"""Request-local admission for actual HTTP model attempts, not logical LLM nodes."""

import asyncio
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

_stage: ContextVar[str] = ContextVar("loop_model_stage", default="query")
_tool_calls: ContextVar[list[int] | None] = ContextVar("loop_tool_calls", default=None)


class LoopLimit(RuntimeError):
    pass


@contextmanager
def model_stage(stage: str) -> Iterator[None]:
    token = _stage.set(stage)
    try:
        yield
    finally:
        _stage.reset(token)


def current_model_stage() -> str:
    return _stage.get()


@dataclass
class LoopBudget:
    # timeout_at(), provider planners and tool admission share the running loop's
    # clock. Its epoch need not match time.monotonic() (notably macOS uvloop).
    started: float = field(default_factory=lambda: asyncio.get_running_loop().time())
    turn_seconds: float = 240
    query_seconds: float = 190
    answer_seconds: float = 45
    cleanup_seconds: float = 5
    query_limit: int = 8
    tool_limit: int = 8
    total_limit: int = 67
    answer_limit: int = 3
    calls: Counter = field(default_factory=Counter)
    tool_attempts: int = 0
    query_transport_retries: int = 0
    answer_deadline: float | None = None
    closed: bool = False

    @property
    def turn_deadline(self) -> float:
        return self.started + self.turn_seconds

    @property
    def query_deadline(self) -> float:
        return min(
            self.started + self.query_seconds,
            self.turn_deadline - self.answer_seconds - self.cleanup_seconds,
        )

    def begin_answer(self) -> None:
        self.answer_deadline = min(
            asyncio.get_running_loop().time() + self.answer_seconds,
            self.turn_deadline - self.cleanup_seconds,
        )

    def deadline(self) -> float:
        if current_model_stage() == "answer":
            if self.answer_deadline is None:
                raise LoopLimit("answer_not_started")
            return self.answer_deadline
        return self.query_deadline

    def check(self) -> None:
        if self.closed or asyncio.get_running_loop().time() >= self.deadline():
            raise LoopLimit("stage_deadline")

    def charge_model(self) -> str:
        self.check()
        stage = current_model_stage()
        total = sum(self.calls.values())
        if total >= self.total_limit:
            raise LoopLimit("total_model_budget")
        if stage == "answer":
            if self.calls[stage] >= self.answer_limit:
                raise LoopLimit("answer_model_budget")
        else:
            if total - self.calls["answer"] >= self.total_limit - self.answer_limit:
                raise LoopLimit("query_shared_budget")
            if stage == "query" and self.calls[stage] >= self.query_limit:
                raise LoopLimit("query_model_budget")
            if stage.startswith("tool"):
                counter = _tool_calls.get()
                if counter is None:
                    raise LoopLimit("tool_model_outside_attempt")
                if counter[0] >= 7:
                    raise LoopLimit("tool_model_budget")
                counter[0] += 1
        self.calls[stage] += 1
        return stage

    def before_tool(self, operation) -> float:
        self.check()
        if self.tool_attempts >= self.tool_limit:
            raise LoopLimit("tool_attempt_budget")
        self.tool_attempts += 1
        return self.query_deadline

    @contextmanager
    def tool_scope(self, operation) -> Iterator[None]:
        token = _tool_calls.set([0])
        try:
            with model_stage("tool_planning"):
                yield
        finally:
            _tool_calls.reset(token)


def find_loop_limit(exc: BaseException) -> LoopLimit | None:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, LoopLimit):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None
