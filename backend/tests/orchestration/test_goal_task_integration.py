
from copy import deepcopy

import pytest

from app.agent.answer_context import build_answer_context
from app.repositories.catalog_search import CatalogRecord
from app.schemas.chat import ChatRequest
from app.schemas.context import WorkingMemoryV2
from app.services.context import _next_working_memory
from tests.support.runtime import (
    goal,
    keyboard_plan,
    message,
    setup_runtime,
    task,
    through_tools,
)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_unknown_name_search_binds_identity_into_scoped_object_facets(monkeypatch):
    text = "查询测试键盘1的规格"
    lookup = task("查询测试键盘1", answer_role="internal")
    specs = task("这款有什么规格", "task_2", "catalog_facets", depends_on=["task_1"],
                 bindings=[{"source": "task_output", "ref": "task_1", "kind": "product",
                            "selection": "parent_model"}])
    runtime, model, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text)], [{"goal_id": "goal_1", "tasks": [lookup, specs]}],
        [{"operation": "lookup", "purpose": "lookup", "name": "测试键盘1"}],
    )
    state, _ = await through_tools(runtime, text, waves=2)
    assert [call[0] for call in executor.calls] == ["catalog_search", "catalog_facets"]
    assert executor.calls[1][1]["scope"] == {"sku_ids": [], "spu_ids": [1]}
    returned = state["tool_results"][-1]["execution"]
    assert returned["ok"] and returned["output"]["is_exhaustive"]
    assert {sku for item in returned["output"]["items"]
            for value in item["values"] for sku in value["sku_ids"]} == {1}
    assert not model.calls["summarize_goals"]


@pytest.mark.asyncio
async def test_known_product_scope_and_hint_are_preserved_for_facets(monkeypatch):
    text = "这款有什么规格"
    planned = task(text, tool="catalog_facets")
    planned["arguments"].update(scope={"sku_ids": [2]}, category_hint="键盘")
    runtime, model, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text, refs=["product_1"])],
        [{"goal_id": "goal_1", "tasks": [planned]}],
        memory={"catalog": {"candidate_sku_ids": [2]}},
    )
    state, _ = await through_tools(runtime, text)
    assert executor.calls[0][1]["category_hint"] == "键盘"
    returned = state["tool_results"][-1]["execution"]
    assert returned["ok"] and returned["output"]["scope"] == {"sku_ids": [2], "spu_ids": []}
    assert not model.calls["summarize_goals"]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit,expected", [(None, 3), (2, 2)])
async def test_catalog_candidate_count_flows_from_plan_to_real_tool(monkeypatch, limit, expected):
    text = "查询键盘" if limit is None else "给我两款键盘"
    planned = task(text)
    if limit is not None:
        planned["arguments"]["limit"] = limit
    runtime, _, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text)],
        [{"goal_id": "goal_1", "tasks": [planned]}],
        [{"category": "键盘", "category_evidence": "键盘"}],
    )
    state, _ = await through_tools(runtime, text)
    assert len(state["products"]) == expected
    assert ("limit" in executor.calls[0][1]) == (limit is not None)
    assert "offset" not in executor.calls[0][1]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_direct_goals_skip_llm2_and_reach_llm3(monkeypatch):
    text = "你是谁，能做什么"
    runtime, model, executor, context = setup_runtime(
        monkeypatch, text, [goal(text, disposition="direct_response")], []
    )
    result = await runtime.run(ChatRequest(message=text), 1)
    assert result.answer == "我是商城客服助手。"
    assert len(model.calls["summarize_goals"]) == 1
    assert not model.calls["submit_execution_plan"] and not executor.calls
    assert context.completed == 1


