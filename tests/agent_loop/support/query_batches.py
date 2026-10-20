"""Helpers for inspecting real query sessions and replacing isolated domain handlers."""

from app.agent import runtime as runtime_module
from app.tools.contracts import BoundTool, ToolCatalog


def capture_sessions(monkeypatch):
    sessions = []
    original = runtime_module.QuerySession

    def create(*args):
        session = original(*args)
        sessions.append(session)
        return session

    monkeypatch.setattr(runtime_module, "QuerySession", create)
    return sessions


def with_handlers(catalog, **handlers):
    return ToolCatalog([
        BoundTool(bound.contract, handlers.get(bound.contract.llm_name, bound.handler))
        for bound in catalog.list_bound_tools()
    ])
