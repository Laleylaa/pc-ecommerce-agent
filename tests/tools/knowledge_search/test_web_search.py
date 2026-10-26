import asyncio
import json

import httpx
import pytest
from app.core.config import Settings
from app.core.tracing import trace_data
from app.schemas.chat import EvidenceItem
from app.tools.contracts import (
    BoundTool,
    DefaultToolContractProvider,
    build_tool_catalog,
    execute_bound_tool,
)
from app.tools.schemas import WebSearchInput
from app.tools.web_search import WebSearchError, WebSearchToolService
from pydantic import ValidationError

from tests.support.knowledge_web_search import (
    HIT,
    execute,
    service,
)


def test_contract_and_runtime_binding_use_web_search():
    contract = DefaultToolContractProvider().get_contract("knowledge_search")
    assert set(contract.public_input_model.model_fields) == {"query", "limit"}
    for fields in ({"document_type": "brand"}, {"retrieval_mode": "vector"}, {"api_key": "fake"}):
        with pytest.raises(ValidationError):
            WebSearchInput(query="磁轴", **fields)
    catalog = build_tool_catalog(
        None, settings=Settings(
            _env_file=None, llm_api_key="", deepseek_search_api_key="test-secret",
        ),
    )
    bound = catalog.get_by_llm_name("knowledge_search")
    assert isinstance(bound.handler.__self__, WebSearchToolService)
    assert bound.handler.__self__._api_key == "test-secret"
    assert trace_data({"deepseek_search_api_key": "test-secret"}) == {
        "deepseek_search_api_key": "[redacted]",
    }


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_search_preserves_query_and_returns_sources():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [
            HIT, HIT, {**HIT, "url": "https://example.com/2"},
            {**HIT, "url": "https://example.com/3"},
        ]})

    result = await execute(
        service(respond).search, {"query": "磁轴和机械轴有什么区别？", "limit": 2},
    )
    assert result.ok and result.output["result_type"] == "documents"
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://api.deepseek.com/v1/tools/search"
    assert request.headers["Authorization"] == "Bearer test-secret"
    assert json.loads(request.content) == {
        "query": "磁轴和机械轴有什么区别？", "count": 2, "need_content": False,
    }
    assert result.output["search_strategy"] == "web"
    hits = result.output["documents"]
    assert [h["url"] for h in hits] == [HIT["url"], "https://example.com/2"]
    assert hits[0]["source_type"] == "web_page" and hits[0]["snippet"] == HIT["summary"]
    assert hits[0]["metadata"] == {"provider": "deepseek"}
    assert hits[0]["score"] == 2.4
    assert hits[0]["source_id"] < 2**53
    assert EvidenceItem.model_validate(hits[0]).url == HIT["url"]
    again = await execute(service(respond).search)
    assert again.output["documents"][0]["source_id"] == hits[0]["source_id"]
    assert "test-secret" not in result.model_dump_json()


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_missing_key_never_sends_request():
    def forbidden(request):
        raise AssertionError("must not contact provider")

    result = await execute(service(forbidden, key="").search)
    assert not result.ok and result.error.code == "dependency_unavailable"
    assert result.error.next_action == "stop"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [
    401, 403, 429, 402, 500, 400, 302, 408, 504,
])
async def test_http_errors_do_not_become_empty_results_or_leak_body(status):
    web = service(lambda request: httpx.Response(status, text="test-secret"))
    result = await execute(web.search)
    assert not result.ok and result.error.code == "dependency_unavailable"
    assert result.error.next_action == "stop"
    assert result.output is None and "test-secret" not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"results": None}, {"results": [{}]}, {
    "results": [{**HIT, "url": "javascript:alert(1)"}],
}])
async def test_malformed_provider_output_is_error(body):
    result = await execute(service(lambda request: httpx.Response(200, json=body)).search)
    assert not result.ok and result.error.code == "dependency_unavailable"