@pytest.mark.asyncio
async def test_search_and_facets_share_one_complete_goal(monkeypatch):
    query = "办公室用，白色蓝牙键盘300元以内"
    text = query + "，看看可选布局"
    runtime, model, executor, context = setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [
            {
                "goal_id": "goal_1",
                "tasks": [task(query), task("键盘有哪些布局", "task_2", "catalog_facets")],
            }
        ],
        [keyboard_plan()],
    )
    state, visited = await through_tools(runtime, text)
    assert [c[0] for c in executor.calls] == ["catalog_search", "catalog_facets"]
    assert "catalog_context" not in executor.calls[0][2]
    assert state["task_artifacts"]["task_1"]["value"]["match_kind"] == "exact"
    assert state["task_artifacts"]["task_2"]["value"]["items"][0]["value"] == "75%"
    answer = build_answer_context(state)
    assert len(answer["goals"]) == 1
    assert answer["goals"][0]["query"] == text
    assert answer["goals"][0]["disposition"] == "tool_planning"
    assert executor.calls[0][1]["query"] == query
    assert "unresolved_requirements" not in state["execution_plan"]
    assert "unresolved_requirements" not in answer
    assert not model.calls["summarize_goals"] and context.completed == 0
    assert "summarize_goals" not in visited


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["sku", "spu"])
@pytest.mark.parametrize("spelling", ["{} 2", "{}编号2", "{}编号：2"])
@pytest.mark.parametrize("scoped", [False, True])
async def test_explicit_id_spellings_share_provenance_and_tool_contract(
    monkeypatch, kind, spelling, scoped,
):
    text = "查询" + spelling.format(kind.upper()) + "的价格"
    normalized = f"查询 {kind.upper()} 2 的价格"
    planned = task(text)
    if scoped:
        planned["arguments"]["scope"] = {f"{kind}_ids": [2]}
    runtime, model, executor, _ = setup_runtime(
        monkeypatch, text, [{**goal(normalized), "source_text": text}],
        [{"goal_id": "goal_1", "tasks": [planned]}],
        [{"operation": "lookup", "target": {"kind": kind, "id": 2}}],
    )
    state, _ = await through_tools(runtime, text)
    assert len(model.calls["route_request"]) == len(model.calls["submit_execution_plan"]) == 1
    assert executor.calls[0][1]["query"] == text
    assert ("scope" in executor.calls[0][1]) is scoped
    assert state["task_artifacts"]["task_1"]["value"]["products"][0]["sku_id"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limited", [False, True])
async def test_reference_identity_is_not_implicitly_a_search_boundary(monkeypatch, limited):
    text = "以第二款为参照，找300元以下的键盘"
    query = "以 SKU 2 为参照，找300元以下的键盘"
    refs = ["product_2"]
    planned = task(query)
    if limited:
        text += "，只在第一款里查"
        query += "，只在 SKU 1 中筛选"
        refs.append("product_1")
        # The scope itself identifies the candidate; query may mention another reference.
        planned["arguments"]["scope"] = {"sku_ids": [1]}
    runtime, _, executor, _ = setup_runtime(
        monkeypatch, text, [{**goal(query, refs=refs), "source_text": text}],
        [{"goal_id": "goal_1", "tasks": [planned]}],
        [{"category": "键盘", "category_evidence": "键盘", "conditions": [
            {"key": "price", "operator": "lte", "values": ["300"], "evidence": "300元以下"},
        ]}],
        {"catalog": {"candidate_sku_ids": [1, 2]}},
    )
    state, _ = await through_tools(runtime, text)
    assert executor.calls[0][1]["query"] == planned["arguments"]["query"]
    assert ("scope" in executor.calls[0][1]) is limited
    products = state["task_artifacts"]["task_1"]["value"]["products"]
    assert [p["sku_id"] for p in products] == ([1] if limited else [1, 2])


@pytest.mark.asyncio
async def test_named_object_query_keeps_name_for_search(monkeypatch):
    text = "查询测试键盘1价格"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [{"goal_id": "goal_1", "tasks": [task(text)]}],
        [{"operation": "lookup", "purpose": "lookup", "name": "测试键盘1"}],
    )
    state, _ = await through_tools(runtime, text)
    assert "scope" not in executor.calls[0][1]
    assert state["task_artifacts"]["task_1"]["value"]["products"][0]["sku_id"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_llm_resolved_context_preserves_identity_without_forced_scope(monkeypatch, scoped):
    text = "第二款现在多少钱"
    memory = {
        "catalog": {
            "query_plan": {"selection_scope": "sku"},
            "candidate_sku_ids": [1, 2],
            "candidate_spu_ids": [1, 2],
        }
    }
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [{**goal("查询 SKU 2 的价格", refs=["product_2"]),
          "source_text": text}],
        [
            {
                "goal_id": "goal_1",
                "tasks": [
                    task(
                        "查询 SKU 2 的价格",
                        arguments={
                            "query": "查询 SKU 2 的价格",
                            **({"scope": {"sku_ids": [2]}} if scoped else {}),
                        },
                        bindings=[{"source": "context", "ref": "product_2", "kind": "product"}],
                    )
                ],
            }
        ],
        [{"operation": "lookup", "purpose": "lookup", "target": {"kind": "sku", "id": 2}}],
        memory,
    )
    state, _ = await through_tools(runtime, text)
    assert executor.calls[0][1].get("scope") == (
        {"sku_ids": [2], "spu_ids": []} if scoped else None
    )
    assert executor.calls[0][1]["query"] == "查询 SKU 2 的价格"
    assert state["products"][0].match_kind == "exact"
    assert state["products"][0].candidate_sku_ids == [2]


