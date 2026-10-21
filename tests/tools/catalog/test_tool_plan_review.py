"""Business corrections through the real reviewed planners, no external I/O."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from app.core.model_usage import collect_model_calls, model_call_counts
from app.tools.catalog import CatalogToolService, LLMCatalogQueryPlanner
from app.tools.catalog_search.planner import CatalogSearchPlanner
from app.tools.catalog_search.service import CatalogSearchService
from app.tools.contracts import BoundTool, DefaultToolContractProvider, execute_bound_tool
from app.tools.orders import LLMOrderQueryPlanner, OrderToolService

from tests.support.tool_plan_fixtures import facet_plan, order_plan, search_plan
from tests.support.tool_planning import Repository, product


class ReviewModel:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.before_call = None

    async def ainvoke(self, messages):
        if self.before_call:
            self.before_call()
        self.calls.append(list(messages))
        value = self.outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return SimpleNamespace(content=value if isinstance(value, str) else json.dumps(value))


def assessment(query, verdict="approve", path="/conditions", status="matched"):
    return {"verdict": verdict, "checks": [{
        "evidence": query, "plan_paths": [path] if path else [],
        "status": status, "explanation": "核对原文要求及其计划表达",
    }]}


def connection(value, evidence):
    return {"key": "connection_type", "values": [value], "evidence": evidence}


async def invoke(monkeypatch, tool, query, *outputs):
    repository = Repository([
        product(1, 1, category="鼠标", connection_type="三模", price="180"),
        product(2, 2, category="鼠标", connection_type="蓝牙", price="280"),
    ])
    monkeypatch.setattr(
        "app.repositories.catalog_search.CatalogSearchRepository.load",
        lambda self, category, scope: repository.load(category, scope),
    )
    model = ReviewModel(*outputs)

    def no_early_read():
        assert not repository.calls, "Catalog queried before semantic approval"

    model.before_call = no_early_read
    handler = (
        CatalogSearchService(repository, CatalogSearchPlanner(model)).search
        if tool == "catalog_search"
        else CatalogToolService(None, planner=LLMCatalogQueryPlanner(model)).facets
    )
    with collect_model_calls():
        result = await execute_bound_tool(
            BoundTool(DefaultToolContractProvider().get_contract(tool), handler),
            {"query": query}, {"user_id": 7},
        )
        counts = model_call_counts()
    return result, repository, model, counts


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,wire", [
    ("catalog_search", search_plan), ("catalog_facets", facet_plan),
])
async def test_review_corrects_both_connections_before_read(monkeypatch, tool, wire):
    query = "蓝牙和2.4G都要的鼠标有哪些品牌"
    wrong = wire({"category": "鼠标", "conditions": [{
        "key": "connection_type", "operator": "in", "values": ["蓝牙", "2.4G"],
        "evidence": "蓝牙和2.4G都要",
    }]})
    corrected = wire({"category": "鼠标", "conditions": [
        connection("蓝牙", "蓝牙"), connection("2.4G", "2.4G"),
    ]})
    result, repo, model, counts = await invoke(
        monkeypatch, tool, query, wrong,
        assessment("蓝牙和2.4G都要", "revise", status="mismatched"),
        corrected, assessment(query),
    )
    assert result.ok and bool(repo.calls)
    assert counts == {"tool_planning": 2, "tool_review": 2}
    if tool == "catalog_search":
        assert result.output["products"][0]["candidate_sku_ids"] == [1]
    else:
        assert sum(i["sku_count"] for i in result.output["items"]) == 1
    # Fresh review does not inherit previous generator explanations or conversations.
    assert len(model.calls[1]) == len(model.calls[3]) == 2
    assert json.loads(model.calls[1][1].content)["original_request"]["query"] == query
    assert "validation_feedback" in json.loads(model.calls[2][-1].content)
    # Search omits common spec prose; its explicit examples must still reach both phases.
    for messages in (model.calls[0], model.calls[1]):
        assert '“蓝牙或者2.4G都行”' in messages[0].content
        assert '“蓝牙必须有，最好也支持2.4G”' in messages[0].content
    semantics = json.loads(model.calls[3][1].content)["program_checks"]["assessment"][
        "condition_semantics"
    ]
    assert semantics["requirements"] == "all_on_same_sku"
    assert semantics["examples_only"] is True
    for condition in semantics["conditions"]:
        examples = {item["value"]: item["matches"] for item in condition["record_examples"]}
        assert examples["三模"] is True


@pytest.mark.asyncio
async def test_review_explanation_preserves_preference_and_exclusion(monkeypatch):
    query = "找不支持蓝牙的鼠标，最好有线连接"
    plan = search_plan({"category": "鼠标", "conditions": [
        {"key": "connection_type", "operator": "ne", "values": ["蓝牙"],
         "evidence": "不支持蓝牙"},
        {"key": "connection_type", "values": ["有线"], "strength": "preference",
         "evidence": "最好有线连接", "preference_evidence": "最好有线连接"},
    ]})
    result, _, model, _ = await invoke(
        monkeypatch, "catalog_search", query, plan, assessment(query),
    )
    assert result.ok
    semantics = json.loads(model.calls[1][1].content)["program_checks"]["assessment"][
        "condition_semantics"
    ]
    assert semantics["preferences"] == "ranking_only"
    negative, preference = semantics["conditions"]
    assert {i["value"]: i["matches"] for i in negative["record_examples"]}["三模"] is False
    assert preference["strength"] == "preference"


@pytest.mark.asyncio
async def test_review_restores_missing_budget(monkeypatch):
    query = "找200元以内的蓝牙鼠标"
    wrong = search_plan({"category": "鼠标", "conditions": [connection("蓝牙", "蓝牙")]})
    corrected = deepcopy(wrong)
    corrected["conditions"].append({
        "key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内",
        "strength": "requirement", "preference_evidence": None,
    })
    result, repo, _, counts = await invoke(
        monkeypatch, "catalog_search", query, wrong,
        assessment("200元以内", "revise", path=None, status="missing"),
        corrected, assessment(query),
    )
    assert result.ok and bool(repo.calls)
    assert result.output["products"][0]["candidate_sku_ids"] == [1]
    assert counts == {"tool_planning": 2, "tool_review": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize("query,values", [
    ("找蓝牙鼠标", ["蓝牙"]),
    ("找蓝牙和2.4G都能用的鼠标", ["蓝牙", "2.4G"]),
    ("找三模鼠标，蓝牙也要支持", ["三模", "蓝牙"]),
])
async def test_correct_plan_is_approved_without_modification(monkeypatch, query, values):
    plan = search_plan({"category": "鼠标", "conditions": [connection(v, v) for v in values]})
    result, repo, _, counts = await invoke(
        monkeypatch, "catalog_search", query, plan, assessment(query),
    )
    assert result.ok and bool(repo.calls)
    assert result.output["query_plan"]["conditions"] == plan["conditions"]
    assert counts == {"tool_planning": 1, "tool_review": 1}


@pytest.mark.asyncio
async def test_valid_wireless_charging_limitation_is_approved_without_query(monkeypatch):
    query = "支持无线充电的鼠标有哪些品牌"
    plan = facet_plan({"unresolved_requirements": [{
        "evidence": "支持无线充电", "reason": "filter_data_unavailable",
    }]})
    result, repo, _, counts = await invoke(
        monkeypatch, "catalog_facets", query, plan,
        assessment("支持无线充电", path="/unresolved_requirements/0", status="unresolved"),
    )
    assert result.ok and not repo.calls
    assert result.output["diagnostics"][0]["code"] == "filter_data_unavailable"
    assert counts == {"tool_planning": 1, "tool_review": 1}


@pytest.mark.asyncio
async def test_uncertain_review_returns_terminal_error_not_empty_results(monkeypatch):
    result, repo, _, _ = await invoke(
        monkeypatch, "catalog_search", "找办公鼠标", search_plan(),
        assessment("办公", "uncertain", path=None, status="missing"),
    )
    assert not repo.calls and not result.ok and result.output is None
    assert result.error.code == "planning_review_inconclusive"
    assert not result.error.retryable and result.error.recommended_action == "stop"


@pytest.mark.asyncio
async def test_repeating_rejected_plan_cannot_trigger_another_review(monkeypatch):
    plan = search_plan({"category": "鼠标"})
    result, repo, model, _ = await invoke(
        monkeypatch, "catalog_search", "找白色鼠标", plan,
        assessment("白色", "revise", path=None, status="missing"), plan,
    )
    assert not result.ok and not repo.calls and len(model.calls) == 3
    assert result.error.details["stop_reason"] == "repeated_plan"


@pytest.mark.asyncio
async def test_program_rejects_invalid_semantic_correction_before_reviewer(monkeypatch):
    query = "找200元以内的鼠标"
    missing = search_plan({"category": "鼠标"})
    invalid = search_plan({"category": "鼠标", "conditions": [{
        "key": "price", "operator": "lte", "values": ["200瓦"], "evidence": "200元以内",
    }]})
    fixed = deepcopy(invalid)
    fixed["conditions"][0]["values"] = ["200元"]
    result, repo, model, counts = await invoke(
        monkeypatch, "catalog_search", query, missing,
        assessment("200元以内", "revise", path=None, status="missing"),
        invalid, fixed, assessment(query),
    )
    assert result.ok and bool(repo.calls)
    assert counts == {"tool_planning": 3, "tool_review": 2}
    assert "validation_feedback" in json.loads(model.calls[3][-1].content)


@pytest.mark.asyncio
async def test_review_cannot_approve_with_fabricated_evidence(monkeypatch):
    result, repo, _, counts = await invoke(
        monkeypatch, "catalog_search", "找鼠标", search_plan({"category": "鼠标"}),
        assessment("预算100元"), assessment("找鼠标"),
    )
    assert result.ok and bool(repo.calls)
    assert counts == {"tool_planning": 1, "tool_review": 2}


@pytest.mark.asyncio
async def test_review_cancellation_never_reaches_repository(monkeypatch):
    with pytest.raises(asyncio.CancelledError):
        await invoke(
            monkeypatch, "catalog_search", "找鼠标", search_plan({"category": "鼠标"}),
            asyncio.CancelledError(),
        )


@pytest.mark.asyncio
async def test_order_review_restores_status_before_counting_owned_orders(monkeypatch):
    query = "我买的键盘里，待发货订单有几笔"
    missing = order_plan({
        "operation": "total_count", "requested_fields": ["order_count"],
        "filter_evidence": ["键盘"],
    })
    missing["filters"]["product_terms"] = ["键盘"]
    fixed = deepcopy(missing)
    fixed["filters"]["statuses"] = [2]
    fixed["filter_evidence"].append("待发货")
    model = ReviewModel(
        missing, assessment("待发货", "revise", path="/filters/statuses", status="missing"),
        fixed, assessment(query, path="/filters"),
    )
    calls = []

    class Orders:
        def __init__(self, session):
            pass

        async def count_orders(self, user_id, *, filters):
            assert len(model.calls) == 4
            calls.append((user_id, filters))
            # Synthetic rows: one owned pending keyboard order, plus unrelated rows.
            rows = [(7, 2, "键盘"), (7, 3, "键盘"), (8, 2, "键盘"), (7, 2, "鼠标")]
            return sum(owner == user_id and status in filters.statuses
                       and item in filters.product_terms for owner, status, item in rows)

    monkeypatch.setattr("app.tools.orders.OrderRepository", Orders)
    handler = OrderToolService(None, planner=LLMOrderQueryPlanner(model)).lookup
    result = await execute_bound_tool(
        BoundTool(DefaultToolContractProvider().get_contract("order_lookup"), handler),
        {"query": query}, {"user_id": 7},
    )
    assert result.ok and result.output["total_match_count"] == 1
    assert len(calls) == 1 and calls[0][0] == 7
    for messages in model.calls:
        payload = json.loads(messages[1].content)
        assert "user_id" not in payload and "user_id" not in payload.get("original_request", {})


@pytest.mark.asyncio
async def test_two_corrections_do_not_authorize_incomplete_shopping_query(monkeypatch):
    query = "找200元以内的白色蓝牙鼠标"
    first = search_plan({"category": "鼠标"})
    second = search_plan({"category": "鼠标", "conditions": [{
        "key": "price", "operator": "lte", "values": ["200"], "evidence": "200元以内",
    }]})
    third = deepcopy(second)
    third["conditions"].append({
        "key": "color", "operator": "eq", "values": ["白色"], "evidence": "白色",
        "strength": "requirement", "preference_evidence": None,
    })
    result, repo, model, counts = await invoke(
        monkeypatch, "catalog_search", query,
        first, assessment("200元以内", "revise", path=None, status="missing"),
        second, assessment("白色", "revise", path=None, status="missing"),
        third, assessment("蓝牙", "revise", path=None, status="missing"),
    )
    assert not repo.calls and not result.ok and not model.outputs
    assert counts == {"tool_planning": 3, "tool_review": 3}
    assert result.error.code == "planning_output_invalid" and not result.error.retryable


@pytest.mark.asyncio
async def test_transient_review_timeout_keeps_original_plan(monkeypatch):
    result, repo, _, counts = await invoke(
        monkeypatch, "catalog_search", "找蓝牙鼠标",
        search_plan({"category": "鼠标", "conditions": [connection("蓝牙", "蓝牙")]}),
        TimeoutError(), assessment("找蓝牙鼠标"),
    )
    assert result.ok and repo.calls
    assert counts == {"tool_planning": 1, "tool_review": 2}
