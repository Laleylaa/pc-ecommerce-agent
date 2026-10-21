import asyncio

import pytest
from app.core.config import Settings
from app.tools.contracts import RegistryToolExecutor
from app.tools.schemas import OrderLookupOutput

from tests.support.execution_tool_concurrency import (
    SessionFactory,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "error", "cancel", "timeout"])
@pytest.mark.parametrize("name", ["order_lookup", "catalog_search", "catalog_facets"])
async def test_tool_sessions_are_per_call_and_closed_on_every_exit(monkeypatch, outcome, name):
    factory = SessionFactory()
    chat_session = object()
    seen = []

    async def lookup(self, request):
        session = self.repository.session if name == "catalog_search" else self.session
        seen.append((session, getattr(request, "user_id", None)))
        if outcome == "error":
            raise RuntimeError("synthetic failure")
        if outcome == "cancel":
            raise asyncio.CancelledError()
        if outcome == "timeout":
            await asyncio.Event().wait()
        if name == "catalog_search":
            from app.schemas.catalog_search import CatalogSearchResult
            return CatalogSearchResult(
                result_type="empty", status="clarification_required",
                reason_code="query_needs_clarification", recommended_action="clarify_query",
            )
        if name == "catalog_facets":
            from app.tools.schemas import CatalogFacetOutput
            return CatalogFacetOutput(result_type="empty", facet="category")
        return OrderLookupOutput(result_type="not_found")

    target = {
        "order_lookup": "app.tools.orders.OrderToolService.lookup",
        "catalog_search": "app.tools.catalog_search.service.CatalogSearchService.search",
        "catalog_facets": "app.tools.catalog.CatalogToolService.facets",
    }[name]
    monkeypatch.setattr(target, lookup)
    executor = RegistryToolExecutor(
        chat_session, Settings(_env_file=None, llm_api_key=""), tool_session_factory=factory,
    )
    contract = executor.catalog.get_by_llm_name(name).contract
    for _ in range(2):
        if outcome == "timeout":
            from app.tools.contracts import BoundTool, execute_bound_tool
            bound = executor.catalog.get_by_llm_name(name)
            call = execute_bound_tool(
                BoundTool(contract.model_copy(update={"timeout_seconds": .01}), bound.handler),
                {"query": "查询"}, {"user_id": 7},
            )
        else:
            call = executor.execute(contract, {"query": "查询"}, {"user_id": 7})
        if outcome == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await call
        else:
            result = await call
            assert result.ok == (outcome == "success")
            if outcome == "timeout":
                assert result.error.code == "timeout"
    assert len(factory.opened) == 2 and factory.closed == factory.opened
    assert factory.opened[0] is not factory.opened[1]
    assert all(session is not chat_session for session, _ in seen)
    if name == "order_lookup":
        assert [user for _, user in seen] == [7, 7]