@pytest.mark.asyncio
async def test_budget_replacement_uses_new_query_and_preserves_remaining_conditions(monkeypatch):
    query = "白色蓝牙键盘300元以内"
    text = "预算改成300"
    g = {**goal(query), "source_text": text}
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [g],
        [{"goal_id": "goal_1", "tasks": [task(query)]}],
        [keyboard_plan()],
        {"catalog": {"query_plan": {"query": "白色蓝牙键盘500元以内"}}},
    )
    state, _ = await through_tools(runtime, text)
    assert executor.calls[0][1]["query"] == query
    memory = _next_working_memory(
        WorkingMemoryV2(), {k: v for k, v in state.items() if k != "working_memory"}
    )
    assert memory.catalog.query_plan["query"] == query
    assert "search_page" not in memory.catalog.model_dump()
    assert memory.catalog.query_plan["conditions"][0]["values"] == ["300"]


@pytest.mark.asyncio
async def test_alternative_is_explained_and_cannot_feed_exact_dependency(monkeypatch):
    query = "白色蓝牙键盘100元以内"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [
            {
                "goal_id": "goal_1",
                "tasks": [
                    task(query, answer_role="internal"),
                    task(
                        "查看找到的键盘",
                        "task_2",
                        depends_on=["task_1"],
                        bindings=[{"source": "task_output", "ref": "task_1", "kind": "product"}],
                    ),
                ],
            }
        ],
        [keyboard_plan(100)],
    )
    state, _ = await through_tools(runtime, query)
    result = state["task_artifacts"]["task_1"]["value"]
    assert result["match_kind"] == "alternative" and result["total_match_count"] == 0
    assert result["candidate_count"] == 3
    assert state["task_status"]["task_2"]["status"] == "blocked"
    assert len(executor.calls) == 1

    # The existing partial-success scenario must keep useful prerequisite facts in the answer.
    context = build_answer_context(state)
    assert context["resolved_task_ids"] == []
    assert context["answerable_source_tool_call_ids"] == []
    assert context["supporting_source_tool_call_ids"] == ["planned_task_1"]
    assert context["supporting_tasks"][0]["partial_answer_goal_ids"] == ["goal_1"]
    assert context["recommended_control_action"] == "finish_partial"
    assert context["supporting_tasks"][0]["artifact"]["facts"]["match_kind"] == "alternative"

    response = "100元以内没有匹配项；已查到的替代键盘最低200元，超出预算，后续查询尚未执行。"
    partial = deepcopy(state)
    partial["decision"] = {
        "type": "partial_response", "control_action": "finish_partial", "response": response,
        "used_tool_call_ids": ["planned_task_1"], "unavailable_parts": ["查看找到的键盘"],
        "covered_goal_ids": ["goal_1"],
    }
    await runtime._terminal_guard(partial)
    assert partial["terminal_guard_status"] == "accepted"
    await runtime._finalize_response(partial)
    assert partial["answer"] == response
    assert partial["task_status"]["task_2"]["status"] == "blocked"

    # The same source cannot certify downstream completion; rejection never starts a retry.
    rejected = deepcopy(state)
    rejected["decision"] = {
        **partial["decision"], "type": "grounded_response", "control_action": "finish_answer",
    }
    await runtime._terminal_guard(rejected)
    assert rejected["terminal_guard_status"] == "rejected"
    await runtime._finalize_response(rejected)
    assert rejected["decision"]["control_action"] == "finish_partial"
    assert rejected["decision"]["used_tool_call_ids"] == ["planned_task_1"]
    assert "测试键盘1" in rejected["answer"] and "200" in rejected["answer"]
    assert len(executor.calls) == 1

    # Invalidated provenance is not revived just because an artifact remains in memory.
    stale = deepcopy(state)
    stale["subquery_ledger"][0]["status"] = "superseded"
    assert build_answer_context(stale)["supporting_source_tool_call_ids"] == []
    stale["decision"] = deepcopy(partial["decision"])
    await runtime._terminal_guard(stale)
    assert stale["terminal_guard_status"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["object", "candidate_versions", "parent_model"])