@pytest.mark.asyncio
async def test_real_empty_is_successful_empty():
    web = service(lambda request: httpx.Response(200, json={"results": []}))
    result = await execute(web.search)
    assert result.ok and result.output["result_type"] == "empty"
    assert result.output["documents"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["total_timeout", "transport_timeout", "network"])
async def test_request_deadline_and_transport_errors(monkeypatch, failure):
    monkeypatch.setattr("app.tools.web_search.WEB_SEARCH_TIMEOUT_SECONDS", 0.02)
    calls = []

    async def respond(request):
        calls.append(request)
        if failure == "total_timeout":
            await asyncio.sleep(1)
        if failure == "network":
            raise httpx.ConnectError("test-secret", request=request)
        raise httpx.ReadTimeout("test-secret", request=request)

    result = await execute(service(respond).search)
    assert not result.ok and result.error.next_action == "stop"
    assert result.error.code == "dependency_unavailable"
    assert len(calls) == 1 and "test-secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_outer_tool_deadline_also_disables_paid_retry():
    cancelled = asyncio.Event()

    async def respond(request):
        try:
            await asyncio.sleep(1)
        finally:
            cancelled.set()

    contract = DefaultToolContractProvider().get_contract("knowledge_search")
    contract = contract.model_copy(update={"timeout_seconds": 0.01})
    result = await execute_bound_tool(
        BoundTool(contract, service(respond).search), {"query": "蓝牙延迟原理"}, {"user_id": 7},
    )
    assert result.error.code == "dependency_unavailable" and result.error.next_action == "stop"
    assert cancelled.is_set()


@pytest.mark.parametrize("config,expected", [
    ({"llm_api_key": "model-secret"}, "model-secret"),
    ({"llm_api_key": "model-secret", "llm_base_url": "https://api.deepseek.com/v1"},
     "model-secret"),
    ({"llm_api_key": "other-secret", "llm_provider": "openai"}, ""),
    ({"llm_api_key": "proxy-secret", "llm_base_url": "https://proxy.example/v1"}, ""),
    ({"llm_api_key": "model-secret", "deepseek_search_api_key": "search-secret"},
     "search-secret"),
])
def test_search_credentials_resolve_in_formal_binding(config, expected):
    settings = Settings(_env_file=None, **config)
    catalog = build_tool_catalog(None, settings=settings)
    bound = catalog.get_by_llm_name("knowledge_search")
    assert bound.handler.__self__._api_key == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"query": "轴" * 129}, {"query": "   "},
    {"query": "磁轴", "limit": 0}, {"query": "磁轴", "limit": 11},
])
async def test_invalid_arguments_stop_before_http(args):
    def forbidden(request):
        raise AssertionError("invalid arguments must not reach the provider")

    result = await execute(service(forbidden).search, args)
    assert result.error.code == "invalid_arguments"
    assert result.error.next_action == "revise_arguments"


@pytest.mark.asyncio
async def test_max_query_length_and_summary_fallback():
    def respond(request):
        assert json.loads(request.content)["query"] == "轴" * 128
        return httpx.Response(200, json={"results": [{**HIT, "summary": "  "}]})

    result = await execute(service(respond).search, {"query": "轴" * 128})
    assert result.ok
    assert result.output["documents"][0]["snippet"] == HIT["snippet"]
    assert WebSearchInput.model_json_schema()["properties"]["query"]["maxLength"] == 128


@pytest.mark.asyncio
@pytest.mark.parametrize("status,reason", [
    (402, "web_search_quota_exceeded"), (429, "web_search_quota_exceeded"),
    (408, "web_search_timeout"), (504, "web_search_timeout"),
])
async def test_provider_error_keeps_internal_diagnostic_reason(status, reason):
    web = service(lambda request: httpx.Response(status, text="provider-private-detail"))
    with pytest.raises(WebSearchError) as error:
        await web.search(WebSearchInput(query="鼠标传感器"))
    assert error.value.code == reason
    assert "provider-private-detail" not in str(error.value)