@pytest.mark.parametrize("query_length", [12, 2000])
async def test_dependency_binding_is_resolved_only_after_search(
    monkeypatch, selection, query_length,
):
    query = "白色蓝牙键盘300元以内"
    downstream_query = "查询第一款当前价格".ljust(query_length, "。")
    runtime, model, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [
            {
                "goal_id": "goal_1",
                "tasks": [
                    task(query, answer_role="internal"),
                    task(
                        downstream_query,
                        "task_2",
                        depends_on=["task_1"],
                        bindings=[
                            {
                                "source": "task_output",
                                "ref": "task_1",
                                "kind": "product",
                                "positions": [1],
                                "selection": selection,
                            }
                        ],
                    ),
                ],
            }
        ],
        [keyboard_plan(), {"operation": "lookup", "purpose": "lookup"}],
    )
    state, _ = await through_tools(runtime, query, waves=2)
    assert len(model.calls["submit_execution_plan"]) == 1
    scope_key = "sku_ids" if selection == "candidate_versions" else "spu_ids"
    assert executor.calls[1][1]["scope"][scope_key] == [1]
    assert executor.calls[1][1]["query"] == downstream_query
    result = state["task_artifacts"]["task_2"]["value"]
    assert result["query_plan"]["target"] == {"kind": scope_key[:3], "id": 1}
    assert state["task_status"]["task_2"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_bounded_schema_repair_in_each_module(monkeypatch):
    query = "白色蓝牙键盘300元以内"
    bad_route = message(
        "route_request", {"rewritten_query": query, "goals": [goal(query, disposition="invented")]}
    )
    bad_tasks = message(
        "submit_execution_plan",
        {"tasks": [{**task(query, tool="invented_tool"), "goal_id": "goal_1"}]},
    )
    runtime, model, executor, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query)]}],
        [keyboard_plan()],
        bad_route=bad_route,
        bad_tasks=bad_tasks,
    )
    await through_tools(runtime, query)
    assert len(model.calls["route_request"]) == len(model.calls["submit_execution_plan"]) == 2
    assert "validation_feedback" in model.calls["submit_execution_plan"][1][-1].content
    assert len(executor.calls) == 1


@pytest.mark.asyncio
async def test_planner_failure_preserves_old_identity(monkeypatch):
    query = "查询键盘"
    runtime, _, _, _ = setup_runtime(
        monkeypatch,
        query,
        [goal(query)],
        [{"goal_id": "goal_1", "tasks": [task(query)]}],
        [{"category": "键盘", "category_evidence": "不是原文"}],
    )
    state, _ = await through_tools(runtime, query)
    assert state["normalized_tool_results"][0]["outcome"] == "error"
    old = WorkingMemoryV2.model_validate({"catalog": {"candidate_sku_ids": [2]}})
    updated = _next_working_memory(old, {k: v for k, v in state.items() if k != "working_memory"})
    assert updated.catalog.candidate_sku_ids == [2]


@pytest.mark.asyncio
async def test_removed_catalog_page_binding_is_rejected_without_tools(monkeypatch):
    text = "继续看更多"
    query = "查询键盘"
    memory = {
        "catalog": {
            "candidate_sku_ids": [1],
            "search_page": {
                "query": query,
                "limit": 1,
                "offset": 0,
                "next_offset": 1,
                "candidate_count": 3,
                "match_kind": "exact",
                "selection_scope": "spu",
            },
        }
    }
    runtime, model, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [{"goal_id": "goal_1", "tasks": [task(text, page_from="catalog_page")]}],
        [{"category": "键盘", "category_evidence": "键盘"}],
        memory,
    )
    model.scripts["submit_execution_plan"] *= 2
    await runtime.run(ChatRequest(message=text), 1)
    assert len(model.calls["submit_execution_plan"]) == 2
    assert "page_from" in str(model.calls["submit_execution_plan"][1])
    assert not executor.calls


@pytest.mark.asyncio
async def test_two_object_reads_are_two_schema_valid_tool_calls(monkeypatch):
    text = "分别查询测试键盘1和测试键盘2价格"
    runtime, model, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [
            {
                "goal_id": "goal_1",
                "tasks": [task("查询测试键盘1价格"), task("查询测试键盘2价格", identity="task_2")],
            }
        ],
        [
            {"operation": "lookup", "name": "测试键盘1"},
            {"operation": "lookup", "name": "测试键盘2"},
        ],
    )
    state, _ = await through_tools(runtime, text)
    assert len(model.calls["submit_execution_plan"]) == 1
    assert len(executor.calls) == 2
    assert all(value["status"] == "succeeded" for value in state["task_status"].values())
    assert {p.spu_id for p in state["products"]} == {1, 2}


@pytest.mark.asyncio
async def test_object_unknown_facts_survive_until_answer_context(monkeypatch):
    text = "查询测试键盘1，必须蓝牙"
    runtime, _, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [{"goal_id": "goal_1", "tasks": [task(text)]}],
        [
            {
                "operation": "lookup",
                "name": "测试键盘1",
                "conditions": [{"key": "connection_type", "values": ["蓝牙"], "evidence": "蓝牙"}],
            }
        ],
    )
    executor.search.repository.rows[0].product.specs.pop("connection_type")
    state, _ = await through_tools(runtime, text)
    facts = state["task_artifacts"]["task_1"]["value"]
    assert facts["match_kind"] == "object_facts"
    assert facts["products"][0]["condition_checks"][0]["verdict"] == "unknown"
    assert facts["unknown_sku_count"] == 1 and not facts["matching_complete"]
    from app.agent.responses import _fallback_answer

    assert "不能确认" in _fallback_answer(state)
    assert "unknown" in str(build_answer_context(state))


@pytest.mark.asyncio
async def test_internal_search_still_supports_offsets_without_public_pagination(monkeypatch):
    from app.schemas.catalog_search import CatalogSearchRequest

    text = "查询键盘"
    _, _, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text)], [],
        [{"category": "键盘", "category_evidence": "键盘"}],
    )
    result = await executor.search.search(CatalogSearchRequest(query=text, offset=10, limit=1))
    assert result.products == []
    assert result.total_match_count == 3
    assert result.offset == 10


@pytest.mark.asyncio
async def test_repeated_invented_scope_stops_before_execution(monkeypatch):
    text = "查询键盘"
    bad = task(text)
    bad["arguments"]["scope"] = {"sku_ids": [999]}
    runtime, model, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [goal(text)],
        [{"goal_id": "goal_1", "tasks": [bad]}],
    )
    model.scripts["submit_execution_plan"] *= 2
    result = await runtime.run(ChatRequest(message=text), 1)
    assert result.answer
    assert len(model.calls["submit_execution_plan"]) == 2
    assert not executor.calls
    assert not model.calls["summarize_goals"]
    assert "未能完成" in result.answer and "规划" in result.answer
    assert "outside the trusted goal" in model.calls["submit_execution_plan"][1][-1].content


@pytest.mark.asyncio
async def test_task_scope_is_not_inherited_by_independent_task(monkeypatch):
    text = "第二款多少钱，另外看看全店键盘布局"
    query = "查询SKU 2价格，另外看看全店键盘布局"
    g = {**goal(query, refs=["product_2"]), "source_text": text}
    scoped_task = task("查询SKU 2价格")
    scoped_task["arguments"]["scope"] = {"sku_ids": [2]}
    runtime, model, executor, _ = setup_runtime(
        monkeypatch,
        text,
        [g],
        [
            {
                "goal_id": "goal_1",
                "tasks": [scoped_task, task("全店键盘有哪些布局", "task_2", "catalog_facets")],
            }
        ],
        [{"operation": "lookup", "purpose": "lookup", "target": {"kind": "sku", "id": 2}}],
        {"catalog": {"query_plan": {"selection_scope": "sku"}, "candidate_sku_ids": [1, 2]}},
    )
    state, _ = await through_tools(runtime, text)
    assert len(model.calls["route_request"]) == len(model.calls["submit_execution_plan"]) == 1
    assert state["route_plan"]["subqueries"][0]["scope"] is None
    assert executor.calls[0][1]["scope"]["sku_ids"] == [2]
    assert "scope" not in executor.calls[1][1]
    assert "SKU" not in executor.calls[1][1]["query"]
    assert len(state["execution_plan"]["tasks"]) == 2
    assert "goals" not in state["execution_plan"]
    assert build_answer_context(state)["goals"][0]["scope"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["sku", "spu"])
@pytest.mark.parametrize("missing_spec", [False, True])
async def test_comparison_goal_reads_independent_objects_through_tools(
    monkeypatch, scope, missing_spec,
):
    text = f"比较 {scope.upper()} 1 和 {scope.upper()} 2 的价格和连接方式"
    tasks = [task(
        f"查询 {scope.upper()} {identity} 的价格和连接方式", f"task_{identity}",
    ) for identity in (1, 2)]
    runtime, model, executor, _ = setup_runtime(
        monkeypatch, text, [goal(text)], [{"goal_id": "goal_1", "tasks": tasks}],
        [{"operation": "lookup", "purpose": "lookup", "target": {"kind": scope, "id": identity}}
         for identity in (1, 2)],
    )
    if missing_spec:
        executor.search.repository.rows[1].product.specs.pop("connection_type")
    if scope == "spu":
        for row in list(executor.search.repository.rows[:2]):
            variant = row.product.model_copy(deep=True)
            variant.sku_id += 10
            variant.price += 50
            variant.specs["color"] = "黑色"
            executor.search.repository.rows.append(CatalogRecord(product=variant, active=True))
    state, visited = await through_tools(runtime, text)
    assert "answer" not in visited
    assert len(executor.calls) == 2 and state["tool_wave_count"] == 1
    assert len(model.calls["route_request"]) == len(model.calls["submit_execution_plan"]) == 1
    assert not model.calls["summarize_goals"]
    for index, identity in enumerate((1, 2)):
        assert executor.calls[index][0] == "catalog_search"
        assert executor.calls[index][1] == tasks[index]["arguments"]
        artifact = state["task_artifacts"][f"task_{identity}"]
        assert artifact["source_tool_name"] == "catalog_search" and artifact["usable"]
        products = artifact["value"]["products"]
        assert len(products) == 1 and products[0][f"{scope}_id"] == identity
        assert products[0]["price"] == ("200" if identity == 1 else "250")
        if scope == "spu":
            variants = products[0]["series_variants"]
            assert {v["sku_id"] for v in variants} == {identity, identity + 10}
            assert {v["specs"]["color"] for v in variants} == {"白色", "黑色"}
        if missing_spec and identity == 2:
            assert "connection_type" not in products[0]["specs"]
